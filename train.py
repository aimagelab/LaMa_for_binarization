import argparse
import random
import sys
import time
import uuid
import traceback
from pathlib import Path
from datetime import timedelta

import numpy as np
import torch
import wandb
import yaml
from torchvision.transforms import functional

from trainer.LaMaTrainer import LaMaTrainingModule, set_seed
from trainer.Validator import Validator
from utils.WandbLog import WandbLog
from utils.htr_logging import get_logger, DEBUG
from utils.ioutils import store_images

logger = get_logger('main')

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
assert torch.cuda.is_available(), 'CUDA is not available. Please use a GPU to run this code.'


def train(config_args, config):
    wandb_log = None

    trainer = LaMaTrainingModule(config, device=device)

    if config_args.use_wandb:  # Configure WandB
        tags = [Path(path).name for path in config_args.train_data_path]
        wandb_id = wandb.util.generate_id()
        if trainer.checkpoint is not None and 'wandb_id' in trainer.checkpoint:
            wandb_id = trainer.checkpoint['wandb_id']
        wandb_log = WandbLog(experiment_name=config_args.experiment_name, tags=tags,
                             dir=config_args.wandb_dir, id=wandb_id)
        wandb_log.setup(config)

    trainer.model.to(device)

    if wandb_log:
        wandb_log.add_watch(trainer.model)

    threshold = config['threshold'] if config['threshold'] else 0.5
    train_validator = Validator(apply_threshold=config['apply_threshold_to_train'], threshold=threshold)

    try:
        start_time = time.time()

        patience = config['patience']

        for epoch in range(1, config['num_epochs']):
            wandb_logs = dict()

            if config_args.train:
                logger.info("Training has been started") if epoch == 1 else None
                logger.info(f"Epoch {trainer.epoch} of {trainer.num_epochs}")

                train_loss = 0.0
                # visualization = torch.zeros((1, config['train_patch_size'], config['train_patch_size']), device=device)

                trainer.model.train()

                train_validator.reset()
                data_times = []
                train_times = []

                start_data_time = time.time()
                start_epoch_time = time.time()
                for batch_idx, (train_in, train_out) in enumerate(trainer.train_data_loader):
                    data_times.append(time.time() - start_data_time)
                    start_train_time = time.time()
                    inputs, outputs = train_in.to(device), train_out.to(device)

                    trainer.optimizer.zero_grad()
                    predictions = trainer.model(inputs)
                    loss = trainer.criterion(predictions, outputs)
                    loss.backward()
                    trainer.optimizer.step()
                    trainer.lr_scheduler.step()
                    trainer.update_ema()

                    train_loss += loss.item()

                    # code to gnerate the diff mask
                    # tensor_bin = torch.where(predictions > threshold, 1., 0.)
                    # tensor_diff = torch.abs(tensor_bin - outputs)
                    # visualization += torch.sum(tensor_diff, dim=0)

                    train_times.append(time.time() - start_train_time)

                    with torch.no_grad():

                        metrics = train_validator.compute(predictions, outputs)

                        if batch_idx % config['train_log_every'] == 0:
                            size = batch_idx * len(inputs)
                            percentage = 100. * size / len(trainer.train_dataset)

                            elapsed_time = time.time() - start_time
                            time_per_iter = elapsed_time / (size + 1)
                            remaining_time = (len(trainer.train_dataset) - size - 1) * time_per_iter
                            eta = str(timedelta(seconds=remaining_time))

                            stdout = f"Train Loss: {loss.item():.6f} - PSNR: {metrics['psnr']:0.4f} -"
                            if 'precision' in metrics and 'recall' in metrics:
                                stdout += f" Precision: {metrics['precision']:0.4f}% - Recall: {metrics['recall']:0.4f}%"
                                stdout += f" \t[{size} / {len(trainer.train_dataset)}]"
                            stdout += f" ({percentage:.2f}%)  Epoch eta: {eta}"
                            logger.info(stdout)
                    start_data_time = time.time()

                    if batch_idx == 3:
                        break

                avg_train_loss = train_loss / len(trainer.train_dataset)
                avg_train_metrics = train_validator.get_metrics()

                stdout = f"AVG training loss: {avg_train_loss:0.4f} - AVG training PSNR: {avg_train_metrics['psnr']:0.4f}"
                if 'precision' in avg_train_metrics and 'recall' in avg_train_metrics:
                    stdout += f" AVG training precision: {avg_train_metrics['precision']:0.4f}%"
                    stdout += f" AVG training recall: {avg_train_metrics['recall']:0.4f}%"
                logger.info(stdout)

                wandb_logs['train_learning_rate'] = trainer.lr_scheduler.get_last_lr()[0]
                wandb_logs['train_avg_loss'] = avg_train_loss
                wandb_logs['train_avg_psnr'] = avg_train_metrics['psnr']
                if 'precision' in avg_train_metrics and 'recall' in avg_train_metrics:
                    wandb_logs['train_avg_precision'] = avg_train_metrics['precision']
                    wandb_logs['train_avg_recall'] = avg_train_metrics['recall']
                wandb_logs['train_data_time'] = np.array(data_times).mean()
                wandb_logs['train_time_per_iter'] = np.array(train_times).mean()

                original = inputs[0]
                pred = predictions[0].expand(3, -1, -1)
                output = outputs[0].expand(3, -1, -1)
                union = torch.cat((original, pred, output), 2)
                wandb_logs['Random Sample'] = wandb.Image(functional.to_pil_image(union), caption=f"Example")

                # Make error images
                # rescaled = torch.div(visualization, config['train_max_value'])
                # rescaled = torch.clamp(rescaled, min=0., max=1.)
                # wandb_logs['Errors'] = wandb.Image(functional.to_pil_image(rescaled))

                # Validation
                trainer.model.eval()
                train_validator.reset()

                with torch.no_grad():
                    start_test_time = time.time()
                    test_metrics, test_loss, images = trainer.test()

                    if config['ema_rates']:
                        ema_test_metrics, ema_test_loss, ema_images = trainer.test_ema()

                        for i, rate in enumerate(trainer.ema_rates):
                            wandb_logs[f'test_avg_ema_{rate}_psnr'] = ema_test_metrics[i]['psnr']
                            if 'precision' in ema_test_metrics[i] and 'recall' in ema_test_metrics[i]:
                                wandb_logs[f'test_avg_ema_{rate}_precision'] = ema_test_metrics[i]['precision']
                                wandb_logs[f'test_avg_ema_{rate}_recall'] = ema_test_metrics[i]['recall']
                            wandb_logs[f'test_avg_ema_{rate}_loss'] = ema_test_loss[i]

                    wandb_logs['test_time'] = time.time() - start_test_time
                    wandb_logs['test_avg_loss'] = test_loss
                    wandb_logs['test_avg_psnr'] = test_metrics['psnr']
                    if 'precision' in test_metrics and 'recall' in test_metrics:
                        wandb_logs['test_avg_precision'] = test_metrics['precision']
                        wandb_logs['test_avg_recall'] = test_metrics['recall']

                    name_image, (test_img, pred_img, gt_test_img) = list(images.items())[0]
                    target_height = 512
                    test_img = test_img.resize((target_height, int(target_height * test_img.height / test_img.width)))
                    pred_img = pred_img.resize((target_height, int(target_height * pred_img.height / pred_img.width)))
                    gt_test_img = gt_test_img.resize((target_height, int(target_height * gt_test_img.height / gt_test_img.width)))

                    wandb_logs['test_results'] = [wandb.Image(test_img, caption=f"Sample: {name_image}"),
                                             wandb.Image(pred_img, caption=f"Predicted Sample: {name_image}"),
                                             wandb.Image(gt_test_img, caption=f"Ground Truth Sample: {name_image}")]

                    start_valid_time = time.time()
                    if trainer.training_only_with_patch_square:
                        valid_metrics, valid_loss = trainer.validation_patch_square()
                    else:
                        valid_metrics, valid_loss, images = trainer.validation()

                    wandb_logs['valid_time'] = time.time() - start_valid_time
                    wandb_logs['valid_avg_loss'] = valid_loss
                    wandb_logs['valid_avg_psnr'] = valid_metrics['psnr']
                    if 'precision' in valid_metrics and 'recall' in valid_metrics:
                        wandb_logs['valid_avg_precision'] = valid_metrics['precision']
                        wandb_logs['valid_avg_recall'] = valid_metrics['recall']
                    wandb_logs['valid_patience'] = patience

                    trainer.psnr_list.append(valid_metrics['psnr'])
                    psnr_running_mean = sum(trainer.psnr_list[-3:]) / len(trainer.psnr_list[-3:])
                    if valid_metrics['psnr'] > trainer.best_psnr:
                        trainer.best_psnr = valid_metrics['psnr']
                    if valid_metrics['psnr'] > trainer.best_psnr_running_mean:
                        trainer.best_psnr_running_mean = psnr_running_mean
                        patience = config['patience']
                        if 'precision' in valid_metrics and 'recall' in valid_metrics:
                            trainer.best_precision = valid_metrics['precision']
                            trainer.best_recall = valid_metrics['recall']

                        trainer.save_checkpoints(filename=config_args.experiment_name + '_best_psnr')

                        # Save images
                        # names = images.keys()
                        # predicted_images = [item[1] for item in list(images.values())]
                        # store_images(parent_directory='results/training', directory=config_args.experiment_name,
                        #              names=names, images=predicted_images)
                    else:
                        patience -= 1
                    trainer.save_checkpoints(filename=config_args.experiment_name)

                # Log best values
                wandb_logs['Best PSNR'] = trainer.best_psnr
                wandb_logs['Psnr Running Mean'] = psnr_running_mean
                wandb_logs['Best PSNR Running Mean'] = trainer.best_psnr_running_mean
                wandb_logs['Best Precision'] = trainer.best_precision
                wandb_logs['Best Recall'] = trainer.best_recall

                stdout = f"Validation Loss: {valid_loss:.4f} - PSNR: {valid_metrics['psnr']:.4f}"
                if 'precision' in valid_metrics and 'recall' in valid_metrics:
                    stdout += f" Precision: {valid_metrics['precision']:.4f}% - Recall: {valid_metrics['recall']:.4f}%"
                stdout += f" Best Loss: {trainer.best_psnr:.3f}"
                logger.info(stdout)

                stdout = f"Test Loss: {test_loss:.4f} - PSNR: {test_metrics['psnr']:.4f}"
                if 'precision' in test_metrics and 'recall' in test_metrics:
                    stdout += f" Precision: {test_metrics['precision']:.4f}% - Recall: {test_metrics['recall']:.4f}%"
                    stdout += f" Best Loss: {trainer.best_psnr:.3f}"
                logger.info(stdout)

                trainer.epoch += 1
                wandb_logs['epoch'] = trainer.epoch
                wandb_logs['epoch_time'] = time.time() - start_epoch_time
                logger.info('-' * 75)

                if wandb_log:
                    wandb_log.on_log(wandb_logs)

                if patience == 0:
                    stdout = f"There has been no update of Best PSNR value in the last {config['patience']} epochs."
                    stdout += " Training will be stopped."
                    logger.info(stdout)
                    sys.exit()

    except KeyboardInterrupt:
        logger.warning("Training interrupted by user")
    except Exception as e:
        traceback.print_exc()
        logger.error(f"Training failed due to {e}")
    finally:
        logger.info("Training finished")
        exit()





