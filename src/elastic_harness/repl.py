"""The interactive session: slash commands, @file mentions, agent/local modes."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from rich.table import Table

from . import __version__
from .agent_chat import history_lines, run_turn, usage_line
from .client import ApiError, Elastic
from .config import STATE_DIR, Config, ConfigError
from .guard import expand_mentions
from .ingest import ingest
from .local_agent import LocalSession
from .render import Ui, short
from .tools import Ctx

COMMANDS = {
    "help": "show this list",
    "agents": "list Agent Builder agents",
    "agent": "/agent <id>  switch agent (new conversation)",
    "models": "list chat models on the cluster",
    "model": "/model <words>  switch model, e.g. /model sonnet 5.5 · /model default",
    "mode": "/mode agent|local  agent = an Elastic agent answers · local = the harness runs tools on this Mac",
    "new": "start a fresh conversation",
    "resume": "/resume [n|id]  list or reopen a conversation",
    "profiles": "list profiles",
    "profile": "/profile <name>  switch cluster profile",
    "ingest": "/ingest <path> [--index name] [--tool] [--agent id]  load local files into Elastic",
    "tools": "list the tools the current mode can use",
    "usage": "token usage this session",
    "doctor": "check connection, agents and models",
    "debug": "toggle raw event display",
    "clear": "clear the screen",
    "exit": "quit",
}
_PREFERRED = ["5.5-sonnet", "4.6-sonnet", "sonnet", "haiku"]


def pick_default_model(models: list[str]) -> str:
    for pref in _PREFERRED:
        for m in models:
            if pref in m:
                return m
    return models[0] if models else ""


def match_models(models: list[str], query: str) -> list[str]:
    q = query.strip().lower()
    if q in models:
        return [q]
    words = q.replace("-", " ").split()
    return [m for m in models if all(w in m.lower().replace("-", " ") for w in words)]


class Repl:
    def __init__(self, config: Config, ui: Ui, profile: str | None = None, agent: str | None = None,
                 model: str | None = None, mode: str | None = None):
        self.config, self.ui = config, ui
        self.cwd = Path.cwd()
        self.always: set[str] = set()
        self.agents_cache: list[dict] | None = None
        self.models_cache: list[str] | None = None
        self.conversation_id: str | None = None
        self.local: LocalSession | None = None
        self.tokens = [0, 0]  # agent-mode in / out
        self.connect(profile)
        if agent:
            ids = [a["id"] for a in self.agents()]
            if agent not in ids:
                raise ConfigError(f"no agent {agent!r} (have: {', '.join(ids)})")
            self.agent_id = agent
        if model and model.lower() not in ("default", "cluster", "none"):
            hits = match_models(self.models(), model)
            if len(hits) != 1:
                raise ConfigError(f"model {model!r} matches {len(hits)} models" + (": " + ", ".join(hits[:6]) if hits else " (see: eh models)"))
            self.model = hits[0]
        elif model:
            self.model = ""
        if mode:
            self.mode = mode

    # -- connection / state -----------------------------------------------------------------
    def connect(self, name: str | None = None) -> None:
        self.raw = self.config.get(name)
        self.client = Elastic(self.raw)
        self.agent_id = self.raw.default_agent or "elastic-ai-agent"
        self.model = self.raw.default_model
        self.mode = self.raw.mode or "agent"
        self.agents_cache = self.models_cache = None
        self.conversation_id, self.local = None, None
        self.always = set()

    def agents(self) -> list[dict]:
        if self.agents_cache is None:
            self.agents_cache = self.client.agents()
        return self.agents_cache

    def models(self) -> list[str]:
        if self.models_cache is None:
            try:
                self.models_cache = self.client.models()
            except ApiError as exc:
                self.ui.warn(f"could not list models: {exc}")
                self.models_cache = []
        return self.models_cache

    def _resolve_model(self, query: str) -> str:
        if query.lower() in ("default", "cluster", "none", ""):
            return ""
        hits = match_models(self.models(), query)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            self.ui.error(f"no model matches {query!r} (try /models)")
        else:
            self.ui.warn("several match, be more specific:\n  " + "\n  ".join(hits[:12]))
        return ""

    def _persist(self, **changes) -> None:
        for k, v in changes.items():
            setattr(self.raw, k, v)
        try:
            self.config.save()
        except OSError as exc:
            self.ui.warn(f"could not save defaults: {exc}")

    # -- sending ----------------------------------------------------------------------------
    def send(self, text: str) -> None:
        text, notes = expand_mentions(text, self.cwd)
        for n in notes:
            colour = "yellow" if n["status"].startswith(("refused", "skipped")) else "dim"
            self.ui.line(f"[{colour}]@ {n['path']} · {n['status']}" + (f" · {n['chars']:,} chars" if n["chars"] else "") + f"[/{colour}]")
        try:
            if self.mode == "local":
                self._send_local(text)
            else:
                self._send_agent(text)
        except KeyboardInterrupt:
            self.ui.flush()
            self.ui.warn("interrupted")
        except ApiError as exc:
            self.ui.error(str(exc))

    def _send_agent(self, text: str) -> None:
        # in agent mode the model is the cluster default unless the user picked one
        res = run_turn(self.client, self.ui, agent_id=self.agent_id, text=text,
                       conversation_id=self.conversation_id, model=self.model or None)
        self.conversation_id = res["conversation_id"]
        if res["usage"]:
            self.tokens[0] += res["usage"].get("input_tokens", 0)
            self.tokens[1] += res["usage"].get("output_tokens", 0)
            self.ui.usage(usage_line(res["usage"]))

    def _send_local(self, text: str) -> None:
        if not self.model:
            self.model = pick_default_model(self.models())
            if not self.model:
                self.ui.error("no chat models found on this cluster")
                return
            self.ui.info(f"local mode needs a model; using {self.model} (change with /model)")
        if self.local is None:
            self.local = LocalSession(Ctx(self.client, self.ui, self.cwd, self.model, self.always), self.agents())
        self.local.ctx.model = self.model
        self.local.turn(text)
        self.ui.usage(f"{self.local.tokens_in:,} in / {self.local.tokens_out:,} out tokens this session · {self.model}")

    # -- commands ---------------------------------------------------------------------------
    def handle(self, line: str) -> bool:
        """Run one line. Returns False to quit."""
        line = line.strip()
        if not line:
            return True
        if line in ("exit", "quit"):
            return False
        if not line.startswith("/"):
            self.send(line)
            return True
        name, _, arg = line[1:].partition(" ")
        if name in ("quit", "q"):
            return False
        fn = getattr(self, f"cmd_{name}", None)
        if fn is None:
            self.ui.error(f"unknown command /{name} (try /help)")
            return True
        try:
            fn(arg.strip())
        except (ApiError, ConfigError, OSError, ValueError, PermissionError) as exc:
            self.ui.error(str(exc))
        return True

    def cmd_help(self, arg: str) -> None:
        t = Table(show_header=False, box=None, padding=(0, 2))
        for k, v in COMMANDS.items():
            t.add_row(f"[cyan]/{k}[/cyan]", v)
        self.ui.console.print(t)
        self.ui.console.print("[dim]@path attaches a file or folder listing to your message · Alt+Enter adds a newline · Ctrl+C interrupts[/dim]")

    def cmd_agents(self, arg: str) -> None:
        self.agents_cache = None
        t = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        for col in ("", "id", "name", "access", "description"):
            t.add_column(col)
        for a in self.agents():
            mark = "●" if a["id"] == self.agent_id else ""
            t.add_row(mark, a["id"], a.get("name", ""), (a.get("access_control") or {}).get("access_mode", ""), short(a.get("description", ""), 70))
        self.ui.console.print(t)

    def cmd_agent(self, arg: str) -> None:
        if not arg:
            return self.cmd_agents("")
        ids = [a["id"] for a in self.agents()]
        hits = [i for i in ids if i == arg] or [i for i in ids if arg.lower() in i.lower()]
        if len(hits) != 1:
            self.ui.error(f"{'no agent' if not hits else 'several agents'} match {arg!r}: {', '.join(hits) or ', '.join(ids)}")
            return
        self.agent_id, self.conversation_id = hits[0], None
        self._persist(default_agent=self.agent_id)
        self.ui.ok(f"agent: {self.agent_id} (new conversation)")
        if self.mode == "local":
            self.ui.info("in local mode agents are reached through the ask_agent tool; /mode agent to chat with one directly")

    def cmd_models(self, arg: str) -> None:
        self.models_cache = None
        for m in self.models():
            self.ui.console.print(f"{'[green]●[/green]' if m == self.model else ' '} {m}")
        self.ui.info("agent mode with no model picked uses the cluster default · /model <words> to switch")

    def cmd_model(self, arg: str) -> None:
        if not arg:
            return self.ui.info(f"model: {self.model or 'cluster default'}")
        if arg.lower() in ("default", "cluster", "none"):
            self.model = ""
        else:
            m = self._resolve_model(arg)
            if not m:
                return
            self.model = m
        if self.local:
            self.local.ctx.model = self.model
        self._persist(default_model=self.model)
        self.ui.ok(f"model: {self.model or 'cluster default'}")

    def cmd_mode(self, arg: str) -> None:
        if arg not in ("agent", "local"):
            return self.ui.info(f"mode: {self.mode} (use /mode agent or /mode local)")
        self.mode = arg  # session-only: a one-shot or a new session always starts in the profile's mode
        self.ui.ok(f"mode: {arg}" + (" · the harness runs tools on this Mac" if arg == "local" else " · an Agent Builder agent answers"))

    def cmd_new(self, arg: str) -> None:
        self.conversation_id, self.local = None, None
        self.ui.ok("new conversation")

    def cmd_resume(self, arg: str) -> None:
        if self.mode == "local":
            saved = LocalSession.list_saved()
            if not arg:
                for i, s in enumerate(saved, 1):
                    self.ui.console.print(f"[cyan]{i:>2}[/cyan] {s['saved'][:16]}  {s['first']}")
                return self.ui.info("/resume <n> to reopen") if saved else self.ui.info("no saved local sessions")
            sid = saved[int(arg) - 1]["id"] if arg.isdigit() and int(arg) <= len(saved) else arg
            self.local = LocalSession(Ctx(self.client, self.ui, self.cwd, self.model, self.always), self.agents(), sid)
            self.ui.ok(f"resumed {sid} ({self.local.load(sid)} messages)")
            return
        convs = sorted(self.client.conversations(), key=lambda c: c.get("updated_at", ""), reverse=True)[:15]
        if not arg:
            for i, c in enumerate(convs, 1):
                self.ui.console.print(f"[cyan]{i:>2}[/cyan] {c.get('updated_at', '')[:16]}  [dim]{c.get('agent_id', '')}[/dim]  {short(c.get('title', ''), 60)}")
            return self.ui.info("/resume <n> to reopen") if convs else self.ui.info("no conversations yet")
        conv_id = convs[int(arg) - 1]["id"] if arg.isdigit() and int(arg) <= len(convs) else arg
        conv = self.client.conversation(conv_id)
        self.conversation_id = conv_id
        self.agent_id = conv.get("agent_id") or self.agent_id
        self.ui.ok(f"resumed '{short(conv.get('title', ''), 60)}' with agent {self.agent_id}")
        for role, text in history_lines(conv):
            self.ui.line(f"[bold]{role}[/bold] [dim]{short(text, 160)}[/dim]")

    def cmd_profiles(self, arg: str) -> None:
        for n, p in self.config.profiles.items():
            self.ui.console.print(f"{'[green]●[/green]' if n == self.raw.name else ' '} {n}  [dim]{p.space} · {p.kibana_url or p.creds_file}[/dim]")

    def cmd_profile(self, arg: str) -> None:
        if not arg:
            return self.cmd_profiles("")
        self.connect(arg)
        self.config.active = arg
        self.config.save()
        self.ui.ok(f"profile: {arg} · Kibana {self.client.version()} · space {self.client.space}")

    def cmd_ingest(self, arg: str) -> None:
        argv = shlex.split(arg)
        if not argv:
            return self.ui.info("usage: /ingest <path> [--index name] [--tool] [--agent id]")
        path, slug, tool, agent, i = argv[0], None, False, None, 1
        while i < len(argv):
            if argv[i] == "--index" and i + 1 < len(argv):
                slug, i = argv[i + 1], i + 2
            elif argv[i] == "--agent" and i + 1 < len(argv):
                agent, i = argv[i + 1], i + 2
            elif argv[i] == "--tool":
                tool, i = True, i + 1
            else:
                return self.ui.error(f"unexpected {argv[i]!r}")
        res = ingest(self.client, path, slug, tool, agent, say=self.ui.info)
        self.ui.ok("ingested {files} files → {chunks} chunks into {index} ({skipped} skipped, {failed} failed)".format(**res))
        if res.get("tool"):
            self.ui.ok(f"tool {res['tool']} created" + (f" and attached to {res['attached_to_agent']}" if res.get("attached_to_agent") else ""))
        elif not tool:
            self.ui.info("add --tool (and --agent <id>) so an Agent Builder agent can search it")

    def cmd_tools(self, arg: str) -> None:
        if self.mode == "local":
            from .tools import build_tools
            for t in build_tools():
                self.ui.console.print(f"[cyan]{t.name}[/cyan]  [dim]{t.description}[/dim]")
            return
        cfg = self.client.agent(self.agent_id).get("configuration", {})
        ids = [t for g in cfg.get("tools", []) for t in g.get("tool_ids", [])]
        self.ui.info(f"{self.agent_id}: {len(ids)} tools" + (" + Elastic built-ins" if cfg.get("enable_elastic_capabilities", True) else ""))
        for t in ids:
            self.ui.console.print(f"  {t}")

    def cmd_usage(self, arg: str) -> None:
        self.ui.info(f"agent mode: {self.tokens[0]:,} in / {self.tokens[1]:,} out" +
                     (f" · local mode: {self.local.tokens_in:,} in / {self.local.tokens_out:,} out" if self.local else ""))

    def cmd_doctor(self, arg: str) -> None:
        self.ui.ok(f"Kibana {self.client.version()} · space {self.client.space} · {self.client.kibana_url}")
        try:
            who = self.client.whoami()
            self.ui.ok(f"authenticated as {who.get('username')} (realm {(who.get('authentication_realm') or {}).get('type')})")
        except ApiError as exc:
            self.ui.warn(f"Elasticsearch: {exc}")
        self.ui.ok(f"{len(self.agents())} agents · {len(self.models())} chat models")
        self.ui.info(f"agent {self.agent_id} · model {self.model or 'cluster default'} · mode {self.mode}")

    def cmd_debug(self, arg: str) -> None:
        self.ui.debug = not self.ui.debug
        self.ui.info(f"debug {'on' if self.ui.debug else 'off'}")

    def cmd_clear(self, arg: str) -> None:
        self.ui.console.clear()

    def cmd_exit(self, arg: str) -> None:  # handled in handle(); kept so /help is honest
        pass

    # -- interactive loop -------------------------------------------------------------------
    def banner(self) -> None:
        self.ui.console.print(f"[bold]Elastic Harness[/bold] {__version__} · profile [cyan]{self.raw.name}[/cyan] · "
                              f"Kibana {self.client.version()} · space {self.client.space}")
        self.ui.console.print(f"[dim]agent {self.agent_id} · model {self.model or 'cluster default'} · mode {self.mode} · /help for commands[/dim]")

    def run(self) -> None:
        if not sys.stdin.isatty():
            for line in sys.stdin:
                if not self.handle(line):
                    break
            return
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion, PathCompleter
        from prompt_toolkit.document import Document
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        repl = self

        class Comp(Completer):
            path = PathCompleter(expanduser=True)

            def get_completions(self, document, event):
                text = document.text_before_cursor
                m = re.search(r"@(\S*)$", text)
                if m:
                    for c in self.path.get_completions(Document(m.group(1)), event):
                        yield Completion(c.text, c.start_position, display=c.display)
                    return
                if not text.startswith("/"):
                    return
                cmd, sp, rest = text[1:].partition(" ")
                if not sp:
                    for k in COMMANDS:
                        if k.startswith(cmd):
                            yield Completion(k, -len(cmd), display=f"/{k}", display_meta=COMMANDS[k][:50])
                    return
                pool = {"agent": [a["id"] for a in repl.agents_cache or []], "model": repl.models_cache or [],
                        "mode": ["agent", "local"], "profile": list(repl.config.profiles)}.get(cmd, [])
                for v in pool:
                    if rest.lower() in v.lower():
                        yield Completion(v, -len(rest))

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        session = PromptSession(history=FileHistory(str(STATE_DIR / "history")), completer=Comp(), key_bindings=kb,
                                bottom_toolbar=lambda: (f" {repl.raw.name} · {repl.client.space} · agent {repl.agent_id} · "
                                                        f"model {repl.model or 'cluster default'} · mode {repl.mode} "),
                                complete_while_typing=True)
        self.banner()
        try:  # warm the completion caches so tab completion has data
            self.agents(), self.models()
        except ApiError:
            pass
        while True:
            try:
                label = "local" if self.mode == "local" else self.agent_id
                line = session.prompt([("bold ansicyan", f"{label} › ")])
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not self.handle(line):
                break
