BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS performance_platforms (
    platform TEXT PRIMARY KEY, label TEXT NOT NULL);
INSERT OR IGNORE INTO performance_platforms VALUES
    ('instagram_reels','Instagram Reels'),('tiktok','TikTok'),('youtube_shorts','YouTube Shorts');
CREATE TABLE IF NOT EXISTS performance_posts (
    post_id TEXT PRIMARY KEY,
    clip_id TEXT NOT NULL REFERENCES clips(clip_id),
    platform TEXT NOT NULL REFERENCES performance_platforms(platform),
    account TEXT NOT NULL, posted_at TEXT NOT NULL,
    post_reference TEXT NOT NULL, features_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(clip_id,platform,account,posted_at));
CREATE TABLE IF NOT EXISTS performance_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES performance_posts(post_id),
    captured_at TEXT NOT NULL, age_hours REAL NOT NULL CHECK(age_hours>=0),
    raw_metrics_json TEXT NOT NULL, derived_metrics_json TEXT NOT NULL,
    extraction_json TEXT NOT NULL, import_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, UNIQUE(post_id,captured_at));
CREATE TABLE IF NOT EXISTS performance_images (
    snapshot_id TEXT NOT NULL REFERENCES performance_snapshots(snapshot_id),
    image_index INTEGER NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL,
    sha256 TEXT NOT NULL, content BLOB NOT NULL,
    PRIMARY KEY(snapshot_id,image_index));
CREATE INDEX IF NOT EXISTS performance_posts_cohort ON performance_posts(platform,account,posted_at);
CREATE INDEX IF NOT EXISTS performance_snapshots_post ON performance_snapshots(post_id,age_hours);
PRAGMA user_version=2;
COMMIT;
