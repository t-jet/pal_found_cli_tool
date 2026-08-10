# Widgets metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `widgets.dev_mode_settings.enable` | BLOCKED | Enables dev mode (token-scoped write) |
| `widgets.dev_mode_settings.set_widget_set_by_id` | BLOCKED | Mutates dev mode settings by widget ID |
| `widgets.release.delete` | BLOCKED | Deletes a widget set release |
| `widgets.release.get` | PERMITTED | Widget set release metadata |
| `widgets.release.list` | PERMITTED | Widget set release list metadata |
| `widgets.repository.get` | PERMITTED | Repository metadata |
| `widgets.repository.publish` | BLOCKED | Publishes a widget set release zip |
| `widgets.widget_set.get` | PERMITTED | Widget set metadata |
