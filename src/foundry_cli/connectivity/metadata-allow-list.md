# Connectivity metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `connectivity.connection.create` | BLOCKED | Creates a connection |
| `connectivity.connection.get` | PERMITTED | Connection metadata |
| `connectivity.connection.get_configuration` | PERMITTED | Connection configuration metadata |
| `connectivity.connection.get_configuration_batch` | PERMITTED | Batch connection configuration metadata |
| `connectivity.connection.update_export_settings` | BLOCKED | Mutates connection export settings |
| `connectivity.connection.update_secrets` | BLOCKED | Mutates connection secrets |
| `connectivity.connection.upload_custom_jdbc_drivers` | BLOCKED | Uploads JDBC driver binary content |
| `connectivity.file_import.create` | BLOCKED | Creates a file import |
| `connectivity.file_import.delete` | BLOCKED | Deletes a file import |
| `connectivity.file_import.execute` | BLOCKED | Runs a file import build |
| `connectivity.file_import.get` | PERMITTED | File import metadata |
| `connectivity.file_import.list` | PERMITTED | File import metadata |
| `connectivity.file_import.replace` | BLOCKED | Replaces a file import |
| `connectivity.table_import.create` | BLOCKED | Creates a table import |
| `connectivity.table_import.delete` | BLOCKED | Deletes a table import |
| `connectivity.table_import.execute` | BLOCKED | Runs a table import build |
| `connectivity.table_import.get` | PERMITTED | Table import metadata |
| `connectivity.table_import.list` | PERMITTED | Table import metadata |
| `connectivity.table_import.replace` | BLOCKED | Replaces a table import |
| `connectivity.virtual_table.create` | BLOCKED | Creates a virtual table |
