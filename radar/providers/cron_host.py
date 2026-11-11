"""The host's cron as a provider: create scheduled jobs through the host's own cron tool and read their outputs.

Jobs are created and read back with ``cronjob_manage``, dispatched through the plugin context (the same tool the
agent uses), so the host validates the schedule and the delivery target and the plugin imports nothing of the
host's cron internals. Outputs are the files the host writes under ``<hermes home>/cron/output``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

TOOL = "cronjob_manage"


def deliver_target(origin: dict[str, Any] | None) -> str:
    """``platform:chat_id[:thread_id]`` for the destination a job reports to; empty (the host's default) without one."""
    if not origin or not origin.get("chat_id"):
        return ""
    parts = [str(origin.get("platform") or "telegram"), str(origin["chat_id"])]
    if origin.get("thread_id"):
        parts.append(str(origin["thread_id"]))
    return ":".join(parts)


class HostCronProvider:
    provider_id = "cron_host"

    def __init__(self, *, allow_write: bool = False, dispatch: Callable[[str, dict[str, Any]], Any] | None = None,
                 hermes_home: str | Path | None = None):
        self.read_only = not allow_write
        self._dispatch = dispatch
        self._home = hermes_home

    def _call(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._dispatch is None:
            raise RuntimeError("the host's cron tool is not reachable from here")
        raw = self._dispatch(TOOL, args)
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise RuntimeError("the host's cron tool returned no result")
        if payload.get("error") or payload.get("success") is False:
            raise RuntimeError(str(payload.get("error") or payload.get("message") or "the host's cron tool refused")[:300])
        return payload

    def create(self, *, prompt: str, schedule: str, name: str, origin: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.read_only:
            raise PermissionError("cron.create is not permitted on this provider")
        args: dict[str, Any] = {"action": "create", "prompt": prompt, "schedule": schedule, "name": name}
        target = deliver_target(origin)
        if target:
            args["deliver"] = target
        record = self._call(args)
        job = record.get("job") if isinstance(record.get("job"), dict) else {}
        return {"id": str(record.get("job_id") or job.get("job_id") or ""), "name": str(record.get("name") or job.get("name") or name), "schedule": schedule}

    def get(self, job_id: str) -> dict[str, Any] | None:
        listed = self._call({"action": "list", "include_disabled": True})
        for job in listed.get("jobs") or []:
            if isinstance(job, dict) and str(job.get("job_id") or "") == str(job_id):
                return dict(job)
        return None

    def output_dir(self) -> Path:
        home = self._home
        if home is None:
            try:
                from hermes_constants import get_hermes_home  # type: ignore[import-not-found]  # host package (public)

                home = get_hermes_home()
            except ImportError:
                home = Path.home() / ".hermes"
        return Path(home) / "cron" / "output"
