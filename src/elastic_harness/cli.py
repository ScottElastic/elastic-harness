"""eh: command-line entry point."""

from __future__ import annotations

import argparse
import atexit
import getpass
import os
import sys
from pathlib import Path

from . import __version__, agentcode, clusters
from .client import ApiError, Elastic
from .config import Config, ConfigError
from .ingest import ingest
from .render import Ui
from .status import TabStatus
from .repl import Repl


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eh", description="Elastic Harness: a terminal harness for Elastic Agent Builder")
    p.add_argument("--version", action="version", version=f"elastic-harness {__version__}")
    p.add_argument("--cluster", "--profile", dest="cluster", help="cluster to use (default: the active one)")
    p.add_argument("--space", help="Kibana space for this run (default: the cluster's space)")
    p.add_argument("--agent", help="Agent Builder agent id")
    p.add_argument("--model", help="model words, e.g. 'sonnet 5.5' (see: eh models)")
    p.add_argument("--mode", choices=["agent", "local"], help="agent = an Elastic agent answers; local = tools run on this Mac")
    p.add_argument("-p", "--print", dest="prompt", metavar="PROMPT", help="run one prompt, print the answer, exit")
    p.add_argument("--yes", action="store_true", help="auto-approve confirmations (one-shot runs, agent push, …)")
    p.add_argument("--debug", action="store_true", help="show raw stream events")
    sub = p.add_subparsers(dest="cmd")

    cl = sub.add_parser("cluster", aliases=["profile"], help="manage the clusters you can connect to")
    cs = cl.add_subparsers(dest="action", required=True)
    add = cs.add_parser("add", help="add or replace a cluster")
    add.add_argument("name")
    add.add_argument("--creds", help="JSON file with KIBANA_URL, ES_URL, API_KEY: read in place, never copied")
    add.add_argument("--import", dest="import_file", help="copy KIBANA_URL, ES_URL and API_KEY from this JSON file into the key store")
    add.add_argument("--kibana-url")
    add.add_argument("--es-url")
    add.add_argument("--api-key-env", help="read the API key from this environment variable")
    add.add_argument("--space", dest="cluster_space", default="default")
    add.add_argument("--default-agent", default="elastic-ai-agent")
    add.add_argument("--use", action="store_true", help="make it the active cluster")
    cs.add_parser("list", help="list clusters")
    for name, helptext in (("use", "make a cluster the default"), ("rm", "remove a cluster and its stored key"),
                           ("test", "connect and report, without switching"), ("secure", "move a plaintext key into the Keychain")):
        sp = cs.add_parser(name, help=helptext)
        sp.add_argument("name", nargs="?" if name == "test" else None)

    ag = sub.add_parser("agent", help="agents as code: push, pull, diff")
    ags = ag.add_subparsers(dest="action", required=True)
    push = ags.add_parser("push", help="create or update an agent from a Markdown file")
    push.add_argument("file")
    push.add_argument("--dry-run", action="store_true", help="show the diff, change nothing")
    push.add_argument("--allow-risky", action="store_true", help="allow tools that can run or approve workflows")
    diff = ags.add_parser("diff", help="show how a file differs from the agent on the cluster")
    diff.add_argument("file")
    pull = ags.add_parser("pull", help="write agents on the cluster to Markdown files")
    pull.add_argument("agent_id", nargs="?")
    pull.add_argument("-o", "--out", default="agents")
    pull.add_argument("--all", action="store_true")
    pull.add_argument("--force", action="store_true")
    ags.add_parser("list", help="list agents")

    sub.add_parser("agents", help="list agents")
    sub.add_parser("models", help="list chat models")
    sub.add_parser("doctor", help="check connection, agents and models")
    ing = sub.add_parser("ingest", help="load local files into an eh-local-* index")
    ing.add_argument("path")
    ing.add_argument("--index")
    ing.add_argument("--tool", action="store_true", help="also create an index_search tool for it")
    ing.add_argument("--agent", dest="attach_agent", help="attach that tool to this agent")
    return p


