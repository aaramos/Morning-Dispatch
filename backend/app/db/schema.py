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
  schedule     TEXT NOT NULL CHECK(schedule IN ('hourly','daily','weekdays','weekly','monthly')),
  sources      TEXT NOT NULL,
  status       TEXT DEFAULT 'active',
  threshold    REAL DEFAULT 0.45,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_profiles (
  topic_id        TEXT PRIMARY KEY,
  statement       TEXT NOT NULL,
  profile_json    TEXT NOT NULL,
  schedule        TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS refinement_sessions (
  session_id     TEXT PRIMARY KEY,
  statement      TEXT NOT NULL,
  profile_json   TEXT NOT NULL,
  messages_json  TEXT NOT NULL,
  pending_field  TEXT,
  turn_count     INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL CHECK(status IN ('active','finalized')),
  topic_id       TEXT REFERENCES topic_profiles(topic_id),
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS explorations (
  exploration_id         TEXT PRIMARY KEY,
  topic_id               TEXT NOT NULL REFERENCES topic_profiles(topic_id),
  mode                   TEXT NOT NULL CHECK(mode IN ('show_now','scheduled')),
  source_selection_json  TEXT NOT NULL,
  progress_json          TEXT NOT NULL DEFAULT '{}',
  status                 TEXT NOT NULL CHECK(status IN ('queued','running','complete','failed')),
  brief_ref              TEXT,
  emailed                INTEGER NOT NULL DEFAULT 0,
  started_at             TEXT NOT NULL,
  finished_at            TEXT,
  deleted_at             TEXT,
  delete_after           TEXT,
  purged_at              TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS promoted_sources (
  id          TEXT PRIMARY KEY,
  topic_id    TEXT NOT NULL REFERENCES topic_profiles(topic_id),
  adapter     TEXT NOT NULL,
  ref         TEXT NOT NULL,
  has_feed    INTEGER NOT NULL DEFAULT 0,
  feed_url    TEXT,
  created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS collection_files (
  id                TEXT PRIMARY KEY,
  collection_name   TEXT NOT NULL,
  file_path         TEXT NOT NULL UNIQUE,
  relative_path     TEXT NOT NULL,
  file_type         TEXT NOT NULL,
  last_modified     REAL NOT NULL,
  last_indexed      REAL,
  status            TEXT NOT NULL CHECK(status IN ('pending','indexed','failed','unsupported')),
  error_message     TEXT,
  chunk_count       INTEGER NOT NULL DEFAULT 0,
  updated_at        TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS collection_chunks (
  id                TEXT PRIMARY KEY,
  file_id           TEXT NOT NULL REFERENCES collection_files(id) ON DELETE CASCADE,
  collection_name   TEXT NOT NULL,
  file_path         TEXT NOT NULL,
  relative_path     TEXT NOT NULL,
  chunk_index       INTEGER NOT NULL,
  text              TEXT NOT NULL,
  created_at        TEXT NOT NULL
) STRICT;

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
  run_metadata    TEXT NOT NULL DEFAULT '{}',
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
  route_name            TEXT,
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

CREATE TABLE IF NOT EXISTS podcast_metrics (
  id                    TEXT PRIMARY KEY,
  digest_id             TEXT NOT NULL REFERENCES digests(id),
  inference_run_id      TEXT,
  ts                    TEXT NOT NULL,
  show_name             TEXT,
  episode_id            TEXT,
  episode_title         TEXT,
  feed_url              TEXT,
  audio_url             TEXT,
  episode_url           TEXT,
  apple_podcasts_url    TEXT,
  published_at          TEXT,
  duration_seconds      INTEGER,
  quality_score         REAL,
  transcript_source     TEXT,
  status                TEXT NOT NULL,
  error_detail          TEXT,
  feed_fetch_ms         INTEGER,
  audio_download_ms     INTEGER,
  transcription_ms      INTEGER,
  total_ms              INTEGER,
  audio_bytes           INTEGER,
  transcript_words      INTEGER,
  cache_hit             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS youtube_quota_usage (
  usage_date       TEXT PRIMARY KEY,
  units_used       INTEGER NOT NULL DEFAULT 0,
  updated_at       TEXT NOT NULL
) STRICT;

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

CREATE TABLE IF NOT EXISTS digest_delivery_settings (
  digest_id             TEXT PRIMARY KEY REFERENCES digests(id),
  recipient_email       TEXT,
  enabled               INTEGER NOT NULL DEFAULT 0,
  last_delivery_status  TEXT,
  last_delivered_at     TEXT,
  last_error            TEXT,
  updated_at            TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS feedback (
  id                  TEXT PRIMARY KEY,
  digest_item_id      TEXT REFERENCES digest_items(id),
  article_id          TEXT REFERENCES articles(id),
  digest_id           TEXT NOT NULL REFERENCES digests(id),
  exploration_id      TEXT REFERENCES explorations(exploration_id),
  url                 TEXT,
  source_type         TEXT,
  source_name         TEXT,
  adapter             TEXT,
  tags_json           TEXT,
  query_metadata_json TEXT,
  signal              TEXT NOT NULL CHECK(signal IN ('click', 'love', 'like', 'neutral', 'dislike', 'up', 'down')),
  created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS podcast_discovery_cache (
  query_normalized TEXT NOT NULL,
  provider         TEXT NOT NULL,
  lookback_bucket  TEXT NOT NULL,
  results_json     TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  PRIMARY KEY (query_normalized, provider, lookback_bucket)
) STRICT;

CREATE TABLE IF NOT EXISTS podcast_resolution_cache (
  episode_url_normalized TEXT PRIMARY KEY,
  feed_url               TEXT,
  podcast_index_id       TEXT,
  apple_url              TEXT,
  resolved_at            TEXT NOT NULL,
  expires_at             TEXT NOT NULL
) STRICT;

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



CREATE TABLE IF NOT EXISTS gmail_senders (
  id           TEXT PRIMARY KEY,
  sender       TEXT NOT NULL UNIQUE,
  sender_name  TEXT,
  state        TEXT NOT NULL CHECK(state IN ('approved','candidate','rejected')),
  reason       TEXT,
  source       TEXT,
  message_count INTEGER NOT NULL DEFAULT 0,
  last_seen_at TEXT,
  metadata     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS served_undated_items (
  id              TEXT PRIMARY KEY,
  topic_id        TEXT NOT NULL,
  item_key        TEXT NOT NULL,
  title           TEXT,
  source_name     TEXT,
  url             TEXT,
  first_seen_at   TEXT NOT NULL,
  UNIQUE(topic_id, item_key)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_digests_profile_id ON digests(profile_id);
CREATE INDEX IF NOT EXISTS idx_topic_profiles_updated_at ON topic_profiles(updated_at);
CREATE INDEX IF NOT EXISTS idx_refinement_sessions_updated_at ON refinement_sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_explorations_topic_id ON explorations(topic_id);
CREATE INDEX IF NOT EXISTS idx_explorations_status ON explorations(status);
CREATE INDEX IF NOT EXISTS idx_promoted_sources_topic_id ON promoted_sources(topic_id);
CREATE INDEX IF NOT EXISTS idx_collection_files_collection ON collection_files(collection_name);
CREATE INDEX IF NOT EXISTS idx_collection_files_status ON collection_files(status);
CREATE INDEX IF NOT EXISTS idx_collection_chunks_collection ON collection_chunks(collection_name);
CREATE INDEX IF NOT EXISTS idx_digest_runs_digest_id ON digest_runs(digest_id);
CREATE INDEX IF NOT EXISTS idx_inference_metrics_run_id ON inference_metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_inference_metrics_article_id ON inference_metrics(article_id);
CREATE INDEX IF NOT EXISTS idx_inference_metrics_model_ts ON inference_metrics(model, ts);
CREATE INDEX IF NOT EXISTS idx_model_enrichment_jobs_created_at ON model_enrichment_jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_digest_issues_digest_id ON digest_issues(digest_id);
CREATE INDEX IF NOT EXISTS idx_digest_items_digest_id ON digest_items(digest_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_run_id ON agent_decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_digest_id ON agent_decisions(digest_id);
CREATE INDEX IF NOT EXISTS idx_podcast_metrics_digest_ts ON podcast_metrics(digest_id, ts);
CREATE INDEX IF NOT EXISTS idx_podcast_metrics_inference_run_id ON podcast_metrics(inference_run_id);
CREATE INDEX IF NOT EXISTS idx_podcast_metrics_status ON podcast_metrics(status);
CREATE INDEX IF NOT EXISTS idx_discoveries_article_id ON article_discoveries(article_id);
CREATE INDEX IF NOT EXISTS idx_model_enrichment_cache_url ON model_enrichment_cache(canonical_url);
CREATE INDEX IF NOT EXISTS idx_model_enrichment_cache_model ON model_enrichment_cache(model_name);

CREATE INDEX IF NOT EXISTS idx_served_undated_items_topic ON served_undated_items(topic_id);
CREATE INDEX IF NOT EXISTS idx_gmail_senders_state ON gmail_senders(state);
"""
