"""A small stdlib HTTP client for Kibana (Agent Builder) and Elasticsearch.

No third-party HTTP dependency: urllib plus a Server-Sent Events parser.
"""

from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request
import uuid
from typing import Iterable, Iterator

from .config import Profile

API_VERSION = "2023-10-31"


class ApiError(Exception):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.url = url


def parse_sse(lines: Iterable[bytes | str]) -> Iterator[tuple[str, str]]:
    """Yield (event, data) from an SSE byte/str line stream.

    Comment lines (starting with ':') are ignored: Kibana pads its stream with
    ``: 000…`` keep-alive comments. A leading BOM (Elasticsearch adds one) is stripped.
    """
    event, data = "message", []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.lstrip("﻿").rstrip("\r\n")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield event, "\n".join(data)


def _error_message(body: str) -> str:
    try:
        d = json.loads(body, strict=False)
    except ValueError:
        return body[:400]
    if isinstance(d, dict):
        err = d.get("error")
        if isinstance(err, dict):
            return str(err.get("reason") or err.get("message") or err)[:400]
        return str(d.get("message") or err or d)[:400]
    return str(d)[:400]


def classify_abort_probe(status: int, body: str) -> str:
    """Read the answer to a probe of the abort route (see Elastic.probe_abort_route)."""
    try:
        data = json.loads(body, strict=False)
    except ValueError:
        data = None
    if status == 200 and isinstance(data, dict) and "acknowledged" in data:
        return "present"
    if status == 404 and isinstance(data, dict) and data.get("message") == "Not Found":
        return "missing"  # Kibana's generic route-not-found; a handler's own 404 would say something else
    return "unknown"


