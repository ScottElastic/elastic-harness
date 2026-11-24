"""Agents as code: one Markdown file per agent (front matter + instructions) that you can push, pull and diff.

    ---
    id: kestrel-analyst
    name: Kestrel Analyst
    description: Answers from the Kestrel notes
    labels: [kestrel, demo]
    access: public                 # public | private
    elastic_capabilities: false    # true = also get Elastic's built-in skills and tools
    tools:
      - eh-search-kestrel
    ---
    You answer questions about Project Kestrel using only the search tool. Quote names and dates exactly.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Callable

from . import frontmatter
from .client import ApiError, Elastic

KNOWN = {"id", "name", "description", "labels", "avatar_color", "avatar_symbol", "access", "elastic_capabilities",
         "tools", "skills"}
# built-ins that let an agent run or approve workflows, or call arbitrary connector actions
RISKY = re.compile(r"(resume_workflow|execute_workflow|run_workflow|execute_connector)", re.I)
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _as_list(v) -> list:
    return v if isinstance(v, list) else ([] if v in ("", None) else [v])


def parse_agent_file(text: str) -> tuple[dict, str]:
    meta, body = frontmatter.parse(text)
    unknown = sorted(set(meta) - KNOWN)
    if unknown:
        raise ValueError(f"unknown front matter key(s): {', '.join(unknown)} (known: {', '.join(sorted(KNOWN))})")
    if not meta.get("id") or not _ID.match(str(meta["id"])):
        raise ValueError("front matter needs an `id` of lowercase letters, digits, '.', '_' or '-'")
    if not meta.get("name"):
        raise ValueError("front matter needs a `name`")
    if not body.strip():
        raise ValueError("the body (the agent's instructions) is empty")
    if meta.get("access") not in (None, "public", "private"):
        raise ValueError("`access` must be public or private")
    return meta, body


def build_agent_body(meta: dict, instructions: str) -> dict:
    cfg: dict = {"instructions": instructions.strip(),
                 "tools": [{"tool_ids": [str(t) for t in _as_list(meta.get("tools"))]}],
                 "enable_elastic_capabilities": bool(meta.get("elastic_capabilities", True))}
    if _as_list(meta.get("skills")):
        cfg["skill_ids"] = [str(s) for s in _as_list(meta["skills"])]
    body = {"id": str(meta["id"]), "name": str(meta["name"]), "description": str(meta.get("description") or ""),
            "labels": [str(x) for x in _as_list(meta.get("labels"))], "configuration": cfg}
    for k in ("avatar_color", "avatar_symbol"):
        if meta.get(k):
            body[k] = str(meta[k])
    return body


def render_agent(agent: dict) -> str:
    """Agent JSON (from the API, or a build_agent_body dict) back into the file format."""
    cfg = agent.get("configuration") or {}
    tools = [t for g in cfg.get("tools") or [] for t in g.get("tool_ids", [])]
    access = (agent.get("access_control") or {}).get("access_mode")
    meta = {"id": agent.get("id"), "name": agent.get("name"), "description": agent.get("description"),
            "labels": agent.get("labels") or [], "avatar_color": agent.get("avatar_color"),
            "avatar_symbol": agent.get("avatar_symbol"),
            "access": access if access in ("public", "private") else None,
            "elastic_capabilities": bool(cfg.get("enable_elastic_capabilities", True)),
            "tools": tools, "skills": cfg.get("skill_ids") or []}
    return frontmatter.dump(meta, cfg.get("instructions") or "")


def risky_tools(tool_ids: list[str]) -> list[str]:
    return [t for t in tool_ids if RISKY.search(t)]


def _diff(old: str, new: str, name: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"{name} (cluster)", f"{name} (file)", lineterm=""))


def push(client: Elastic, path: str, *, confirm: Callable[[str, str], bool], say: Callable[[str], None] = print,
         allow_risky: bool = False, dry_run: bool = False) -> dict:
    meta, instructions = parse_agent_file(Path(path).expanduser().read_text(encoding="utf-8"))
    body = build_agent_body(meta, instructions)
    tools = body["configuration"]["tools"][0]["tool_ids"]

    bad = risky_tools(tools)
    if bad and not allow_risky:
        raise PermissionError(f"refusing: {', '.join(bad)} can run or approve workflows. An analyst-facing agent "
                              f"should not have it (pass --allow-risky if you really mean it)")
    known = {t["id"] for t in client.tools()}
    missing = [t for t in tools if t not in known]
    if missing:
        raise ValueError(f"unknown tool id(s) on cluster {client.profile.name}: {', '.join(missing)} "
                         f"(create them first, or see /tools)")

    try:
        existing = client.agent(body["id"])
    except ApiError as exc:
        if exc.status != 404:
            raise
        existing = None
    target = f"cluster {client.profile.name} / space {client.space}"
    if existing and existing.get("readonly"):
        raise PermissionError(f"{body['id']} is a built-in, read-only agent")

    if existing:
        before, after = render_agent(existing), render_agent({**body, "access_control": {"access_mode": meta.get("access")}
                                                              if meta.get("access") else existing.get("access_control")})
        if before.strip() == after.strip():
            return {"id": body["id"], "action": "unchanged"}
        say(_diff(before, after, body["id"]))
        action = "update"
    else:
        say(f"new agent {body['id']!r}: {len(tools)} tools, {len(instructions)} chars of instructions")
        action = "create"
    if dry_run:
        return {"id": body["id"], "action": f"would {action}"}
    if not confirm(f"{action.capitalize()} agent {body['id']} on {target}?", ""):
        return {"id": body["id"], "action": "cancelled"}

    if existing:
        client.kb("PUT", f"/api/agent_builder/agents/{body['id']}", {k: v for k, v in body.items() if k != "id"})
    else:
        extra = {"access_control": {"access_mode": meta["access"]}} if meta.get("access") else {}
        client.kb("POST", "/api/agent_builder/agents", {**body, **extra})
    if existing and meta.get("access"):
        _set_access(client, body["id"], meta["access"])
    return {"id": body["id"], "action": action + "d"}


def _set_access(client: Elastic, agent_id: str, mode: str) -> None:
    try:
        client.kb("PUT", f"/api/agent_builder/agents/{agent_id}/access_control", {"access_mode": mode})
    except ApiError:
        client.kb("PUT", f"/api/agent_builder/agents/{agent_id}", {"access_control": {"access_mode": mode}})


def pull(client: Elastic, agent_id: str, out_dir: str, *, force: bool = False) -> Path:
    agent = client.agent(agent_id)
    out = Path(out_dir).expanduser() / f"{agent_id}.md"
    if out.exists() and not force:
        raise FileExistsError(f"{out} exists (use --force to overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_agent(agent), encoding="utf-8")
    return out


def pull_all(client: Elastic, out_dir: str, *, force: bool = False) -> list[Path]:
    return [pull(client, a["id"], out_dir, force=force) for a in client.agents()
            if not a.get("readonly") and a["id"] != "elastic-ai-agent"]
