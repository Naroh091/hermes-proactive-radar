-- What Radar has learnt about the owner's habits from their own mail and calendar (the warmup pass) and from
-- what it observes: who they correspond with, which kinds of event they attend, which charges recur.
CREATE TABLE IF NOT EXISTS habits (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 kind TEXT NOT NULL, key TEXT NOT NULL, value_json TEXT NOT NULL, origin TEXT NOT NULL,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, kind, key)
);
CREATE INDEX IF NOT EXISTS idx_habits_kind ON habits(profile_key, owner_id, kind);
