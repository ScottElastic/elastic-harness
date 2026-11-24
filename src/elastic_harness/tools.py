"""Local tools for ``local`` mode, plus the bridges back into Elastic.

Read-only tools run without asking. Anything that changes the machine (write_file,
edit_file, bash) or hands work to a server-side agent (ask_agent) asks first.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .client import Elastic
from .guard import SKIP_DIRS, is_secret_path, is_text_file
from .ingest import iter_files
from .render import Ui

MAX_RESULT_CHARS = 30_000


@dataclass
class Ctx:
    client: Elastic
    ui: Ui
    cwd: Path = field(default_factory=Path.cwd)
    model: str = ""
    always: set = field(default_factory=set)  # confirmations remembered for this session


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: list
    fn: Callable[[dict, Ctx], str]
    describe: Callable[[dict], tuple[str, str, str]] | None = None  # (title, detail, remember-key) if it must ask
    remember: bool = True

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.properties, "required": self.required}}}


def _path(args: dict, ctx: Ctx, key: str = "path", default: str = ".") -> Path:
    p = Path(str(args.get(key) or default)).expanduser()
    return (p if p.is_absolute() else ctx.cwd / p).resolve()


def _cap(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n… [truncated, {len(text) - MAX_RESULT_CHARS} more characters]"


# -- read-only ---------------------------------------------------------------------------------
def read_file(args: dict, ctx: Ctx) -> str:
    p = _path(args, ctx)
    if reason := is_secret_path(p):
        return f"refused: {reason}"
    if not p.is_file():
        return f"error: {p} is not a file"
    if not is_text_file(p):
        return f"error: {p} looks binary"
    start = max(int(args.get("offset") or 1), 1)
    limit = min(int(args.get("limit") or 400), 2000)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[start - 1:start - 1 + limit]
    body = "\n".join(f"{i:>5}  {ln}" for i, ln in enumerate(chunk, start))
    more = f"\n… {len(lines) - (start - 1 + limit)} more lines (use offset)" if len(lines) > start - 1 + limit else ""
    return _cap(f"{p} ({len(lines)} lines)\n{body}{more}")


def list_dir(args: dict, ctx: Ctx) -> str:
    p = _path(args, ctx)
    if not p.is_dir():
        return f"error: {p} is not a directory"
    rows = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if e.name in SKIP_DIRS:
            continue
        rows.append(f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)")
    return _cap(f"{p}\n" + "\n".join(rows[:300]))


def glob_files(args: dict, ctx: Ctx) -> str:
    root = _path(args, ctx, "root")
    pat = str(args.get("pattern") or "*")
    hits = []
    for f in root.glob(pat):
        if any(part in SKIP_DIRS for part in f.relative_to(root).parts):
            continue
        if f.is_file() and not is_secret_path(f):
            hits.append(str(f))
        if len(hits) >= 200:
            break
    return "\n".join(sorted(hits)) or "no matches"


def grep(args: dict, ctx: Ctx) -> str:
    root = _path(args, ctx)
    try:
        rx = re.compile(str(args["pattern"]), re.IGNORECASE if args.get("ignore_case", True) else 0)
    except re.error as exc:
        return f"error: bad regex: {exc}"
    file_glob = str(args.get("glob") or "*")
    out: list[str] = []
    for f in iter_files(root):
        if not fnmatch.fnmatch(f.name, file_glob) or is_secret_path(f) or not is_text_file(f):
            continue
        try:
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f}:{n}: {line.strip()[:200]}")
                    if len(out) >= 100:
                        return _cap("\n".join(out) + "\n… stopped at 100 matches")
        except OSError:
            continue
    return _cap("\n".join(out)) or "no matches"


# -- changes (ask first) -----------------------------------------------------------------------
def write_file(args: dict, ctx: Ctx) -> str:
    p = _path(args, ctx)
    if reason := is_secret_path(p):
        return f"refused: {reason}"
    content = str(args.get("content", ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} characters to {p}"


def _describe_write(args: dict) -> tuple[str, str, str]:
    content = str(args.get("content", ""))
    head = "\n".join(content.splitlines()[:12])
    return f"Write {args.get('path')} ({len(content)} chars)", head, "write_file"


def edit_file(args: dict, ctx: Ctx) -> str:
    p = _path(args, ctx)
    if reason := is_secret_path(p):
        return f"refused: {reason}"
    if not p.is_file():
        return f"error: {p} is not a file"
    old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
    text = p.read_text(encoding="utf-8")
    if not old or text.count(old) != 1:
        return f"error: old_string must match exactly once (found {text.count(old) if old else 0})"
    p.write_text(text.replace(old, new), encoding="utf-8")
    return f"edited {p}"


def _describe_edit(args: dict) -> tuple[str, str, str]:
    diff = "\n".join(difflib.unified_diff(str(args.get("old_string", "")).splitlines(),
                                          str(args.get("new_string", "")).splitlines(), "old", "new", lineterm=""))
    return f"Edit {args.get('path')}", diff, "edit_file"


def bash(args: dict, ctx: Ctx) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EH_")}  # never hand the shell our credentials
    try:
        r = subprocess.run(str(args["command"]), shell=True, cwd=ctx.cwd, env=env, capture_output=True, text=True,
                           timeout=min(int(args.get("timeout") or 60), 300), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "error: command timed out"
    err = "\n[stderr]\n" + r.stderr if r.stderr else ""
    return _cap(f"exit {r.returncode}\n{r.stdout}{err}")


# -- Elastic bridges ---------------------------------------------------------------------------
def _table(res: dict, max_rows: int = 50) -> str:
    cols = [c["name"] for c in res.get("columns", [])]
    rows = res.get("values", [])
    lines = [" | ".join(cols)]
    for r in rows[:max_rows]:
        lines.append(" | ".join("" if v is None else str(v)[:120] for v in r))
    if len(rows) > max_rows:
        lines.append(f"… {len(rows) - max_rows} more rows")
    return _cap("\n".join(lines) if rows else "no rows")


def elastic_esql(args: dict, ctx: Ctx) -> str:
    return _table(ctx.client.esql(str(args["query"])))


def list_agents(args: dict, ctx: Ctx) -> str:
    rows = [f"{a['id']}: {a.get('name', '')} — {(a.get('description') or '')[:100]}" for a in ctx.client.agents()]
    return "\n".join(rows) or "no agents"


def ask_agent(args: dict, ctx: Ctx) -> str:
    res = ctx.client.converse(agent_id=str(args["agent_id"]), text=str(args["question"]),
                              conversation_id=args.get("conversation_id") or None, model=ctx.model or None)
    msg = (res.get("response") or {}).get("message", "")
    used = [s.get("tool_id") for s in res.get("steps", []) if s.get("type") == "tool_call"]
    return _cap(f"{msg}\n\n[agent={args['agent_id']} conversation_id={res.get('conversation_id')} tools_used={used}]")


def _describe_ask_agent(args: dict) -> tuple[str, str, str]:
    return f"Ask Elastic agent '{args.get('agent_id')}' (it may run its own tools)", str(args.get("question", "")), f"ask_agent:{args.get('agent_id')}"


def elastic_tool(args: dict, ctx: Ctx) -> str:
    tid = str(args["tool_id"])
    meta = next((t for t in ctx.client.tools() if t["id"] == tid), None)
    if meta is None:
        return f"error: no tool {tid!r}"
    if meta.get("type") not in ("esql", "index_search"):
        return f"refused: only esql and index_search tools can be run directly (this is {meta.get('type')}); use ask_agent"
    res = ctx.client.execute_tool(tid, args.get("params") or {})
    return _cap(json.dumps(res.get("results", res), ensure_ascii=False, default=str))


def build_tools() -> list[Tool]:
    s = lambda desc: {"type": "string", "description": desc}  # noqa: E731
    return [
        Tool("read_file", "Read a text file with line numbers.", {"path": s("File path"), "offset": {"type": "integer", "description": "First line (1-based)"}, "limit": {"type": "integer", "description": "Max lines (default 400)"}}, ["path"], read_file),
        Tool("list_dir", "List a directory.", {"path": s("Directory (default: working directory)")}, [], list_dir),
        Tool("glob_files", "Find files by glob pattern, e.g. '**/*.md'.", {"pattern": s("Glob pattern"), "root": s("Root directory (default: working directory)")}, ["pattern"], glob_files),
        Tool("grep", "Search file contents with a regex (case-insensitive by default).", {"pattern": s("Regex"), "path": s("File or directory"), "glob": s("Filename filter, e.g. '*.py'"), "ignore_case": {"type": "boolean"}}, ["pattern"], grep),
        Tool("write_file", "Create or overwrite a file. Asks the user first.", {"path": s("File path"), "content": s("Full file content")}, ["path", "content"], write_file, _describe_write),
        Tool("edit_file", "Replace one exact string in a file. Asks the user first.", {"path": s("File path"), "old_string": s("Exact text to replace (must occur once)"), "new_string": s("Replacement")}, ["path", "old_string", "new_string"], edit_file, _describe_edit),
        Tool("bash", "Run a shell command in the working directory. Always asks the user first.", {"command": s("Shell command"), "timeout": {"type": "integer", "description": "Seconds (max 300)"}}, ["command"], bash, lambda a: ("Run shell command", str(a.get("command", "")), "bash"), remember=False),
        Tool("elastic_esql", "Run a read-only ES|QL query against the connected Elasticsearch cluster. Always include a LIMIT.", {"query": s("ES|QL query")}, ["query"], elastic_esql),
        Tool("list_agents", "List the Elastic Agent Builder agents available to the user.", {}, [], list_agents),
        Tool("ask_agent", "Ask a question of an Elastic Agent Builder agent (it runs server-side with its own tools and data). Pass conversation_id from an earlier answer to continue.", {"agent_id": s("Agent id"), "question": s("The question"), "conversation_id": s("Optional, to continue a conversation")}, ["agent_id", "question"], ask_agent, _describe_ask_agent),
        Tool("elastic_tool", "Run a custom esql or index_search Agent Builder tool directly.", {"tool_id": s("Tool id"), "params": {"type": "object", "description": "Tool parameters"}}, ["tool_id"], elastic_tool),
    ]


def run_tool(tool: Tool, args: dict, ctx: Ctx) -> str:
    if tool.describe is not None:
        title, detail, key = tool.describe(args)
        if key not in ctx.always:
            ans = ctx.ui.confirm(title, detail, allow_always=tool.remember)
            if ans == "n":
                return "The user declined this action."
            if ans == "a" and tool.remember:
                ctx.always.add(key)
    try:
        return tool.fn(args, ctx)
    except Exception as exc:  # tool errors go back to the model, not up the stack
        return f"error: {type(exc).__name__}: {exc}"
