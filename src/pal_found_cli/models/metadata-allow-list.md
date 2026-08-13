# Models metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `models.experiment.get` | PERMITTED | Experiment metadata |
| `models.experiment.search` | PERMITTED | Experiment metadata search |
| `models.experiment_artifact_table.json` | BLOCKED | Streams table content bytes |
| `models.experiment_artifact_table.parquet` | BLOCKED | Streams table content bytes |
| `models.experiment_series.json` | BLOCKED | Streams series content bytes |
| `models.experiment_series.parquet` | BLOCKED | Streams series content bytes |
| `models.live_deployment.transform_json` | BLOCKED | Executes model inference |
| `models.model.create` | BLOCKED | Creates a model |
| `models.model.get` | PERMITTED | Model metadata |
| `models.model.promote_version` | BLOCKED | Mutates model versions |
| `models.model_studio.create` | BLOCKED | Creates a Model Studio |
| `models.model_studio.get` | PERMITTED | Model Studio metadata |
| `models.model_studio.launch` | BLOCKED | Launches billable training runs |
| `models.model_studio_config_version.create` | BLOCKED | Creates a configuration version |
| `models.model_studio_config_version.get` | PERMITTED | Configuration version metadata |
| `models.model_studio_config_version.latest` | PERMITTED | Configuration version metadata |
| `models.model_studio_config_version.list` | PERMITTED | Configuration version metadata |
| `models.model_studio_run.list` | PERMITTED | Run metadata |
| `models.model_studio_trainer.get` | PERMITTED | Trainer metadata |
| `models.model_studio_trainer.list` | PERMITTED | Trainer metadata |
| `models.model_version.create` | BLOCKED | Creates a model version |
| `models.model_version.get` | PERMITTED | Model version metadata |
| `models.model_version.list` | PERMITTED | Model version metadata |
