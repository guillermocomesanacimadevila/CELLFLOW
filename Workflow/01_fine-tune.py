# 01_fine-tune.py

import os, sys
from pathlib import Path
import logging
import platform
from datetime import datetime
import yaml
import git
import configargparse

import torch
from torch.utils.data import ConcatDataset, Subset, Dataset

# -- Setup paths and tarrow import --
script_dir = Path(__file__).resolve().parent
tarrow_path = (script_dir.parent / "TAP" / "tarrow").resolve()
if str(tarrow_path) not in sys.path:
    sys.path.insert(0, str(tarrow_path))

import tarrow
from tarrow.models import TimeArrowNet
from tarrow.data import TarrowDataset, get_augmenter
from tarrow.visualizations import create_visuals

# --- Logging setup ---
logging.basicConfig(
    format="%(filename)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# --- Argument parser ---
def get_argparser():
    p = configargparse.ArgParser(
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        config_file_parser_class=configargparse.YAMLConfigFileParser,
        allow_abbrev=False,
    )

    p.add("-c", "--config", is_config_file=True, help="Path to YAML config file.")
    p.add("--name", type=str, default=None)
    p.add("--input_train", type=str, nargs="+", required=False)
    p.add("--input_val", type=str, nargs="*", default=None)
    p.add("--read_recursion_level", type=int, default=0)
    p.add("--split_train", type=float, nargs=2, action="append", required=False)
    p.add("--split_val", type=float, nargs="+", action="append", required=False)
    p.add("-e", "--epochs", type=int, default=200)
    p.add("--seed", type=int, default=42)
    p.add("--backbone", type=str, default="unet")
    p.add("--projhead", default="minimal_batchnorm")
    p.add("--classhead", default="minimal")
    p.add("--perm_equiv", type=tarrow.utils.str2bool, default=True)
    p.add("--features", type=int, default=32)
    p.add("--n_images", type=int, default=None)
    p.add("-o", "--outdir", type=str, default="runs")
    p.add("--size", type=int, default=96)
    p.add("--cam_size", type=int, default=None)
    p.add("--batchsize", type=int, default=128)
    p.add("--train_samples_per_epoch", type=int, default=100000)
    p.add("--val_samples_per_epoch", type=int, default=10000)
    p.add("--channels", type=int, default=0)
    p.add("--reject_background", type=tarrow.utils.str2bool, default=False)
    p.add("--cam_subsampling", type=int, default=3)
    p.add("--write_final_cams", type=tarrow.utils.str2bool, default=False)
    p.add("--augment", type=int, default=5)
    p.add("--subsample", type=int, default=1)
    p.add("--delta", type=int, nargs="+", default=[1])
    p.add("--frames", type=int, default=2)
    p.add("--lr", type=float, default=1e-4)
    p.add("--lr_scheduler", default="cyclic")
    p.add("--lr_patience", type=int, default=50)
    p.add("--ndim", type=int, default=2)
    p.add("--binarize", action="store_true")
    p.add("--decor_loss", type=float, default=0.01)
    p.add("--save_checkpoint_every", type=int, default=25)
    p.add("--num_workers", type=int, default=8)
    p.add("--gpu", "-g", type=str, default="0")
    p.add("--tensorboard", type=tarrow.utils.str2bool, default=True)
    p.add("--visual_dataset_frequency", type=int, default=10)
    p.add("--timestamp", action="store_true")

    return p

# --- Config saving functions (improved!) ---
def save_full_config(args, outdir):
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "python_version": platform.python_version(),
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
    }
    try:
        repo = git.Repo(search_parent_directories=True)
        metadata["git_commit"] = repo.head.object.hexsha
    except Exception as e:
        logger.warning(f"Could not get git commit: {e}")
        metadata["git_commit"] = "unknown"

    config_path = outdir / "train_args_full.yaml"
    try:
        with open(config_path, "w") as f:
            yaml.safe_dump({**vars(args), **metadata}, f, sort_keys=False)
        logger.info(f"Saved full config to {config_path}")
    except Exception as e:
        logger.error(f"Failed to save full config: {e}")

def save_partial_config(args, outdir):
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    partial_config_path = outdir / "train_args.yaml"
    try:
        with open(partial_config_path, "w") as f:
            yaml.safe_dump(vars(args), f, sort_keys=False)
        logger.info(f"Saved training args to {partial_config_path}")
    except Exception as e:
        logger.error(f"Failed to save training args: {e}")

