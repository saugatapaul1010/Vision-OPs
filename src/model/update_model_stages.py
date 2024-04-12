"""This module updates registered model version stages in a MLflow registry
and saves metric plots for a production stage model.
"""

import argparse
import json
import logging
import os
from pathlib import Path

import mlflow

from src.utils import draw_production_model_metric_history_plots, get_param_config_yaml

logging.basicConfig(level=logging.INFO, filename='pipe.log',
                    format="%(asctime)s -- [%(levelname)s]: %(message)s")


def update_registered_model_version_stages(mlclient, registered_model_name):
    """Change the stage from 'None' to 'Production' for the latest model version
    and from 'Production' to 'Archived' if such a version exists but is not the latest.
    """
    # Get information about the registered model
    model_registry_info = mlclient.get_latest_versions(registered_model_name)
    model_latest_version = max([m.version for m in model_registry_info])

    # Update model version stages
    for m in model_registry_info:
        if m.version == model_latest_version:
            if m.current_stage == 'Production':
                continue
            else:
                m = mlclient.transition_model_version_stage(name=registered_model_name,
                                                            version=m.version,
                                                            stage='Production')
        else:
            if m.current_stage == 'Production':
                m = mlclient.transition_model_version_stage(name=registered_model_name,
                                                            version=m.version,
                                                            stage='Archived')

    # View updated model version stages
    prod_run_id = 0
    prod_model_id = 0
    for m in mlclient.get_latest_versions(registered_model_name):
        logging.info("Updated model version stages: ")
        logging.info(f"{m.name}: version: {m.version}, current stage: {m.current_stage}")

        if m.current_stage == 'Production':
            prod_run_id = m.run_id
            prod_model_id = 'models:/{0}/{1}'.format(m.name, m.version)

    return prod_run_id, prod_model_id


def main(project_path, param_config, save_metric_plots=False):
    """Update version stages for a registered model, return run and model ids,
    and create and return metric plots for a model with 'Production' stage.
    """
    registered_model_name = param_config['object_detection_model']['registered_name']
    client = mlflow.MlflowClient()
    production_run_id, production_model_id = update_registered_model_version_stages(
        client, registered_model_name)
    logging.info("Stages are updated.")

    mltraining_conf = param_config['model_training_inference_conf']
    save_path = (project_path.joinpath(mltraining_conf['save_model_output_dir'])
                 if save_metric_plots else None)
    metric_plots = []

    for metric in mltraining_conf['metrics_to_plot']:
        metric_plots += draw_production_model_metric_history_plots(metric, client,
                                                                   registered_model_name,
                                                                   save_path=save_path)
    logging.info("Metric plots of production stage model are saved.")
    return production_run_id, production_model_id, metric_plots


if __name__ == '__main__':
    project_path = Path.cwd()
    param_config = get_param_config_yaml(project_path)
    mlflow.set_tracking_uri(os.environ.get('MLFLOW_TRACKING_URI',
                            param_config['mlflow_tracking_conf']['mltracking_uri']))

    run_parser = argparse.ArgumentParser(
        description='Specify a condition to run this module.',
        add_help=False)
    run_parser.add_argument(
        '--only_if_test_score_is_best', type=bool,
        default=False, help='whether to run this module only if the test score is the best')

    if run_parser.parse_args().only_if_test_score_is_best:
        test_score_path = project_path.joinpath(
            param_config['model_training_inference_conf']['save_model_output_dir'],
            'test_outs/test_score.json')
        with open(test_score_path) as f:
            test_score_is_best = json.load(f)['best']

        if test_score_is_best:
            _ = main(project_path, param_config, save_metric_plots=True)
        else:
            logging.info("Stage update did not run: the test score is not the best!")

    else:
        _ = main(project_path, param_config)
