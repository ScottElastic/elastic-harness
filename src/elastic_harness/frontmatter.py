"""A tiny YAML-subset front-matter reader/writer (no PyYAML dependency).

Supported: `key: scalar`, `key: [a, b]`, block lists (`key:` then `  - item`) and nested block maps (`key:` then
indented `sub: value` lines, any depth). Scalars are strings, quoted strings, booleans and integers. A ` # comment`
after a value is ignored. Enough for agent files, tool files and custom commands; anything else is an error.
"""

from __future__ import annotations

import json
import re


def _strip_comment(v: str) -> str:
    """Drop a trailing ` # comment`, leaving quoted text alone (`"a # b"` stays whole)."""
    quote, i = "", 0
    while i < len(v):
        ch = v[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'" and (i == 0 or v[i - 1] in " \t[,"):
            quote = ch
        elif ch == "#" and (i == 0 or v[i - 1] in " \t"):
            return v[:i].rstrip()
        i += 1
    return v.strip()


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        try:
            return json.loads(v)  # exact inverse of what dump() writes
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _inline_list(v: str) -> list:
    inner, items, cur, quote = v.strip()[1:-1], [], "", ""
    for ch in inner:
        if quote:
            cur += ch
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            cur += ch
            quote = ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur)
    return [_scalar(i) for i in items if i.strip()]


_KEY = re.compile(r"^([A-Za-z_][\w\-]*):\s*(.*)$")
_ITEM = re.compile(r"^-(\s+(.*))?$")


def _block(items: list[tuple[int, str, str]], pos: int, indent: int):
    """Parse the list or map whose lines sit at `indent`, starting at items[pos]. Returns (value, next pos)."""
    if _ITEM.match(items[pos][1]):
        out_list: list = []
        while pos < len(items) and items[pos][0] == indent and _ITEM.match(items[pos][1]):
            out_list.append(_scalar(_strip_comment(_ITEM.match(items[pos][1]).group(2) or "")))
            pos += 1
        return out_list, pos
    out: dict = {}
    while pos < len(items) and items[pos][0] == indent:
        _, text, raw = items[pos]
        kv = _KEY.match(text)
        if not kv:
            raise ValueError(f"cannot read front matter line: {raw!r}")
        key, val = kv.group(1), _strip_comment(kv.group(2))
        pos += 1
        if val == "":
            nxt = items[pos] if pos < len(items) else None
            # a child block: indented deeper, or a list written at the key's own indent
            if nxt and (nxt[0] > indent or (nxt[0] == indent and _ITEM.match(nxt[1]))):
                out[key], pos = _block(items, pos, nxt[0])
            else:
                out[key] = []
        elif val.startswith("["):
            out[key] = _inline_list(val)
        else:
            out[key] = _scalar(val)
    return out, pos


def parse(text: str) -> tuple[dict, str]:
    """Return (meta, body). A file without front matter is all body."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("front matter is not closed with a second '---' line") from None
    items = [(len(ln) - len(ln.lstrip(" ")), ln.strip(), ln) for ln in lines[1:end]
             if ln.strip() and not ln.lstrip().startswith("#")]
    meta: dict = {}
    if items:
        if items[0][0] != 0:
            raise ValueError(f"cannot read front matter line: {items[0][2]!r}")
        meta, pos = _block(items, 0, 0)
        if pos < len(items):
            raise ValueError(f"cannot read front matter line: {items[pos][2]!r}")
        if not isinstance(meta, dict):
            raise ValueError(f"cannot read front matter line: {items[0][2]!r}")
    return meta, "\n".join(lines[end + 1:]).lstrip("\n")


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    needs = (not s or s != s.strip() or s[0] in "[{&*!|>'\"%@`#-" or ": " in s or " #" in s or "\n" in s
             or s.lower() in ("true", "false", "yes", "no") or re.fullmatch(r"-?\d+", s))
    return json.dumps(s, ensure_ascii=False) if needs else s


def _dump_map(meta: dict, pad: str) -> list[str]:
    out: list[str] = []
    for k, v in meta.items():
        if isinstance(v, dict):
            if v:
                out.append(f"{pad}{k}:")
                out += _dump_map(v, pad + "  ")
        elif isinstance(v, list):
            if v:
                out.append(f"{pad}{k}:")
                out += [f"{pad}  - {_fmt(i)}" for i in v]
        elif v is None or v == "":
            continue
        else:
            out.append(f"{pad}{k}: {_fmt(v)}")
    return out


def dump(meta: dict, body: str) -> str:
    return "\n".join(["---", *_dump_map(meta, ""), "---"]) + "\n" + body.strip("\n") + "\n"
