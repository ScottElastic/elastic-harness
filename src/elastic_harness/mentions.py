"""@alert:<id>, @case:<id> and @index:<pattern>: pull Elastic objects into the prompt.

The objects are fetched through Kibana/Elasticsearch with the profile's own key, so what you can @-mention is
what that key can already read in the current space.
"""

from __future__ import annotations

import json
import re

from pathlib import Path

from .client import ApiError, Elastic
from .guard import attach_blocks, gather_files, neutralize

_ELASTIC = re.compile(r"(?<![\w@/.])@(alert|case|index):([\w.\-*,:/]+[\w*])")
MAX_BLOCK = 7000
_INCOMPLETE = re.compile(r"(?<![\w@/.])@(alert|case|index):(?![\w.\-*,:/]*[\w*])")
_NEEDS = {"alert": "an alert id (type @alert: and press Tab to pick one)", "case": "a case id (type @case: and press Tab to pick one)",
          "index": "an index pattern, e.g. @index:eh-local-*"}


def incomplete_mentions(text: str) -> list[str]:
    """What is missing after each bare `@alert:`, `@case:` or `@index:` in the text, e.g. 'an alert id (…)'.

    A bare one was sent to the model as plain text once (it answered "your message got cut off"), so callers refuse it.
    """
    return [f"@{k}: needs {_NEEDS[k]}" for k in dict.fromkeys(x.group(1) for x in _INCOMPLETE.finditer(text))]

ALERT_FIRST = [
    "@timestamp", "kibana.alert.rule.name", "kibana.alert.severity", "kibana.alert.risk_score",
    "kibana.alert.workflow_status", "kibana.alert.reason", "kibana.alert.rule.description", "event.category",
    "event.action", "host.name", "host.os.name", "user.name", "process.name", "process.command_line",
    "process.parent.name", "file.path", "source.ip", "destination.ip", "destination.port", "dns.question.name",
    "kibana.alert.rule.threat.technique.name", "kibana.alert.rule.threat.tactic.name",
]
ALERT_SKIP = ("kibana.alert.rule.parameters", "kibana.alert.ancestors", "kibana.alert.rule.execution",
              "kibana.alert.depth", "kibana.alert.rule.uuid", "kibana.alert.rule.rule_id", "kibana.space_ids",
              "kibana.version", "kibana.alert.original_time", "kibana.alert.rule.revision",
              "kibana.alert.rule.risk_score_mapping", "kibana.alert.rule.severity_mapping", "event.ingested",
              "kibana.alert.status", "kibana.alert.uuid", "kibana.alert.rule.consumer", "kibana.alert.rule.producer",
              "kibana.alert.rule.category", "kibana.alert.rule.rule_type_id", "kibana.alert.rule.immutable")


