"""Profiles: which cluster, space, agent and model the harness talks to.

Config lives in ~/.config/elastic-harness/config.json (mode 600). A profile can
point at an existing creds file (``creds_file``) that is read in place and never
copied. Environment variables override everything:

    EH_KIBANA_URL, EH_ES_URL, EH_API_KEY, EH_SPACE
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("EH_CONFIG_DIR", "~/.config/elastic-harness")).expanduser()
STATE_DIR = Path(os.environ.get("EH_STATE_DIR", "~/.local/state/elastic-harness")).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.json"

# keys we accept in a creds file (the same names the demo projects use)
_CREDS_KEYS = {"KIBANA_URL": "kibana_url", "ES_URL": "es_url", "API_KEY": "api_key"}


class ConfigError(Exception):
    pass


@dataclass
class Profile:
    name: str
    kibana_url: str = ""
    es_url: str = ""
    api_key: str = ""
    space: str = "default"
    creds_file: str = ""  # read in place, never copied
    default_agent: str = "elastic-ai-agent"
    default_model: str = ""
    mode: str = "agent"  # agent | local
    extra: dict = field(default_factory=dict)

    def resolved(self) -> "Profile":
        """Return a copy with credentials resolved: env > creds_file > stored values."""
        p = Profile(**{k: getattr(self, k) for k in self.__dataclass_fields__})
        if p.creds_file:
            path = Path(p.creds_file).expanduser()
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise ConfigError(f"cannot read creds_file {path}: {exc}") from exc
            for src, dst in _CREDS_KEYS.items():
                if data.get(src):
                    setattr(p, dst, data[src])
        for env, dst in (("EH_KIBANA_URL", "kibana_url"), ("EH_ES_URL", "es_url"),
                         ("EH_API_KEY", "api_key"), ("EH_SPACE", "space")):
            if os.environ.get(env):
                setattr(p, dst, os.environ[env])
        p.kibana_url = p.kibana_url.rstrip("/")
        p.es_url = p.es_url.rstrip("/")
        return p

    def to_json(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("name", "extra")}
        d.update(self.extra)
        # never persist a key that came from a creds file or the environment
        if self.creds_file:
            d["api_key"] = ""
        return {k: v for k, v in d.items() if v not in ("", None)}


class Config:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.active = ""
        self.profiles: dict[str, Profile] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except ValueError as exc:
            raise ConfigError(f"{self.path} is not valid JSON: {exc}") from exc
        self.active = data.get("active", "")
        known = set(Profile.__dataclass_fields__) - {"name", "extra"}
        for name, raw in data.get("profiles", {}).items():
            fields = {k: v for k, v in raw.items() if k in known}
            extra = {k: v for k, v in raw.items() if k not in known}
            self.profiles[name] = Profile(name=name, extra=extra, **fields)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"active": self.active, "profiles": {n: p.to_json() for n, p in self.profiles.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 600
        tmp.replace(self.path)

    def add(self, profile: Profile, make_active: bool = True) -> None:
        self.profiles[profile.name] = profile
        if make_active or not self.active:
            self.active = profile.name
        self.save()

    def get(self, name: str | None = None) -> Profile:
        name = name or self.active
        if not name or name not in self.profiles:
            have = ", ".join(self.profiles) or "none"
            raise ConfigError(
                f"no profile {name!r} (have: {have}). Create one: eh profile add NAME --creds FILE"
            )
        return self.profiles[name]
