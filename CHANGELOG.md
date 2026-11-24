# Changelog

Notable changes. This project does not promise a stable interface yet (see the README's *Known limits*).

## Unreleased

### Added
- **`@alert:` and `@case:` pickers.** Type `@alert:` for a dropdown of the 20 newest open alerts in the current space, `@case:` for
  the 20 most recently created cases that are not closed. Type to narrow, Tab to insert the id. `@cases`/`@alerts` complete to the
  singular, Backspace reopens the list, an empty space names the spaces that have some, and the list is fetched on a background
  thread and cached for 45 s.
- **Attached text is framed as data.** Blocks built from `@path`, `@alert`, `@case` and `@index` text escape anything that looks
  like one of our own tags, and a notice after them says the content is untrusted and never to be followed as instructions. Local
  mode's system prompt carries the same rule for tool results.
- **Tools as code.** `eh tool push|diff|pull|list` (and `/tool …` in a session) manage custom `esql` and
  `index_search` tools as Markdown files, the same way `eh agent` manages agents: the body of an ES|QL tool is its
  query, `params:` declares the `?name` placeholders, `push` shows a diff and asks first, and an unchanged file is a
  no-op. Built-in tools and tools of other types are never touched.
- **`eh -c` / `--continue`** continues the most recent conversation (with `--agent`, the most recent one with that
  agent); `--resume ID` continues a specific one. They work with `-p`, and in local mode they reopen the newest saved
  session.
- **`eh -p … --json`** prints one JSON object on stdout (answer, conversation id, usage, tools called, errors);
  progress and notes go to stderr.
- **`eh doctor` checks the cluster against what this build was rehearsed on**: it warns when Kibana is not 9.6 and
  confirms Kibana's internal abort route is present, since Ctrl+C depends on it.
- Continuous integration: the offline suite on Python 3.10 to 3.13 (Linux) and 3.12 (macOS).
- `SECURITY.md`.
- `scripts/iterm_check.py` now also types into a real prompt and reads the picker dropdowns back (19 checks).

### Fixed
- **`@path` mentions were also read from alert and case text.** `@alert`/`@case` blocks were appended before `@path` mentions were
  expanded, so a field like `@~/notes.md` inside an alert pulled that local file into the prompt (credential files were still refused).
  `@path` is now read from what you typed only, in `/compare` too.
- A failed `eh -p` (API error, interrupted) now exits non-zero; it used to exit 0.
- Front matter: a trailing `# comment` after a value is ignored. The README's own agent example
  (`access: public   # public | private`) was rejected before.
- Front matter: strings with quotes, backslashes or line breaks now survive a pull and push unchanged.

## 0.1.0

First public release: chat with Agent Builder agents, switch clusters, spaces, agents and models, `/compare`,
`@path`, `@alert`, `@case` and `@index` mentions, `/ingest`, local mode with approval-gated tools, agents as code,
custom commands, iTerm2 tab status, and Ctrl+C that stops the server-side run.
