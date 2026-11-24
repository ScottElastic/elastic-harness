# Architecture and verified API facts

All facts verified on Kibana/Elasticsearch **9.6.0** (Elastic Cloud, traditional) in 2026-11 unless marked
*(source)*, meaning read from Kibana `main` and not exercised, or *(docs)*.

## What the harness calls

| Need | Call | Notes |
|---|---|---|
| List agents | `GET /api/agent_builder/agents` | `results[]`; each has `access_control`, `configuration`, `type`, `readonly` |
| Chat, streaming | `POST /api/agent_builder/converse/async` | SSE. Body: `agent_id`, `input`, `conversation_id`, `inference_id` *or* `connector_id`, `configuration_overrides`, `prompts`, `attachments` |
| Chat, single reply | `POST /api/agent_builder/converse` | Returns `conversation_id`, `response.message`, `steps`, `model_usage` |
| Conversations | `GET/DELETE /api/agent_builder/conversations[/{id}]` | History is stored in Kibana, per user |
| List / run tools | `GET /api/agent_builder/tools`, `POST …/tools/_execute {tool_id, tool_params}` | 81 built-in tools on this cluster |
| Create tool | `POST /api/agent_builder/tools` | `index_search` takes `configuration.pattern` *(docs)* |
| Attach tool to agent | `PUT /api/agent_builder/agents/{id} {configuration}` | Full `configuration` round-trips cleanly |
| List models | `GET /_inference` filtered to `task_type: chat_completion` | 58 endpoints here; ids like `.anthropic-claude-5.5-sonnet-chat_completion` |
| Local-mode LLM | `POST /_inference/chat_completion/{id}/_stream` | OpenAI-style SSE with streamed `tool_calls`; `[DONE]` terminates |
| ES\|QL | `POST /_query` | Read-only by nature |
| Spaces | prefix `/s/<space>` before `/api/…` *(docs)* | The default space needs no prefix |
| List spaces | `GET /api/spaces/space` | Not space-scoped; returned `default`, `my-space` |
| Stop a run | `POST /internal/agent_builder/executions/{execution_id}/abort` | **Internal** API: needs `x-elastic-internal-origin: kibana` and `Elastic-Api-Version: 1`; returns `{acknowledged, terminal_persisted}` |
| Alert by id | `POST /api/detection_engine/signals/search` `{query:{ids:{values:[id]}}}` | Space-scoped and RBAC-respecting; `_source` has dotted keys (about 90 fields) |
| Case | `GET /api/cases/{id}`, `/comments/_find?perPage=20`, `/alerts` | `GET /cases/{id}/comments` itself is 404 |

`/api/oas` returns 404 on 9.6.0, so there is no machine-readable spec from the cluster. The Kibana API reference
and the `docs-content` repo are the sources.

## Model switching

- `inference_id` takes the `/_inference` endpoint id and works on `/converse` and `/converse/async` on 9.6.0.
  `connector_id` is the older form; the two are mutually exclusive (`400` if both). One model list serves both modes.
- Agents are not bound to a model, so any agent runs on any model.
- `configuration_overrides: {enable_elastic_capabilities: false, tools: [], skill_ids: []}` took the default agent's
  prompt from about 45k to about 9k input tokens for a one-line chat.

## Streaming events (agent mode)

Observed on 9.6.0: `conversation_id_set`, `reasoning`, `message_chunk`, `browser_tool_call`, `tool_result`, and via the
rehearsal `tool_call`, `message_complete`, `round_complete`. The full enum *(source)* also has `prompt_request`,
`tool_progress`, `round_interrupted`, `model_reasoning_chunk`, `conversation_created`, and more. Kibana pads the
stream with `: 000…` comment lines (ignored); Elasticsearch's stream starts each event with a BOM (stripped).

## Stopping a run (Ctrl+C)