class Elastic:
    def __init__(self, profile: Profile, timeout: int = 60):
        p = profile.resolved()
        if not p.kibana_url or not p.api_key:
            raise ValueError("profile needs kibana_url and an API key (creds_file, env or profile)")
        self.profile = p
        self.timeout = timeout
        self.kibana_url = p.kibana_url
        self.es_url = p.es_url
        self.space = p.space or "default"

    # -- plumbing ---------------------------------------------------------------------------
    def _headers(self, kibana: bool, extra: dict | None = None) -> dict:
        h = {"Authorization": "ApiKey " + self.profile.api_key, "Content-Type": "application/json"}
        if kibana:
            h["kbn-xsrf"] = "eh"
            h["Elastic-Api-Version"] = API_VERSION
        h.update(extra or {})
        return h

    def kb_url(self, path: str) -> str:
        prefix = "" if self.space in ("", "default") else f"/s/{self.space}"
        return f"{self.kibana_url}{prefix}{path}"

    def _open(self, method: str, url: str, body, kibana: bool, timeout: int | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers(kibana), method=method)
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, _error_message(exc.read().decode("utf-8", "replace")), url) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"cannot reach {url.split('/api')[0]}: {exc.reason}", url) from None

    def _json(self, method: str, url: str, body=None, kibana: bool = True, timeout: int | None = None):
        with self._open(method, url, body, kibana, timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw, strict=False) if raw.strip() else {}

    def kb(self, method: str, path: str, body=None, timeout: int | None = None):
        return self._json(method, self.kb_url(path), body, True, timeout)

    def es(self, method: str, path: str, body=None, timeout: int | None = None):
        if not self.es_url:
            raise ApiError(0, "this profile has no es_url (needed for models, ES|QL and ingest)")
        return self._json(method, self.es_url + path, body, False, timeout)

    # -- cluster ----------------------------------------------------------------------------
    def version(self) -> str:
        return self.kb("GET", "/api/status").get("version", {}).get("number", "?")

    def whoami(self) -> dict:
        return self.es("GET", "/_security/_authenticate")

    # -- spaces, alerts, cases ---------------------------------------------------------------
    def spaces(self) -> list[dict]:
        """Spaces this key can see. Not space-scoped, so no /s/ prefix."""
        return self._json("GET", f"{self.kibana_url}/api/spaces/space")

    def alert(self, alert_id: str) -> dict | None:
        res = self.kb("POST", "/api/detection_engine/signals/search",
                      {"query": {"ids": {"values": [alert_id]}}, "size": 1})
        hits = (res.get("hits") or {}).get("hits") or []
        return hits[0] if hits else None

    def in_space(self, space: str) -> "Elastic":
        """A copy of this client pointed at another space. A background thread uses one, so it can never change the
        space the live session is working in."""
        other = copy.copy(self)
        other.space = space
        return other

    # What a picker lists. Both verified on 9.6.0 (read-only): about 0.4 s each.
    _OPEN_ALERTS = {"term": {"kibana.alert.workflow_status": "open"}}

    def open_alerts(self, limit: int = 20) -> list[dict]:
        """The newest open detection alerts in this space: hits carrying only the fields a picker row shows."""
        res = self.kb("POST", "/api/detection_engine/signals/search", {
            "query": self._OPEN_ALERTS, "sort": [{"@timestamp": "desc"}], "size": limit,
            "_source": ["kibana.alert.rule.name", "kibana.alert.severity", "host.name", "user.name", "@timestamp"]})
        return (res.get("hits") or {}).get("hits") or []

    def open_cases(self, limit: int = 20) -> list[dict]:
        """The most recently created cases in this space that are not closed (open or in progress), newest first."""
        res = self.kb("GET", f"/api/cases/_find?status=open&status=in-progress&perPage={int(limit)}"
                             f"&sortField=createdAt&sortOrder=desc")
        return [c for c in res.get("cases", []) if c.get("status") != "closed"]

    def count_open(self, kind: str) -> int:
        """How many open alerts, or open and in-progress cases, this space has."""
        if kind == "alert":
            res = self.kb("POST", "/api/detection_engine/signals/search",
                          {"query": self._OPEN_ALERTS, "size": 0, "track_total_hits": True})
            total = (res.get("hits") or {}).get("total") or 0
            return int(total.get("value", 0) if isinstance(total, dict) else total)
        return int(self.kb("GET", "/api/cases/_find?status=open&status=in-progress&perPage=1").get("total") or 0)

    def case(self, case_id: str) -> dict:
        return self.kb("GET", f"/api/cases/{case_id}")

    def case_comments(self, case_id: str) -> list[dict]:
        return self.kb("GET", f"/api/cases/{case_id}/comments/_find?perPage=20").get("comments", [])

    def case_alerts(self, case_id: str) -> list[dict]:
        res = self.kb("GET", f"/api/cases/{case_id}/alerts")
        return res if isinstance(res, list) else []

    def abort_execution(self, execution_id: str, wait: bool = True, timeout: int = 15) -> bool:
        """Stop a running agent execution. Dropping the SSE connection does NOT stop it (verified 9.6.0).

        Internal Kibana API (no stability promise): returns False instead of raising if it is unavailable.
        """
        url = self.kb_url(f"/internal/agent_builder/executions/{execution_id}/abort?wait_for_terminal={str(wait).lower()}")
        h = self._headers(True, {"Elastic-Api-Version": "1", "x-elastic-internal-origin": "kibana"})
        try:
            req = urllib.request.Request(url, data=b"", headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return bool(json.loads(r.read().decode() or "{}").get("acknowledged"))
        except (urllib.error.URLError, ValueError, OSError):
            return False

    def probe_abort_route(self, timeout: int = 15) -> str:
        """Is Kibana's internal abort route there? 'present', 'missing' or 'unknown'.

        Posts to it with a random execution id. Nothing is running under that id, so nothing is stopped: a Kibana that
        has the route answers 200 {"acknowledged": false}, one that has not answers its generic 404 (verified 9.6.0).
        """
        url = self.kb_url(f"/internal/agent_builder/executions/{uuid.uuid4()}/abort?wait_for_terminal=false")
        h = self._headers(True, {"Elastic-Api-Version": "1", "x-elastic-internal-origin": "kibana"})
        try:
            req = urllib.request.Request(url, data=b"", headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, body = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError):
            return "unknown"
        return classify_abort_probe(status, body)

    # -- Agent Builder ----------------------------------------------------------------------
    def agents(self) -> list[dict]:
        return self.kb("GET", "/api/agent_builder/agents").get("results", [])

    def agent(self, agent_id: str) -> dict:
        return self.kb("GET", f"/api/agent_builder/agents/{agent_id}")

    def tools(self) -> list[dict]:
        return self.kb("GET", "/api/agent_builder/tools").get("results", [])

    def tool(self, tool_id: str) -> dict:
        return self.kb("GET", f"/api/agent_builder/tools/{tool_id}")

    def execute_tool(self, tool_id: str, params: dict) -> dict:
        return self.kb("POST", "/api/agent_builder/tools/_execute", {"tool_id": tool_id, "tool_params": params}, timeout=120)

    def conversations(self) -> list[dict]:
        return self.kb("GET", "/api/agent_builder/conversations").get("results", [])

    def conversation(self, conversation_id: str) -> dict:
        return self.kb("GET", f"/api/agent_builder/conversations/{conversation_id}")

    def delete_conversation(self, conversation_id: str) -> None:
        self.kb("DELETE", f"/api/agent_builder/conversations/{conversation_id}")

    def converse_body(self, agent_id: str, text: str, conversation_id: str | None = None,
                      model: str | None = None, overrides: dict | None = None,
                      prompts: dict | None = None, execution_id: str | None = None) -> dict:
        body: dict = {"agent_id": agent_id}
        if text:
            body["input"] = text
        if conversation_id:
            body["conversation_id"] = conversation_id
        if model:
            body["inference_id"] = model  # endpoint id; same list the local loop uses
        if overrides:
            body["configuration_overrides"] = overrides
        if prompts:
            body["prompts"] = prompts
        if execution_id:
            body["execution_id"] = execution_id  # lets us abort this run later
        return body

    def converse(self, **kw) -> dict:
        eid = kw.pop("execution_id", None) or str(uuid.uuid4())
        try:
            return self.kb("POST", "/api/agent_builder/converse", self.converse_body(execution_id=eid, **kw), timeout=300)
        except KeyboardInterrupt:
            self.abort_execution(eid, wait=False)
            raise

    def converse_stream(self, **kw) -> Iterator[tuple[str, dict]]:
        """Yield (event_type, data) from POST /converse/async. ``data`` is the inner payload."""
        r = self._open("POST", self.kb_url("/api/agent_builder/converse/async"),
                       self.converse_body(**kw), True, timeout=300)
        try:
            for event, raw in parse_sse(r):
                try:
                    payload = json.loads(raw, strict=False)
                except ValueError:
                    continue
                yield event, (payload.get("data", payload) if isinstance(payload, dict) else {})
        finally:
            r.close()

    def create_tool(self, body: dict) -> dict:
        try:
            return self.kb("POST", "/api/agent_builder/tools", body)
        except ApiError as exc:
            if exc.status == 409 or "already exists" in exc.message.lower():
                tid = body["id"]
                upd = {k: v for k, v in body.items() if k not in ("id", "type")}
                return self.kb("PUT", f"/api/agent_builder/tools/{tid}", upd)
            raise

    def attach_tool(self, agent_id: str, tool_id: str) -> bool:
        """Append a tool to an agent's tool list. Returns False if it was already there."""
        a = self.agent(agent_id)
        cfg = a.get("configuration", {})
        groups = cfg.get("tools") or [{"tool_ids": []}]
        if any(tool_id in g.get("tool_ids", []) for g in groups):
            return False
        groups[0].setdefault("tool_ids", []).append(tool_id)
        cfg["tools"] = groups
        self.kb("PUT", f"/api/agent_builder/agents/{agent_id}", {"configuration": cfg})
        return True

    # -- models (Elasticsearch inference API) -----------------------------------------------
    def models(self) -> list[str]:
        eps = self.es("GET", "/_inference").get("endpoints", [])
        ids = sorted(e["inference_id"] for e in eps if e.get("task_type") == "chat_completion")
        return ids

    def chat_stream(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                    max_tokens: int = 4096) -> Iterator[dict]:
        """Stream an OpenAI-style chat completion from an Elastic inference endpoint."""
        body: dict = {"messages": messages, "max_completion_tokens": max_tokens}
        if tools:
            body["tools"] = tools
        r = self._open("POST", f"{self.es_url}/_inference/chat_completion/{model}/_stream", body, False, timeout=300)
        try:
            for _event, raw in parse_sse(r):
                if raw.strip() == "[DONE]":
                    return
                try:
                    yield json.loads(raw, strict=False)
                except ValueError:
                    continue
        finally:
            r.close()

    # -- data -------------------------------------------------------------------------------
    def esql(self, query: str) -> dict:
        return self.es("POST", "/_query", {"query": query}, timeout=120)

    def ensure_index(self, index: str, mappings: dict) -> bool:
        try:
            self.es("GET", f"/{index}")
            return False
        except ApiError as exc:
            if exc.status != 404:
                raise
        self.es("PUT", f"/{index}", {"mappings": mappings})
        return True

    def bulk(self, index: str, docs: list[tuple[str, dict]]) -> int:
        """Index (id, doc) pairs. Returns the number of failed items."""
        if not docs:
            return 0
        lines = []
        for doc_id, doc in docs:
            lines.append(json.dumps({"index": {"_index": index, "_id": doc_id}}))
            lines.append(json.dumps(doc))
        req = urllib.request.Request(self.es_url + "/_bulk?refresh=wait_for", data=("\n".join(lines) + "\n").encode(),
                                     headers=self._headers(False, {"Content-Type": "application/x-ndjson"}), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.loads(r.read().decode(), strict=False)
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, _error_message(exc.read().decode("utf-8", "replace"))) from None
        return sum(1 for item in res.get("items", []) if item.get("index", {}).get("error"))
