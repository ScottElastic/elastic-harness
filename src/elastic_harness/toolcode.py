"""Tools as code: one Markdown file per custom Agent Builder tool that you can push, pull and diff.

An ES|QL tool: the body is the query, `?name` placeholders are the parameters.

    ---
    id: kestrel-risks-by-owner
    type: esql
    description: Open Kestrel risks owned by one person
    tags: [kestrel, demo]
    params:
      owner:
        type: string
        description: Owner's full name
      limit:
        type: integer
        description: Most rows to return
        optional: true
        defaultValue: 5
    ---
    FROM eh-local-kestrel | WHERE owner == ?owner | LIMIT ?limit

An index search tool has no query: it names the index pattern it searches and has an empty body.

    ---
    id: kestrel-notes
    type: index_search
    description: Search the Kestrel notes
    pattern: eh-local-kestrel
    ---

Only `esql` and `index_search` tools are handled. Workflow, MCP and built-in tools are not touched.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Callable

from . import frontmatter
from .client import ApiError, Elastic

KNOWN = {"id", "type", "description", "tags", "params", "pattern"}
TYPES = ("esql", "index_search")
PARAM_TYPES = ("string", "integer", "float", "boolean", "date", "array")
PARAM_KEYS = ("type", "description", "optional", "defaultValue")  # also the order they are written in
_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")  # the rule Kibana enforces
_NAME = re.compile(r"^[A-Za-z_]\w*$")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_PLACEHOLDER = re.compile(r"\?\??([A-Za-z_]\w*)")


def _as_list(v) -> list:
    return v if isinstance(v, list) else ([] if v in ("", None) else [v])


def _coerce_default(ptype: str, v, name: str):
    """Front matter gives strings, ints and bools; make the default match the declared type."""
    try:
        if ptype == "integer":
            if isinstance(v, bool):
                raise ValueError
            return int(v)
        if ptype == "float":
            if isinstance(v, bool):
                raise ValueError
            return float(v)
        if ptype == "boolean":
            if isinstance(v, bool):
                return v
            raise ValueError
        if ptype == "array":
            return v if isinstance(v, list) else [v]
    except (TypeError, ValueError):
        raise ValueError(f"param {name!r}: defaultValue {v!r} is not a valid {ptype}") from None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _norm_param(name: str, p) -> dict:
    if not isinstance(p, dict):
        raise ValueError(f"param {name!r} must be a block with at least `type:`")
    unknown = sorted(set(p) - set(PARAM_KEYS))
    if unknown:
        raise ValueError(f"param {name!r}: unknown key(s) {', '.join(unknown)} (known: {', '.join(PARAM_KEYS)})")
    ptype = p.get("type")
    if ptype not in PARAM_TYPES:
        raise ValueError(f"param {name!r}: `type` must be one of {', '.join(PARAM_TYPES)} (got {ptype!r})")
    out: dict = {"type": ptype}
    if p.get("description"):
        out["description"] = str(p["description"])
    if "optional" in p:
        if not isinstance(p["optional"], bool):
            raise ValueError(f"param {name!r}: `optional` must be true or false")
        if p["optional"]:  # false is the default; leaving it out keeps files and the cluster's copy comparable
            out["optional"] = True
    if p.get("defaultValue") not in (None, ""):
        out["defaultValue"] = _coerce_default(ptype, p["defaultValue"], name)
    return out


def _placeholders(query: str) -> set[str]:
    return set(_PLACEHOLDER.findall(_STRING.sub('""', query)))


def parse_tool_file(text: str) -> tuple[dict, str]:
    """Validate a tool file. Returns (meta with params normalised, body)."""
    meta, body = frontmatter.parse(text)
    unknown = sorted(set(meta) - KNOWN)
    if unknown:
        raise ValueError(f"unknown front matter key(s): {', '.join(unknown)} (known: {', '.join(sorted(KNOWN))})")
    if not meta.get("id") or not _ID.match(str(meta["id"])):
        raise ValueError("front matter needs an `id` of lowercase letters, digits, '.', '_' or '-' "
                         "(starting and ending with a letter or digit)")
    if meta.get("type") not in TYPES:
        raise ValueError(f"front matter needs `type: esql` or `type: index_search` (got {meta.get('type')!r})")
    if not str(meta.get("description") or "").strip():
        raise ValueError("front matter needs a `description`: it is what the agent reads to decide when to use the tool")
    meta = dict(meta)
    raw_params = meta.get("params") or {}
    if not isinstance(raw_params, dict):
        raise ValueError("`params` must be a block of `name:` entries, each with a `type:`")
    if meta["type"] == "esql":
        if meta.get("pattern"):
            raise ValueError("`pattern` is only for index_search tools")
        if not body.strip():
            raise ValueError("the body (the ES|QL query) is empty")
        for name in raw_params:
            if not _NAME.match(str(name)):
                raise ValueError(f"param name {name!r} must be letters, digits and underscores")
        meta["params"] = {str(n): _norm_param(str(n), p) for n, p in raw_params.items()}
        undeclared = sorted(_placeholders(body) - set(meta["params"]))
        if undeclared:
            raise ValueError(f"the query uses ?{', ?'.join(undeclared)} but `params` does not declare "
                             f"{'it' if len(undeclared) == 1 else 'them'}")
    else:
        if not str(meta.get("pattern") or "").strip():
            raise ValueError("an index_search tool needs `pattern:` (the index or pattern it searches)")
        if raw_params:
            raise ValueError("`params` is only for esql tools")
        if body.strip():
            raise ValueError("an index_search tool has no query: leave the body empty and put the index in `pattern`")
        meta["params"] = {}
    return meta, body


def lint(meta: dict, body: str) -> list[str]:
    """Things that are legal but probably a mistake."""
    unused = sorted(set(meta.get("params") or {}) - _placeholders(body))
    return [f"param {n!r} is declared but the query never uses it" for n in unused]


def build_tool_body(meta: dict, body: str) -> dict:
    """The API's create body (with id and type) for a validated file."""
    if meta["type"] == "esql":
        cfg: dict = {"query": body.strip(), "params": meta.get("params") or {}}
    else:
        cfg = {"pattern": str(meta["pattern"]).strip()}
    return {"id": str(meta["id"]), "type": meta["type"], "description": str(meta["description"]).strip(),
            "tags": [str(t) for t in _as_list(meta.get("tags"))], "configuration": cfg}


