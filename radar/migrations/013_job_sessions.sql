-- The session a delegated job runs in is learnt from the gateway's hooks while the job runs (its prompt carries the
-- job mark); the hooks look sessions up by it on every turn and tool call.
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(profile_key, owner_id, session_ref);
