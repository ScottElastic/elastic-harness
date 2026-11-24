# Security policy

Elastic Harness is an experimental personal project, not an Elastic product, and Elastic does not support it. It
does handle API keys and can run commands on your machine, so security reports are welcome.

## Reporting a problem

Please report it **privately**: on this repository open the **Security** tab and choose **Report a vulnerability**
(GitHub private vulnerability reporting). Do not put the details in a public issue or pull request. Include what you
did, what you expected and what happened, and the version (`eh --version`).

You will get a reply, but this is a spare-time project: no response-time guarantee.

## What counts

In scope, because the README makes promises about them:

- An API key reaching a model, a log, a transcript, the config file, a test fixture or the terminal. Keys belong in
  the macOS Keychain or in a creds file that is read in place.
- A way around the secret-file guard (`@path`, `read_file`, `grep`, `glob_files`, `/ingest`) so a credentials file
  is read anyway.
- Local mode doing something without asking: `bash`, `write_file` and `edit_file` must show a preview and wait, and
  non-interactive runs must deny them unless `--yes`.
- `eh agent push` or `eh tool push` changing something that was not shown first, or `--allow-risky` not being needed
  for tools that can run or approve workflows.
- Anything that makes `eh` write outside the places it documents.

Known and documented, not a vulnerability by themselves: local mode's file tools are not sandboxed; text from files,
alerts and cases is sent to a model, which can be steered by instructions hidden in that text; the credential guard
is best effort. Please still tell me if you find a practical way to make any of these worse.

Out of scope: flaws in Elasticsearch, Kibana or Agent Builder themselves. Report those to Elastic through
<https://www.elastic.co/community/security>.

## Supported versions

The latest commit on `main`. There are no maintained older releases.
