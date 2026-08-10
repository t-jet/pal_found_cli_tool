# DataHealth metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `data_health.check.create` | BLOCKED | Creates a check |
| `data_health.check.delete` | BLOCKED | Deletes a check |
| `data_health.check.get` | PERMITTED | Single check metadata |
| `data_health.check.replace` | BLOCKED | Replaces a check configuration |
| `data_health.check_report.get` | PERMITTED | Single check report metadata |
| `data_health.check_report.get_latest` | PERMITTED | Latest check report metadata |
