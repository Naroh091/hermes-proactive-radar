"""The name Hermes knows this plugin by.

It is the ``name`` of ``plugin.yaml`` and of ``dashboard/manifest.json``, and the host derives from it the config
entry (``plugins.entries.<id>``), the ``plugins.enabled`` item and the panel's API prefix (``/api/plugins/<id>``).
The Python package, the tools (``radar_*``), the ``/radar`` command and the data directory keep the short name.
"""
PLUGIN_ID = "hermes-proactive-radar"
ENTRY = f"plugins.entries.{PLUGIN_ID}"
