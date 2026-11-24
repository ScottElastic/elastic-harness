# Elastic Harness

A Claude Code-style terminal harness for **Elastic Agent Builder**. Chat with your custom agents, switch clusters,
spaces, agents and models on the fly, point them at files on your Mac or at alerts and cases in Kibana, and let the
harness run tools locally when you want it to.

```
eh-demo › @index:eh-local-kestrel what fields does this have? @case:<id> is it related?
@ index:eh-local-kestrel · attached
@ case:<id> · attached
…
eh-demo › /compare sonnet 5.5 | haiku 4.5 :: who owns Kestrel?
  model              time     in tokens   out tokens   tools
  …haiku             5.2s ⚡   7,402       4            -
  …sonnet            10.1s    21,223      411          eh-search-kestrel
```

## Install

Python 3.10+ and a Mac or Linux shell.

```bash
./install.sh                                              # .venv, install, link ~/.local/bin/eh
eh cluster add my-cluster --creds ~/path/to/creds.local.json   # or: eh cluster add my-cluster  (prompts, key goes to the Keychain)
eh doctor                                                 # connection, identity, agents, models
eh                                                        # start chatting
```

## Clusters and credentials

`/cluster` (or `eh cluster …`) is the list of clusters you can talk to. Switch with `/cluster <name>` (any unique part
of the name works). Each cluster remembers its own default agent and model.

| Command | What it does |
|---|---|
| `/cluster` | List clusters: host, space, where the key lives |
| `/cluster <name>` | Switch (starts a fresh conversation; conversations belong to one cluster) |
| `/cluster add <name>` | Prompts for Kibana URL, Elasticsearch URL and the API key (hidden). Proves the key works, then saves it |
| `/cluster add <name> --creds FILE` | Point at an existing `creds.local.json` (`KIBANA_URL`, `ES_URL`, `API_KEY`). **Read in place, never copied** |
| `/cluster add <name> --import FILE` | Copy that file's key into the key store, so you can move or delete the file |
| `/cluster test [name]` | Connect and report (version, user, agents, models) without switching |
| `/cluster rm <name>` / `secure <name>` | Remove a cluster and its key / move a plaintext key into the Keychain |

Where keys live, in order: environment (`EH_KIBANA_URL`, `EH_ES_URL`, `EH_API_KEY`, `EH_SPACE`) → the creds file a
cluster points at → the **macOS Keychain** (service `elastic-harness`, one entry per cluster) → the mode-600 config
file `~/.config/elastic-harness/config.json`, used only where there is no Keychain. The config file never contains a key
that lives in the Keychain or a creds file. The first Keychain access may show a macOS prompt: choose Always Allow.

**API key privileges** (from the Elastic docs; tested with a full-access key only): Kibana
`feature_agentBuilder.read` to chat; `manage_agents` and `manage_tools` for `/ingest --tool --agent` and
`eh agent push`; cluster `monitor_inference` for model calls; `read` on indices you query or `@index:`; and
`create_index`, `write`, `delete` on `eh-local-*` for `/ingest`. `@alert` and `@case` need the Security alert and case
privileges in that space. `/spaces` needs permission to list spaces (otherwise `/space <id>` still works).

## Two modes

| | `agent` mode (default) | `local` mode |
|---|---|---|
| Who runs the loop | An Agent Builder agent, server-side | The harness, on your Mac |
| Model | `inference_id` per request, or the cluster default | An Elastic-hosted `chat_completion` endpoint |
| Sees your files via | `@path`, `/ingest` | `read_file`, `grep`, `glob_files`, `list_dir`, `@path` |
| Reaches Elastic data via | The agent's own tools | `elastic_esql`, `elastic_tool`, or `ask_agent` (delegates to any agent) |
| Changes your machine | Never | `write_file`, `edit_file`, `bash`, each **asks first** |
| History | In Kibana (`/resume`) | `~/.local/state/elastic-harness/sessions`, auto-compacted |

