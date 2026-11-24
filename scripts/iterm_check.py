#!/usr/bin/env python3
"""Check the iTerm2 tab status for real: run `eh` inside an iTerm session and read back what iTerm reports.

    python scripts/iterm_check.py                 # opens a throwaway iTerm window, tests, closes it
    python scripts/iterm_check.py --session ID    # reuse an existing throwaway session (it is left open)

Needs iTerm2 3.7+ with the Python API enabled and its `it2` tool (~/.config/iterm2/it2). Uses a live cluster
profile like scripts/rehearse.py: agent `eh-demo` must exist (run rehearse.py first). It never touches your
other tabs; it only reads the status and screen of the session it created. Section 3 (the @alert:/@case: pickers)
needs a space with open alerts and cases and skips itself where there is none.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IT2 = shutil.which("it2") or str(Path.home() / ".config/iterm2/it2")
results: list[tuple[str, bool, str]] = []


def it2(*args: str, timeout: int = 20) -> str:
    r = subprocess.run([IT2, *args], capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def status(sid: str) -> dict:
    try:
        return json.loads(it2("session", "get-status", "--session", sid))
    except (ValueError, subprocess.TimeoutExpired):
        return {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    return bool(ok)


def watch(sid: str, until, timeout: float = 90, label: str = "") -> list[dict]:
    """Poll the session's status, recording each distinct state, until `until(status)` is true."""
    seen, t0 = [], time.time()
    while time.time() - t0 < timeout:
        s = status(sid)
        key = (s.get("status"), s.get("dot_color"), s.get("text_color"))
        if not seen or (seen[-1].get("status"), seen[-1].get("dot_color"), seen[-1].get("text_color")) != key:
            seen.append(s)
        if until(s):
            return seen
        time.sleep(0.25)
    print(f"       (timed out waiting for {label or 'condition'}; saw {[x.get('status') for x in seen]})")
    return seen


def states(seen: list[dict]) -> list:
    return [s.get("status") for s in seen]


def screen_text(sid: str) -> str:
    return it2("session", "read", "--session", sid)


def wait_screen(sid: str, needles: list[str], timeout: float = 10) -> tuple[bool, str]:
    """Poll the visible screen until every needle is on it (the dropdown is drawn after a background fetch)."""
    t0, screen = time.time(), ""
    while time.time() - t0 < timeout:
        screen = screen_text(sid)
        if all(n in screen for n in needles):
            return True, screen
        time.sleep(0.5)
    return False, screen


