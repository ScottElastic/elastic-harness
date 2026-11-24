"""The interactive session: slash commands, @mentions, agent/local modes."""

from __future__ import annotations

import argparse
import getpass
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

from rich.table import Table

from . import __version__, agentcode, clusters, pickers, toolcode, transcript
from .agent_chat import history_lines, run_turn, usage_line
from .client import ApiError, Elastic
from .compare import MAX_MODELS, render as render_compare, run_compare
from .config import STATE_DIR, Config, ConfigError
from .ingest import ingest
from .local_agent import LocalSession
from .mentions import expand_all, incomplete_mentions
from .project import context_text, load_commands
from .render import Ui, short
from .tools import Ctx

COMMANDS = {
    "help": "show this list",
    "cluster": "/cluster [name]  list or switch · add · rm · test · secure",
    "space": "/space <id>  switch Kibana space (agents are per space) · /spaces lists them",
    "agents": "list Agent Builder agents",
    "agent": "/agent <id>  switch · push|pull|diff <file>  agents as code",
    "tool": "/tool push|pull|diff <file> · list  custom tools (esql, index_search) as code",
    "models": "list chat models on the cluster",
    "model": "/model <words>  switch model, e.g. /model sonnet 5.5 · /model default",
    "mode": "/mode agent|local  agent = an Elastic agent answers · local = the harness runs tools on this Mac",
    "compare": "/compare sonnet 5.5 | haiku 4.5 :: prompt   same prompt on 2-4 models (no :: = your last prompt)",
    "retry": "re-run your last prompt on a fresh conversation (--keep to continue this one)",
    "copy": "/copy [code]  last answer (or its last code block) to the clipboard",
    "export": "/export [file.md]  save this conversation as Markdown",
    "open": "/open [agent|case <id>]  open this conversation in Kibana",
    "compact": "summarise older local-mode history to save tokens",
    "new": "start a fresh conversation",
    "resume": "/resume [n|id]  list or reopen a conversation",
    "ingest": "/ingest <path> [--index name] [--tool] [--agent id]  load local files into Elastic",
    "tools": "list the tools the current mode can use",
    "usage": "token usage this session",
    "doctor": "check connection, agents and models",
    "tabstatus": "/tabstatus on|basic|off  iTerm2 tab status (working/waiting/done); basic hides the answer snippet",
    "debug": "toggle raw event display",
    "clear": "clear the screen",
    "exit": "quit",
}
_SEVERITY_STYLE = {"critical": "ansibrightred bold", "high": "ansired", "medium": "ansiyellow", "low": "ansigreen"}
_KIND_TEXTS = ("alert:", "case:")          # the text of the completions that offer the pickers
_PICK_WORD = re.compile(r"@(?:alert|case):\S*$")  # the cursor is in the word after `@alert:` or `@case:`
_KIND_HELP = {"alert:": "pick from the newest open alerts", "case:": "pick from recent open cases", "index:": "Elastic object"}
_PREFERRED = ["5.5-sonnet", "4.6-sonnet", "sonnet", "haiku"]
# slash commands slow enough to deserve "working" on the tab (the quick ones would only flicker it)
SLOW_COMMANDS = {"compare", "ingest", "compact", "doctor", "cluster", "agent", "tool", "resume", "space", "export", "agents", "models"}
_PATH_CMDS = re.compile(r"^/(ingest|export|(?:agent|tool) (?:push|pull|diff))\s+(\S*)$")


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


def _flags(argv: list[str], build) -> argparse.Namespace:
    ap = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    ap.error = lambda msg: (_ for _ in ()).throw(ValueError(msg))  # never exit the session on a bad flag
    build(ap)
    return ap.parse_args(argv)


