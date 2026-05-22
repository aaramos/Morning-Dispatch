SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS profiles (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  is_default  INTEGER DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
  id           TEXT PRIMARY KEY,
  profile_id   TEXT NOT NULL REFERENCES profiles(id),
  name         TEXT NOT NULL,
  interest     TEXT NOT NULL,
  schedule     TEXT NOT NULL CHECK(schedule IN ('hourly','daily','weekly','monthly')),
  sources      TEXT NOT NULL,
  status       TEXT DEFAULT 'active',
  threshold    REAL DEFAULT 0.45,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_runs (
  id              TEXT PRIMARY KEY,
  digest_id       TEXT NOT NULL REFERENCES digests(id),
  inference_run_id TEXT,
  run_at          TEXT NOT NULL,
  lookback_days   INTEGER NOT NULL,
  item_count      INTEGER DEFAULT 0,
  failed_count    INTEGER DEFAULT 0,
  fallback_count  INTEGER DEFAULT 0,
  newsletter_count INTEGER DEFAULT 0,
  link_count       INTEGER DEFAULT 0,
  fetched_article_count INTEGER DEFAULT 0,
  model_cache_hit_count INTEGER DEFAULT 0,
  model_cache_miss_count INTEGER DEFAULT 0,
  model_cache_write_count INTEGER DEFAULT 0,
  duration_seconds REAL,
  trigger         TEXT DEFAULT 'manual',
  cold_start      INTEGER DEFAULT 0,
  partial         INTEGER DEFAULT 0,
  status          TEXT DEFAULT 'pending',
  snapshot        TEXT,
  completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS inference_metrics (
  id                    TEXT PRIMARY KEY,
  run_id                TEXT NOT NULL,
  article_id            TEXT NOT NULL,
  ts                    TEXT NOT NULL,
  model                 TEXT NOT NULL,
  model_tag             TEXT,
  quantization          TEXT,
  backend               TEXT,
  mode                  TEXT NOT NULL,
  queue_wait_ms         INTEGER,
  ttft_ms               INTEGER,
  generation_ms         INTEGER,
  total_ms              INTEGER NOT NULL,
  prompt_tokens         INTEGER,
  completion_tokens     INTEGER,
  tokens_per_sec        REAL,
  classification_label  TEXT,
  classification_confidence REAL,
  schema_valid          INTEGER NOT NULL DEFAULT 0,
  summary_word_count    INTEGER,
  fallback_triggered    INTEGER NOT NULL DEFAULT 0,
  status                TEXT NOT NULL CHECK(status IN (
    'success',
    'timeout',
    'parse_error',
    'empty_output',
    'truncated',
    'rate_limited',
    'model_capacity',
    'http_error',
    'model_error'
  )),
  error_detail          TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS model_enrichment_jobs (
  id                TEXT PRIMARY KEY,
  model_name        TEXT NOT NULL,
  status            TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
  limit_count       INTEGER NOT NULL,
  include_cached    INTEGER NOT NULL DEFAULT 0,
  processed_count   INTEGER NOT NULL DEFAULT 0,
  success_count     INTEGER NOT NULL DEFAULT 0,
  cache_hit_count   INTEGER NOT NULL DEFAULT 0,
  failure_count     INTEGER NOT NULL DEFAULT 0,
  avg_total_ms      REAL,
  estimated_100_seconds REAL,
  error_detail      TEXT,
  created_at        TEXT NOT NULL,
  started_at        TEXT,
  completed_at      TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS articles (
  id              TEXT PRIMARY KEY,
  canonical_url   TEXT UNIQUE,
  original_url    TEXT,
  domain          TEXT,
  publisher       TEXT,
  author          TEXT,
  published_at    TEXT,
  title           TEXT,
  cleaned_text    TEXT,
  summary         TEXT,
  keywords        TEXT,
  content_type    TEXT,
  embedding       BLOB,
  fetch_status    TEXT NOT NULL DEFAULT 'fetched',
  quality_flag    TEXT DEFAULT 'ok',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_enrichment_cache (
  id               TEXT PRIMARY KEY,
  cache_key        TEXT NOT NULL UNIQUE,
  canonical_url    TEXT NOT NULL,
  source_text_hash TEXT NOT NULL,
  model_name       TEXT NOT NULL,
  title            TEXT NOT NULL,
  summary          TEXT NOT NULL,
  keywords         TEXT NOT NULL,
  content_type     TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_discoveries (
  id                    TEXT PRIMARY KEY,
  article_id            TEXT NOT NULL REFERENCES articles(id),
  discovery_source_type TEXT NOT NULL,
  discovery_source_name TEXT NOT NULL,
  sender_email          TEXT,
  message_id            TEXT,
  thread_id             TEXT,
  issue_date            TEXT,
  link_text             TEXT,
  newsletter_snippet    TEXT,
  discovered_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_items (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES digest_runs(id),
  digest_id       TEXT NOT NULL REFERENCES digests(id),
  article_id      TEXT NOT NULL REFERENCES articles(id),
  discovery_id    TEXT REFERENCES article_discoveries(id),
  relevance_score REAL,
  tier            TEXT DEFAULT 'main',
  section         TEXT,
  editor_summary  TEXT,
  editor_note     TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_decisions (
  id               TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES digest_runs(id),
  digest_id        TEXT NOT NULL REFERENCES digests(id),
  inference_run_id TEXT,
  agent            TEXT NOT NULL,
  target           TEXT NOT NULL,
  decision         TEXT NOT NULL,
  action           TEXT DEFAULT 'none',
  confidence       REAL,
  reason           TEXT,
  model_name       TEXT,
  metadata         TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_issues (
  id           TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES digest_runs(id),
  digest_id    TEXT NOT NULL REFERENCES digests(id),
  title        TEXT NOT NULL,
  snapshot     TEXT,
  html_path    TEXT,
  html_content TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  id             TEXT PRIMARY KEY,
  digest_item_id TEXT NOT NULL REFERENCES digest_items(id),
  article_id     TEXT NOT NULL REFERENCES articles(id),
  digest_id      TEXT NOT NULL REFERENCES digests(id),
  signal         TEXT NOT NULL CHECK(signal IN ('up','down')),
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_watermarks (
  digest_id      TEXT NOT NULL,
  source_key     TEXT NOT NULL,
  last_fetched   TEXT NOT NULL,
  last_id        TEXT,
  PRIMARY KEY (digest_id, source_key)
);

CREATE TABLE IF NOT EXISTS source_weights (
  digest_id   TEXT NOT NULL,
  source_name TEXT NOT NULL,
  weight      REAL DEFAULT 1.0,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (digest_id, source_name)
);

CREATE TABLE IF NOT EXISTS reddit_sources (
  id                    TEXT PRIMARY KEY,
  digest_id             TEXT NOT NULL REFERENCES digests(id),
  subreddit             TEXT NOT NULL,
  state                 TEXT NOT NULL CHECK(state IN ('active','search_only','candidate','retired')),
  category              TEXT,
  score                 REAL NOT NULL DEFAULT 0,
  reason                TEXT,
  last_reviewed_at      TEXT,
  last_seen_post_at     TEXT,
  consecutive_stale_runs INTEGER NOT NULL DEFAULT 0,
  metadata              TEXT NOT NULL DEFAULT '{}',
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  UNIQUE(digest_id, subreddit)
) STRICT;

CREATE TABLE IF NOT EXISTS source_scout_runs (
  id               TEXT PRIMARY KEY,
  digest_id        TEXT NOT NULL REFERENCES digests(id),
  run_at           TEXT NOT NULL,
  status           TEXT NOT NULL CHECK(status IN ('completed','partial','failed')),
  sampled_count    INTEGER NOT NULL DEFAULT 0,
  active_count     INTEGER NOT NULL DEFAULT 0,
  candidate_count  INTEGER NOT NULL DEFAULT 0,
  retired_count    INTEGER NOT NULL DEFAULT 0,
  summary          TEXT,
  error_detail     TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS source_scout_decisions (
  id               TEXT PRIMARY KEY,
  scout_run_id     TEXT NOT NULL REFERENCES source_scout_runs(id),
  digest_id        TEXT NOT NULL REFERENCES digests(id),
  agent            TEXT NOT NULL DEFAULT 'source_scout',
  subreddit        TEXT NOT NULL,
  decision         TEXT NOT NULL,
  action           TEXT NOT NULL DEFAULT 'none',
  confidence       REAL,
  reason           TEXT,
  metadata         TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_digests_profile_id ON digests(profile_id);
CREATE INDEX IF NOT EXISTS idx_digest_runs_digest_id ON digest_runs(digest_id);
CREATE INDEX IF NOT EXISTS idx_inference_metrics_run_id ON inference_metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_inference_metrics_article_id ON inference_metrics(article_id);
CREATE INDEX IF NOT EXISTS idx_inference_metrics_model_ts ON inference_metrics(model, ts);
CREATE INDEX IF NOT EXISTS idx_model_enrichment_jobs_created_at ON model_enrichment_jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_digest_issues_digest_id ON digest_issues(digest_id);
CREATE INDEX IF NOT EXISTS idx_digest_items_digest_id ON digest_items(digest_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_run_id ON agent_decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_digest_id ON agent_decisions(digest_id);
CREATE INDEX IF NOT EXISTS idx_discoveries_article_id ON article_discoveries(article_id);
CREATE INDEX IF NOT EXISTS idx_model_enrichment_cache_url ON model_enrichment_cache(canonical_url);
CREATE INDEX IF NOT EXISTS idx_model_enrichment_cache_model ON model_enrichment_cache(model_name);
CREATE INDEX IF NOT EXISTS idx_reddit_sources_digest_id ON reddit_sources(digest_id);
CREATE INDEX IF NOT EXISTS idx_reddit_sources_state ON reddit_sources(state);
CREATE INDEX IF NOT EXISTS idx_source_scout_runs_digest_id ON source_scout_runs(digest_id);
CREATE INDEX IF NOT EXISTS idx_source_scout_decisions_digest_id ON source_scout_decisions(digest_id);
"""
