"""This module implements fine-tuning of object detection model."""

import gc
import logging
import os
import random
from pathlib import Path

import mlflow
import numpy as np
import torch
import torchvision

from src.data.image_dataloader import create_dataloaders
from src.model.object_detection_model import faster_rcnn_mob_model_for_n_classes
from src.train.train_inference_fns import eval_one_epoch, train_one_epoch
from src.utils import (draw_bboxes_on_image, get_device, get_param_config_yaml,
                       save_model_state)

logging.basicConfig(level=logging.INFO, filename='pipe.log',
                    format="%(asctime)s -- [%(levelname)s]: %(message)s")

# Set partial reproducibility
SEED = 0
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)


def run_train(train_dataloader, val_dataloader, model, epochs, optimizer_name,
              optimizer_parameters, save_best_model_weights_path=None, lr_scheduler_name=None,
              lr_scheduler_parameters=None, device=torch.device('cpu'),  # noqa: B008
              metric_to_find_best_model=None, init_metric_value=0.0,
              eval_iou_thresh=0.5, eval_beta=1, model_name='best_model', save_best_ckpt=False,
              checkpoint=None, log_metrics=False, register_best_log_model=False,
              reg_model_name='best_model', save_random_best_model_output_path=None):
    """Run a new model training and evaluation cycle for the fixed number of epochs
    or continue if a checkpoint is set, while saving the best model weights
    (or a checkpoint).

    Parameters
    ----------
    train_dataloader: DataLoader
        Images, labels, and boxes for a training step.
    val_dataloader: DataLoader
        Images, labels, and boxes for an evaluation step.
    model: nn.Module
        Object detection model.
    epochs: int
        The number of training epochs.
    optimizer_name: str
        Optimizer name from torch.optim.
    optimizer_parameters: dict
        Relevant parameters for the optimizer.
    save_best_model_weights_path: Path, optional
        Path to save the best model weights (state directory)
        or checkpoint (default None).
    lr_scheduler_name: str, optional
        Learning rate scheduler name from torch.optim.lr_scheduler (default None).
    lr_scheduler_parameters: dict, optional
        Relevant parameters for the learning rate scheduler (default None).
    device: torch.device('cpu'|'cuda')
        Type of device used (default torch.device('cpu')).
    metric_to_find_best_model: str, optional
        Metric name to track its values to find the best model (default None).
    init_metric_value: float
        Initial metric value to find the best model (default 0.0).
    eval_iou_thresh: float
        IOU threshold to determine correct predicted boxes (default 0.5).
    eval_beta: int
        Beta value for f_beta score (default 1).
    model_name: str
        Part of file name to save the best model weights or checkpoint
        (default 'best_model').
    save_best_ckpt: bool
        Whether to save the best model weights (default) or checkpoint (default False).
    checkpoint: dict, optional
        Checkpoint to continue training (default None).
    log_metrics: bool
        Whether to log metrics into MLflow (default False).
    register_best_log_model: bool
        Whether to log and register the best model into MLflow (default False).
    reg_model_name: str
        Model registration name (default 'best_model').
    save_random_best_model_output_path: Path, optional
        Path to a directory to save a random image with
        the best model prediction boxes and scores drawn on it (default None).

    Return
    ------
        A dictionary of training and evaluation results.
    """
    logging.info(f"Device: {device}")
    start_epoch = 0
    best_epoch_score = init_metric_value
    lr_scheduler = None

    model_params = [p for p in model.parameters() if p.requires_grad]
    # Construct an optimizer
    optimizer = getattr(torch.optim, optimizer_name)(model_params, **optimizer_parameters)

    if lr_scheduler_name is not None:
        if lr_scheduler_parameters is None:
            lr_scheduler_parameters = {}
        # Construct a learning rate scheduler
        lr_scheduler = getattr(torch.optim.lr_scheduler, lr_scheduler_name)(optimizer,
                                                                            **lr_scheduler_parameters)

    if checkpoint is not None:
        # Get state parameters from the checkpoint
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch']
        best_epoch_score = (checkpoint[metric_to_find_best_model + '_score']
                            if metric_to_find_best_model else 0.0)

    model.to(device)

    for epoch in range(1, epochs + 1):
        current_epoch = start_epoch + epoch
        logging.info(f"EPOCH [{current_epoch}/{start_epoch + epochs}]: ")

        # Training step
        logging.info("TRAIN:")
        train_res = train_one_epoch(train_dataloader, model, optimizer, device)
        logging.info("  epoch loss: {0}:\n    {1}".format(train_res['epoch_loss'],
                                                          train_res['epoch_dict_losses']))

        if lr_scheduler is not None:
            lr_scheduler.step()

        # Evaluation step
        logging.info("EVAL:")
        eval_res = eval_one_epoch(val_dataloader, model, eval_iou_thresh, eval_beta, device)
        logging.info("\n  epoch scores: {}".format(eval_res['epoch_scores']))

        if metric_to_find_best_model:
            # Save a model with the maximum epoch score
            if best_epoch_score < eval_res['epoch_scores'][metric_to_find_best_model]:
                best_epoch_score = eval_res['epoch_scores'][metric_to_find_best_model]
                ckpt_dict = None
                filename = model_name + f'_best_{metric_to_find_best_model}_{eval_beta}_weights'

                if register_best_log_model:
                    # Log and register the best model into MLflow
                    mlflow.pytorch.log_model(model, filename,
                                             registered_model_name=reg_model_name,
                                             await_registration_for=10,
                                             pip_requirements=[f'torch={torch.__version__}',
                                                               f'torchvision={torchvision.__version__}'])
                if save_best_ckpt:
                    ckpt_dict = {'epoch': current_epoch,
                                 'optimizer_state_dict': optimizer.state_dict(),
                                 metric_to_find_best_model + '_score': best_epoch_score}
                    filename += '_ckpt'

                if save_best_model_weights_path is not None:
                    save_model_state(model, save_best_model_weights_path / f'{filename}.pt',
                                     ckpt_dict)
                    logging.info("Model weights are saved. --- The best {} score: {}".format(
                        metric_to_find_best_model, best_epoch_score))

                with torch.no_grad():
                    if save_random_best_model_output_path:
                        sample_imgs, _ = next(iter(val_dataloader))
                        sample_idx = random.randint(0, len(sample_imgs) - 1)  # nosec
                        preds = eval_res['results'][sample_idx]
                        save_img_out_path = (save_random_best_model_output_path /  # noqa: W504
                                             f'val_outs/epoch_{current_epoch}.jpg')
                        _ = draw_bboxes_on_image(sample_imgs[sample_idx],
                                                 preds['boxes'], preds['scores'],
                                                 save_img_out_path=save_img_out_path)
                        del sample_imgs
                        del preds

            if log_metrics:
                # Log losses and scores into MLflow
                mlflow.log_metric('train_epoch_loss', train_res['epoch_loss'],
                                  step=current_epoch)
                mlflow.log_metrics(train_res['epoch_dict_losses'], step=current_epoch)
                mlflow.log_metrics(eval_res['epoch_scores'], step=current_epoch)
                logging.info("Metrics are logged.")

        # Free up memory
        gc.collect()
        if str(device) == 'cuda':
            torch.cuda.empty_cache()

        logging.info("-" * 60)

    logging.info("DONE!")
    return {'train_res': train_res,
            'eval_res': eval_res}