# --- Data loading and dataset utils ---
def _get_paths_recursive(paths, level):
    input_rec = paths
    for _ in range(level):
        new_inps = []
        for i in input_rec:
            p = Path(i)
            if p.is_dir():
                children = [str(x) for x in p.iterdir() if x.is_dir() or x.suffix == ".tif"]
                new_inps.extend(children)
            elif p.suffix == ".tif":
                new_inps.append(str(p))
        input_rec = new_inps
    return input_rec

def _build_dataset(
    imgs, split, size, args, n_frames, delta_frames,
    augmenter=None, permute=True, random_crop=True, reject_background=False,
):
    return TarrowDataset(
        imgs=imgs,
        split_start=split[0],
        split_end=split[1],
        n_images=args.n_images,
        n_frames=n_frames,
        delta_frames=delta_frames,
        subsample=args.subsample,
        size=size,
        mode="flip",
        permute=permute,
        augmenter=augmenter,
        device="cpu",
        channels=args.channels,
        binarize=args.binarize,
        random_crop=random_crop,
        reject_background=reject_background,
    )

def _subset(data: Dataset, split=(0, 1.0)):
    low, high = int(len(data) * split[0]), int(len(data) * split[1])
    return Subset(data, range(low, high))

def _create_loader(dataset, args, num_samples, num_workers, idx=None, sequential=False):
    return torch.utils.data.DataLoader(
        dataset,
        sampler=(
            torch.utils.data.SequentialSampler(
                torch.utils.data.Subset(
                    dataset,
                    torch.multinomial(
                        torch.ones(len(dataset)), num_samples, replacement=True
                    ),
                )
            )
            if sequential
            else torch.utils.data.RandomSampler(
                dataset, replacement=True, num_samples=num_samples
            )
        ),
        batch_size=args.batchsize,
        num_workers=num_workers,
        drop_last=False,
        persistent_workers=True if num_workers > 0 else False,
    )

def _build_outdir_path(args) -> Path:
    name = ""
    if args.timestamp:
        timestamp = f'{datetime.now().strftime("%m-%d-%H-%M-%S")}'
        name = f"{timestamp}_"
    suffix = f"backbone_{args.backbone}"
    name = f"{name}{args.name}_{suffix}"
    outdir = Path(args.outdir).resolve()
    outdir_path = outdir / name
    if outdir_path.exists():
        logger.info(f"Run name `{name}` already exists, prepending timestamp.")
        timestamp = f'{datetime.now().strftime("%m-%d-%H-%M-%S")}'
        name = f"{timestamp}_{name}"
        outdir_path = outdir / name
    else:
        logger.info(f"Run name `{name}`")
    return outdir_path

def _convert_to_split_pairs(lst):
    if all(isinstance(x, (tuple, list)) and len(x) == 2 for x in lst):
        return tuple(lst)
    else:
        lst = tuple(
            elem for x in lst for elem in (x if isinstance(x, (list, tuple)) else (x,))
        )
        if len(lst) % 2 == 0:
            return tuple(lst[i : i + 2] for i in range(0, len(lst), 2))
        else:
            raise ValueError(f"length of split {lst} should be even!")

def _write_cams(data_visuals, model, device):
    for i, data in enumerate(data_visuals):
        create_visuals(
            dataset=data,
            model=model,
            device=device,
            max_height=720,
            outdir=model.outdir / "visuals" / f"dataset_{i}",
        )

