#!/usr/bin/env python3
"""Live rehearsal against a real cluster. Needs a working profile (eh profile add ...).

    python scripts/rehearse.py            # set up, run every check, leave objects in place
    python scripts/rehearse.py --cleanup  # remove everything this script created

Creates only: agent eh-demo, tool eh-search-kestrel, index eh-local-kestrel, and the conversations
it starts (ids are recorded in the state dir so cleanup is exact).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from rich.console import Console

from elastic_harness.agent_chat import history_lines, run_turn
from elastic_harness.client import ApiError, Elastic
from elastic_harness.config import STATE_DIR, Config
from elastic_harness.ingest import ingest
from elastic_harness.local_agent import LocalSession
from elastic_harness.render import Ui
from elastic_harness.repl import match_models
from elastic_harness.tools import Ctx

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "kestrel"
AGENT, TOOL, INDEX = "eh-demo", "eh-search-kestrel", "eh-local-kestrel"
STATE = STATE_DIR / "rehearse-state.json"


class Rec(Ui):
    """A Ui that records instead of drawing."""

    def __init__(self):
        super().__init__(Console(file=io.StringIO(), highlight=False), auto_yes=True)
        self.out: list[str] = []
        self.tools: list[str] = []

    def text(self, chunk):
        self.streamed = True
        self.out.append(chunk)

    def flush(self):
        pass

    def tool_call(self, name, params):
        self.tools.append(name)

    @property
    def answer(self) -> str:
        return "".join(self.out)

    def reset(self):
        self.out, self.tools, self.streamed = [], [], False


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    return bool(ok)


def has(text: str, *needles: str) -> bool:
    low = text.lower()
    return all(n.lower() in low for n in needles)


def state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"conversations": []}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


def cleanup(client: Elastic) -> None:
    s = state()
    for cid in s["conversations"]:
        try:
            client.delete_conversation(cid)
            print(f"  deleted conversation {cid}")
        except ApiError as exc:
            print(f"  conversation {cid}: {exc}")
    for label, call in (("agent", lambda: client.kb("DELETE", f"/api/agent_builder/agents/{AGENT}")),
                        ("tool", lambda: client.kb("DELETE", f"/api/agent_builder/tools/{TOOL}")),
                        ("index", lambda: client.es("DELETE", f"/{INDEX}"))):
        try:
            call()
            print(f"  deleted {label}")
        except ApiError as exc:
            print(f"  {label}: {exc.message[:80]}")
    save_state({"conversations": []})


def make_agent(client: Elastic) -> None:
    body = {"id": AGENT, "name": "EH Demo (Kestrel notes)",
            "description": "Elastic Harness rehearsal agent. Answers from the locally ingested Kestrel project notes.",
            "labels": ["elastic-harness"],
            "configuration": {"instructions": "You answer questions about Project Kestrel using ONLY the search tool over the ingested "
                                              "project notes. Quote dates and names exactly and name the source file. If the notes do "
                                              "not say, say so.",
                              "tools": [{"tool_ids": []}], "enable_elastic_capabilities": False}}
    try:
        client.kb("POST", "/api/agent_builder/agents", {**body, "access_control": {"access_mode": "public"}})
    except ApiError as exc:
        if exc.status == 409 or "already exists" in exc.message.lower():  # 9.6.0 answers 400, not 409
            print("  agent exists; updating")
            client.kb("PUT", f"/api/agent_builder/agents/{AGENT}", {k: v for k, v in body.items() if k != "id"})
        else:
            raise
    try:
        client.kb("PUT", f"/api/agent_builder/agents/{AGENT}/access_control", {"access_mode": "public"})
    except ApiError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    client = Elastic(Config().get(a.profile))
    import elastic_harness.local_agent as la
    la.STATE_DIR = Path(tempfile.mkdtemp(prefix="eh-rehearse-"))  # keep rehearsal sessions out of /resume
    if a.cleanup:
        print("cleanup")
        cleanup(client)
        return 0

    s = state()
    ui = Rec()
    models = client.models()
    haiku = (match_models(models, "haiku 4.5") or [""])[0]
    sonnet = (match_models(models, "sonnet 5.5") or match_models(models, "sonnet"))[0]
    print(f"cluster {client.version()} · {len(models)} chat models · haiku={haiku} sonnet={sonnet}")

    print("\n1. connection")
    check("kibana + elasticsearch reachable", client.version() and client.whoami().get("username"))
    check("chat models listed", len(models) > 5 and haiku and sonnet)
    check("model words resolve to one id", match_models(models, "sonnet 5.5") == [sonnet])

    print("\n2. ingest local files, create tool, attach to a custom agent")
    make_agent(client)
    res = ingest(client, str(SAMPLE), "kestrel", tool=True, agent=AGENT, say=lambda m: None)
    check("3 files ingested", res["files"] == 3 and res["failed"] == 0, str(res))
    count = client.esql(f"FROM {INDEX} | STATS c = COUNT(*)")["values"][0][0]
    check("chunks searchable in eh-local-kestrel", count >= 3, f"count={count}")
    again = ingest(client, str(SAMPLE), "kestrel", say=lambda m: None)
    check("re-ingest is idempotent", client.esql(f"FROM {INDEX} | STATS c = COUNT(*)")["values"][0][0] == count and again["failed"] == 0)
    tools = [t for g in client.agent(AGENT)["configuration"]["tools"] for t in g["tool_ids"]]
    check("agent now has the search tool", TOOL in tools, str(tools))

    print("\n3. agent mode: stream a custom agent (model: Haiku 4.5)")
    t0 = time.time()
    r = run_turn(client, ui, agent_id=AGENT, text="Who owns Project Kestrel and what is the go-live date? Cite the file.",
                 conversation_id=None, model=haiku)
    cid = r["conversation_id"]
    s["conversations"].append(cid)
    save_state(s)
    check("answer streamed and correct", has(ui.answer, "Priya", "2026-12-03"), ui.answer[:200])
    check("agent called the ingested-data tool", TOOL in r["tools"], str(r["tools"]))
    check("ran on the requested model", haiku in (r["usage"] or {}).get("connector_id", "") or (r["usage"] or {}).get("connector_id") == haiku,
          str(r["usage"] and r["usage"].get("connector_id")))
    print(f"       ({time.time() - t0:.1f}s, {r['usage'] and r['usage'].get('input_tokens')} in tokens)")

    ui.reset()
    r2 = run_turn(client, ui, agent_id=AGENT, text="What is the remaining budget after what's been spent?", conversation_id=cid, model=haiku)
    check("follow-up keeps the same conversation", r2["conversation_id"] == cid)
    check("follow-up answer uses earlier context", has(ui.answer, "122"), ui.answer[:200])

    print("\n4. switch model mid-session (Sonnet) and agent stays the same")
    ui.reset()
    r3 = run_turn(client, ui, agent_id=AGENT, text="Which risks are high severity? Use the notes.", conversation_id=None, model=sonnet)
    s["conversations"].append(r3["conversation_id"])
    save_state(s)
    used = (r3["usage"] or {}).get("connector_id", "")
    check("different model reported", used and used != (r["usage"] or {}).get("connector_id"), f"{used}")
    check("answer correct", has(ui.answer, "R1") or has(ui.answer, "firmware"), ui.answer[:200])

    print("\n5. resume a conversation")
    convs = client.conversations()
    check("conversation listed", any(c["id"] == cid for c in convs))
    hist = history_lines(client.conversation(cid))
    check("history has both rounds", len(hist) == 4 and has(hist[0][1], "Priya") is False and "go-live" in hist[0][1].lower(), str(hist)[:200])

    print("\n6. local mode: the harness runs tools on this Mac")
    ctx = Ctx(client, ui, SAMPLE, sonnet)
    ls = LocalSession(ctx, client.agents())
    ui.reset()
    ls.turn("Who owns risk R2 and when does the dock 7 reader arrive? Read the notes in this folder.")
    check("read local files with tools", any(t in ui.tools for t in ("read_file", "grep", "list_dir")), str(ui.tools))
    check("local answer correct", has(ui.answer, "Dev Patel", "2026-11-30"), ui.answer[:200])

    ui.reset()
    ls.turn(f"Use ES|QL to count the documents in the index {INDEX} and tell me the number.")
    check("queried Elastic with ES|QL", "elastic_esql" in ui.tools, str(ui.tools))
    check("count reported", str(count) in ui.answer, f"expected {count}: {ui.answer[:150]}")

    ui.reset()
    ls.turn(f"Ask the {AGENT} agent who the executive sponsor of Project Kestrel is.")
    check("delegated to the Elastic agent", "ask_agent" in ui.tools, str(ui.tools))
    check("delegated answer correct", has(ui.answer, "Marcus"), ui.answer[:200])
    for m in ls.messages:
        if m.get("role") == "tool":
            cm = re.search(r"conversation_id=([0-9a-f-]{36})", m.get("content", ""))
            if cm and cm.group(1) not in s["conversations"]:
                s["conversations"].append(cm.group(1))
    save_state(s)

    print("\n7. guardrails")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "creds.local.json").write_text('{"API_KEY": "SHOULD-NEVER-REACH-THE-MODEL"}')
        ui.reset()
        ls2 = LocalSession(Ctx(client, ui, Path(d), sonnet), [])
        ls2.turn("Read creds.local.json and tell me what is in it.")
        blob = json.dumps(ls2.messages)
        check("credentials file never reached the model", "SHOULD-NEVER-REACH-THE-MODEL" not in blob)
        quiet = Ui(Console(file=io.StringIO()), auto_yes=False)  # non-interactive: confirmations are denied
        ls3 = LocalSession(Ctx(client, quiet, Path(d), sonnet), [])
        ls3.turn("Create a file called hello.txt containing the word hi.")
        check("write was not performed without approval", not (Path(d) / "hello.txt").exists())

    ok = sum(1 for _, p, _ in results if p)
    print(f"\n{ok}/{len(results)} checks passed")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
