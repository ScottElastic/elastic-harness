#!/usr/bin/env python3
"""Check the iTerm2 tab status for real: run `eh` inside an iTerm session and read back what iTerm reports.

    python scripts/iterm_check.py                 # opens a throwaway iTerm window, tests, closes it
    python scripts/iterm_check.py --session ID    # reuse an existing throwaway session (it is left open)

Needs iTerm2 3.7+ with the Python API enabled and its `it2` tool (~/.config/iterm2/it2). Uses a live cluster
profile like scripts/rehearse.py: agent `eh-demo` must exist (run rehearse.py first). It never touches your
other tabs; it only reads the status of the session it created.
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
    finally:
        if created:
            it2("session", "close", "--session", sid, "--force")
            print("\nclosed the throwaway window")
    ok = sum(1 for _, p, _ in results if p)
    print(f"\n{ok}/{len(results)} iTerm checks passed")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
