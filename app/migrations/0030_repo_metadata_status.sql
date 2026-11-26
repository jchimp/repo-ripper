-- Result of the last metadata capture (issues, pull requests, comments) per
-- repository. The data itself is in JSON files next to the mirror.
ALTER TABLE repos ADD COLUMN meta_sync_at REAL;
-- ok | partial | rate_limited | error
ALTER TABLE repos ADD COLUMN meta_status TEXT;
ALTER TABLE repos ADD COLUMN meta_message TEXT;
ALTER TABLE repos ADD COLUMN meta_issues INTEGER;
ALTER TABLE repos ADD COLUMN meta_pulls INTEGER;
