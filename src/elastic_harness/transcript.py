"""/copy, /export and /open helpers."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path


def clipboard_command() -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        return ["pbcopy"]
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


def copy_to_clipboard(text: str) -> str:
    cmd = clipboard_command()
    if cmd is None:
        raise RuntimeError("no clipboard tool found (pbcopy, wl-copy, xclip or xsel)")
    subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=10)
    return cmd[0]


def last_code_block(text: str) -> str | None:
    blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
    return blocks[-1].rstrip("\n") if blocks else None


def open_url(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def default_export_path(cwd: Path) -> Path:
    return cwd / f"eh-transcript-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def agent_markdown(conv: dict, header: dict) -> str:
    out = _header(header, conv.get("title") or "Conversation")
    for rnd in conv.get("rounds", []):
        out.append(f"## You\n\n{(rnd.get('input') or {}).get('message', '')}\n")
        tools = [s for s in rnd.get("steps", []) if s.get("type") == "tool_call"]
        if tools:
            out.append("Tool calls:\n")
            out += [f"- `{s.get('tool_id')}` {_short(s.get('params', {}), 140)}" for s in tools]
            out.append("")
        out.append(f"## Agent\n\n{(rnd.get('response') or {}).get('message', '')}\n")
        usage = rnd.get("model_usage") or {}
        if usage:
            out.append(f"_{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out tokens, "
                       f"{usage.get('connector_id', '')}_\n")
    return "\n".join(out)


def local_markdown(messages: list[dict], header: dict) -> str:
    out = _header(header, "Local session")
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append(f"## You\n\n{m.get('content', '')}\n")
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                out.append(f"- tool `{fn.get('name')}` {_short(fn.get('arguments', ''), 140)}")
            if m.get("content"):
                out.append(f"\n## Assistant\n\n{m['content']}\n")
        elif role == "tool":
            out.append(f"  - result: {_short(m.get('content', ''), 200)}")
    return "\n".join(out)


def _header(h: dict, title: str) -> list[str]:
    lines = [f"# {title}", ""]
    lines += [f"- **{k}:** {v}" for k, v in h.items() if v]
    lines += ["", "---", ""]
    return lines
