-- Per-repository switch for the download of release files (attached files and
-- GitHub's source archives). Off by default: release files can be very large.
ALTER TABLE repos ADD COLUMN assets_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE repos ADD COLUMN assets_sync_at REAL;
ALTER TABLE repos ADD COLUMN assets_status TEXT;
ALTER TABLE repos ADD COLUMN assets_message TEXT;
ALTER TABLE repos ADD COLUMN assets_files INTEGER;
ALTER TABLE repos ADD COLUMN assets_bytes INTEGER;