def main(project_path, param_config):
    """Perform fine-tuning of object detection model."""
    img_data_paths = param_config['image_data_paths']
    TRAIN_EVAL_PARAMS = param_config['model_training_inference_conf']
    device = get_device(TRAIN_EVAL_PARAMS['device_cuda'])

    # Get DataLoader objects
    imgs_path, train_csv_path, bbox_csv_path = [
        project_path / fpath for fpath in [img_data_paths['images'],
                                           img_data_paths['train_csv_file'],
                                           img_data_paths['bboxes_csv_file']]]
    batch_size = param_config['image_dataset_conf']['batch_size']
    train_dl, val_dl = create_dataloaders(imgs_path, train_csv_path, bbox_csv_path, batch_size,
                                          train_test_split_data=True, transform_train_imgs=True)

    # Load a modified model
    model_params = param_config['object_detection_model']['load_parameters']
    num_classes = param_config['object_detection_model']['number_classes']
    faster_rcnn_mob_model = faster_rcnn_mob_model_for_n_classes(num_classes, **model_params)

    # Load the best parameters for training if a file with them exists
    best_params_path = param_config['hyperparameter_optimization']['save_best_parameters_path']
    best_params = None
    if best_params_path and (project_path / best_params_path).exists():
        best_params = get_param_config_yaml(project_path, best_params_path)
        logging.info(f"The best training parameters are loaded: \n{best_params}")

    # Set training parameters
    train_params = {}
    for param in ['optimizer', 'lr_scheduler']:
        for k in ['name', 'parameters']:
            val = best_params[param][k] if best_params else TRAIN_EVAL_PARAMS[param][k]
            train_params['_'.join([param, k])] = val

    init_metric_value = TRAIN_EVAL_PARAMS['initial_metric_value']
    add_train_params = {'epochs': TRAIN_EVAL_PARAMS['epochs'],
                        'eval_iou_thresh': TRAIN_EVAL_PARAMS['evaluation_iou_threshold'],
                        'eval_beta': TRAIN_EVAL_PARAMS['evaluation_beta'],
                        'device': device}

    checkpoint = None
    save_dir = param_config['object_detection_model']['save_dir']
    if TRAIN_EVAL_PARAMS['checkpoint']:
        checkpoint_path = project_path / save_dir / TRAIN_EVAL_PARAMS['checkpoint']
        checkpoint = torch.load(checkpoint_path) if checkpoint_path.exists() else None

    # Set paths to save the best model weights and outputs
    save_best_model_weights_path = project_path / save_dir if save_dir else None
    save_output_path = (project_path / TRAIN_EVAL_PARAMS['save_model_output_dir']
                        if TRAIN_EVAL_PARAMS['save_model_output_dir'] else None)

    # Train the model (fine-tune) and log metrics and parameters into MLflow
    mlflow_conf = param_config['mlflow_tracking_conf']
    ftm_exp = mlflow.get_experiment_by_name(mlflow_conf['experiment_name'])

    if ftm_exp is not None:
        ftm_exp_id = ftm_exp.experiment_id
    else:
        ftm_exp_id = mlflow.create_experiment(
            mlflow_conf['experiment_name'],
            artifact_location=project_path.joinpath(
                mlflow_conf['artifact_location']).as_uri())

    with mlflow.start_run(run_name=mlflow_conf['run_name'],
                          experiment_id=ftm_exp_id):

        mlflow.set_tags({'training_process': 'fine_tuning',
                         'model_name': param_config['object_detection_model']['name'],
                         'tools.training': 'PyTorch'})

        # Run model training cycles
        _ = run_train(train_dl, val_dl, faster_rcnn_mob_model,
                      save_best_model_weights_path=save_best_model_weights_path,
                      metric_to_find_best_model=TRAIN_EVAL_PARAMS['metric_to_find_best'],
                      init_metric_value=init_metric_value,
                      log_metrics=TRAIN_EVAL_PARAMS['log_metrics'],
                      save_best_ckpt=TRAIN_EVAL_PARAMS['save_best_ckpt'],
                      model_name=param_config['object_detection_model']['name'],
                      register_best_log_model=TRAIN_EVAL_PARAMS['register_best_log_model'],
                      reg_model_name=param_config['object_detection_model']['registered_name'],
                      save_random_best_model_output_path=save_output_path,
                      checkpoint=checkpoint, **train_params, **add_train_params)

        # Log the parameters into MLflow
        mlflow.log_params(model_params)
        mlflow.log_params({'seed': SEED,
                           'batch_size': batch_size,
                           'num_classes': num_classes})
        mlflow.log_params(add_train_params)

        for params in train_params:
            if params in ['optimizer_parameters', 'lr_scheduler_parameters']:
                if train_params[params] is not None:
                    mlflow.log_params(train_params[params])
            else:
                mlflow.log_param(params, train_params[params])

        logging.info("Parameters are logged.")


if __name__ == '__main__':
    project_path = Path.cwd()
    param_config = get_param_config_yaml(project_path)
    mlflow.set_tracking_uri(os.environ.get('MLFLOW_TRACKING_URI',
                            param_config['mlflow_tracking_conf']['mltracking_uri']))
    main(project_path, param_config)
