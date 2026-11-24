# Elastic Harness: project notes for Claude

A terminal harness for Elastic Agent Builder (`eh`). Python 3.10+, runtime deps `rich`, `prompt_toolkit` and (macOS) `keyring`;
the HTTP and SSE client is stdlib. Read `README.md` for usage and `docs/ARCHITECTURE.md` for verified API facts.

## Run
- `.venv/bin/python -m unittest discover -s tests` (53 tests, offline, no cluster)
- `.venv/bin/python scripts/rehearse.py` (live, uses the active profile) and `--cleanup` afterwards
- `./install.sh` to (re)install and link `~/.local/bin/eh`

## Rules
- Never commit or print credentials. `config.json`, `*.local.json`, `.env*` are gitignored. A profile's `creds_file`
  is read in place; `Config.save` never writes a key that came from a creds file.
- Anything the model can read goes through `guard.is_secret_path`. Tools that change the machine or hand work to an
  agent must set `describe` so `run_tool` asks first.
- Only touch objects this tool created: indices `eh-local-*`, tools `eh-search-*`, the `eh-demo` and `eh-code-demo` rehearsal agents.
- Verify API behaviour on a live cluster before relying on Kibana source or docs; record what you verified with a
  version in `docs/ARCHITECTURE.md`.
- Mode and space are session-only; agent and model choices persist per cluster.
- API keys belong in the Keychain (`credstore`) or a creds file read in place: never in code, tests or docs. Tests use
  `tests/fakes.py` (fake client, fake Keychain); the live rehearsal uses a temp config copy and a dummy Keychain value.
- Closing an SSE connection does not stop an Agent Builder run: always pass an `execution_id` and abort on interrupt.
