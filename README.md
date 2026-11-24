# Elastic Harness

A Claude Code-style terminal harness for **Elastic Agent Builder**. Chat with your custom agents, switch agents
and models on the fly, point them at files on your Mac, and let the harness run tools locally when you want it to.

```
eh-demo › @examples/kestrel/overview.md who owns this project and when is go-live?
@ …/overview.md · attached · 550 chars
Priya Nair owns Project Kestrel and the go-live date is 2026-12-03.
7,622 in / 30 out tokens · 1 LLM call(s) · .anthropic-claude-4.5-haiku-chat_completion
eh-demo › /mode local
local › which risks are high severity? check the CSV and ask the eh-demo agent about the owners
⏺ read_file({"path": "examples/kestrel/risks.csv"})
⏺ ask_agent({"agent_id": "eh-demo", "question": "who owns R1 and R4?"})
```

## Install

Python 3.10+ and a Mac or Linux shell.

```bash
./install.sh                 # creates .venv, installs, links ~/.local/bin/eh
eh profile add my-cluster --creds ~/path/to/creds.local.json   # KIBANA_URL, ES_URL, API_KEY
eh doctor                    # connection, identity, agents, models
eh                           # start chatting
```

A profile can point at an existing creds file (`--creds`). It is **read in place and never copied**; only the path
is stored. Without `--creds`, `eh profile add NAME` asks for the URLs and the API key (hidden prompt) and stores the
key in `~/.config/elastic-harness/config.json` (mode 600). Env vars `EH_KIBANA_URL`, `EH_ES_URL`, `EH_API_KEY`,
`EH_SPACE` override a profile.

**API key privileges** (from the Elastic docs; I tested with a full-access key only, not a scoped one): Kibana
`feature_agentBuilder.read` to chat; add `feature_agentBuilder.manage_agents` and `manage_tools` for
`/ingest --tool --agent`; cluster `monitor_inference` for model calls; `read` on the indices you query; and
`create_index`, `write` and `delete` on `eh-local-*` for `/ingest`.

## Two modes

| | `agent` mode (default) | `local` mode |
|---|---|---|
| Who runs the loop | An Agent Builder agent, server-side | The harness, on your Mac |
| Model | `inference_id` per request, or the cluster default | An Elastic-hosted `chat_completion` endpoint |
| Sees your files via | `@path` mentions, `/ingest` | `read_file`, `grep`, `glob_files`, `list_dir`, `@path` |
| Reaches Elastic data via | The agent's own tools | `elastic_esql`, `elastic_tool`, or `ask_agent` (delegates to any agent) |
| Changes your machine | Never | `write_file`, `edit_file`, `bash`, each **asks first** |
| Conversation history | In Kibana (`/resume`) | `~/.local/state/elastic-harness/sessions` |

Switch with `/mode agent|local`. `/agent` and `/model` work in both; in local mode agents are reached through
`ask_agent`.

## Getting local data in front of an agent

Agent Builder runs inside Kibana, so it cannot read your disk, and its client-side (`browser_api_tools`) hook is
fire-and-forget (see `docs/ARCHITECTURE.md`). The harness bridges that three ways:

| Way | Use it for | How |
|---|---|---|
| **A. `@path`** | A file or folder you want an agent to see right now | Inlined into the message (up to ~90k chars); folders attach a listing |
| **B. `/ingest`** | Data an agent should be able to search repeatedly | Chunks text files into index `eh-local-<name>`; `--tool` creates an `index_search` tool, `--agent ID` attaches it |
| **C. `/mode local`** | Exploring and editing files with an LLM that actually runs tools | The harness owns the loop; Elastic data and agents are tools |

```text
/ingest ~/Documents/project-notes --index notes --tool --agent my-agent
```

Re-ingesting updates in place (stable ids, stale chunks removed).

## Commands

`/help` `/agents` `/agent <id>` `/models` `/model <words>` `/mode` `/new` `/resume [n]` `/profiles` `/profile <name>`
`/ingest` `/tools` `/usage` `/doctor` `/debug` `/clear` `/exit`

- `/model sonnet 5.5` matches by words against the cluster's chat endpoints; ambiguous words list the candidates.
  `/model default` returns agent mode to the cluster's default model.
- `@path` completes with Tab. Alt+Enter inserts a newline. Ctrl+C interrupts a turn.
- Agent and model choices are remembered per profile. The mode is not: every session starts in agent mode.
- One-shot: `eh --agent my-agent --model "haiku 4.5" -p "question"`, or pipe context in:
  `cat risks.csv | eh --mode local -p "summarise"`. Add `--yes` to approve tool confirmations non-interactively.
- Other subcommands: `eh agents`, `eh models`, `eh doctor`, `eh ingest PATH [--index X] [--tool] [--agent ID]`,
  `eh profile add|list|use|rm`.

## Safety defaults

- **Credentials never reach a model.** `@path`, `read_file`, `grep`, `glob_files` and `/ingest` refuse files that look
  like credentials (`creds*.json`, `.env*`, `*.pem`, `id_rsa*`, `~/.ssh`, `~/.aws`, this tool's own config, …). The
  `bash` tool's environment has every `EH_*` variable removed. The list is best effort, not a guarantee.
- **Read-only tools run freely; everything else asks.** `write_file`, `edit_file` and `bash` prompt with a preview
  (`bash` every time). `ask_agent` prompts per agent, because an agent may run its own tools. Non-interactive runs
  deny them unless `--yes`.
- `elastic_tool` only runs custom `esql` and `index_search` tools; workflow and MCP tools go through `ask_agent`.
- Ingest only writes to indices prefixed `eh-local-` and tools prefixed `eh-search-`.
- **Where your data goes:** `@path` and local tools send file contents to the model through Elastic, and `/ingest`
  copies text into your cluster. Don't point either at data you wouldn't put there.

## Verify it

```bash
.venv/bin/python -m unittest discover -s tests      # 19 offline tests (SSE, guard, chunking, local loop with a fake model)
.venv/bin/python scripts/rehearse.py                # 24 live checks against the active profile
.venv/bin/python scripts/rehearse.py --cleanup      # removes eh-demo, eh-search-kestrel, eh-local-kestrel, test chats
```

The rehearsal creates the `eh-demo` agent, ingests `examples/kestrel` (synthetic project notes), then checks agent
streaming, model switching, follow-ups, resume, local files, ES|QL, delegation and both guardrails.

## Known limits

- Built and rehearsed on Kibana **9.6.0** (Elastic Cloud, traditional). Event names beyond the ones listed in
  `docs/ARCHITECTURE.md` come from Kibana source and are not verified on other versions.
- Approval prompts from an agent (`prompt_request`: confirmations, authorization, questions) are implemented from the
  Kibana source but have **not been exercised live**: no agent on the test cluster has an approval-gated tool.
- Local mode's file tools are plain Python, not sandboxed. Treat `bash` like a terminal you handed to an assistant.
- `/ingest` handles text files only (no PDFs or Office formats) and uses lexical search, no embeddings.
