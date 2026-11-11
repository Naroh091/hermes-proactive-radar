"""Credentials come from the host, never from a file the plugin opens.

``read_secret`` resolves a variable through the host's ``agent.secret_scope.get_secret``, which honours the active
profile's scope (the owner's secret store, the profile's own values under a multiplexed gateway). The plugin never
reads or writes ``.env``, ``auth.json`` or another tool's login. Outside the host (unit tests, a bare import) the
process environment is the only source there is.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def read_secret(name: str, default: str = "") -> str:
    try:
        from agent.secret_scope import get_secret  # type: ignore[import-not-found]  # host package
    except ImportError:
        return os.environ.get(name, default)
    try:
        value = get_secret(name, default)
    except Exception as exc:  # no profile scope on a multiplexed gateway: fail closed, never another profile's value
        log.warning("radar could not read %s from the host's secret scope: %s", name, type(exc).__name__)
        return default
    return default if value is None else str(value)