# --- Main training logic ---
def main(args):
    if not args.input_train:
        raise ValueError("Missing required field: input_train (use CLI or YAML).")
    if not args.split_train:
        raise ValueError("Missing required field: split_train (use CLI or YAML).")
    if not args.split_val:
        raise ValueError("Missing required field: split_val (use CLI or YAML).")

    if platform.system() == "Darwin":
        args.num_workers = 0
        logger.warning("Setting num_workers to 0 to avoid MacOS multiprocessing issues.")

    if args.input_val is None:
        args.input_val = args.input_train

    args.split_train = _convert_to_split_pairs(args.split_train)
    args.split_val = _convert_to_split_pairs(args.split_val)

    outdir = _build_outdir_path(args)
    outdir.mkdir(parents=True, exist_ok=True)

    for p in args.input_train:
        if not Path(p).exists():
            raise FileNotFoundError(f"Training path not found: {p}")
    if args.input_val:
        for p in args.input_val:
            if not Path(p).exists():
                raise FileNotFoundError(f"Validation path not found: {p}")

    try:
        repo = git.Repo(Path(__file__).resolve().parents[1])
        args.tarrow_experiments_commit = str(repo.commit())
    except git.InvalidGitRepositoryError:
        pass

    # ---- BEGIN DEVICE SETUP BLOCK (AUTOMATED) ----
    tarrow.utils.seed(args.seed)
    try:
        use_gpu = (
            hasattr(args, "gpu")
            and args.gpu is not None
            and str(args.gpu).lower() not in ["none", "cpu"]
            and torch.cuda.is_available()
        )
        if use_gpu:
            device, n_gpus = tarrow.utils.set_device(args.gpu)
            if n_gpus > 1:
                raise NotImplementedError("Multi-GPU training not implemented yet.")
        else:
            raise RuntimeError("No GPU available or requested. Using CPU.")
    except Exception as e:
        logger.warning(f"Could not set GPU device ({e}), falling back to CPU.")
        device = torch.device("cpu")
        n_gpus = 0
    logger.info(f"Using device: {device}")
    # ---- END DEVICE SETUP BLOCK (AUTOMATED) ----

    augmenter = get_augmenter(args.augment)

    # Collect input paths (recursively if needed)
    inputs = {}
    for inp, phase in zip((args.input_train, args.input_val), ("train", "val")):
        inputs[phase] = _get_paths_recursive(inp, args.read_recursion_level)
        logger.debug(f"{phase} datasets: {inputs[phase]}")

    # Build visualisation datasets
    logger.info("Build visualisation datasets.")
    data_visuals = tuple(
        _build_dataset(
            inp,
            split=(0, 1.0),
            size=None if args.cam_size is None else (args.cam_size,) * args.ndim,
            args=args,
            n_frames=args.frames,
            delta_frames=args.delta[-1:],
            permute=False,
            random_crop=False,
        )
        for inp in set([*inputs["train"], *inputs["val"]])
    )

    # Build train datasets
    logger.info("Build training datasets.")
    data_train = ConcatDataset(
        _build_dataset(
            inp,
            split=split,
            size=(args.size,) * args.ndim,
            args=args,
            n_frames=args.frames,
            delta_frames=args.delta,
            augmenter=augmenter,
            reject_background=args.reject_background,
        )
        for split in args.split_train
        for inp in inputs["train"]
    )

    # Build validation datasets
    logger.info("Build validation datasets.")
    data_val = ConcatDataset(
        _build_dataset(
            inp,
            split,
            size=(args.size,) * args.ndim,
            args=args,
            n_frames=args.frames,
            delta_frames=args.delta,
        )
        for split in args.split_val
        for inp in inputs["val"]
    )

    loader_train = _create_loader(
        data_train, args=args, num_samples=args.train_samples_per_epoch, num_workers=args.num_workers
    )
    loader_val = _create_loader(
        data_val, args=args, num_samples=args.val_samples_per_epoch, num_workers=0
    )

    logger.info(f"Training set: {len(data_train)} images")
    logger.info(f"Validation set: {len(data_val)} images")

    model_kwargs = dict(
        backbone=args.backbone,
        projection_head=args.projhead,
        classification_head=args.classhead,
        n_frames=args.frames,
        n_input_channels=args.channels if args.channels > 0 else 1,
        n_features=args.features,
        device=device,
        symmetric=args.perm_equiv,
        outdir=outdir,
    )

    model = TimeArrowNet(**model_kwargs)
    model.to(device)

    logger.info(f"Number of params: {sum(p.numel() for p in model.parameters())/1.e6:.2f} M")

    # --- Save configs with improved error handling ---
    save_partial_config(args, outdir)
    save_full_config(args, outdir)

    assert args.ndim == 2

    model.fit(
        loader_train=loader_train,
        loader_val=loader_val,
        lr=args.lr,
        lr_scheduler=args.lr_scheduler,
        lr_patience=args.lr_patience,
        epochs=args.epochs,
        steps_per_epoch=args.train_samples_per_epoch // args.batchsize,
        visual_datasets=tuple(
            Subset(d, list(range(0, len(d), 1 + (len(d) // args.cam_subsampling))))
            for d in data_visuals
        ),
        visual_dataset_frequency=args.visual_dataset_frequency,
        tensorboard=bool(args.tensorboard),
        save_checkpoint_every=args.save_checkpoint_every,
        lambda_decorrelation=args.decor_loss,
    )

    if args.write_final_cams:
        _write_cams(data_visuals, model, device)

if __name__ == "__main__":
    parser = get_argparser()
    args, unknown = parser.parse_known_args()
    if unknown:
        logger.warning(f"Unknown config fields detected (ignored): {unknown}")
    main(args)
