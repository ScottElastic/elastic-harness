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

import dataclasses
import os
import subprocess

from elastic_harness import agentcode, transcript
from elastic_harness.agent_chat import history_lines, run_turn
from elastic_harness.client import ApiError, Elastic
from elastic_harness.compare import run_compare
from elastic_harness.config import STATE_DIR, Config
from elastic_harness.ingest import ingest
from elastic_harness.local_agent import LocalSession
from elastic_harness.mentions import expand_elastic_mentions
from elastic_harness.render import Ui
from elastic_harness.repl import Repl, match_models
from elastic_harness.tools import Ctx

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "kestrel"
AGENT, TOOL, INDEX = "eh-demo", "eh-search-kestrel", "eh-local-kestrel"
CODE_AGENT = "eh-code-demo"
STATE = STATE_DIR / "rehearse-state.json"


class Rec(Ui):
    """A Ui that records instead of drawing."""

    def __init__(self):
        super().__init__(Console(file=io.StringIO(), highlight=False), auto_yes=True)
        self.out: list[str] = []
        self.tools: list[str] = []
        self.conv = None

    def text(self, chunk):
        self.streamed = True
        self._run += chunk
        self.out.append(chunk)

    def flush(self):
        if self._run.strip():
            self.last_answer = self._run
        self._run = ""

    def raw(self, event, data):
        if event == "conversation_id_set":
            self.conv = data.get("conversation_id")

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
                        ("code agent", lambda: client.kb("DELETE", f"/api/agent_builder/agents/{CODE_AGENT}")),
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

    new_features(client, s, models, haiku, sonnet, a, count)

    ok = sum(1 for _, p, _ in results if p)
    print(f"\n{ok}/{len(results)} checks passed")
    return 0 if ok == len(results) else 1


class Bomb(Rec):
    """Raises KeyboardInterrupt after n streamed chunks: the same path as the user pressing Ctrl+C."""

    def __init__(self, n):
        super().__init__()
        self.n, self.chunks = n, 0

    def text(self, chunk):
        super().text(chunk)
        self.chunks += 1
        if self.chunks >= self.n:
            raise KeyboardInterrupt


def isolated_config(profile_name: str | None):
    """A temp config holding a copy of the active profile, so the rehearsal never edits the real one."""
    real = Config()
    prof = real.get(profile_name)
    tmp = Path(tempfile.mkdtemp(prefix="eh-rehearse-cfg-"))
    cfg = Config(tmp / "config.json")
    cfg.add(dataclasses.replace(prof))
    return cfg, prof


