-- Hackathon review-platform slice (Yelp + Goodreads demo items)

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL,
    persona_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL,
    name TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    dataset TEXT NOT NULL,
    stars REAL NOT NULL CHECK (stars >= 1 AND stars <= 5),
    text TEXT NOT NULL DEFAULT '',
    reviewed_at TIMESTAMPTZ,
    is_holdout BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id, is_holdout);
CREATE INDEX IF NOT EXISTS idx_reviews_item ON reviews(item_id);
CREATE INDEX IF NOT EXISTS idx_items_dataset ON items(dataset);
CREATE INDEX IF NOT EXISTS idx_users_dataset ON users(dataset);
