"""The whole chat thread behind one session id.

A context compaction ends a session and starts a new one (``sessions.parent_session_id`` links them, and the
whole chain keeps one stable ``sessions.session_key``). So a task opened before a compaction and the turn that
resolves it after one live under *different* session ids: on 2026-11-02 the owner's "envía la renuncia" and the
agent's "enviado a RRHH" landed in a session minted at 03:03, while the task had been opened in the session
before it, and Radar — correlating by the per-window session id — never connected them and reminded hours later.

``thread_sessions`` reads the host's ``state.db`` (read-only) and returns every session id of the same thread,
so correlation spans the conversation, not one window. It never raises: an unknown or unreachable lineage
degrades to ``[session_id]`` (today's behaviour)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_SESSIONS = 40


def _state_db_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / "state.db"


def _connect(db_path: str | os.PathLike[str] | None, *, live: bool = False) -> sqlite3.Connection | None:
    """Read-only handle on the host's state.db. ``immutable`` skips locking and never sees the WAL: fine for lineage
    (compactions are old news), wrong for what is happening right now, which ``live`` reads with ordinary locking."""
    path = Path(db_path) if db_path is not None else _state_db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro" + ("" if live else "&immutable=1"), uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def host_activity_at(session_ids: list[str], *, now_dt: datetime | None = None, db_path: str | os.PathLike[str] | None = None) -> datetime | None:
    """When the host last saw one of these sessions do anything: the newest ``sessions.last_activity_at`` (the host
    updates it on every tool call, so a long turn of browser work counts even though no turn is recorded until it
    ends), or now when a turn lease (``session_turn_leases``) held for one of them is still in force. None when the
    host records nothing or the database is unreachable; never raises. On 2026-11-08 Radar judged a chat 34 minutes
    after its last recorded turn while the agent was still in that chat buying a domain, and asked the owner whether
    to buy it in the very minute the purchase went through."""
    ids = [str(s) for s in session_ids if s]
    if not ids:
        return None
    now = now_dt or datetime.now(timezone.utc)
    conn = None
    try:
        conn = _connect(db_path, live=True)
        if conn is None:
            return None
        marks = ",".join("?" for _ in ids)
        latest: datetime | None = None
        row = conn.execute(f"SELECT MAX(last_activity_at) AS at FROM sessions WHERE id IN ({marks})", ids).fetchone()
        if row is not None and row["at"]:
            latest = datetime.fromtimestamp(float(row["at"]), tz=timezone.utc)
        try:
            leases = conn.execute("SELECT conversation_id, holder, expires_at FROM session_turn_leases WHERE expires_at > ?", (now.timestamp(),)).fetchall()
        except sqlite3.Error:
            leases = []
        for lease in leases:
            holder = str(lease["holder"] or "")
            if str(lease["conversation_id"] or "") in ids or any(sid in holder for sid in ids):
                return now  # a turn is in progress right now
        return latest
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def thread_sessions(session_id: str, *, db_path: str | os.PathLike[str] | None = None) -> list[str]:
    """Every session id of the thread ``session_id`` belongs to, this one first.

    The thread is the set of sessions sharing one ``session_key`` (the stable identity across compactions). When
    that is unknown the ``parent_session_id`` chain is walked instead, upward to the first window and downward to the
    ones minted later. On any error the result is ``[session_id]``.
    """
    sid = str(session_id or "")
    if not sid:
        return []
    conn = None
    try:
        conn = _connect(db_path)
        if conn is None:
            return [sid]
        row = conn.execute("SELECT session_key FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return [sid]
        key = row["session_key"]
        if key:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE session_key = ? ORDER BY started_at LIMIT ?",
                (key, MAX_SESSIONS),
            ).fetchall()
            ids = [str(r["id"]) for r in rows if r["id"]]
            if sid in ids:
                return [sid] + [i for i in ids if i != sid]
            return [sid] + ids
        # No session_key: walk parents (compaction links) upward...
        chain = [sid]
        seen = {sid}
        current = sid
        for _ in range(MAX_SESSIONS):
            parent = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
            pid = str(parent["parent_session_id"]) if parent and parent["parent_session_id"] else ""
            if not pid or pid in seen:
                break
            chain.append(pid)
            seen.add(pid)
            current = pid
        # ...and downward: the windows a compaction minted after any of these. A task born in an earlier window must
        # see the thread's later activity (2026-11-08: the agent bought the domain in the window after the one that
        # opened the task, and the task's window looked quiet).
        frontier = list(chain)
        while frontier and len(chain) < MAX_SESSIONS:
            marks = ",".join("?" for _ in frontier)
            rows = conn.execute(f"SELECT id FROM sessions WHERE parent_session_id IN ({marks}) ORDER BY started_at", frontier).fetchall()
            frontier = [str(r["id"]) for r in rows if r["id"] and str(r["id"]) not in seen]
            for child in frontier:
                seen.add(child)
                chain.append(child)
        return chain[:MAX_SESSIONS]
    except Exception:
        return [sid]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