def _get(src: dict, key: str):
    if key in src:
        return src[key]
    cur = src
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _val(v, limit: int = 300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_alert(alert_id: str, hit: dict) -> str:
    src = hit.get("_source", {})
    lines, seen = [], set()
    for key in ALERT_FIRST:
        v = _get(src, key)
        if v not in (None, "", [], {}):
            lines.append(f"{key}: {_val(v)}")
            seen.add(key)
    for key in sorted(k for k in src if "." in k or not isinstance(src[k], dict)):
        if key in seen or key.startswith(ALERT_SKIP) or src[key] in (None, "", [], {}):
            continue
        lines.append(f"{key}: {_val(src[key], 200)}")
    body = "\n".join(lines)
    if len(body) > MAX_BLOCK:
        body = body[:MAX_BLOCK] + "\n… [truncated]"
    return f'<elastic_alert id="{alert_id}" index="{hit.get("_index", "")}">\n{neutralize(body)}\n</elastic_alert>'


def format_case(case: dict, comments: list[dict], alert_ids: list[str]) -> str:
    head = [f"title: {case.get('title')}", f"status: {case.get('status')}", f"severity: {case.get('severity')}",
            f"tags: {', '.join(case.get('tags') or [])}", f"created: {case.get('created_at')}",
            f"assignees: {len(case.get('assignees') or [])}", f"attached alerts: {case.get('totalAlerts')}",
            f"description: {_val(case.get('description') or '', 1200)}"]
    notes = [f"- {_val(c.get('comment') or '', 600)}" for c in comments if c.get("type") == "user"][:10]
    if notes:
        head += ["comments:"] + notes
    if alert_ids:
        head.append("alert ids: " + ", ".join(alert_ids[:20]) + "  (use @alert:<id> to pull one in)")
    body = "\n".join(head)
    return f'<elastic_case id="{case.get("id")}">\n{neutralize(body[:MAX_BLOCK])}\n</elastic_case>'


def format_index(pattern: str, res: dict) -> str:
    cols = res.get("columns", [])
    rows = res.get("values", [])
    schema = ", ".join(f"{c['name']}:{c['type']}" for c in cols)
    samples = []
    for r in rows[:3]:
        samples.append(json.dumps({c["name"]: v for c, v in zip(cols, r) if v not in (None, [], "")},
                                  ensure_ascii=False, default=str)[:1500])
    body = f"fields ({len(cols)}): {schema[:2500]}\nsample documents ({len(samples)}):\n" + "\n".join(samples)
    return f'<elastic_index pattern="{pattern}">\n{neutralize(body[:MAX_BLOCK])}\n</elastic_index>'


def expand_elastic_mentions(text: str, client: Elastic) -> tuple[str, list[dict]]:
    """Replace @alert:/@case:/@index: tokens with plain words and append the fetched objects as XML-ish blocks."""
    clean, blocks, notes = gather_elastic(text, client)
    return attach_blocks(clean, blocks), notes


def expand_all(text: str, client: Elastic, cwd: Path | None = None) -> tuple[str, list[dict], list[dict]]:
    """Every @mention in what the user typed: (message to send, Elastic notes, file notes).

    ``@path`` mentions are looked for in the user's own words only. Alert and case text is attacker-influenced, and
    scanning it too would let a field like ``@~/notes.md`` pull a local file into the prompt.
    """
    clean, eblocks, enotes = gather_elastic(text, client)
    fblocks, notes = gather_files(clean, cwd, len(clean) + sum(len(b) for b in eblocks))
    return attach_blocks(clean, eblocks + fblocks), enotes, notes


def gather_elastic(text: str, client: Elastic) -> tuple[str, list[str], list[dict]]:
    """(the user's text with the @alert:/@case:/@index: markers made plain, the fetched blocks, a note for each)."""
    notes: list[dict] = []
    blocks: list[str] = []
    seen: set[tuple[str, str]] = set()

    def fetch(kind: str, ref: str) -> None:
        if (kind, ref) in seen:
            return
        seen.add((kind, ref))
        try:
            if kind == "alert":
                hit = client.alert(ref)
                if not hit:
                    notes.append({"ref": f"alert:{ref}", "status": f"not found in space {client.space}"})
                    return
                blocks.append(format_alert(ref, hit))
            elif kind == "case":
                case = client.case(ref)
                blocks.append(format_case(case, client.case_comments(ref), [a.get("id", "") for a in client.case_alerts(ref)]))
            else:
                blocks.append(format_index(ref, client.esql(f"FROM {ref} | LIMIT 3")))
            notes.append({"ref": f"{kind}:{ref}", "status": "attached"})
        except ApiError as exc:
            notes.append({"ref": f"{kind}:{ref}", "status": f"failed: {exc.message[:90]}"})

    for m in _ELASTIC.finditer(text):
        fetch(m.group(1), m.group(2))
    text = _ELASTIC.sub(lambda m: f"{m.group(1)} {m.group(2)}", text)  # keep the sentence readable, drop the '@'
    return text, blocks, notes
