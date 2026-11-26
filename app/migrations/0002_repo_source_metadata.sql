-- source: 'mine' = found by scanning the token's account,
--         'watched' = a public repository added by hand.
ALTER TABLE repos ADD COLUMN source TEXT NOT NULL DEFAULT 'mine';
-- Per-repository switch for capturing PRs, issues and comments.
ALTER TABLE repos ADD COLUMN metadata_enabled INTEGER NOT NULL DEFAULT 1;
