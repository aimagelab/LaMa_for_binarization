import os
import sys
import argparse

import torch.utils.data
import torchvision
import yaml
from torchvision.transforms import functional

from trainer.LaMaTrainer import LaMaTrainingModule, calculate_psnr
from trainer.Validator import Validator
from utils.htr_logging import get_logger

logger = get_logger('main')
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
logger.info(f"Using {device} device")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('-s', '--save_images', metavar='True or False', type=bool,
                        help=f"If TRUE will be saved the result images", default=False)
    parser.add_argument('-cfg', '--configuration', metavar='<name>', type=str,
                        help=f"The configuration name will use during running", default="evaluation_custom_mse")

    args = parser.parse_args()

    logger.info("Start process ...")
    configuration_path = f"configs/evaluation/{args.configuration}.yaml"

    try:
        with open(configuration_path) as file:
            valid_config = yaml.load(file, Loader=yaml.Loader)
            file.close()
            logger.info(f"Loaded \"{configuration_path}\" configuration file")

        folder = f'results/evaluation/{valid_config["filename_checkpoint"]}/'
        os.makedirs(folder, exist_ok=True)

        trainer = LaMaTrainingModule(valid_config, device=device)
        trainer.load_checkpoints(valid_config['path_checkpoint'], valid_config["filename_checkpoint"])

        if torch.cuda.is_available():
            trainer.model.cuda()

        logger.info("Start validation ...")

        trainer.model.eval()
        validator = Validator()

        with torch.no_grad():

            total_psnr = 0.0

            for item in trainer.valid_data_loader:
                image_name = item['image_name'][0]
                sample = item['sample']
                num_rows = item['num_rows'].item()
                samples_patches = item['samples_patches']
                gt_sample = item['gt_sample']

                samples_patches = samples_patches.squeeze(0)
                valid = samples_patches.to(device)
                gt_valid = gt_sample.to(device)

                valid = valid.squeeze(0).permute(1, 0, 2, 3)
                pred = trainer.model(valid)

                pred = torchvision.utils.make_grid(pred, nrow=num_rows, padding=0, value_range=(0, 1))
                pred = functional.rgb_to_grayscale(pred)
                _, _, height, width = gt_valid.shape
                pred = functional.crop(pred, top=0, left=0, height=height, width=width)
                pred = pred.unsqueeze(0)

                total_psnr += calculate_psnr(pred, gt_valid)

                validator.run(pred, gt_valid)

                # Store images
                if args.save_images:
                    pred = pred.squeeze(0).detach()
                    pred_img = functional.to_pil_image(pred)
                    path = folder + image_name
                    pred_img.save(path)

            avg_psnr = total_psnr / len(trainer.valid_data_loader)
            psnr, precision, recall = validator.get_metrics()
            logger.info(f"Average PSNR: {avg_psnr:.6f} - {psnr:.6f}")
            logger.info(f"Precision {100. * precision:.4f} - Recall {100. * recall:.4f}")

    except KeyboardInterrupt:
        logger.warning("Validation interrupted by user")
    except FileNotFoundError as file_not_found:
        logger.error(f"File \"{file_not_found.filename}\" not found. Exit")
    except Exception as e:
        logger.error(f"Validation failed due to {e}")

    sys.exit()