if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('-e', '--experiment_name', metavar='<name>', type=str,
                        help=f"The experiment name which will use on WandB")
    parser.add_argument('-c', '--configuration', metavar='<name>', type=str,
                        help=f"The configuration name will use on WandB", default="debug_patch_square")
    parser.add_argument('-w', '--use_wandb', type=bool, default=not DEBUG)
    parser.add_argument('-t', '--train', type=bool, default=True)
    parser.add_argument('--attention', type=str, default='none',
                        choices=['none', 'cross', 'self', 'cross_local', 'cross_global'])
    parser.add_argument('--attention_num_heads', type=int, default=4)
    parser.add_argument('--attention_channel_scale_factor', type=int, default=1)
    parser.add_argument('--n_blocks', type=int, default=9)
    parser.add_argument('--n_downsampling', type=int, default=3)
    parser.add_argument('--num_workers', type=int, default=4)
    parser.add_argument('--batch_size', type=int, default=8)
    parser.add_argument('--operation', type=str, default='ffc', choices=['ffc', 'conv'])
    parser.add_argument('--skip', type=str, default='none', choices=['none', 'add', 'cat'])
    parser.add_argument('--resume', type=str, default='none')
    parser.add_argument('--wandb_dir', type=str, default='/tmp')
    parser.add_argument('--unet_layers', type=int, default=0)
    parser.add_argument('--epochs', type=int, default=150)
    parser.add_argument('--seed', type=int, default=742)
    parser.add_argument('--patience', type=int, default=60)
    parser.add_argument('--apply_threshold_to', type=str, default='all', choices=['none', 'val_test', 'test', 'all'])
    parser.add_argument('--loss_kind', type=str, default='binary_cross_entropy',
                        choices=['mean_square_error', 'cross_entropy', 'negative_log_likelihood',
                                 'custom_mse', 'charbonnier'])
    parser.add_argument('--lr', type=int, default=1.5e-4)
    parser.add_argument('--lr_scheduler', type=str, default='constant', choices=['constant', 'exponential', 'multistep', 'linear', 'cosine'])
    parser.add_argument('--lr_scheduler_kwargs', type=eval, default={})
    parser.add_argument('--ema_rate', type=float, default=-1)
    parser.add_argument('--load_data', type=str, default='true', choices=['true', 'false'])
    parser.add_argument('--threshold', type=float, default=0.5)
    parser.add_argument('--train_data_path', type=str, nargs='+', required=True)
    parser.add_argument('--test_data_path', type=str, nargs='+', required=True)

    args = parser.parse_args()

    config_filename = args.configuration

    logger.info("Start process ...")
    configuration_path = f"configs/training/{config_filename}.yaml"
    logger.info(f"Selected \"{configuration_path}\" configuration file")

    with open(configuration_path) as file:
        train_config = yaml.load(file, Loader=yaml.Loader)

    if args.resume != 'none':
        checkpoint_path = Path(train_config['path_checkpoint'])
        checkpoints = sorted(checkpoint_path.glob(f"*_{args.resume}*.pth"))
        assert 1 <= len(checkpoints) <= 2, f"Found {len(checkpoints)} checkpoints with uuid {args.resume}"
        train_config['resume'] = checkpoints[0]
        args.experiment_name = checkpoints[0].stem.rstrip('_best_psnr')

    if args.experiment_name is None:
        exp_name = [
            args.operation.upper(),
            str(args.n_blocks) + 'RB',
            str(train_config['train_patch_size']) + 'PS',
            args.attention + 'ATT',
            args.skip + 'SKIP',
            str(args.unet_layers) + 'UL',
            str(args.n_downsampling) + 'DS',
            args.apply_threshold_to + 'TH',
            str(args.threshold) + 'THR',
            args.loss_kind[:4] + 'LOSS',
            args.lr_scheduler[:4] + 'SCHE',
            str(uuid.uuid4())[:4]
        ]
        if args.ema_rate > 0: exp_name.insert(-1, f"{args.ema_rate}EMA")
        args.experiment_name = '_'.join(exp_name)

    train_config['experiment_name'] = args.experiment_name
    train_config['use_convolutions'] = args.operation == 'conv'
    train_config['skip_connections'] = args.skip
    train_config['unet_layers'] = args.unet_layers
    train_config['n_blocks'] = args.n_blocks
    train_config['n_downsampling'] = args.n_downsampling
    train_config['cross_attention'] = args.attention
    train_config['kind_loss'] = args.loss_kind
    train_config['kind_lr_scheduler'] = args.lr_scheduler
    train_config['lr_scheduler_kwargs'] = args.lr_scheduler_kwargs
    train_config['learning_rate'] = args.lr
    train_config['seed'] = args.seed
    if args.attention == 'self':
        raise NotImplementedError('Self attention is not implemented yet')
    train_config['train_data_path'] = args.train_data_path
    train_config['valid_data_path'] = args.train_data_path
    train_config['test_data_path'] = args.test_data_path

    if args.attention_num_heads and args.attention_channel_scale_factor:
        train_config['cross_attention_args'] = {
            'num_heads': args.attention_num_heads,
            'attention_channel_scale_factor': args.attention_channel_scale_factor}
    else:
        train_config['cross_attention_args'] = None

    train_config['train_kwargs']['num_workers'] = args.num_workers
    train_config['valid_kwargs']['num_workers'] = args.num_workers
    train_config['test_kwargs']['num_workers'] = args.num_workers
    train_config['train_kwargs']['batch_size'] = args.batch_size
    train_config['valid_kwargs']['batch_size'] = 1
    train_config['test_kwargs']['batch_size'] = 1

    train_config['train_batch_size'] = train_config['train_kwargs']['batch_size']
    train_config['valid_batch_size'] = train_config['valid_kwargs']['batch_size']
    train_config['test_batch_size'] = train_config['test_kwargs']['batch_size']

    train_config['num_epochs'] = args.epochs
    train_config['patience'] = args.patience
    train_config['ema_rates'] = [args.ema_rate] if args.ema_rate > 0 else None

    train_config['apply_threshold_to_train'] = args.apply_threshold_to
    train_config['apply_threshold_to_valid'] = args.apply_threshold_to
    train_config['apply_threshold_to_test'] = args.apply_threshold_to
    train_config['threshold'] = args.threshold
    train_config['load_data'] = args.load_data == 'true'

    train_config['apply_threshold_to_train'] = True
    train_config['apply_threshold_to_valid'] = True
    train_config['apply_threshold_to_test'] = True
    if args.apply_threshold_to == 'none':
        train_config['apply_threshold_to_train'] = False
        train_config['apply_threshold_to_valid'] = False
        train_config['apply_threshold_to_test'] = False
    elif args.apply_threshold_to == 'val_test':
        train_config['apply_threshold_to_train'] = False
    elif args.apply_threshold_to == 'test':
        train_config['apply_threshold_to_train'] = False
        train_config['apply_threshold_to_valid'] = False

    set_seed(args.seed)

    train(args, train_config)
    sys.exit()
