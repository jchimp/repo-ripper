-- Totals of metadata and release files at the time of each pull job, for the
-- chart on the Storage page. NULL in rows from before this migration.
ALTER TABLE sync_history ADD COLUMN meta_bytes INTEGER;
ALTER TABLE sync_history ADD COLUMN assets_bytes INTEGER;
