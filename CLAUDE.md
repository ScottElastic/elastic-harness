# Elastic Harness: project notes for Claude

A terminal harness for Elastic Agent Builder (`eh`). Python 3.10+, runtime deps `rich` and `prompt_toolkit` only;
the HTTP and SSE client is stdlib. Read `README.md` for usage and `docs/ARCHITECTURE.md` for verified API facts.

## Run
- `.venv/bin/python -m unittest discover -s tests` (offline, no cluster)
- `.venv/bin/python scripts/rehearse.py` (live, uses the active profile) and `--cleanup` afterwards
- `./install.sh` to (re)install and link `~/.local/bin/eh`

## Rules
- Never commit or print credentials. `config.json`, `*.local.json`, `.env*` are gitignored. A profile's `creds_file`
  is read in place; `Config.save` never writes a key that came from a creds file.
- Anything the model can read goes through `guard.is_secret_path`. Tools that change the machine or hand work to an
  agent must set `describe` so `run_tool` asks first.
- Only touch objects this tool created: indices `eh-local-*`, tools `eh-search-*`, the `eh-demo` rehearsal agent.
- Verify API behaviour on a live cluster before relying on Kibana source or docs; record what you verified with a
  version in `docs/ARCHITECTURE.md`.
- Mode is session-only; agent and model choices persist per profile.
