"""iTerm2 Session Status: the tab's dot and subtitle show idle / working / waiting / done / error.

iTerm2 3.7+ reads a proprietary escape sequence, the same signal its Claude Code integration uses:

    OSC 21337 ; status=<text> ; indicator=<#rrggbb> ; status-color=<#rrggbb> ; detail=<text> ST

Colors for `working` (#ff9500) and `done` (#00d75f) are read off live Claude Code sessions. iTerm sets no
defaults for the other states, so `waiting`, `error` and `idle` are chosen here in the same family.

Control it with EH_TAB_STATUS: `1` (default) full, `basic` hides the answer snippet (use this when sharing your
screen: the detail text shows in iTerm's Cockpit and toolbelt), `0` off. Outside iTerm2 nothing is ever written.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager

# status keyword -> (dot color, subtitle color). An empty color clears that field (iTerm's default shows).
STATES: dict[str, tuple[str, str]] = {
    "idle": ("#00d75f", ""),
    "working": ("#ff9500", "#ff9500"),
    "waiting": ("#ffd60a", "#ffd60a"),
    "done": ("#00d75f", "#00d75f"),
    "error": ("#ff453a", "#ff453a"),
}
MODES = ("on", "basic", "off")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def supported(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("TERM_PROGRAM") == "iTerm.app" or env.get("LC_TERMINAL") == "iTerm2"


def mode_from_env(env=None) -> str:
    v = (os.environ if env is None else env).get("EH_TAB_STATUS", "1").strip().lower()
    return "off" if v in ("0", "off", "false", "no") else ("basic" if v == "basic" else "on")


def clean(text: str, limit: int = 160) -> str:
    """Make free text safe for the payload: no control characters, no ';' (the field separator), one line."""
    text = _CONTROL.sub(" ", str(text or "")).replace(";", ",")
    text = re.sub(r"[*_`#>]+", "", text)  # markdown noise reads badly in a one-line subtitle
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _pick_stream():
    for s in (sys.stdout, sys.stderr):
        try:
            if s is not None and s.isatty():
                return s
        except (ValueError, AttributeError):
            continue
    return None


class TabStatus:
    def __init__(self, stream=None, env=None, mode: str | None = None):
        env = os.environ if env is None else env
        self.stream = stream if stream is not None else _pick_stream()
        self.mode = mode or mode_from_env(env)
        self.supported = supported(env)
        self.tmux = bool(env.get("TMUX"))
        self.state: str | None = None

    @property
    def enabled(self) -> bool:
        return self.supported and self.mode != "off" and self.stream is not None

    def set(self, state: str, detail: str = "") -> None:
        if state not in STATES:
            raise ValueError(f"unknown status {state!r}")
        self.state = state
        if not self.enabled:
            return
        dot, text = STATES[state]
        payload = f"status={state};indicator={dot};status-color={text}"
        payload += f";detail={clean(detail) if self.mode == 'on' else ''}"
        self._emit(payload)

    def clear(self) -> None:
        """Remove everything we set (on exit, so a finished program doesn't leave a stale 'done' on the tab)."""
        if self.enabled and self.state is not None:
            self._emit("status=;indicator=;status-color=;detail=")
        self.state = None

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        was, self.mode = self.state, mode
        if mode == "off":
            self.mode = "on"  # emit the clear even though we are switching off
            self.clear()
            self.mode = "off"
        elif was:
            self.set(was)

    @contextmanager
    def working(self, detail: str = ""):
        """Show `working` for the duration, then restore what was shown before (unless it changed meanwhile)."""
        prev = self.state
        self.set("working", detail)
        try:
            yield
        finally:
            if self.state == "working":
                self.set(prev or "idle")

    def _emit(self, payload: str) -> None:
        seq = f"\x1b]21337;{payload}\x07"
        if self.tmux:  # needs `set -g allow-passthrough on` in tmux
            seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
        try:
            self.stream.write(seq)
            self.stream.flush()
        except (OSError, ValueError):
            self.stream = None  # the terminal went away: stop trying