`/mode agent|local` switches. The mode is session-only: every session starts in agent mode.

## Everything you can type

**Talk to Elastic**

| | |
|---|---|
| `/space <id>`, `/spaces` | Switch Kibana space (agents are per space). Your chosen agent returns as soon as a space has it |
| `/agent <id>`, `/agents` | Switch agent (new conversation) |
| `/model sonnet 5.5`, `/models` | Match by words against the cluster's chat endpoints. `/model default` = the cluster default |
| `/compare sonnet 5.5 \| haiku 4.5 :: prompt` | Same prompt on 2 to 4 models, side by side, with time and tokens. Agent mode runs the real agent once per model; local mode compares plain answers. No `::` = your last prompt. The throwaway chats are deleted |
| `/retry`, `/retry --keep` | Re-run your last prompt on a fresh conversation (or continue this one) |
| `/copy`, `/copy code` | Last answer, or its last code block, to the clipboard |
| `/export [file.md]` | Save the conversation as Markdown (tool calls included) |
| `/open`, `/open agent`, `/open case <id>` | Open the conversation in Kibana |
| `/resume [n]`, `/new` | Reopen or restart a conversation |
| `/compact` | Summarise older local-mode history now |
| `/ingest <path> [--index n] [--tool] [--agent id]` | Load local text files into `eh-local-<n>`; `--tool` adds an `index_search` tool, `--agent` attaches it |
| `/tools`, `/usage`, `/doctor`, `/debug`, `/clear`, `/help`, `/exit` | |

**Mentions** put things in front of the model. They work in both modes and in `/compare`.

| | |
|---|---|
| `@path/to/file` or `@folder` | A file's text, or a folder listing (up to ~90k characters). Tab completes |
| `@alert:<id>` | A detection alert: rule, severity, host, process, reason and the other populated fields |
| `@case:<id>` | A case: title, status, severity, description, notes, attached alert ids |
| `@index:<pattern>` | An index's field list and 3 sample documents (ES\|QL `FROM … \| LIMIT 3`) |

`@alert` and `@case` read with your key in the current space, so `/space my-space` first, then
`triage @alert:<id>`.

**Ctrl+C really stops the run.** Closing the connection does *not* stop an Agent Builder agent: Kibana carries on to
the end and spends the tokens (verified: 934 tokens generated after a dropped connection). The harness calls Kibana's
abort endpoint, so a stopped answer stops costing. Alt+Enter inserts a newline.

## Agents as code

One Markdown file per agent: front matter plus the instructions as the body.

```markdown
---
id: kestrel-analyst
name: Kestrel Analyst
description: Answers from the Kestrel notes
labels: [kestrel, demo]
access: public                   # public | private
elastic_capabilities: false      # true = also Elastic's built-in skills and tools
tools:
  - eh-search-kestrel
---
You answer questions about Project Kestrel using only the search tool. Quote names and dates exactly.
```

```bash
eh agent pull my-agent -o agents/     # or: eh agent pull --all -o agents/
eh agent diff agents/my-agent.md      # what would change on the cluster
eh agent push agents/my-agent.md      # shows the diff, asks, then creates or updates (--yes, --dry-run)
```

Also `/agent push|pull|diff` inside a session. Push targets the current **cluster and space** and says so. It refuses
unknown tool ids, built-in agents, and tools that can run or approve workflows
(`resume_workflow_execution`, `execute_workflow`, connector sub-actions) unless you pass `--allow-risky`.

## Project instructions and custom commands

- **`EH.md`** in the working directory, any parent up to your home folder, or `~/.config/elastic-harness/EH.md`.
  Local mode adds it to the system prompt. Agent mode attaches it once, at the start of a conversation: the agent's
  stored instructions are never replaced.
