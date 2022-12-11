import errno
import math
import os

import numpy as np
import torch
import torch.optim as optim
import torch.utils.data
import wandb
from torchmetrics import PeakSignalNoiseRatio
from torchvision.transforms import functional
from typing_extensions import TypedDict

from data.dataloaders import make_train_dataloader, \
    make_valid_dataloader, make_valid_dataset
from modules.FFC import LaMa
from trainer.Losses import LMSELoss
from utils.htr_logging import get_logger


def calculate_psnr(predicted: torch.Tensor, ground_truth: torch.Tensor, threshold=0.5):
    pred_img = predicted.detach().cpu().numpy()
    gt_img = ground_truth.detach().cpu().numpy()

    pred_img = (pred_img > threshold) * 1.0

    mse = np.mean((pred_img - gt_img) ** 2)
    psnr = 100 if mse == 0 else (20 * math.log10(1.0 / math.sqrt(mse)))
    return psnr


class LaMaTrainingModule:

    def __init__(self, config, device=None, wandb_log=None):

        # Batch Size
        self.train_batch_size = config['train_batch_size']
        self.valid_batch_size = config['valid_batch_size']

        # Split Size
        self.train_split_size = config['train_split_size']
        self.valid_split_size = config['valid_split_size']

        self.train_data_loader = make_train_dataloader(config)
        self.valid_dataset = make_valid_dataset(config)
        self.valid_data_loader = make_valid_dataloader(self.valid_dataset, config)
        self.optimizer = None

        self.device = device
        self.learning_rate = config['learning_rate']
        self.wandb_log = wandb_log

        # TO IMPROVE
        arguments = TypedDict('arguments', {'ratio_gin': float, 'ratio_gout': float})  # REMOVE
        init_conv_kwargs: arguments = {'ratio_gin': 0, 'ratio_gout': 0}  # REMOVE
        down_sample_conv_kwargs: arguments = {'ratio_gin': 0, 'ratio_gout': 0}  # REMOVE
        resnet_conv_kwargs: arguments = {'ratio_gin': 0.75, 'ratio_gout': 0.75}  # REMOVE
        self.model = LaMa(input_nc=config['input_channels'], output_nc=config['output_channels'],
                          init_conv_kwargs=init_conv_kwargs, downsample_conv_kwargs=down_sample_conv_kwargs,
                          resnet_conv_kwargs=resnet_conv_kwargs)

        # Training
        self.epoch = 0
        self.num_epochs = config['num_epochs']
        self.kind_loss = config['kind_loss']

        # Validation
        self.psnr_metric = PeakSignalNoiseRatio().to(device=device)
        self.best_epoch = 0
        self.best_psnr = 0

        # Logging
        self.logger = get_logger(LaMaTrainingModule.__name__)

        # Criterion

        self._create_optimizer()
        self._make_criterion()

    def load_checkpoints(self, folder: str, filename: str):
        checkpoints_path = f"{folder}{filename}_best_psnr.pth"

        if not os.path.exists(path=checkpoints_path):
            self.logger.warning(f"Checkpoints {checkpoints_path} not found.")
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), checkpoints_path)

        checkpoint = torch.load(checkpoints_path, map_location=None)
        self.model.load_state_dict(checkpoint['model'], strict=True)
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.epoch = checkpoint['epoch']
        self.best_psnr = checkpoint['best_psnr']
        self.learning_rate = checkpoint['learning_rate']
        self.logger.info(f"Loaded pretrained checkpoint model from \"{checkpoints_path}\"")

    def save_checkpoints(self, root_folder: str, filename: str):
        os.makedirs(root_folder, exist_ok=True)
        checkpoint = {
            'model': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'epoch': self.epoch,
            'best_psnr': self.best_psnr,
            'learning_rate': self.learning_rate,
        }

        dir_path = root_folder + f"{filename}_best_psnr.pth"
        torch.save(checkpoint, dir_path)
        self.logger.info(f"Stored checkpoints {dir_path}")

    def validation(self, threshold=0.5):
        total_psnr = 0.0
        valid_loss = 0.0
        images = {}

        for item in self.valid_data_loader:

            image_name = item['image_name'][0]
            sample = item['sample']
            num_rows = item['num_rows'].item()
            samples_patches = item['samples_patches']
            gt_sample = item['gt_sample']

            samples_patches = samples_patches.squeeze(0)
            valid = samples_patches.to(self.device)
            gt_valid = gt_sample.to(self.device)

            valid = valid.squeeze(0)
            valid = valid.permute(1, 0, 2, 3)
            pred = self.model(valid)

            # Re-construct image
            pred = self.valid_dataset.reconstruct_image(pred, sample, num_rows)
            pred = pred.to(self.device)
            pred = functional.rgb_to_grayscale(pred)

            # pred = torchvision.utils.make_grid(pred, nrow=num_rows, padding=0, value_range=(0, 1))
            # pred = functional.rgb_to_grayscale(pred)
            # height, width = gt_valid.shape[3], gt_valid.shape[2]
            # pred = functional.crop(pred, top=0, left=0, height=height, width=width)
            # pred = pred.permute(0, 2, 1).unsqueeze(0)

            loss = self.criterion(pred, gt_valid)
            valid_loss += loss.item()

            psnr = calculate_psnr(pred, gt_valid)
            total_psnr += psnr

            self.logger.info(f"\tImage: {image_name}\t Loss: {loss.item():0.6f} - PSNR: {psnr:0.6f}")

            pred = pred.squeeze(0).detach()
            # pred = torch.clamp(pred, min=0, max=1)
            pred = torch.where(pred > threshold, 1., 0.)
            pred_img = functional.to_pil_image(pred)
            images[image_name] = pred_img

            if self.wandb_log:
                valid = sample.squeeze(0).detach()
                gt_valid = gt_valid.squeeze(0).detach()

                valid_img = functional.to_pil_image(valid)
                gt_valid_img = functional.to_pil_image(gt_valid)

                wandb_images = [wandb.Image(valid_img, caption=f"Sample: {image_name}"),
                                wandb.Image(pred_img, caption=f"Predicted Sample: {image_name}"),
                                wandb.Image(gt_valid_img, caption=f"Ground Truth Sample: {image_name}")]

                logs = {
                    "Images": wandb_images
                }
                self.wandb_log.on_log(logs)

        avg_psnr = total_psnr / len(self.valid_data_loader)
        avg_loss = valid_loss / len(self.valid_data_loader)

        if self.wandb_log:  # Logs Valid Parameters
            logs = {
                'valid_avg_loss': avg_loss,
                'valid_avg_psnr': avg_psnr,
            }
            self.wandb_log.on_log(logs)

        return avg_psnr, avg_loss, images

    def _create_optimizer(self):
        self.optimizer = optim.AdamW(self.model.parameters(), lr=self.learning_rate, betas=(0.9, 0.95),
                                     eps=1e-08, weight_decay=0.05, amsgrad=False)

    def _make_criterion(self):
        if self.kind_loss == 'default_mse':
            self.criterion = torch.nn.MSELoss().to(self.device)
        else:
            self.criterion = LMSELoss().to(self.device)