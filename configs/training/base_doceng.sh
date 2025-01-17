#!/bin/bash
#SBATCH --gres=gpu:1
#SBATCH --partition=prod
#SBATCH -e /mnt/beegfs/work/FoMo_AIISDH/vpippi/BiLama/jobs/bilama_doceng_%j.err
#SBATCH -o /mnt/beegfs/work/FoMo_AIISDH/vpippi/BiLama/jobs/bilama_doceng_%j.out
#SBATCH --mem=64G
#SBATCH --exclude=aimagelab-srv-00,aimagelab-srv-10,vegeta,carabbaggio,germano,gervasoni,pippobaudo,rezzonico,ajeje,helmut,lurcanio
#SBATCH -J bilama_abla

cd /homes/fquattrini/LaMa_for_binarization || exit
scontrol update JobID="$SLURM_JOB_ID" name="@{name}"
srun /homes/$(whoami)/.conda/envs/LaMa/bin/python /homes/fquattrini/LaMa_for_binarization/train.py -c base \
  --n_blocks @{n_blocks|3} --operation @{operation|ffc} --attention none --num_workers 2 --epochs @{epochs|none} --skip cat \
  --unet_layers @{unet_layers|2} --lr_scheduler cosine --lr_scheduler_kwargs "dict()" --resume @{resume|none} --ema_rate -1 \
  --loss @{loss|CHAR} --merge_image false --train_transform_variant latin --lr_scheduler_warmup 10 \
  --patch_size 256 --patch_size_raw 384 --batch_size 4 --datasets \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO09 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO10 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO11 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO12 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO13 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO14 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO16 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO17 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets_one_for_eval/DIBCO18 \
    /mnt/beegfs/scratch/fquattrini/binarization_datasets/Nabuco \
    /mnt/beegfs/scratch/fquattrini/doceng/livememory-dataset-1 \
    /mnt/beegfs/scratch/fquattrini/doceng/livememory-dataset-2 \
    /mnt/beegfs/scratch/fquattrini/doceng/mobile-dataset \
    --test_dataset @{test|mobile-dataset} \
    --validation_dataset @{valid|mobile-dataset}