Verified on 9.6.0: starting `/converse/async` with a client-chosen `execution_id`, reading a few chunks and **dropping the
connection does not stop the agent**. Kibana finishes the round (5,418 characters and 934 output tokens were saved to the
conversation 25 s later) and bills for it. The harness therefore sends an `execution_id` with every run and, on Ctrl+C,
calls the internal abort route. The run then stops: a long essay aborted after 6 chunks saved 0 output tokens instead of 934.
The route is absent from the public API reference and is internal-only, hence the defensive `False` return.

## Deep links

`/open` uses `<kibana>/s/<space>/app/agent_builder/agents/<agentId>/conversations/<conversationId>` (route table in the 9.6
branch of Kibana, `agent_builder/public/application/route_config.tsx`). Cases: `/app/security/cases/<id>`.

## Why local mode exists: `browser_api_tools` are fire-and-forget

`browser_api_tools: [{id, description, schema}]` registers client-side tools. On 9.6.0 the model calls the tool, Kibana
emits `browser_tool_call`, then immediately returns a placeholder `tool_result`
(`executeOnClient: true, message: "Browser tool … "`) and the model carries on without the real result. There is no
call-and-response, so it cannot read a local file for the model. It is meant for UI actions in Kibana's own frontend.

So local data reaches an agent three ways (README): inline `@path`, `/ingest` into an index the agent searches, or a
harness-owned loop (`/mode local`) that calls an Elastic-hosted model with local tools.

## Local loop

`LocalSession.turn` streams `chat_completion` chunks, accumulates `tool_calls` by `index` (id and name arrive once,
arguments in fragments), appends the assistant message with `tool_calls`, runs each tool, appends `role: tool` results
with `tool_call_id`, and repeats (max 20 rounds). A Ctrl+C or API error truncates the history back to the start of the
turn so no tool call is left without its result. Sessions are saved as JSON after each completed turn.

## Approval prompts from agents *(source, not exercised)*

A tool that needs approval makes the stream emit `prompt_request` with `prompt: {type, id, …}`
(`confirmation`, `authorization`, `ask_user_question`). The client answers on a follow-up `converse` call with the same
`conversation_id` and `prompts: {<id>: {allow: bool} | {authorized: bool} | {answers: [...]}}`. `agent_chat.py`
implements this.

## Credential store

`credstore.py` wraps the `keyring` package (macOS Keychain, service `elastic-harness`, account = cluster name). A profile
with `key_store: keychain` has no `api_key` in the config file; `Profile.resolved()` reads it at connect time and raises a
clear error if the entry is gone. `Config.store_key` falls back to the mode-600 file where no Keychain exists.

## Compaction

`LocalSession.compact` trims old tool results, then summarises everything before the kept turns, using the *untrimmed*
originals. Lesson from a live run: feeding the summariser tool results cut to 400 characters made it conclude files were
"cut off mid-word" and write a bogus "Correction needed" into the summary. Tool output is now passed up to 4,000
characters with an explicit "shortened for this summary" note, and the prompt forbids speculation.

## Module map

| File | Role |
|---|---|
| `config.py` | Profiles; creds read in place; env overrides; config mode 600 |
| `client.py` | stdlib HTTP, SSE parser, Kibana and Elasticsearch calls |
| `agent_chat.py` | Agent-mode turn: event handling, approval round-trips, `/resume` history |
| `local_agent.py` | Local-mode loop and session persistence |
| `tools.py` | Local tools, permission model (`describe` ⇒ asks first) |
| `guard.py` | Secret-file refusal, `@path` expansion |
| `ingest.py` | Chunking, `eh-local-*` indices, `index_search` tool and agent attach |
| `render.py` | rich output: live Markdown, dim tool lines, confirmations |
| `repl.py`, `cli.py` | Slash commands, completion, argparse entry point |
| `clusters.py`, `credstore.py` | Cluster list (add, test, rm, secure) and the Keychain wrapper |
| `mentions.py` | `@alert:`, `@case:`, `@index:` |
| `agentcode.py`, `frontmatter.py` | Agents as code; the tiny front-matter parser |
| `project.py` | `EH.md` and custom command discovery |
| `compare.py`, `transcript.py` | `/compare`; `/copy`, `/export`, `/open` helpers |
