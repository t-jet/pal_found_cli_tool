# Orchestration metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `orchestration.build.cancel` | BLOCKED | Mutates build state |
| `orchestration.build.create` | BLOCKED | Creates builds |
| `orchestration.build.get` | PERMITTED | Build metadata |
| `orchestration.build.get_batch` | PERMITTED | Build metadata |
| `orchestration.build.jobs` | PERMITTED | Job metadata |
| `orchestration.build.search` | PERMITTED | Build metadata search |
| `orchestration.job.get` | PERMITTED | Job metadata |
| `orchestration.job.get_batch` | PERMITTED | Job metadata |
| `orchestration.schedule.create` | BLOCKED | Creates schedules |
| `orchestration.schedule.delete` | BLOCKED | Deletes schedules |
| `orchestration.schedule.get` | PERMITTED | Schedule metadata |
| `orchestration.schedule.get_affected_resources` | PERMITTED | Affected resource metadata |
| `orchestration.schedule.get_batch` | PERMITTED | Schedule metadata |
| `orchestration.schedule.pause` | BLOCKED | Mutates schedule state |
| `orchestration.schedule.replace` | BLOCKED | Replaces schedules |
| `orchestration.schedule.run` | BLOCKED | Triggers schedule runs |
| `orchestration.schedule.runs` | PERMITTED | Run metadata |
| `orchestration.schedule.unpause` | BLOCKED | Mutates schedule state |
| `orchestration.schedule_version.get` | PERMITTED | Schedule version metadata |
| `orchestration.schedule_version.schedule` | PERMITTED | Schedule metadata |