def new_features(client: Elastic, s: dict, models: list[str], haiku: str, sonnet: str, a, count: int) -> None:
    cfg, prof = isolated_config(a.profile)
    here = Path.cwd()

    print("\n8. spaces: list, switch, agents are per space")
    ui = Rec()
    r = Repl(cfg, ui, profile=prof.name, agent=AGENT, model="haiku 4.5")
    spaces = [x["id"] for x in client.spaces()]
    check("spaces listed", "default" in spaces, str(spaces))
    other = next((x for x in spaces if x != r.client.space), None)
    if other:
        r.handle(f"/space {other}")
        check("switched space and re-listed agents", r.client.space == other and r.agents_cache is not None, ui.console.file.getvalue()[-200:])
        check("fell back when the agent is not in that space", r.agent_id in [x["id"] for x in r.agents_cache], r.agent_id)
        r.handle(f"/space {prof.space}")
        check("switched back", r.client.space == prof.space and r.agent_id)
        r.agent_id = AGENT
    r.handle("/space definitely-not-a-space")
    check("unknown space rejected, space unchanged", r.client.space == prof.space and "no space" in ui.console.file.getvalue())

    print("\n9. clusters: switch, add (creds read in place), remove; Keychain round trip")
    if prof.creds_file:
        r.handle(f"/cluster add eh-rehearse-copy --creds {prof.creds_file}")
        r.handle("/cluster eh-rehearse-copy")
        check("added and switched cluster", r.raw.name == "eh-rehearse-copy" and r.client.version(), r.raw.name)
        check("key not copied into the config file", "API_KEY" not in (cfg.path.read_text()) and prof.creds_file in cfg.path.read_text())
        r.handle(f"/cluster {prof.name}")
        r.handle("/cluster rm eh-rehearse-copy")
        check("switched back and removed the copy", r.raw.name == prof.name and "eh-rehearse-copy" not in cfg.profiles)
        r.agent_id = AGENT
        r.model = haiku
    probe = ("import sys; from elastic_harness import credstore as c; "
             "assert c.available(), 'no keychain'; "
             "assert c.set('eh-rehearse-dummy','not-a-real-key'); "
             "ok = c.get('eh-rehearse-dummy') == 'not-a-real-key'; c.delete('eh-rehearse-dummy'); "
             "ok = ok and c.get('eh-rehearse-dummy') is None; sys.exit(0 if ok else 1)")
    try:
        kc = subprocess.run([sys.executable, "-c", probe], capture_output=True, timeout=25)
        check("macOS Keychain store/read/delete round trip", kc.returncode == 0, kc.stderr.decode()[-200:])
    except subprocess.TimeoutExpired:
        check("macOS Keychain store/read/delete round trip", False, "timed out (an access prompt may be waiting on screen)")

    print("\n10. compare the same prompt on two models")
    before = len(client.conversations())
    res = run_compare(client, [haiku, sonnet], "What is the go-live date of Project Kestrel? Answer in one line.", mode="agent", agent_id=AGENT)
    check("both models answered correctly", all(has(x.answer, "2026-12-03") for x in res), str([(x.model[-30:], x.error or x.answer[:60]) for x in res]))
    check("two different models reported tokens", all(x.tokens_in > 0 for x in res))
    check("comparison chats cleaned up", len(client.conversations()) == before, f"{before} -> {len(client.conversations())}")
    plain = run_compare(client, [haiku, sonnet], "Reply with the single word: ok", mode="local", agent_id=AGENT)
    check("local-mode compare runs plain model answers", all(x.answer.strip() and not x.error for x in plain))

    print("\n11. retry, copy, export, open")
    ui = Rec()
    r = Repl(cfg, ui, profile=prof.name, agent=AGENT, model="haiku 4.5")
    r.handle("Who owns Project Kestrel? One line.")
    s["conversations"].append(r.conversation_id)
    save_state(s)
    check("answer captured for /copy", has(ui.last_answer, "Priya"), ui.last_answer[:100])
    copied = []
    orig = transcript.copy_to_clipboard
    transcript.copy_to_clipboard = lambda t: copied.append(t) or "test"  # never touch your real clipboard
    r.handle("/copy")
    transcript.copy_to_clipboard = orig
    check("/copy hands the last answer to the clipboard", copied and has(copied[0], "Priya"))
    out = Path(tempfile.mkdtemp()) / "t.md"
    r.handle(f"/export {out}")
    check("/export wrote the conversation", out.exists() and has(out.read_text(), "Priya", "Who owns"), "")
    urls = []
    orig_open = transcript.open_url
    transcript.open_url = lambda u: urls.append(u) or True
    r.handle("/open")
    transcript.open_url = orig_open
    check("/open builds the Kibana conversation link", urls and urls[0].endswith(f"/app/agent_builder/agents/{AGENT}/conversations/{r.conversation_id}"), str(urls))
    print(f"       link to try in a browser: {urls[0] if urls else '-'}")
    first = r.conversation_id
    r.handle("/retry")
    s["conversations"].append(r.conversation_id)
    save_state(s)
    check("/retry runs the last prompt on a fresh conversation", r.conversation_id and r.conversation_id != first and has(ui.last_answer, "Priya"))

    print("\n12. real Ctrl+C: stops the run on the server (dropping the connection does not)")
    essay = "Write a 700 word essay about the history of the lighthouse. No tools."
    over = {"enable_elastic_capabilities": False, "tools": [], "skill_ids": []}
    bomb = Bomb(6)
    interrupted = False
    try:
        run_turn(client, bomb, agent_id="elastic-ai-agent", text=essay, conversation_id=None, model=haiku, overrides=over)
    except KeyboardInterrupt:
        interrupted = True
    check("interrupt propagated after aborting", interrupted)
    time.sleep(6)
    if bomb.conv:
        conv = client.conversation(bomb.conv)
        out_tokens = (conv["rounds"][0].get("model_usage") or {}).get("output_tokens", 0)
        check("server stopped generating (full essay is ~900 tokens)", out_tokens < 400, f"output_tokens={out_tokens}")
        client.delete_conversation(bomb.conv)
    else:
        check("server stopped generating", False, "no conversation id captured")

    print("\n13. @index / @alert / @case mentions")
    ui = Rec()
    r = Repl(cfg, ui, profile=prof.name, agent=AGENT, model="haiku 4.5")
    text, notes = expand_elastic_mentions(f"describe @index:{INDEX}", client)
    check("@index attaches fields and sample docs", "<elastic_index" in text and "content:text" in text and notes[0]["status"] == "attached", str(notes))
    r.handle(f"@index:{INDEX} Name two fields this index has. One line.")
    s["conversations"].append(r.conversation_id)
    save_state(s)
    check("agent answered from the attached index description", has(ui.last_answer, "content") or has(ui.last_answer, "path"), ui.last_answer[:120])
    found = None
    probe_client = Elastic(prof)
    for sp in spaces:
        probe_client.space = sp
        try:
            hit = probe_client.kb("POST", "/api/detection_engine/signals/search", {"query": {"match_all": {}}, "size": 1})["hits"]["hits"]
            cases = probe_client.kb("GET", "/api/cases/_find?perPage=1").get("cases", [])
        except ApiError:
            continue
        if hit:
            found = (sp, hit[0]["_id"], cases[0]["id"] if cases else None)
            break
    if found:
        probe_client.space = found[0]
        text, notes = expand_elastic_mentions(f"look at @alert:{found[1]}" + (f" and @case:{found[2]}" if found[2] else ""), probe_client)
        check(f"@alert pulled a real alert (space {found[0]})", "<elastic_alert" in text and "kibana.alert.rule.name" in text, str(notes))
        if found[2]:
            check("@case pulled a real case", "<elastic_case" in text, str(notes))
        _, missing = expand_elastic_mentions("@alert:does-not-exist", probe_client)
        check("a missing alert is reported, not fatal", "not found" in missing[0]["status"])
    else:
        print("       (no space with alerts on this cluster: @alert/@case live checks skipped)")

    print("\n14. agents as code: pull, diff, push, guards")
    work = Path(tempfile.mkdtemp(prefix="eh-agents-"))
    try:  # our own object from an earlier run: start clean so "create" really creates
        client.kb("DELETE", f"/api/agent_builder/agents/{CODE_AGENT}")
    except ApiError:
        pass
    pulled = agentcode.pull(client, AGENT, str(work))
    check("pull wrote a Markdown file with the tool and instructions", has(pulled.read_text(), TOOL, "Kestrel"), "")
    res = agentcode.push(client, str(pulled), confirm=lambda t, d: True, say=lambda m: None)
    check("pushing an unchanged pull is a no-op", res["action"] == "unchanged", str(res))
    new = work / "new.md"
    new.write_text(pulled.read_text().replace(f"id: {AGENT}", f"id: {CODE_AGENT}").replace("EH Demo (Kestrel notes)", "EH Code Demo"))
    res = agentcode.push(client, str(new), confirm=lambda t, d: True, say=lambda m: None)
    check("push created a new agent from the file", res["action"] == "created" and client.agent(CODE_AGENT)["name"] == "EH Code Demo", str(res))
    new.write_text(new.read_text().replace("Quote dates and names exactly", "Quote dates and names exactly, and keep answers to one line"))
    said = []
    res = agentcode.push(client, str(new), confirm=lambda t, d: True, say=said.append)
    check("editing the file shows a diff and updates the agent", res["action"] == "updated" and any("one line" in m for m in said), str(res))
    check("repeat push is unchanged", agentcode.push(client, str(new), confirm=lambda t, d: True, say=lambda m: None)["action"] == "unchanged")
    new.write_text(new.read_text().replace("one line", "two lines"))
    declined = agentcode.push(client, str(new), confirm=lambda t, d: False, say=lambda m: None)
    check("declined confirmation changes nothing", declined["action"] == "cancelled" and "one line" in client.agent(CODE_AGENT)["configuration"]["instructions"])
    ui = Rec()
    run_turn(client, ui, agent_id=CODE_AGENT, text="Who owns Project Kestrel? One line.", conversation_id=None, model=haiku)
    check("the pushed agent answers from the ingested data", has(ui.answer, "Priya"), ui.answer[:100])
    risky = next((t["id"] for t in client.tools() if agentcode.RISKY.search(t["id"])), None)
    if risky:
        bad = work / "bad.md"
        bad.write_text(pulled.read_text().replace(f"id: {AGENT}", "id: eh-risky-never").replace(f"  - {TOOL}", f"  - {TOOL}\n  - {risky}"))
        try:
            agentcode.push(client, str(bad), confirm=lambda t, d: True, say=lambda m: None)
            check("a workflow-executing built-in is refused", False, "push succeeded")
        except PermissionError:
            check("a workflow-executing built-in is refused", True)

    print("\n15. EH.md project instructions and custom commands")
    proj = Path(tempfile.mkdtemp(prefix="eh-proj-"))
    (proj / ".eh" / "commands").mkdir(parents=True)
    (proj / "EH.md").write_text("House rule: end every answer with the exact token DONE-EH.")
    (proj / ".eh" / "commands" / "owner.md").write_text(f"---\ndescription: who owns a project\nagent: {AGENT}\nmodel: haiku 4.5\n---\nWho owns Project $1 and what is its go-live date? One line.")
    os.chdir(proj)
    try:
        ui = Rec()
        r = Repl(cfg, ui, profile=prof.name)
        check("project command discovered", "owner" in r.cmds and r.context_files, str(list(r.cmds)))
        r.handle("/owner Kestrel")
        s["conversations"].append(r.conversation_id)
        save_state(s)
        check("custom command ran with its own agent and model", has(ui.last_answer, "Priya") and has(ui.last_answer, "2026-12-03"), ui.last_answer[:120])
        check("EH.md instructions were followed", "DONE-EH" in ui.last_answer.upper(), ui.last_answer[-80:])
        check("command overrides did not stick", r.agent_id != AGENT or prof.default_agent == AGENT)
    finally:
        os.chdir(here)

    print("\n16. local-mode context compaction")
    import elastic_harness.local_agent as la
    ui = Rec()
    old = la.CONTEXT_TOKENS
    la.CONTEXT_TOKENS = 350  # tiny, so three short files are enough to trigger it
    try:
        ls = LocalSession(Ctx(client, ui, SAMPLE, sonnet), [])
        ls.turn("Read overview.md and tell me the budget. One line.")
        ls.turn("Now read meeting-2026-11-18.md and tell me who has the cracked reader on order. One line.")
        ls.turn("Now read risks.csv and tell me the owner of risk R3. One line.")
        info = ui.console.file.getvalue()
        check("history was compacted automatically", "context compacted" in info, info[-200:])
        check("the summary replaced the older turns", ls.messages[0]["content"].startswith(la.SUMMARY_TAG), str(ls.messages[0])[:120])
        ui.reset()
        ls.turn("Without reading any file again, answer as three short numbered lines: 1) the project budget, "
                "2) who has the cracked reader on order, 3) who owns risk R3.")
        check("facts from every earlier turn survived compaction",
              has(ui.last_answer, "184") and has(ui.last_answer, "Dev Patel") and has(ui.last_answer, "Priya"), ui.last_answer[:200])
        check("answered from the summary without re-reading files", not any(t in ("read_file", "grep") for t in ui.tools[-1:]), str(ui.tools[-3:]))
    finally:
        la.CONTEXT_TOKENS = old


if __name__ == "__main__":
    sys.exit(main())