def render_tool(tool: dict) -> str:
    """Tool JSON (from the API, or a build_tool_body dict) back into the file format."""
    cfg = tool.get("configuration") or {}
    meta: dict = {"id": tool.get("id"), "type": tool.get("type"), "description": tool.get("description"),
                  "tags": tool.get("tags") or []}
    body = ""
    if tool.get("type") == "esql":
        meta["params"] = {n: _norm_param(n, p) for n, p in (cfg.get("params") or {}).items()}
        body = cfg.get("query") or ""
    else:
        meta["pattern"] = cfg.get("pattern")
    return frontmatter.dump(meta, body)


def _diff(old: str, new: str, name: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"{name} (cluster)", f"{name} (file)", lineterm=""))


def push(client: Elastic, path: str, *, confirm: Callable[[str, str], bool], say: Callable[[str], None] = print,
         dry_run: bool = False) -> dict:
    meta, query = parse_tool_file(Path(path).expanduser().read_text(encoding="utf-8"))
    body = build_tool_body(meta, query)
    tid = body["id"]
    for note in lint(meta, query):
        say(f"note: {note}")
    try:
        existing = client.tool(tid)
    except ApiError as exc:
        if exc.status != 404:
            raise
        existing = None
    target = f"cluster {client.profile.name} / space {client.space}"
    if existing and existing.get("readonly"):
        raise PermissionError(f"{tid} is a built-in, read-only tool")
    if existing and existing.get("type") != body["type"]:
        raise ValueError(f"{tid} is an {existing.get('type')} tool on the cluster and a tool's type cannot change "
                         f"(delete it in Kibana, or give the file a new id)")

    if existing:
        before, after = render_tool(existing), render_tool(body)
        if before.strip() == after.strip():
            return {"id": tid, "action": "unchanged"}
        say(_diff(before, after, tid))
        action = "update"
    else:
        say(f"new {body['type']} tool {tid!r}: {len(body['configuration'].get('params') or {})} params")
        action = "create"
    if dry_run:
        return {"id": tid, "action": f"would {action}"}
    if not confirm(f"{action.capitalize()} tool {tid} on {target}?", ""):
        return {"id": tid, "action": "cancelled"}

    if existing:  # PUT takes no id or type
        client.kb("PUT", f"/api/agent_builder/tools/{tid}",
                  {k: v for k, v in body.items() if k not in ("id", "type")})
    else:
        client.kb("POST", "/api/agent_builder/tools", body)
    return {"id": tid, "action": action + "d"}


def pull(client: Elastic, tool_id: str, out_dir: str, *, force: bool = False) -> Path:
    tool = client.tool(tool_id)
    if tool.get("readonly"):
        raise ValueError(f"{tool_id} is a built-in tool: there is nothing of yours to pull")
    return _write(tool, out_dir, force)


def _write(tool: dict, out_dir: str, force: bool) -> Path:
    if tool.get("type") not in TYPES:
        raise ValueError(f"{tool.get('id')} is a {tool.get('type')} tool; only {' and '.join(TYPES)} tools are handled")
    out = Path(out_dir).expanduser() / f"{tool['id']}.md"
    if out.exists() and not force:
        raise FileExistsError(f"{out} exists (use --force to overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_tool(tool), encoding="utf-8")
    return out


def custom_tools(client: Elastic) -> list[dict]:
    """Tools someone created (not built-ins), of the types this module handles."""
    return [t for t in client.tools() if not t.get("readonly") and t.get("type") in TYPES]


def pull_all(client: Elastic, out_dir: str, *, force: bool = False) -> list[Path]:
    return [_write(t, out_dir, force) for t in custom_tools(client)]


def listing(client: Elastic) -> tuple[list[tuple[str, str]], str]:
    """(id, one-line summary) for each custom tool, plus a footer counting the rest. Used by `eh tool list` and `/tool list`."""
    tools = client.tools()
    mine = [t for t in tools if not t.get("readonly") and t.get("type") in TYPES]
    rows = []
    for t in mine:
        cfg = t.get("configuration") or {}
        what = f"{len(cfg.get('params') or {})} params" if t.get("type") == "esql" else cfg.get("pattern", "")
        desc = " ".join((t.get("description") or "").split())
        rows.append((t["id"], f"{t.get('type')} · {what} · {desc[:69] + '…' if len(desc) > 70 else desc}"))
    builtin = sum(1 for t in tools if t.get("readonly"))
    other = len(tools) - len(mine) - builtin
    return rows, (f"{len(mine)} custom tools here (esql, index_search) · {builtin} built-in"
                  + (f" · {other} of other types, not handled" if other else ""))
