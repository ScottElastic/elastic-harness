"""Keep credentials out of the model's context, and expand @file mentions.

Everything the harness reads from disk on the model's behalf (``@file`` mentions,
the ``read_file``/``grep`` tools, ``/ingest``) goes through ``is_secret_path`` first.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

SECRET_NAME_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keychain*",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "creds*.json", "*.local.json", "credentials*", "secrets*", "env.sh",
    ".netrc", ".npmrc", ".pypirc", ".pgpass", "*.kdbx",
]
SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", "elastic-harness"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".idea", ".next", ".tox"}

MAX_FILE_CHARS = 60_000
MAX_TOTAL_CHARS = 90_000  # Agent Builder rejects input over 100,000 characters


def is_secret_path(path: str | Path) -> str | None:
    """Return a reason string if the path looks like it holds credentials, else None."""
    p = Path(path).expanduser()
    name = p.name.lower()
    for pat in SECRET_NAME_PATTERNS:
        if fnmatch.fnmatch(name, pat):
            return f"{p.name} looks like a credentials file"
    for part in p.parts[:-1]:
        if part in SECRET_DIRS:
            return f"{part}/ holds credentials or keys"
    return None


def is_text_file(path: Path, sniff: int = 4096) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" not in fh.read(sniff)
    except OSError:
        return False


def read_text(path: Path, max_chars: int = MAX_FILE_CHARS) -> tuple[str, bool]:
    """Read a text file, returning (text, truncated)."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        text = fh.read(max_chars + 1)
    return (text[:max_chars], True) if len(text) > max_chars else (text, False)


_MENTION = re.compile(r"(?<![\w@/.])@((?:~|\.{1,2})?/?[\w.\-~/]*[\w\-~/])")


def expand_mentions(text: str, cwd: Path | None = None) -> tuple[str, list[dict]]:
    """Inline ``@path`` mentions as <attached_file> blocks appended to the message.

    Returns (new_text, notes). Each note is {"path", "chars", "status"} where status is
    "attached", "truncated", "dir", "refused: …" or "skipped: …". Mentions that do not
    resolve to a real path are left untouched (so ``@someone`` stays text).
    """
    cwd = cwd or Path.cwd()
    notes: list[dict] = []
    blocks: list[str] = []
    seen: set[Path] = set()
    budget = MAX_TOTAL_CHARS - len(text)

    for m in _MENTION.finditer(text):
        raw = m.group(1)
        path = None
        for cand in (raw, raw.rstrip(".,;:)!?")):
            p = Path(cand).expanduser()
            p = p if p.is_absolute() else cwd / p
            if p.exists():
                path = p.resolve()
                break
        if path is None or path in seen:
            continue
        seen.add(path)
        reason = is_secret_path(path)
        if reason:
            notes.append({"path": str(path), "chars": 0, "status": f"refused: {reason}"})
            continue
        if path.is_dir():
            entries = sorted(e.name + ("/" if e.is_dir() else "") for e in path.iterdir() if e.name not in SKIP_DIRS)
            listing = "\n".join(entries[:200])
            blocks.append(f'<attached_directory path="{path}">\n{listing}\n</attached_directory>')
            notes.append({"path": str(path), "chars": len(listing), "status": "dir"})
            continue
        if not is_text_file(path):
            notes.append({"path": str(path), "chars": 0, "status": "skipped: binary file"})
            continue
        body, truncated = read_text(path, min(MAX_FILE_CHARS, max(budget, 0)))
        if budget <= 0:
            notes.append({"path": str(path), "chars": 0, "status": "skipped: message is full"})
            continue
        budget -= len(body)
        blocks.append(f'<attached_file path="{path}"{" truncated" if truncated else ""}>\n{body}\n</attached_file>')
        notes.append({"path": str(path), "chars": len(body), "status": "truncated" if truncated else "attached"})

    if blocks:
        text = text + "\n\n" + "\n\n".join(blocks)
    return text, notes