class Repl:
    def __init__(self, config: Config, ui: Ui, profile: str | None = None, agent: str | None = None,
                 model: str | None = None, mode: str | None = None, space: str | None = None):
        self.config, self.ui = config, ui
        self.cwd = Path.cwd()
        self.always: set[str] = set()
        self.agents_cache: list[dict] | None = None
        self.models_cache: list[str] | None = None
        self.conversation_id: str | None = None
        self.local: LocalSession | None = None
        self.tokens = [0, 0]  # agent-mode in / out
        self.last_prompt: str | None = None
        self.last_error: str | None = None  # set when the last send failed: `eh -p` turns it into an exit code
        self.interrupted = False
        self.last_result: dict = {}  # conversation id, usage and tools of the last send: `eh -p --json` prints it
        self.picker = pickers.Picker()  # the open alerts and cases behind `@alert:` and `@case:` completion
        self.connect(profile)
        self.reload_project()
        if space:
            self.switch_space(space)
        if agent:
            ids = [a["id"] for a in self.agents()]
            if agent not in ids:
                raise ConfigError(f"no agent {agent!r} in space {self.client.space} (have: {', '.join(ids)})")
            self.agent_id = self.preferred_agent = agent
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
        self.agent_id = self.preferred_agent = self.raw.default_agent or "elastic-ai-agent"
        self.model = self.raw.default_model
        self.mode = "agent"  # a mode never carries over from a previous session
        self.agents_cache = self.models_cache = None
        self.conversation_id, self.local = None, None
        self.always = set()

    def reload_project(self) -> None:
        self.context, self.context_files = context_text(self.cwd)
        self.cmds, self.cmd_warnings = load_commands(self.cwd, reserved=set(COMMANDS) | {"profile", "profiles", "quit", "q", "exit"})

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
        self.ui.error(f"no model matches {query!r} (try /models)" if not hits else
                      "several match, be more specific:\n  " + "\n  ".join(hits[:12]))
        return ""

    def _persist(self, **changes) -> None:
        for k, v in changes.items():
            setattr(self.raw, k, v)
        try:
            self.config.save()
        except OSError as exc:
            self.ui.warn(f"could not save defaults: {exc}")

    def kibana_link(self, path: str) -> str:
        prefix = "" if self.client.space in ("", "default") else f"/s/{self.client.space}"
        return f"{self.client.kibana_url}{prefix}/app{path}"

    # -- sending ----------------------------------------------------------------------------
    def send(self, text: str, record: bool = True) -> None:
        missing = incomplete_mentions(text)
        if missing:  # a bare `@alert:` is not a question: say what to do instead of sending it
            self.last_error = missing[0]
            return self.ui.error(f"not sent: {'; '.join(missing)}")
        if record:
            self.last_prompt = text
        text, enotes, notes = expand_all(text, self.client, self.cwd)  # @path is read from what you typed, never from alert text
        for n in enotes:
            bad = n["status"] != "attached"
            self.ui.line(f"[{'yellow' if bad else 'dim'}]@ {n['ref']} · {n['status']}[/{'yellow' if bad else 'dim'}]")
        for n in notes:
            colour = "yellow" if n["status"].startswith(("refused", "skipped")) else "dim"
            self.ui.line(f"[{colour}]@ {n['path']} · {n['status']}" + (f" · {n['chars']:,} chars" if n["chars"] else "") + f"[/{colour}]")
        st = self.ui.status
        st.set("working", self.last_prompt or text)
        self.last_error, self.interrupted, self.last_result = None, False, {}
        try:
            self.ui.last_answer = ""
            if self.mode == "local":
                self._send_local(text)
            else:
                self._send_agent(text)
            st.set("done", self.ui.last_answer)
        except KeyboardInterrupt:
            self.ui.flush()
            self.ui.warn("interrupted")
            self.interrupted, self.last_error = True, "interrupted"
            st.set("idle", "interrupted")
        except ApiError as exc:
            self.ui.error(str(exc))
            self.last_error = str(exc)
            st.set("error", str(exc))

    def _send_agent(self, text: str) -> None:
        if self.conversation_id is None and self.context:  # EH.md goes in once, at the start of a conversation
            text = f"<project_context>\n{self.context}\n</project_context>\n\n{text}"
            self.ui.info(f"EH.md attached ({len(self.context):,} chars) · {', '.join(p.name for p in self.context_files)}")
        res = run_turn(self.client, self.ui, agent_id=self.agent_id, text=text,
                       conversation_id=self.conversation_id, model=self.model or None)
        self.conversation_id = res["conversation_id"]
        self.last_result = {"usage": res["usage"] or {}, "tools": res["tools"]}
        if res["usage"]:
            self.tokens[0] += res["usage"].get("input_tokens", 0)
            self.tokens[1] += res["usage"].get("output_tokens", 0)
            self.ui.usage(usage_line(res["usage"]))

    def result(self) -> dict:
        """The outcome of the last send as one JSON-able dict (`eh -p --json`)."""
        local = self.mode == "local"
        out = {"ok": self.last_error is None, "cluster": self.raw.name, "space": self.client.space, "mode": self.mode,
               "agent": None if local else self.agent_id, "model": self.model or None,
               "conversation_id": (self.local.id if self.local else None) if local else self.conversation_id,
               "answer": self.ui.last_answer, "usage": self.last_result.get("usage") or {}}
        if "tools" in self.last_result:
            out["tools"] = self.last_result["tools"]
        if self.last_error:
            out["error"] = self.last_error
        return out

    def _ensure_local(self) -> bool:
        if not self.model:
            self.model = pick_default_model(self.models())
            if not self.model:
                self.ui.error("no chat models found on this cluster")
                return False
            self.ui.info(f"local mode needs a model; using {self.model} (change with /model)")
        if self.local is None:
            self.local = LocalSession(Ctx(self.client, self.ui, self.cwd, self.model, self.always), self.agents(),
                                      extra_system=self.context)
        self.local.ctx.model = self.model
        return True

    def _send_local(self, text: str) -> None:
        if not self._ensure_local():
            return
        self.local.turn(text)
        self.last_result = {"usage": {"input_tokens": self.local.tokens_in, "output_tokens": self.local.tokens_out}}
        self.ui.usage(f"{self.local.tokens_in:,} in / {self.local.tokens_out:,} out tokens this session · {self.model}")

    # -- @mention completion ----------------------------------------------------------------
    def mention_completions(self, tok: str) -> list:
        """Completions for the word typed after '@' (prompt_toolkit Completion objects).

        `alert`/`case`/`index` become `alert:`/`case:`/`index:`; once `@alert:` or `@case:` is typed, the newest open
        alerts or cases in this space are offered, filtered by what follows the colon. Paths are the caller's job.
        Runs on a completer thread: the list is a cached network call (see pickers.py).
        """
        from prompt_toolkit.completion import Completion
        kind, colon, word = tok.partition(":")
        if colon and kind in pickers.KINDS:
            listing = self.picker.listing(self.client, kind)
            rows = pickers.match(listing.rows, word)
            if not rows:
                # A note instead of a blank menu. prompt_toolkit drops a lone completion that changes nothing, so this one
                # adds a space after the token: harmless if chosen, and it keeps the note on screen.
                note = listing.note or f"no {kind} matches {word!r}"
                return [Completion(f"{tok} ", -len(tok), display=[("ansibrightblack", note)])]
            out = []
            for r in rows:
                title = r.title if len(r.title) <= 60 else r.title[:59] + "…"
                shown = [(_SEVERITY_STYLE.get(r.severity, ""), f"{r.severity:<8}"), ("", f" {title}")]
                if r.where:
                    shown.append(("ansibrightblack", f"  {r.where}"))
                meta = " · ".join(x for x in (r.extra, r.when) if x)
                out.append(Completion(r.id, -len(word), display=shown, display_meta=meta))
            return out
        out = []
        alias = {"alerts": "alert:", "cases": "case:"}.get(tok)
        for k in ("alert:", "case:", "index:"):
            if (k.startswith(tok) and tok != k) or k == alias:
                out.append(Completion(k, -len(tok), display=f"@{k}", display_meta=_KIND_HELP[k]))
                if len(tok) >= 2 and k != "index:":
                    self.picker.prefetch(self.client, k[:-1])  # warm the list so it is there when the ':' is typed
        return out

    def on_tab(self, buf) -> None:
        """Tab. As prompt_toolkit's own (next completion, or start completing), plus: when that turned `@al` into `@alert:`
        carry straight on into the list. Inserting a completion does not count as typing, so without this the list only
        opened once you typed another character (the stale `@alert:` kind row just stayed on screen)."""
        if buf.complete_state:
            buf.complete_next()
        else:
            buf.start_completion(insert_common_part=True)
        cur = buf.complete_state.current_completion if buf.complete_state else None
        if cur is not None and cur.text in _KIND_TEXTS:
            buf.complete_state = None
            buf.start_completion(select_first=False)

    def on_enter(self, buf) -> bool:
        """Enter while a list is open on an `@alert:`/`@case:` word: choose from it, do not send the line. Returns False
        when Enter should submit as usual (no list open, or not in such a word)."""
        st = buf.complete_state
        if not st or not st.completions or not _PICK_WORD.search(buf.document.text_before_cursor):
            return False
        cur = st.current_completion
        if cur is not None:                        # a row is highlighted and its text is already in the line
            buf.complete_state = None
            if cur.text in _KIND_TEXTS:            # ...but it is only the kind: go on to the list
                buf.start_completion(select_first=False)
            return True                            # an id (or a note): that is the choice, send on the next Enter
        first = next((i for i, c in enumerate(st.completions) if c.text not in _KIND_TEXTS and not c.text.endswith(" ")), None)
        if first is not None:                      # nothing highlighted: take the top row
            buf.go_to_completion(first)
        buf.complete_state = None
        return True                                # a note or no rows: still never send a bare `@alert:`

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
        if name in ("exit", "quit", "q"):
            return False
        fn = getattr(self, f"cmd_{name}", None)
        try:
            if fn is not None and name in SLOW_COMMANDS:
                with self.ui.status.working(f"/{name} {arg.strip()}"[:80]):
                    fn(arg.strip())
            elif fn is not None:
                fn(arg.strip())
            elif name in self.cmds:
                self.run_custom(name, arg.strip())
            else:
                self.ui.error(f"unknown command /{name} (try /help)")
        except (ApiError, ConfigError, OSError, ValueError, PermissionError, SystemExit) as exc:
            self.ui.error(str(exc) or "bad arguments")
        return True

    def run_custom(self, name: str, arg: str) -> None:
        c = self.cmds[name]
        saved = (self.mode, self.model, self.agent_id, self.conversation_id)
        if c.mode:
            self.mode = c.mode
        if c.model:
            m = self._resolve_model(c.model)
            if not m:
                return
            self.model = m
        if c.agent:
            self.agent_id, self.conversation_id = c.agent, None
        self.ui.info(f"/{name}" + (f" · {c.description}" if c.description else ""))
        try:
            self.send(c.expand(arg))
        finally:  # a command's mode/model/agent apply to that one run only
            self.mode, self.model = saved[0], saved[1]
            if c.agent:
                self.agent_id, self.conversation_id = saved[2], saved[3]

    def cmd_help(self, arg: str) -> None:
        t = Table(show_header=False, box=None, padding=(0, 2))
        for k, v in COMMANDS.items():
            t.add_row(f"[cyan]/{k}[/cyan]", v)
        for k, c in sorted(self.cmds.items()):
            t.add_row(f"[green]/{k}[/green]", (c.description or "custom command") + f" [dim]({c.source.parent.parent.name if c.source else ''})[/dim]")
        self.ui.console.print(t)
        self.ui.console.print("[dim]@path attaches a file or folder · @alert:<id> @case:<id> @index:<pattern> pull Elastic objects in · "
                              "Alt+Enter = newline · Ctrl+C stops a run[/dim]")
        for w in self.cmd_warnings:
            self.ui.warn(w)
        if self.context_files:
            self.ui.info("project instructions: " + ", ".join(str(p) for p in self.context_files))

    # -- clusters ---------------------------------------------------------------------------
    def cmd_cluster(self, arg: str) -> None:
        argv = shlex.split(arg) if arg else []
        sub = argv[0] if argv else ""
        if not argv:
            return self._cluster_table()
        if sub == "add":
            def build(ap):
                ap.add_argument("name")
                ap.add_argument("--creds")
                ap.add_argument("--import", dest="import_file")
                ap.add_argument("--space", default="default")
                ap.add_argument("--kibana-url", default="")
                ap.add_argument("--es-url", default="")
            a = _flags(argv[1:], build)
            prof, where, version = clusters.add_cluster(self.config, a.name, creds=a.creds, import_file=a.import_file,
                                                        kibana_url=a.kibana_url, es_url=a.es_url, space=a.space,
                                                        ask=self.ui.ask, secret=getpass.getpass)
            self.ui.ok(f"cluster {prof.name} added · key: {where} · Kibana {version} · /cluster {prof.name} to switch")
        elif sub in ("rm", "remove", "delete"):
            if len(argv) != 2:
                return self.ui.info("usage: /cluster rm <name>")
            prof = self.config.get(argv[1])
            if prof.name == self.raw.name:
                return self.ui.error("that is the cluster you are on: switch to another first")
            if self.ui.confirm(f"Remove cluster {prof.name} and its stored key?", Config.key_source(prof), allow_always=False) == "y":
                self.config.remove(prof.name)
                self.ui.ok(f"removed {prof.name}")
        elif sub == "test":
            info = clusters.test_cluster(self.config, argv[1] if len(argv) > 1 else self.raw.name)
            self.ui.ok(f"{info['name']} · {info['host']} · Kibana {info['version']} · space {info['space']} · "
                       f"user {info.get('user')} · {info['agents']} agents · {info['models']} models")
        elif sub == "secure":
            if len(argv) != 2:
                return self.ui.info("usage: /cluster secure <name>  (moves a key from the config file into the Keychain)")
            self.ui.ok(f"{argv[1]}: key now in {self.config.secure(argv[1])}")
        else:
            self.switch_cluster(arg)

    def _cluster_table(self) -> None:
        t = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        for col in ("", "cluster", "host", "space", "key", "default agent"):
            t.add_column(col)
        for r in clusters.rows(self.config):
            here = r["name"] == self.raw.name
            t.add_row("[green]●[/green]" if here else "", f"[bold]{r['name']}[/bold]" if here else r["name"], r["host"],
                      r["space"], r["key"], r["agent"])
        self.ui.console.print(t)
        self.ui.info("/cluster <name> switches · /cluster add <name> · rm · test · secure")

    def switch_cluster(self, name: str) -> None:
        had_session = bool(self.conversation_id or (self.local and self.local.messages))
        self.connect(name)
        self.config.active = self.raw.name
        self.config.save()
        self.ui.line(f"[bold green]→ cluster {self.raw.name}[/bold green] [dim]{clusters.host(self.client.kibana_url)} · "
                     f"Kibana {self.client.version()} · space {self.client.space} · agent {self.agent_id}[/dim]")
        if had_session:
            self.ui.info("started a fresh conversation (conversations belong to one cluster)")
        self.reload_project()

    cmd_profile = cmd_cluster  # older name
    cmd_profiles = cmd_cluster

    # -- spaces -----------------------------------------------------------------------------
    def switch_space(self, space: str) -> None:
        previous = self.client.space
        try:
            known = {s["id"] for s in self.client.spaces()}
            if known and space not in known:
                raise ConfigError(f"no space {space!r} (have: {', '.join(sorted(known))})")
        except ApiError:
            pass  # this key may not be allowed to list spaces: fall back to trying the space itself
        self.client.space = space
        try:
            self.agents_cache = self.client.agents()
        except ApiError as exc:
            self.client.space = previous
            raise ConfigError(f"cannot use space {space!r}: {exc}") from None
        self.conversation_id, self.local = None, None
        ids = [a["id"] for a in self.agents_cache]
        if self.preferred_agent in ids:  # your chosen agent comes back as soon as a space has it
            self.agent_id = self.preferred_agent
        elif self.agent_id not in ids:
            fallback = "elastic-ai-agent" if "elastic-ai-agent" in ids else (ids[0] if ids else "")
            self.ui.info(f"agent {self.preferred_agent!r} is not in space {space}; using {fallback!r} there")
            self.agent_id = fallback

    def cmd_space(self, arg: str) -> None:
        if not arg:
            return self.cmd_spaces("")
        self.switch_space(arg)
        self.ui.ok(f"space: {self.client.space} · {len(self.agents_cache or [])} agents · agent {self.agent_id} · fresh conversation")

    def cmd_spaces(self, arg: str) -> None:
        try:
            spaces = self.client.spaces()
        except ApiError as exc:
            return self.ui.warn(f"cannot list spaces with this key ({exc.message[:80]}); /space <id> still works if you know it")
        for s in spaces:
            self.ui.console.print(f"{'[green]●[/green]' if s['id'] == self.client.space else ' '} {s['id']}  [dim]{s.get('name', '')}[/dim]")

    # -- agents, models, mode ----------------------------------------------------------------
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
        argv = shlex.split(arg)
        if argv[0] in ("push", "pull", "diff"):
            return self._agent_code(argv)
        ids = [a["id"] for a in self.agents()]
        hits = [i for i in ids if i == arg] or [i for i in ids if arg.lower() in i.lower()]
        if len(hits) != 1:
            self.ui.error(f"{'no agent' if not hits else 'several agents'} match {arg!r}: {', '.join(hits) or ', '.join(ids)}")
            return
        self.agent_id, self.conversation_id = hits[0], None
        self.preferred_agent = self.agent_id
        self._persist(default_agent=self.agent_id)
        self.ui.ok(f"agent: {self.agent_id} (new conversation)")
        if self.mode == "local":
            self.ui.info("in local mode agents are reached through the ask_agent tool; /mode agent to chat with one directly")

    def _agent_code(self, argv: list[str]) -> None:
        sub = argv[0]

        def build(ap):
            ap.add_argument("target", nargs="?")
            ap.add_argument("out", nargs="?", default="agents")
            ap.add_argument("--all", action="store_true")
            ap.add_argument("--force", action="store_true")
            ap.add_argument("--dry-run", action="store_true")
            ap.add_argument("--allow-risky", action="store_true")
        a = _flags(argv[1:], build)
        if sub in ("push", "diff"):
            if not a.target:
                return self.ui.info(f"usage: /agent {sub} <file.md>")
            res = agentcode.push(self.client, a.target, confirm=lambda t, d: self.ui.confirm(t, d, allow_always=False) == "y",
                                 say=lambda m: self.ui.console.print(m, markup=False, highlight=False),
                                 allow_risky=a.allow_risky, dry_run=a.dry_run or sub == "diff")
            self.agents_cache = None
            self.ui.ok(f"{res['id']}: {res['action']}")
        else:
            if a.all:
                paths = agentcode.pull_all(self.client, a.target or "agents", force=a.force)
                self.ui.ok(f"pulled {len(paths)} agents into {Path(a.target or 'agents')}/")
            elif a.target:
                self.ui.ok(f"wrote {agentcode.pull(self.client, a.target, a.out, force=a.force)}")
            else:
                self.ui.info("usage: /agent pull <id> [dir] [--force]  or  /agent pull --all [dir]")

    def cmd_tool(self, arg: str) -> None:
        argv = shlex.split(arg)
        if not argv or argv[0] not in ("push", "pull", "diff", "list"):
            return self.ui.info("usage: /tool push|diff <file.md> · /tool pull <id> [dir] · /tool pull --all [dir] · /tool list")
        sub = argv[0]

        def build(ap):
            ap.add_argument("target", nargs="?")
            ap.add_argument("out", nargs="?", default="tools")
            ap.add_argument("--all", action="store_true")
            ap.add_argument("--force", action="store_true")
            ap.add_argument("--dry-run", action="store_true")
        a = _flags(argv[1:], build)
        if sub == "list":
            return self.print_custom_tools()
        if sub in ("push", "diff"):
            if not a.target:
                return self.ui.info(f"usage: /tool {sub} <file.md>")
            res = toolcode.push(self.client, a.target, confirm=lambda t, d: self.ui.confirm(t, d, allow_always=False) == "y",
                                say=lambda m: self.ui.console.print(m, markup=False, highlight=False),
                                dry_run=a.dry_run or sub == "diff")
            self.ui.ok(f"{res['id']}: {res['action']}")
        elif a.all:
            paths = toolcode.pull_all(self.client, a.target or "tools", force=a.force)
            self.ui.ok(f"pulled {len(paths)} tools into {Path(a.target or 'tools')}/")
        elif a.target:
            self.ui.ok(f"wrote {toolcode.pull(self.client, a.target, a.out, force=a.force)}")
        else:
            self.ui.info("usage: /tool pull <id> [dir] [--force]  or  /tool pull --all [dir]")

    def print_custom_tools(self) -> None:
        rows, footer = toolcode.listing(self.client)
        for tid, text in rows:
            self.ui.console.print(f"[cyan]{tid}[/cyan]  [dim]{text}[/dim]", markup=True)
        self.ui.info(footer)

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
        self.mode = arg  # session-only: a new session always starts in agent mode
        self.ui.ok(f"mode: {arg}" + (" · the harness runs tools on this Mac" if arg == "local" else " · an Agent Builder agent answers"))

    # -- compare, retry, copy, export, open --------------------------------------------------
    def cmd_compare(self, arg: str) -> None:
        left, sep, right = arg.partition("::")
        queries = [q.strip() for q in left.split("|") if q.strip()]
        if len(queries) < 2:
            return self.ui.info("usage: /compare sonnet 5.5 | haiku 4.5 [| …] :: your prompt   (no :: = your last prompt)")
        if len(queries) > MAX_MODELS:
            return self.ui.error(f"at most {MAX_MODELS} models")
        models = []
        for q in queries:
            hits = match_models(self.models(), q)
            if len(hits) != 1:
                return self.ui.error(f"{q!r} matches {len(hits)} models" + (": " + ", ".join(hits[:5]) if hits else ""))
            models.append(hits[0])
        prompt = right.strip() if sep and right.strip() else self.last_prompt
        if not prompt:
            return self.ui.error("nothing to compare yet: add ':: your prompt'")
        missing = incomplete_mentions(prompt)
        if missing:
            return self.ui.error(f"not sent: {'; '.join(missing)}")
        self.last_prompt = prompt
        prompt, enotes, _ = expand_all(prompt, self.client, self.cwd)
        what = (f"agent {self.agent_id} (tools run for real)" if self.mode == "agent" else "plain model answers, no tools (local mode)")
        with self.ui.console.status(f"running on {len(models)} models · {what}…"):
            results = run_compare(self.client, models, prompt, mode=self.mode, agent_id=self.agent_id)
        self.ui.console.print(render_compare(results))
        if self.mode == "agent":
            self.ui.info("the comparison chats were deleted from Kibana")

    def cmd_retry(self, arg: str) -> None:
        if not self.last_prompt:
            return self.ui.error("nothing to retry yet")
        if "--keep" not in arg:
            self.conversation_id, self.local = None, None
            self.ui.info("retrying on a fresh conversation")
        self.send(self.last_prompt, record=False)

    def cmd_copy(self, arg: str) -> None:
        text = self.ui.last_answer
        if not text.strip():
            return self.ui.error("no answer to copy yet")
        if arg == "code":
            text = transcript.last_code_block(text) or ""
            if not text:
                return self.ui.error("the last answer has no code block")
        how = transcript.copy_to_clipboard(text)
        self.ui.ok(f"copied {len(text):,} characters ({how})")

    def cmd_export(self, arg: str) -> None:
        header = {"cluster": self.raw.name, "space": self.client.space, "agent": self.agent_id if self.mode == "agent" else "(local mode)",
                  "model": self.model or "cluster default", "mode": self.mode, "exported": datetime.now().strftime("%Y-%m-%d %H:%M")}
        if self.mode == "local":
            if not self.local or not self.local.messages:
                return self.ui.error("nothing to export yet")
            md = transcript.local_markdown(self.local.messages, header)
        else:
            if not self.conversation_id:
                return self.ui.error("nothing to export yet")
            md = transcript.agent_markdown(self.client.conversation(self.conversation_id), header)
        path = Path(arg).expanduser() if arg else transcript.default_export_path(self.cwd)
        if path.exists():
            return self.ui.error(f"{path} already exists")
        path.write_text(md, encoding="utf-8")
        self.ui.ok(f"saved {path} ({len(md):,} chars)")

    def cmd_open(self, arg: str) -> None:
        argv = arg.split()
        if argv[:1] == ["case"] and len(argv) == 2:
            url = self.kibana_link(f"/security/cases/{argv[1]}")
        elif self.conversation_id and argv[:1] != ["agent"]:
            url = self.kibana_link(f"/agent_builder/agents/{self.agent_id}/conversations/{self.conversation_id}")
        else:
            url = self.kibana_link(f"/agent_builder/agents/{self.agent_id}")
        self.ui.console.print(url, markup=False, highlight=False, soft_wrap=True)
        if not transcript.open_url(url):
            self.ui.info("could not launch a browser; copy the link above")

    # -- conversations -----------------------------------------------------------------------
    def cmd_new(self, arg: str) -> None:
        self.conversation_id, self.local = None, None
        self.reload_project()
        self.ui.ok("new conversation")

    def cmd_compact(self, arg: str) -> None:
        if self.mode != "local" or not self.local or not self.local.messages:
            return self.ui.info("compaction applies to local mode, once it has a conversation")
        self._ensure_local()
        with self.ui.console.status("summarising earlier turns…"):
            before, after = self.local.compact(force=True)
        self.ui.ok(f"compacted ~{before // 1000}k → ~{after // 1000}k tokens (estimated)")

    def cmd_resume(self, arg: str) -> None:
        if self.mode == "local":
            saved = LocalSession.list_saved()
            if not arg:
                for i, s in enumerate(saved, 1):
                    self.ui.console.print(f"[cyan]{i:>2}[/cyan] {s['saved'][:16]}  {s['first']}")
                return self.ui.info("/resume <n> to reopen") if saved else self.ui.info("no saved local sessions")
            sid = saved[int(arg) - 1]["id"] if arg.isdigit() and int(arg) <= len(saved) else arg
            return self._resume_local(sid)
        convs = sorted(self.client.conversations(), key=lambda c: c.get("updated_at", ""), reverse=True)[:15]
        if not arg:
            for i, c in enumerate(convs, 1):
                self.ui.console.print(f"[cyan]{i:>2}[/cyan] {c.get('updated_at', '')[:16]}  [dim]{c.get('agent_id', '')}[/dim]  {short(c.get('title', ''), 60)}")
            return self.ui.info("/resume <n> to reopen") if convs else self.ui.info("no conversations yet")
        self._resume_agent(convs[int(arg) - 1]["id"] if arg.isdigit() and int(arg) <= len(convs) else arg)

    def _resume_local(self, sid: str) -> None:
        self.local = LocalSession(Ctx(self.client, self.ui, self.cwd, self.model, self.always), self.agents(), sid,
                                  extra_system=self.context)
        self.ui.ok(f"resumed {sid} ({self.local.load(sid)} messages)")

    def _resume_agent(self, conv_id: str, show_history: bool = True) -> None:
        conv = self.client.conversation(conv_id)
        self.conversation_id = conv_id
        self.agent_id = conv.get("agent_id") or self.agent_id
        self.ui.ok(f"resumed '{short(conv.get('title', ''), 60)}' with agent {self.agent_id}")
        if show_history:
            for role, text in history_lines(conv):
                self.ui.line(f"[bold]{role}[/bold] [dim]{short(text, 160)}[/dim]")

    def resume_for_cli(self, conv_id: str | None, agent: str | None = None) -> None:
        """`eh -c` / `eh --resume ID`: reopen a conversation before the first prompt, without echoing its history.

        No id means the most recent one (with `agent`, the most recent one with that agent). Raises ConfigError if
        there is nothing to continue.
        """
        if self.mode == "local":
            saved = LocalSession.list_saved()
            if not conv_id and not saved:
                raise ConfigError("no saved local sessions to continue")
            return self._resume_local(conv_id or saved[0]["id"])  # session ids start with a timestamp: newest first
        if not conv_id:
            convs = sorted(self.client.conversations(), key=lambda c: c.get("updated_at", ""), reverse=True)
            if agent:
                convs = [c for c in convs if c.get("agent_id") == agent]
            if not convs:
                raise ConfigError("no earlier conversation to continue" + (f" with agent {agent}" if agent else "")
                                  + f" on cluster {self.raw.name}, space {self.client.space}")
            conv_id = convs[0]["id"]
        self._resume_agent(conv_id, show_history=False)

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
        self.ui.info(f"target: cluster {self.raw.name} · space {self.client.space}")
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
        version = self.client.version()
        self.ui.ok(f"cluster {self.raw.name} · Kibana {version} · space {self.client.space} · {clusters.host(self.client.kibana_url)}")
        try:
            who = self.client.whoami()
            self.ui.ok(f"authenticated as {who.get('username')} (realm {(who.get('authentication_realm') or {}).get('type')}) · key in {Config.key_source(self.raw)}")
        except ApiError as exc:
            self.ui.warn(f"Elasticsearch: {exc}")
        self.ui.ok(f"{len(self.agents())} agents · {len(self.models())} chat models")
        for level, msg in clusters.compat_notes(version, self.client.probe_abort_route()):
            {"ok": self.ui.ok, "warn": self.ui.warn}.get(level, self.ui.info)(msg)
        self.ui.info(f"agent {self.agent_id} · model {self.model or 'cluster default'} · mode {self.mode}")

    def cmd_tabstatus(self, arg: str) -> None:
        st = self.ui.status
        if not arg:
            where = "active" if st.enabled else ("off" if st.mode == "off" else "inactive: not running in iTerm2 on a terminal")
            return self.ui.info(f"tab status: {st.mode} ({where}) · /tabstatus on|basic|off")
        if arg not in ("on", "basic", "off"):
            return self.ui.error("use /tabstatus on, basic or off")
        st.set_mode(arg)
        self.ui.ok(f"tab status: {arg}" + (" (no answer text on the tab: safe for screen sharing)" if arg == "basic" else ""))

    def cmd_debug(self, arg: str) -> None:
        self.ui.debug = not self.ui.debug
        self.ui.info(f"debug {'on' if self.ui.debug else 'off'}")

    def cmd_clear(self, arg: str) -> None:
        self.ui.console.clear()

    # -- interactive loop -------------------------------------------------------------------
    def banner(self) -> None:
        self.ui.console.print(f"[bold]Elastic Harness[/bold] {__version__} · cluster [cyan]{self.raw.name}[/cyan] · "
                              f"Kibana {self.client.version()} · space {self.client.space}")
        extras = []
        if self.context_files:
            extras.append("EH.md")
        if self.cmds:
            extras.append(f"{len(self.cmds)} custom commands")
        self.ui.console.print(f"[dim]agent {self.agent_id} · model {self.model or 'cluster default'} · mode {self.mode}"
                              + (f" · {', '.join(extras)}" if extras else "") + " · /help for commands[/dim]")

    def run(self) -> None:
        if not sys.stdin.isatty():
            for line in sys.stdin:
                if not self.handle(line):
                    break
            return
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion, PathCompleter, ThreadedCompleter
        from prompt_toolkit.document import Document
        from prompt_toolkit.enums import DEFAULT_BUFFER
        from prompt_toolkit.filters import has_focus
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        repl = self

        class Comp(Completer):
            path = PathCompleter(expanduser=True)

            def _paths(self, token, event):
                for c in self.path.get_completions(Document(token), event):
                    yield Completion(c.text, c.start_position, display=c.display)

            def get_completions(self, document, event):
                text = document.text_before_cursor
                pm = _PATH_CMDS.match(text)
                if pm:
                    yield from self._paths(pm.group(2), event)
                    return
                m = re.search(r"@(\S*)$", text)
                if m:
                    tok = m.group(1)
                    yield from repl.mention_completions(tok)
                    if ":" not in tok:
                        yield from self._paths(tok, event)
                    return
                if not text.startswith("/"):
                    return
                cmd, sp, rest = text[1:].partition(" ")
                if not sp:
                    for k in list(COMMANDS) + list(repl.cmds):
                        if k.startswith(cmd):
                            meta = COMMANDS.get(k) or repl.cmds[k].description or "custom command"
                            yield Completion(k, -len(cmd), display=f"/{k}", display_meta=meta[:50])
                    return
                pool = {"agent": [a["id"] for a in repl.agents_cache or []] + ["push", "pull", "diff"],
                        "tool": ["push", "pull", "diff", "list"],
                        "model": repl.models_cache or [], "mode": ["agent", "local"],
                        "cluster": list(repl.config.profiles) + ["add", "rm", "test", "secure"],
                        "profile": list(repl.config.profiles)}.get(cmd, [])
                for v in pool:
                    if rest.lower() in v.lower():
                        yield Completion(v, -len(rest))

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        @kb.add("tab", filter=has_focus(DEFAULT_BUFFER))
        def _tab(event):
            repl.on_tab(event.current_buffer)

        @kb.add("enter", filter=has_focus(DEFAULT_BUFFER))
        def _enter(event):
            if not repl.on_enter(event.current_buffer):
                event.current_buffer.validate_and_handle()  # what prompt_toolkit's own Enter does

        @kb.add("backspace")
        def _backspace(event):
            # Deleting closes the completion menu and prompt_toolkit only reopens it on typing. While editing the word after
            # `@alert:` or `@case:` bring the list back, so narrowing and widening the filter both work.
            buf = event.current_buffer
            buf.delete_before_cursor(count=event.arg)
            if re.search(r"@(?:alert|case):\S*$", buf.document.text_before_cursor):
                buf.start_completion(select_first=False)

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        session = PromptSession(history=FileHistory(str(STATE_DIR / "history")), completer=ThreadedCompleter(Comp()), key_bindings=kb,
                                bottom_toolbar=lambda: (f" {repl.raw.name} · {repl.client.space} · agent {repl.agent_id} · "
                                                        f"model {repl.model or 'cluster default'} · mode {repl.mode} "),
                                complete_while_typing=True)
        self.banner()
        try:  # warm the completion caches so tab completion has data
            self.agents(), self.models()
        except ApiError:
            pass
        for w in self.cmd_warnings:
            self.ui.warn(w)
        self.ui.status.set("idle")
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
