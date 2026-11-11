"""Thread continuity across a context compaction, and the recently-closed safety net.

On 2026-11-02 a compaction minted a new session id mid-task: the owner's "envía la renuncia" and the agent's
"enviado a RRHH" landed under a session distinct from the one that opened the task, so Radar never closed it and
reminded hours later. And a lagging job result raised a just-finished MalCare subject again as a duplicate task.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from radar.pipeline import act
from radar.pipeline.lineage import host_activity_at, thread_sessions

NOW = "2026-11-02T12:00:00Z"
NOW_DT = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def _state_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, session_key TEXT, parent_session_id TEXT, started_at REAL)")
    conn.executemany("INSERT INTO sessions (id, session_key, parent_session_id, started_at) VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return str(path)


def test_thread_sessions_shares_session_key(tmp_path):
    # Three windows of one Telegram thread (compaction twice), all one session_key, plus an unrelated session.
    db = _state_db(tmp_path / "state.db", [
        ("s_root", "agent:main:telegram:dm:7", None, 1.0),
        ("s_mid", "agent:main:telegram:dm:7", "s_root", 2.0),
        ("s_new", "agent:main:telegram:dm:7", "s_mid", 3.0),
        ("other", "agent:main:telegram:dm:9", None, 4.0),
    ])
    ids = thread_sessions("s_new", db_path=db)
    assert ids[0] == "s_new"  # this window first
    assert set(ids) == {"s_root", "s_mid", "s_new"}  # the whole thread, not the unrelated session


def test_thread_sessions_walks_parents_without_key(tmp_path):
    db = _state_db(tmp_path / "state.db", [
        ("a", None, None, 1.0),
        ("b", None, "a", 2.0),
        ("c", None, "b", 3.0),
    ])
    assert thread_sessions("c", db_path=db) == ["c", "b", "a"]


def test_thread_sessions_unknown_or_missing_degrades_to_self(tmp_path):
    db = _state_db(tmp_path / "state.db", [("x", "k", None, 1.0)])
    assert thread_sessions("ghost", db_path=db) == ["ghost"]
    assert thread_sessions("x", db_path=str(tmp_path / "does-not-exist.db")) == ["x"]
    assert thread_sessions("", db_path=db) == []


def test_tasks_for_sessions_spans_the_thread(store):
    store.create_task(task_id="t_old", kind="commitment", title="Renuncia reconocimiento", summary="", entities={"session_id": "s_root"}, expectations=[], checklist=[], now=NOW)
    store.create_task(task_id="t_other", kind="trip", title="Berlín", summary="", entities={"session_id": "unrelated"}, expectations=[], checklist=[], now=NOW)
    found = store.tasks_for_sessions(["s_new", "s_mid", "s_root"], statuses=("open", "needs_user", "in_progress", "waiting", "snoozed"))
    assert [t["id"] for t in found] == ["t_old"]  # the task opened earlier in the thread is found from the new window
    assert store.tasks_for_sessions([]) == []
    assert store.tasks_for_session("s_root")[0]["id"] == "t_old"  # the single-session wrapper still works


def test_recently_resolved_matches_same_kind_and_subject(store):
    store.create_task(task_id="done1", kind="check", title="Vulnerabilidad crítica MalCare en Mediahub (31 oct)", summary="", entities={}, expectations=[], checklist=[], now=NOW)
    store.set_task_status("done1", "done", now=NOW)
    # A lagging job result raises the same subject again, same kind, different wording.
    assert act.recently_resolved(store, "check", "Actualizar MalCare vulnerable en Mediahub (Wordfence 31 oct)", NOW_DT) == "done1"
    # A different kind is not the same subject.
    assert act.recently_resolved(store, "work", "Actualizar MalCare vulnerable en Mediahub", NOW_DT) == ""
    # An unrelated subject does not match.
    assert act.recently_resolved(store, "check", "Reserva de hotel en Lisboa", NOW_DT) == ""


def test_recently_resolved_respects_the_cooldown(store):
    store.create_task(task_id="done2", kind="check", title="MalCare en Mediahub", summary="", entities={}, expectations=[], checklist=[], now=NOW)
    store.set_task_status("done2", "done", now=NOW)
    later = NOW_DT + timedelta(hours=act.RECENT_CLOSED_COOLDOWN_HOURS + 1)
    assert act.recently_resolved(store, "check", "MalCare en Mediahub otra vez", later) == ""
    still = NOW_DT + timedelta(hours=act.RECENT_CLOSED_COOLDOWN_HOURS - 1)
    assert act.recently_resolved(store, "check", "MalCare en Mediahub otra vez", still) == "done2"


def test_host_activity_counts_tool_work_and_turns_in_progress(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, session_key TEXT, parent_session_id TEXT, started_at REAL, last_activity_at REAL)")
    conn.execute("CREATE TABLE session_turn_leases (conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at REAL NOT NULL, expires_at REAL NOT NULL)")
    now = NOW_DT.timestamp()
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
        ("root", None, None, now - 7200, now - 3000),        # the window the task was born in: quiet for 50 min
        ("child", None, "root", now - 1800, now - 120),      # minted by a compaction; the agent made a tool call 2 min ago
        ("other", None, None, now - 600, now - 10),
    ])
    conn.commit()
    conn.close()
    # The thread's newest activity counts, not the first window's.
    assert host_activity_at(["root", "child"], now_dt=NOW_DT, db_path=str(path)) == NOW_DT - timedelta(seconds=120)
    assert host_activity_at(["root"], now_dt=NOW_DT, db_path=str(path)) == NOW_DT - timedelta(seconds=3000)
    assert host_activity_at(["nope"], now_dt=NOW_DT, db_path=str(path)) is None and host_activity_at([], now_dt=NOW_DT, db_path=str(path)) is None
    # A turn in progress (a lease still in force, held for the thread) means now, whatever the last activity says.
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO session_turn_leases VALUES (?,?,?,?)", ("root", "pid=1:turn=child:child:abc:platform=desktop", now - 30, now + 450))
    conn.commit()
    conn.close()
    assert host_activity_at(["root"], now_dt=NOW_DT, db_path=str(path)) == NOW_DT
    assert host_activity_at(["child"], now_dt=NOW_DT, db_path=str(path)) == NOW_DT  # named in the holder
    # An expired lease counts for nothing; a missing database for nothing either.
    assert host_activity_at(["other"], now_dt=NOW_DT + timedelta(hours=1), db_path=str(path)) == NOW_DT - timedelta(seconds=10)
    assert host_activity_at(["root"], now_dt=NOW_DT, db_path=str(tmp_path / "missing.db")) is None
