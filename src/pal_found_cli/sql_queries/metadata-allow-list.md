# SqlQueries metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `sql_queries.sql_query.cancel` | BLOCKED | Cancels a running query |
| `sql_queries.sql_query.execute` | BLOCKED | Executes billable SQL work |
| `sql_queries.sql_query.execute_ontology` | BLOCKED | Executes billable SQL against the Ontology |
| `sql_queries.sql_query.get_results` | BLOCKED | Streams query result content bytes |
| `sql_queries.sql_query.get_status` | PERMITTED | Query status metadata |
