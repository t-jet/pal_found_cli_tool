# AIP Agents metadata allow-list

| SDK Path | Status | Rationale |
|---|---|---|
| `aip_agents.agent.all_sessions` | BLOCKED | Cross-agent session content |
| `aip_agents.agent.get` | PERMITTED | Agent metadata |
| `aip_agents.agent_version.get` | PERMITTED | Version metadata |
| `aip_agents.agent_version.list` | PERMITTED | Version metadata |
| `aip_agents.content.get` | BLOCKED | Session content |
| `aip_agents.session.blocking_continue` | BLOCKED | Mutates session |
| `aip_agents.session.cancel` | BLOCKED | Mutates session |
| `aip_agents.session.create` | BLOCKED | Creates session |
| `aip_agents.session.delete` | BLOCKED | Deletes session |
| `aip_agents.session.get` | PERMITTED | Session metadata |
| `aip_agents.session.list` | PERMITTED | Session metadata |
| `aip_agents.session.purge` | BLOCKED | Deletes local state |
| `aip_agents.session.rag_context` | BLOCKED | Processes session input |
| `aip_agents.session.streaming_continue` | BLOCKED | Mutates session and returns content |
| `aip_agents.session.update_title` | BLOCKED | Mutates session |
| `aip_agents.session_trace.get` | PERMITTED | Trace metadata |
