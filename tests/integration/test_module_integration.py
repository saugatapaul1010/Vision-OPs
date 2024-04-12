import shutil
from pathlib import Path

import mlflow
import pandas as pd
import pytest
import yaml

# isort: off
from src.data.update_raw_data import main as update_raw_data
from src.data.prepare_data import main as prepare_data
from src.train.optimize_hyperparams import main as optimize_hyperparams
from src.train.fine_tune_model import main as fine_tune_model
from src.train.model_test_performance import main as model_test_performance
from src.model.update_model_stages import main as update_model_stages
from src.model.generate_model_report import main as generate_model_report

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def example_config():
    config_path = Path('tests/data_samples/example_config.yaml').absolute()
    with open(config_path) as conf:
        config = yaml.safe_load(conf)
    return config


def test_src_package_pipeline(example_config, val_df, train_val_df, tmp_path):
    # Arrange
    _ = shutil.copytree(Path.cwd().joinpath('tests/data_samples'), tmp_path / 'datas',
                        ignore=shutil.ignore_patterns('*.yaml', '*val.csv', '*train.csv'))
    mlflow.set_tracking_uri(f'sqlite:///{tmp_path}/tmlruns.db')
    exp_id = mlflow.create_experiment(example_config['mlflow_tracking_conf']['experiment_name'],
                                      tmp_path.joinpath('tmlruns').as_uri())

    # Act
    _ = prepare_data(tmp_path, example_config, save_eda_plots=True)
    update_raw_data(tmp_path, example_config)
    optimize_hyperparams(tmp_path, example_config)
    fine_tune_model(tmp_path, example_config)
    _ = model_test_performance(tmp_path, example_config, get_random_prediction_image=True)
    _ = update_model_stages(tmp_path, example_config, save_metric_plots=True)
    generate_model_report(tmp_path, example_config)

    # Result
    prepared_test_df = (
        pd.read_csv(tmp_path / example_config['image_data_paths']['test_csv_file']))
    updated_train_df = (
        pd.read_csv(tmp_path / example_config['image_data_paths']['train_csv_file'])
        .sort_values('Name', ignore_index=True))
    client = mlflow.MlflowClient()
    model_reg_info = client.get_latest_versions('best_tfrcnn')
    test_res_run = client.search_runs(
        [exp_id], "attributes.run_name='test-fine-tuning'")[0].info.run_id
    mlst_version = max([m.version for m in model_reg_info])

    assert prepared_test_df.equals(val_df)
    assert len([ch for ch in (tmp_path / 'res/plots/eda').iterdir()]) == 4
    assert updated_train_df.equals(train_val_df.sort_values('Name', ignore_index=True))
    assert (tmp_path / 'res/hyper_opt_studies.db').exists()
    assert len([ch for ch in (tmp_path / 'res/tfrcnn_study/plots').iterdir()]) == 7
    assert (tmp_path / 'res/best_tparams.yaml').exists()
    assert [ch for ch in (tmp_path / 'res/val_outs').iterdir()]
    assert client.get_metric_history(test_res_run, 'f_beta')
    assert [ch for ch in (tmp_path / 'res/test_outs').iterdir()]
    assert client.get_model_version('best_tfrcnn', mlst_version).current_stage == 'Production'
    assert len([ch for ch in (tmp_path / 'res/plots/metrics').iterdir()]) == 2
    assert (tmp_path / 'reports/model_report.md').exists()
