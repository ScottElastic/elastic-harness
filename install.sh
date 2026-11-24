#!/usr/bin/env bash
# Create a project-local venv, install Elastic Harness into it, and link `eh` onto your PATH.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10+ is required (found: $("$PY" --version 2>&1))"; exit 1; }

"$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .

mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/.venv/bin/eh" "$HOME/.local/bin/eh"
echo "installed: $HOME/.local/bin/eh -> $PWD/.venv/bin/eh"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: add this to your shell profile so 'eh' is found:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
echo "next: eh profile add NAME --creds /path/to/creds.local.json && eh doctor"
