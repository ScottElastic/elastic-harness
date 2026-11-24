"""A tiny YAML-subset front-matter reader/writer (no PyYAML dependency).

Supported: `key: scalar`, `key: [a, b]`, and block lists (`key:` then `  - item`). Scalars are strings, quoted
strings, booleans and integers. Enough for agent files and custom commands; anything else is an error.
"""

from __future__ import annotations

import json
import re


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
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


def parse(text: str) -> tuple[dict, str]:
    """Return (meta, body). A file without front matter is all body."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("front matter is not closed with a second '---' line") from None
    meta: dict = {}
    current = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and current is not None and isinstance(meta.get(current), list):
            meta[current].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z_][\w\-]*):\s*(.*)$", line)
        if not kv:
            raise ValueError(f"cannot read front matter line: {line!r}")
        key, val = kv.groups()
        if val == "":
            meta[key], current = [], key
        elif val.startswith("["):
            meta[key], current = _inline_list(val), None
        else:
            meta[key], current = _scalar(val), None
    return meta, "\n".join(lines[end + 1:]).lstrip("\n")


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    needs = (not s or s != s.strip() or s[0] in "[{&*!|>'\"%@`#-" or ": " in s or " #" in s
             or s.lower() in ("true", "false", "yes", "no") or re.fullmatch(r"-?\d+", s))
    return json.dumps(s, ensure_ascii=False) if needs else s


def dump(meta: dict, body: str) -> str:
    out = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            if not v:
                continue
            out.append(f"{k}:")
            out += [f"  - {_fmt(i)}" for i in v]
        elif v is None or v == "":
            continue
        else:
            out.append(f"{k}: {_fmt(v)}")
    out.append("---")
    return "\n".join(out) + "\n" + body.strip("\n") + "\n"