def picker_data() -> dict | None:
    """Find, through the API, what the dropdowns should show: a space with open alerts and cases, and an empty one."""
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from elastic_harness import pickers
        from elastic_harness.client import ApiError, Elastic
        from elastic_harness.config import Config
        client = Elastic(Config().get(None))
        found, empty = None, None
        for sp in [x["id"] for x in client.spaces()]:
            c = client.in_space(sp)
            try:
                alerts, cases = c.open_alerts(pickers.LIMIT), c.open_cases(pickers.LIMIT)
            except ApiError:
                continue
            if alerts and cases and not found:
                found = (sp, [pickers.alert_row(a) for a in alerts], [pickers.case_row(x) for x in cases])
            elif not alerts and not empty:
                empty = sp
        return {"space": found[0], "alerts": found[1], "cases": found[2], "empty": empty} if found else None
    except Exception as exc:  # noqa: BLE001 - a diagnostic: say why and skip
        print(f"       (could not look up picker data: {exc})")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    a = ap.parse_args()
    if not Path(IT2).exists():
        print("it2 not found: needs iTerm2 3.7+ (it2 lives in ~/.config/iterm2/)")
        return 2
    sid, created = a.session, False
    if not sid:
        before = {line.split("\t")[0] for line in it2("session", "list").splitlines()}
        print(it2("window", "new"))
        time.sleep(2)
        new = [line.split("\t")[0] for line in it2("session", "list").splitlines() if line.split("\t")[0] not in before]
        if not new:
            print("could not find the new session")
            return 2
        sid, created = new[0], True
    eh = f"cd {ROOT} && EH_TAB_STATUS=1 .venv/bin/eh --agent eh-demo --model 'haiku 4.5'"
    try:
        print(f"throwaway session {sid}")
        # start from a clean slate: a stale status on a reused session would make the first checks meaningless
        it2("session", "run", "--session", sid, "printf '\\033]21337;status=;indicator=;status-color=;detail=\\007'; clear")
        watch(sid, lambda s: not s.get("status"), timeout=10, label="a clean session")
        print("\n1. one-shot (-p): working while it runs, then cleared when the process exits (done lasts milliseconds here)")
        it2("session", "run", "--session", sid, f"{eh} -p 'Who owns Project Kestrel? One line.'")
        seen = watch(sid, lambda s: s.get("status") == "working", timeout=30, label="working")
        check("went working (orange)", any(s.get("status") == "working" and s.get("dot_color") == "#ff9500" and s.get("text_color") == "#ff9500" for s in seen), str(states(seen)))
        gone = watch(sid, lambda s: not s.get("status"), timeout=60, label="clear")
        check("cleared when the process exited", not gone[-1].get("status"), str(gone[-1]))
        check("and the answer was printed", "Priya" in it2("session", "read", "--session", sid))

        print("\n2. interactive: idle at the prompt, working, waiting on a confirmation, done, cleared on /exit")
        it2("session", "run", "--session", sid, f"{eh} --mode local --model 'haiku 4.5'")
        seen = watch(sid, lambda s: s.get("status") == "idle", timeout=40, label="idle at the prompt")
        check("idle once the prompt is up", states(seen)[-1] == "idle", str(states(seen)))
        it2("session", "run", "--session", sid, "Use the bash tool to run: echo tab-status-test")
        seen = watch(sid, lambda s: s.get("status") == "waiting", timeout=60, label="waiting")
        w = seen[-1]
        check("working while the model thinks", "working" in states(seen), str(states(seen)))
        check("waiting (yellow) while it asks to run a command", w.get("status") == "waiting" and w.get("dot_color") == "#ffd60a" and "shell" in (w.get("detail") or "").lower(), str(w))
        screen = it2("session", "read", "--session", sid)
        check("the confirmation prompt is on screen", "Run shell command" in screen and "tab-status-test" in screen)
        it2("session", "run", "--session", sid, "n")
        seen = watch(sid, lambda s: s.get("status") == "done", timeout=60, label="done after declining")
        check("back to working, then done after you answer", states(seen)[-1] == "done" and "working" in states(seen), str(states(seen)))
        it2("session", "run", "--session", sid, "Say the single word: ok")
        seen = watch(sid, lambda s: s.get("status") == "done", timeout=60, label="done with detail")
        d = seen[-1]
        check("done (green) carries the answer as detail", d.get("status") == "done" and d.get("dot_color") == "#00d75f" and d.get("text_color") == "#00d75f"
              and "ok" in (d.get("detail") or "").lower(), str(d))
        it2("session", "run", "--session", sid, "/tabstatus basic")
        time.sleep(1)
        it2("session", "run", "--session", sid, "Say the single word: ok")
        seen = watch(sid, lambda s: s.get("status") == "done" and not s.get("detail"), timeout=60, label="done without detail")
        check("/tabstatus basic hides the answer text", seen[-1].get("status") == "done" and not seen[-1].get("detail"), str(seen[-1]))
        it2("session", "run", "--session", sid, "/exit")
        gone = watch(sid, lambda s: not s.get("status"), timeout=15, label="clear")
        check("cleared on /exit", not gone[-1].get("status"), str(gone[-1]))

        print("\n3. @alert: and @case: pickers: type into a real prompt and read the dropdown back off the screen")
        data = picker_data()
        if not data:
            print("       (no space with open alerts and cases: skipped)")
        else:
            sp, alerts, cases = data["space"], data["alerts"], data["cases"]
            it2("session", "run", "--session", sid, f"cd {ROOT} && EH_TAB_STATUS=1 .venv/bin/eh --space {sp}")
            watch(sid, lambda s: s.get("status") == "idle", timeout=40, label="idle at the prompt")
            first, last = alerts[0], alerts[-1]
            it2("session", "send", "--session", sid, "triage @alert:")
            ok, screen = wait_screen(sid, [first.title[:24], first.where[:10]])
            check("typing @alert: lists the open alerts (rule and host on screen)", ok, screen[-300:])
            check("rows show their severity", first.severity in screen, first.severity)
            word = next((w for w in last.title.lower().replace(":", " ").split() if len(w) >= 5 and w not in first.haystack), "")
            if word:
                it2("session", "send", "--session", sid, word)
                ok, screen = wait_screen(sid, [last.title[:20]], timeout=5)
                check(f"typing '{word}' narrows the list", ok and first.title[:24] not in screen, screen[-300:])
                it2("session", "send", "--session", sid, "\x7f" * len(word))  # backspace: back to the full list
                ok, screen = wait_screen(sid, [first.title[:24]], timeout=6)
                check("backspacing re-opens the full list", ok, screen[-300:])
            it2("session", "send", "--session", sid, "\t")
            time.sleep(1)
            flat = "".join(screen_text(sid).split())  # the long id wraps across lines
            # the id is 64 characters and wraps under the menu, so only its start is visible: match the first alert's own prefix
            check("Tab inserts the first alert's id after @alert:", f"@alert:{first.id[:24]}" in flat, flat[-200:])
            it2("session", "send", "--session", sid, "\x15")  # Ctrl+U: clear the line
            it2("session", "send", "--session", sid, "@case:")
            ok, screen = wait_screen(sid, [cases[0].title[:20]])
            check("typing @case: lists the recent open cases", ok, screen[-300:])
            it2("session", "send", "--session", sid, "\t")
            time.sleep(1)
            flat = "".join(screen_text(sid).split())
            check("Tab inserts the chosen case's id after @case:", cases[0].id.replace("-", "")[:8] in flat.replace("-", "") and "@case:" in flat, flat[-200:])
            it2("session", "send", "--session", sid, "\x15")
            if data["empty"]:
                it2("session", "run", "--session", sid, f"/space {data['empty']}")
                time.sleep(2)
                it2("session", "send", "--session", sid, "@alert:")
                ok, screen = wait_screen(sid, ["no open alerts in", f"/space {sp}"], timeout=12)
                check("an empty space says so and points at the space that has alerts", ok, screen[-300:])
                it2("session", "send", "--session", sid, "\x15")
            it2("session", "run", "--session", sid, "/exit")
            watch(sid, lambda s: not s.get("status"), timeout=15, label="clear")
    finally:
        if created:
            it2("session", "close", "--session", sid, "--force")
            print("\nclosed the throwaway window")
    ok = sum(1 for _, p, _ in results if p)
    print(f"\n{ok}/{len(results)} iTerm checks passed")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
