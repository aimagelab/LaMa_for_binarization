from pathlib import Path
import os
import argparse
import sys
import subprocess
import re
import csv

regex = r'([ a-zA-Z()-]+)\t*:\s*(\d+\.?\d+)'


def run_process(exe):
    proc = subprocess.run(exe, text=True, capture_output=True)
    return proc.stdout


def main(gt_path, p_path):
    # gt_path = Path("C:\Users\\fabio\Downloads\docentrd19\gt_imgs")
    # pred_path = Path("C:\Users\\fabio\Downloads\docentrd19\DIBCO19b")

    pred = {p.stem.split('_')[0]: p for p in p_path.glob('*pred*.png')}
    gt = {p.stem.split('_')[0]: p for p in gt_path.glob('*gt*.png')}
    assert len(pred) == len(gt) and all(k in gt for k in pred.keys())

    results = {}

    for id in pred.keys():
        gt_path = gt[id]
        pred_path = pred[id]
        recall_path = pred_path.parent / f'{pred_path.stem}_RWeights.dat'
        precision_path = pred_path.parent / f'{pred_path.stem}_PWeights.dat'
        if not recall_path.exists() or not precision_path.exists():
            run_process(f'{weights_exe_path} {pred_path}'.split())
        assert recall_path.exists() and precision_path.exists()
        exe = f'{metrics_exe_path.absolute()} {gt_path} {pred_path} {recall_path} {precision_path}'
        exe = exe.replace('\\', '/')
        output = run_process(exe.split())

        output = re.findall(regex, output)
        output = {k: float(v) for k, v in output}
        print(f'{id}: {output}')
        results[id] = output

    keys = list(results.values())[0].keys()
    average = {k: sum([v[k] for v in results.values() if k in v]) / len(results) for k in keys}
    results['average'] = average
    print(f'Average: {average}')

    with open(p_path / 'results.csv', 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=['path', 'id'] + list(keys))
        writer.writeheader()
        for id in sorted(results.keys()):
            results[id]['id'] = id
            results[id]['path'] = str(p_path.stem)
            writer.writerow(results[id])

    return results


if __name__ == "__main__":
    weights_exe_path = Path('evaluation-tool/BinEvalWeights/BinEvalWeights.exe')
    metrics_exe_path = Path('evaluation-tool/DIBCO_metrics/DIBCO_metrics.exe')
    parser = argparse.ArgumentParser()
    parser.add_argument('--gt_path', type=str)
    parser.add_argument('--paths', type=str, nargs='+', required=True)
    args = parser.parse_args()
    os.environ['PATH'] = 'C:\\Program Files\\MATLAB\\MATLAB Runtime\\v90\\runtime\\win64' + ';' + os.environ['PATH']

    results_all = []
    for path in args.paths:
        if not args.gt_path:
            args.gt_path = path
        print(f'Processing {path}')
        results_all.append(main(Path(args.gt_path), Path(path)))

    print(f"Saving results to 20230513_FFC_all_patch_size_stride_sweep_paper_plot.csv")
    with open(f'20230513_all_patch_size_stride_sweep_paper_plot.csv', 'a') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=results_all[0].keys())
        writer.writeheader()
        writer.writerows(results_all)
    print(f"Done! \n")
    sys.exit()
