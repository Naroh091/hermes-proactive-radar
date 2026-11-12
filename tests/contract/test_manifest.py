"""The plugin's own contract with the host: what ``plugin.yaml`` declares is what ``register`` provides."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
yaml = pytest.importorskip("yaml")

import radar  # noqa: E402


class Recorder:
    profile_name = "contract"
    llm = None

    def __init__(self):
        self.tools, self.hooks, self.commands, self.aux = [], [], [], []

    def register_tool(self, name, **_):
        self.tools.append(name)

    def register_hook(self, name, callback, **_):
        self.hooks.append(name)

    def register_command(self, name, *a, **_):
        self.commands.append(name)

    def register_cli_command(self, *a, **_):
        pass

    def register_auxiliary_task(self, name, *a, **_):
        self.aux.append(name)

    def register_platform_handler(self, *a, **_):
        pass

    def on_unload(self, *a, **_):
        pass

    def get_config(self, key, default=None):
        return default

    def set_config(self, *a, **_):
        pass

    def __getattr__(self, name):  # any further capability is a no-op here
        return lambda *a, **k: None


@pytest.fixture(scope="module")
def manifest():
    return yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def registered():
    ctx = Recorder()
    radar.register(ctx)
    return ctx


def test_every_tool_registered_is_declared_and_the_other_way_round(manifest, registered):
    assert sorted(registered.tools) == sorted(manifest["provides_tools"])


def test_every_hook_registered_is_declared_and_the_other_way_round(manifest, registered):
    assert sorted(set(registered.hooks)) == sorted(manifest["provides_hooks"])


def test_the_versions_agree():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))["version"]
    assert f'version = "{declared}"' in project


def test_the_manifest_asks_hermes_for_the_capabilities_radar_uses():
    """The model fallback is granted by Hermes at install/enable, not written by the plugin (catalog review, 2026-11-11)."""
    from radar.identity import PLUGIN_ID
    from radar.llm import OVERRIDE_CAPABILITIES

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_ID == "hermes-proactive-radar"
    assert sorted(manifest["capabilities"]) == sorted(OVERRIDE_CAPABILITIES)
    capabilities = pytest.importorskip("hermes_cli.plugin_capabilities")
    known = str(vars(capabilities))
    assert all(capability in known for capability in OVERRIDE_CAPABILITIES)  # ids this host can grant
    import json

    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))["name"] == PLUGIN_ID
