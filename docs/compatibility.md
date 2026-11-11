# Radar: compatibility

## Hosts

| Hermes | Status |
|---|---|
| 0.23.x | Tested. Runs the author's daily setup. |
| 0.22.x | Supported floor (`requires_hermes: ">=0.22.0"`). Every surface below exists there except the `post_compaction` hook, which is simply never fired; conversation lineage then comes only from the session store. |

## Host surfaces used

- **Plugin context:** `register(ctx)`, `ctx.register_tool`, `ctx.register_hook`, `ctx.register_command`, `ctx.register_cli_command`, `ctx.register_platform_handler` (on Telegram, a PTB `CallbackQueryHandler` scoped to Radar's own callback prefixes; on every other platform, only the adapter's `send`, `edit_message` and `send_document`), `ctx.dispatch_tool`, `ctx.spawn_task`, `ctx.llm` with plugin-owned auxiliary slots, `ctx.get_config` / `ctx.set_config`, `ctx.on_unload`.
- **Hooks:** `on_session_start`, `post_llm_call`, `subagent_stop`, `post_compaction`, `pre_gateway_dispatch`, `pre_llm_call`, `pre_tool_call`.
- **Host tools, dispatched through the plugin context:** `session_search` (past sessions) and `cronjob_manage` (create and list scheduled jobs).
- **Public modules:** `hermes_constants`, `hermes_cli.config`, `hermes_state.SessionDB`, `agent.secret_scope`, `gateway.session_context.get_session_env`.
- **Host CLI used by delegated jobs:** `hermes chat --query-file - --oneshot --format stream-json --max-turns N [--in DIR] [-t ...] [-s ...] [-m ...]`.
- **Gateway contracts:** events of the `webhook` platform reach `pre_gateway_dispatch` with `source.user_id == "webhook:<route>"` and the payload as `raw_message`; `{"action": "skip"}` avoids the agent turn. `MessageEvent.reply_to_message_id` identifies replies to notices.

## Outside the public list

- `plugins.memory.load_memory_provider`, to ask the host's active memory provider a question from background work. There is no public seam for this yet; the call is defensive and recall simply returns nothing when it fails.
- The session store file (`state.db`) is also read directly, read-only, for two things the public API does not offer: the lineage of older compactions and whether a conversation has a turn in progress.

## How it is checked

- `tests/unit`: the pipeline with a simulated model, the store (scope isolation, leases, migrations), delivery, replies, jobs, the tool barrier, native operations, sources and connectors.
- `tests/contract`: run under the host's own interpreter with `HERMES_AGENT_DIR` set (command in the README), so a host upgrade that moves a surface fails there and not in production: MCP tool naming, `pre_tool_call` arguments and veto, hook names, the fail-closed timeout, the host's tool names, the secret scope.
- `hermes plugins validate .`: the catalog's admission checks.
- `scenarios/`: situations run through the real pipeline with the real model (`hermes radar simulate`).
