"""Project instructions (EH.md) and custom slash commands (Markdown files).

EH.md: found in the working directory or any parent up to your home folder, plus
~/.config/elastic-harness/EH.md. In local mode it joins the system prompt; in agent mode it is attached once, at
the start of a new conversation (the agent's own stored instructions are never replaced).

Commands: ~/.config/elastic-harness/commands/*.md and <project>/.eh/commands/*.md (project wins). The file name is
the command: triage.md -> /triage. Optional front matter: description, mode (agent|local), agent, model. In the
body, $ARGS is everything typed after the command and $1..$9 are single words; with no placeholder the arguments
are appended.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter
from .config import CONFIG_DIR

MAX_CONTEXT = 20_000
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


def find_context(cwd: Path, home: Path | None = None) -> list[tuple[Path, str]]:
    home = (home or Path.home()).resolve()
    found: list[tuple[Path, str]] = []
    glob = CONFIG_DIR / "EH.md"
    if glob.is_file():
        found.append((glob, glob.read_text(encoding="utf-8", errors="replace")))
    chain = []
    d = cwd.resolve()
    while True:
        chain.append(d)
        if d == home or d.parent == d:
            break
        d = d.parent
    for d in reversed(chain):  # outermost first, so the nearest file has the last word
        f = d / "EH.md"
        if f.is_file():
            found.append((f, f.read_text(encoding="utf-8", errors="replace")))
    return found


def context_text(cwd: Path, home: Path | None = None) -> tuple[str, list[Path]]:
    files = find_context(cwd, home)
    text = "\n\n".join(f"# from {p}\n{t.strip()}" for p, t in files)
    return text[:MAX_CONTEXT], [p for p, _ in files]


@dataclass
class Command:
    name: str
    body: str
    description: str = ""
    mode: str = ""
    agent: str = ""
    model: str = ""
    source: Path | None = None
    extra: dict = field(default_factory=dict)

    def expand(self, args: str) -> str:
        words = args.split()
        out = self.body.strip()
        used = "$ARGS" in out or bool(re.search(r"\$[1-9]", out))
        out = out.replace("$ARGS", args)
        out = re.sub(r"\$([1-9])", lambda m: words[int(m.group(1)) - 1] if int(m.group(1)) <= len(words) else "", out)
        return out if used or not args else f"{out}\n\n{args}"


def load_commands(cwd: Path, reserved: set[str] = frozenset()) -> tuple[dict[str, Command], list[str]]:
    """Return ({name: Command}, warnings). Project commands override user commands."""
    cmds: dict[str, Command] = {}
    warnings: list[str] = []
    for d in (CONFIG_DIR / "commands", cwd / ".eh" / "commands"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            name = f.stem.lower()
            if not _NAME.match(name):
                warnings.append(f"{f.name}: command names are lowercase letters, digits, - and _")
                continue
            if name in reserved:
                warnings.append(f"{f}: /{name} is a built-in command and cannot be replaced")
                continue
            try:
                meta, body = frontmatter.parse(f.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                warnings.append(f"{f.name}: {exc}")
                continue
            if meta.get("mode") not in (None, "agent", "local"):
                warnings.append(f"{f.name}: mode must be agent or local")
                continue
            desc = str(meta.get("description") or next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), ""))
            cmds[name] = Command(name, body, desc[:90], str(meta.get("mode") or ""), str(meta.get("agent") or ""),
                                 str(meta.get("model") or ""), f)
    return cmds, warnings