- **Custom commands**: `~/.config/elastic-harness/commands/*.md` and `<project>/.eh/commands/*.md` (project wins).
  `triage.md` becomes `/triage`. Optional front matter: `description`, `mode`, `agent`, `model` (apply to that one run).
  In the body, `$ARGS` is everything after the command and `$1`…`$9` are single words.

  ```markdown
  ---
  description: Triage an alert with the SOC agent
  agent: soc-analyst
  model: sonnet 5.5
  ---
  Triage @alert:$1. Give a verdict, the evidence, and the next two steps.
  ```

## Getting local data in front of an agent

Agent Builder runs inside Kibana, so it cannot read your disk, and its client-side (`browser_api_tools`) hook is
fire-and-forget (`docs/ARCHITECTURE.md`). Three ways in: **A. `@path`** (inline, one-off), **B. `/ingest`** (an index the
agent searches repeatedly; re-ingesting updates in place), **C. `/mode local`** (the harness owns the loop and its tools).

## Local mode and long sessions

Every turn re-sends the history, so it grows in cost and eventually in context. At about 60k estimated tokens
(`EH_CONTEXT_TOKENS`), old tool output is trimmed, then everything before the last two turns is summarised by the model.
`/compact` does it now, keeping only the latest turn. A context-limit error from the model triggers one compaction and a
retry. The summariser is told to keep every concrete fact and not to speculate.

## Safety defaults

- **Credentials never reach a model.** `@path`, `read_file`, `grep`, `glob_files` and `/ingest` refuse files that look
  like credentials (`creds*.json`, `.env*`, `*.pem`, `id_rsa*`, `~/.ssh`, `~/.aws`, this tool's own config, …); the
  `bash` tool's environment has every `EH_*` variable removed. Best effort, not a guarantee.
- **Read-only tools run freely; everything else asks.** `write_file`, `edit_file` and `bash` prompt with a preview
  (`bash` every time); `ask_agent` prompts per agent; `eh agent push` and `/cluster rm` confirm before changing anything.
  Non-interactive runs deny all of these unless `--yes`.
- `elastic_tool` only runs custom `esql` and `index_search` tools. Ingest writes only `eh-local-*` indices and
  `eh-search-*` tools. Workflows and response actions are deliberately not exposed.
- **Where your data goes:** `@path`, `@alert`, `@case`, `@index` and local tools send content to the model through
  Elastic, and `/ingest` copies text into your cluster. Don't point them at data you wouldn't put there.

## Verify it

```bash
.venv/bin/python -m unittest discover -s tests      # 53 offline tests (fake Elastic client, fake Keychain, fake model)
.venv/bin/python scripts/rehearse.py                # 65 live checks against the active cluster
.venv/bin/python scripts/rehearse.py --cleanup      # removes eh-demo, eh-code-demo, eh-search-kestrel, eh-local-kestrel, test chats
```

The rehearsal copies your active cluster into a temp config, so it never edits your real settings. It creates the
`eh-demo` and `eh-code-demo` agents and ingests `examples/kestrel` (synthetic project notes), then checks every feature
above against the live cluster, including a real Keychain round trip with a dummy value and a Ctrl+C-equivalent abort.

## Known limits

- Built and rehearsed on Kibana **9.6.0** (Elastic Cloud). The abort endpoint is an **internal** Kibana API (no
  stability promise): if it ever disappears the harness says it could not confirm the abort.
- `/open` builds `…/app/agent_builder/agents/<agent>/conversations/<id>` from Kibana's route table (9.6 branch); it has
  not been clicked through in a browser.
- Approval prompts from an agent (`prompt_request`) are implemented from the Kibana source but **not exercised live**: no
  agent on the test cluster has an approval-gated tool.
- Scoped (non-admin) API keys are untested. Linux has no Keychain unless a secret service is installed; keys then fall
  back to the mode-600 config file.
- Local mode's file tools are plain Python, not sandboxed: treat `bash` like a terminal you handed to an assistant.
- `/ingest` handles text files only and uses lexical search, no embeddings.
- Automatic compaction uses a token *estimate* (characters ÷ 3.5), not the model's own count.