def _cluster_cmd(cfg: Config, a: argparse.Namespace, ui: Ui) -> int:
    if a.action == "list":
        for r in clusters.rows(cfg):
            ui.console.print(f"{'[green]●[/green]' if r['active'] else ' '} {r['name']}  [dim]{r['host']} · space {r['space']} · key: {r['key']}[/dim]")
        return 0
    if a.action == "use":
        cfg.active = cfg.get(a.name).name
        cfg.save()
        ui.ok(f"active cluster: {cfg.active}")
        return 0
    if a.action == "rm":
        name = cfg.get(a.name).name
        cfg.remove(name)
        ui.ok(f"removed {name} (and its stored key)")
        return 0
    if a.action == "secure":
        ui.ok(f"{a.name}: key now in {cfg.secure(a.name)}")
        return 0
    if a.action == "test":
        info = clusters.test_cluster(cfg, a.name)
        ui.ok(f"{info['name']} · {info['host']} · Kibana {info['version']} · space {info['space']} · user {info.get('user')} · "
              f"{info['agents']} agents · {info['models']} models")
        return 0
    secret = (lambda _q: os.environ.get(a.api_key_env, "")) if a.api_key_env else getpass.getpass
    prof, where, version = clusters.add_cluster(cfg, a.name, creds=a.creds, import_file=a.import_file, kibana_url=a.kibana_url or "",
                                                es_url=a.es_url or "", space=a.cluster_space, default_agent=a.default_agent,
                                                make_active=True if a.use else None, ask=ui.ask, secret=secret)
    ui.ok(f"cluster {prof.name} added · key: {where} · Kibana {version}" + ("" if cfg.active == prof.name else f" · `eh cluster use {prof.name}` to make it the default"))
    return 0


def _agent_cmd(cfg: Config, a: argparse.Namespace, ui: Ui) -> int:
    client = Elastic(cfg.get(a.cluster))
    if a.space:
        client.space = a.space
    if a.action == "list":
        for ag in client.agents():
            ui.console.print(f"{ag['id']}  [dim]{ag.get('name', '')} · {(ag.get('access_control') or {}).get('access_mode', '')}[/dim]")
        return 0
    if a.action in ("push", "diff"):
        res = agentcode.push(client, a.file, confirm=lambda t, d: ui.confirm(t, d, allow_always=False) == "y",
                             say=lambda m: ui.console.print(m, markup=False, highlight=False),
                             allow_risky=getattr(a, "allow_risky", False), dry_run=a.action == "diff" or a.dry_run)
        ui.ok(f"{res['id']}: {res['action']}  [cluster {client.profile.name} · space {client.space}]")
        return 0
    if a.all:
        paths = agentcode.pull_all(client, a.out, force=a.force)
        ui.ok(f"pulled {len(paths)} agents into {a.out}/")
    elif a.agent_id:
        ui.ok(f"wrote {agentcode.pull(client, a.agent_id, a.out, force=a.force)}")
    else:
        raise ValueError("give an agent id, or --all")
    return 0


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    status = TabStatus()
    atexit.register(status.clear)  # never leave a stale "done" on the tab after we exit
    ui = Ui(debug=a.debug, auto_yes=a.yes, status=status)
    try:
        cfg = Config()
        if a.cmd in ("cluster", "profile"):
            return _cluster_cmd(cfg, a, ui)
        if a.cmd == "agent":
            return _agent_cmd(cfg, a, ui)
        repl = Repl(cfg, ui, profile=a.cluster, agent=a.agent, model=a.model, mode=a.mode, space=a.space)
        if a.cmd == "agents":
            repl.cmd_agents("")
        elif a.cmd == "models":
            repl.cmd_models("")
        elif a.cmd == "doctor":
            repl.cmd_doctor("")
        elif a.cmd == "ingest":
            ui.info(f"target: cluster {repl.raw.name} · space {repl.client.space}")
            res = ingest(repl.client, a.path, a.index, a.tool, a.attach_agent, say=ui.info)
            ui.ok("ingested {files} files → {chunks} chunks into {index} ({skipped} skipped, {failed} failed)".format(**res))
            if res.get("tool"):
                ui.ok(f"tool {res['tool']}" + (f" attached to {res['attached_to_agent']}" if res.get("attached_to_agent") else ""))
        elif a.prompt is not None:
            text = a.prompt if sys.stdin.isatty() else (a.prompt + "\n\n" + sys.stdin.read()).strip()
            repl.send(text)
        else:
            repl.run()
        return 0
    except (ConfigError, ValueError) as exc:
        ui.error(str(exc))
        return 2
    except (ApiError, OSError, PermissionError) as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
