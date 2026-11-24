# Elastic Harness: project notes for Claude

A terminal harness for Elastic Agent Builder (`eh`). Python 3.10+, runtime deps `rich`, `prompt_toolkit` and (macOS) `keyring`;
the HTTP and SSE client is stdlib. Read `README.md` for usage and `docs/ARCHITECTURE.md` for verified API facts.

## Run
- `.venv/bin/python -m unittest discover -s tests` (142 tests, offline, no cluster)
- `.venv/bin/python scripts/rehearse.py` (live, uses the active profile) and `--cleanup` afterwards
- `.venv/bin/python scripts/iterm_check.py` (23 checks; needs iTerm2 3.7+; opens and force-closes a throwaway window; also types into a
  real prompt to check the @alert:/@case: dropdowns, which unit tests cannot see. Don't pipe it: `| tee` hides its exit code)
- `./install.sh` to (re)install and link `~/.local/bin/eh`
- CI (`.github/workflows/tests.yml`) runs the offline suite on Python 3.10 to 3.13 (Linux) and 3.12 (macOS). Keep tests free of a
  real cluster, Keychain and terminal, and the code free of post-3.10 syntax or stdlib. Linux has no `keyring`.
- Scripting `eh -p` (or `rehearse.py`): redirect stdin (`</dev/null`). With stdin not a terminal, `-p` reads it to the end, so an
  open pipe makes it wait forever.

## Rules
- Never commit or print credentials. `config.json`, `*.local.json`, `.env*` are gitignored. A profile's `creds_file`
  is read in place; `Config.save` never writes a key that came from a creds file.
- Anything the model can read goes through `guard.is_secret_path`. Tools that change the machine or hand work to an
  agent must set `describe` so `run_tool` asks first.
- Only touch objects this tool created: indices `eh-local-*`, tools `eh-search-*` and `eh-tool-demo`, the `eh-demo` and `eh-code-demo` rehearsal agents.
- Verify API behaviour on a live cluster before relying on Kibana source or docs; record what you verified with a
  version in `docs/ARCHITECTURE.md`.
- Text from `@path`, `@alert`, `@case`, `@index` is attacker-influenced. Build its blocks only with `guard.neutralize`/`attach_blocks`, and
  expand mentions only through `mentions.expand_all` on what the user typed: never run `@path` expansion over text that already carries blocks.
- prompt_toolkit: a lone completion that changes nothing is silently dropped (so a "no results" note must add something, e.g. a space), and
  deleting text closes the menu (the Backspace binding reopens it in `@alert:`/`@case:` words). Completion runs on a thread: no unguarded shared state. Inserting a completion (Tab) is not typing, so it does not
  re-trigger completion. Test a picker the way a person reaches it (`@al` + Tab, Enter), not only by typing the whole token.
- Mode and space are session-only; agent and model choices persist per cluster.
- API keys belong in the Keychain (`credstore`) or a creds file read in place: never in code, tests or docs. Tests use
  `tests/fakes.py` (fake client, fake Keychain); the live rehearsal uses a temp config copy and a dummy Keychain value.
- Closing an SSE connection does not stop an Agent Builder run: always pass an `execution_id` and abort on interrupt.
- Tab status (`status.py`) writes only in iTerm2 on a tty and must be cleared at exit; free text in `detail` goes through
  `status.clean` (no `;`, no control characters). Closing a session with `it2` needs `--force` or iTerm shows a dialog.
- **This repo is PUBLIC** (github.com/ScottElastic/elastic-harness, Apache-2.0). Never commit credentials, cluster URLs or ids,
  customer or project names, or real data from a cluster. Commits use the GitHub noreply identity set in this repo's
  git config; never put a real name or work email in commit metadata. Run the secret scan before every push.
- Never force-push this repo: GitHub's public activity API lists force-push hashes and keeps the replaced commits fetchable.
  If history ever needs rewriting, create a fresh repo instead (that is how the first version was scrubbed).
