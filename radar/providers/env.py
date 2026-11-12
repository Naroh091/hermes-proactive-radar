"""The environment Radar gives a third-party binary it starts (``gh``, ``gws``, a mail CLI).

The gateway's own environment carries every provider key Hermes holds (model APIs, bot tokens). A tool that reads
mail or lists pull requests has no business seeing them, so it gets an explicit, minimal environment: where to find
programs and its own configuration, the locale, and the variables that belong to that tool.
"""
from __future__ import annotations

import os
from typing import Mapping

BASE = ("PATH", "HOME", "LANG", "LANGUAGE", "XDG_CONFIG_HOME")
BASE_PREFIXES = ("LC_",)


def tool_env(prefixes: tuple[str, ...] = (), extra: Mapping[str, str] | None = None, *, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """``PATH``, ``HOME``, the locale and ``XDG_CONFIG_HOME`` from the parent, plus the variables whose name starts
    with one of ``prefixes`` (the tool's own: ``GH_``, ``GOOGLE_WORKSPACE_CLI_``), plus ``extra``. Nothing else."""
    parent = os.environ if source is None else source
    keep = BASE_PREFIXES + tuple(prefixes)
    env = {name: value for name, value in parent.items() if name in BASE or name.startswith(keep)}
    env.update(extra or {})
    return env
