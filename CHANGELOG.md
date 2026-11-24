# Changelog

Notable changes. This project does not promise a stable interface yet (see the README's *Known limits*).

## Unreleased

### Added
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

### Fixed
- A failed `eh -p` (API error, interrupted) now exits non-zero; it used to exit 0.
- Front matter: a trailing `# comment` after a value is ignored. The README's own agent example
  (`access: public   # public | private`) was rejected before.
- Front matter: strings with quotes, backslashes or line breaks now survive a pull and push unchanged.

## 0.1.0

First public release: chat with Agent Builder agents, switch clusters, spaces, agents and models, `/compare`,
`@path`, `@alert`, `@case` and `@index` mentions, `/ingest`, local mode with approval-gated tools, agents as code,
custom commands, iTerm2 tab status, and Ctrl+C that stops the server-side run.
