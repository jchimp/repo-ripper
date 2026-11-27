-- Size on disk of <name>.meta/ without assets/ (release files have their own
-- column, assets_bytes). The metadata job records it after each capture.
-- NULL = not measured yet.
ALTER TABLE repos ADD COLUMN meta_bytes INTEGER;
