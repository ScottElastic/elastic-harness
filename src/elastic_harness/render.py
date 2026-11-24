"""Terminal rendering with rich: streamed markdown, dim tool lines, confirmations."""

from __future__ import annotations

import json
import sys

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.prompt import Prompt


def short(value, limit: int = 140) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


class Ui:
    def __init__(self, console: Console | None = None, debug: bool = False, auto_yes: bool = False):
        self.console = console or Console(highlight=False)
        self.debug = debug
        self.auto_yes = auto_yes
        self._live: Live | None = None
        self._buf = ""
        self._run = ""  # the text block being streamed now
        self.last_answer = ""  # the last complete text block (for /copy)
        self.streamed = False

    # -- model text --------------------------------------------------------------------------
    def text(self, chunk: str) -> None:
        self.streamed = True
        self._run += chunk
        if not self.console.is_terminal:
            sys.stdout.write(chunk)
            sys.stdout.flush()
            return
        if self._live is None:
            self._buf = ""
            self._live = Live(Markdown(""), console=self.console, refresh_per_second=10, vertical_overflow="visible")
            self._live.start()
        self._buf += chunk
        self._live.update(Markdown(self._buf))

    def flush(self) -> None:
        if self._run.strip():
            self.last_answer = self._run
        self._run = ""
        if self._live is not None:
            self._live.stop()
            self._live = None
            self._buf = ""
        elif not self.console.is_terminal and self.streamed:
            sys.stdout.write("\n")
        self.streamed = False

    # -- events ------------------------------------------------------------------------------
    def line(self, markup: str) -> None:
        self.flush()
        self.console.print(markup)

    def info(self, msg: str) -> None:
        self.line(f"[dim]{escape(msg)}[/dim]")

    def ok(self, msg: str) -> None:
        self.line(f"[green]✓[/green] {escape(msg)}")

    def warn(self, msg: str) -> None:
        self.line(f"[yellow]![/yellow] {escape(msg)}")

    def error(self, msg: str) -> None:
        self.line(f"[red]✗ {escape(msg)}[/red]")

    def reasoning(self, text: str) -> None:
        self.line(f"[dim italic]· {escape(short(text, 200))}[/dim italic]")

    def tool_call(self, name: str, params) -> None:
        self.line(f"[cyan]⏺ {escape(name)}[/cyan][dim]({escape(short(params, 120))})[/dim]")

    def tool_result(self, summary: str) -> None:
        self.line(f"  [dim]⎿ {escape(short(summary, 200))}[/dim]")

    def usage(self, text: str) -> None:
        self.line(f"[dim]{escape(text)}[/dim]")

    def raw(self, event: str, data) -> None:
        if self.debug:
            self.line(f"[magenta]· {escape(event)}[/magenta] [dim]{escape(short(data, 300))}[/dim]")

    # -- input -------------------------------------------------------------------------------
    def confirm(self, title: str, detail: str = "", allow_always: bool = True) -> str:
        """Ask the user. Returns 'y', 'n' or 'a' (always, this session)."""
        self.flush()
        if self.auto_yes:
            self.info(f"auto-approved: {title}")
            return "y"
        if not sys.stdin.isatty():
            self.warn(f"denied (non-interactive; pass --yes to approve): {title}")
            return "n"
        self.console.print(f"[bold yellow]? {escape(title)}[/bold yellow]")
        if detail:
            self.console.print(f"[dim]{escape(detail[:1500])}[/dim]")
        choices = ["y", "n", "a"] if allow_always else ["y", "n"]
        hint = "y = yes, n = no, a = yes and don't ask again this session" if allow_always else "y = yes, n = no"
        return Prompt.ask(f"  [dim]{hint}[/dim]", choices=choices, default="n", console=self.console)

    def ask(self, question: str, default: str = "") -> str:
        self.flush()
        return Prompt.ask(question, default=default, console=self.console)
