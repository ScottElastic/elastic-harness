"""Cluster API keys live in the macOS Keychain when it is available (via the `keyring` package).

The config file then holds only non-secret metadata. Where there is no keychain (Linux without a secret
service, or `keyring` not installed) callers fall back to the mode-600 config file.
"""

from __future__ import annotations

import os

SERVICE = os.environ.get("EH_KEYCHAIN_SERVICE", "elastic-harness")

_backend = None
_checked = False


def use_backend(obj) -> None:
    """Inject a backend with get_password/set_password/delete_password (tests)."""
    global _backend, _checked
    _backend, _checked = obj, True


def _load():
    global _backend, _checked
    if _checked:
        return _backend
    _checked = True
    try:
        import keyring
        from keyring.backends import fail

        _backend = None if isinstance(keyring.get_keyring(), fail.Keyring) else keyring
    except Exception:  # not installed, or no usable backend
        _backend = None
    return _backend


def available() -> bool:
    return _load() is not None


def get(name: str) -> str | None:
    b = _load()
    if b is None:
        return None
    try:
        return b.get_password(SERVICE, name)
    except Exception:
        return None


def set(name: str, key: str) -> bool:  # noqa: A001 - mirrors keyring's vocabulary
    b = _load()
    if b is None:
        return False
    try:
        b.set_password(SERVICE, name, key)
        return True
    except Exception:
        return False


def delete(name: str) -> None:
    b = _load()
    if b is None:
        return
    try:
        b.delete_password(SERVICE, name)
    except Exception:
        pass
