-- A compaction ends one session and continues the conversation in another. The host tells radar at that moment
-- (the post_compaction hook), and radar keeps the link itself, so a task opened before a compaction and the turn
-- that resolves it afterwards are one conversation without reading the host's own database.
CREATE TABLE IF NOT EXISTS session_links (
 profile_key TEXT NOT NULL, owner_id TEXT NOT NULL, old_session_id TEXT NOT NULL, session_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY (profile_key, owner_id, old_session_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_session_links_new ON session_links(profile_key, owner_id, session_id);
