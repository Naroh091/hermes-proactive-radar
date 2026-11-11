"""Puts the real Hermes host within reach of a contract test, or skips the module.

Contract tests run against an actual checkout, under its own interpreter, with an empty ``HERMES_HOME``::

    uv pip install --target .contract-deps --python ~/.hermes/hermes-agent/venv/bin/python pytest
    HERMES_AGENT_DIR=~/.hermes/hermes-agent PYTHONPATH=.contract-deps \
        ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/contract

Nothing happens without ``HERMES_AGENT_DIR``: the unit suite assumes the host is absent, so the two never share a run.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

HOST = os.environ.get("HERMES_AGENT_DIR", "")
if not HOST:
    pytest.skip("HERMES_AGENT_DIR not set: host contracts need a real Hermes checkout", allow_module_level=True)

os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="radar-contract-home-")
os.environ.setdefault("HERMES_PROFILE", "default")
for path in (str(Path(HOST).expanduser()), str(Path(__file__).resolve().parents[2])):
    if path not in sys.path:
        sys.path.insert(0, path)
