export type SourceKey = "web_search" | "foreign_media" | "gmail" | "podcasts" | "youtube" | "collections" | "markets" | "reddit" | "google_news" | "academic" | "regulatory" | "hacker_news";
export type FlowState = "idle" | "refining" | "confirm" | "building" | "ready" | "schedule";
export type SortMode = "recent" | "name";
export type SchedulePreset = "daily" | "weekdays" | "weekly" | "monthly";
export type SourceScope = "breaking" | "recent" | "last_year" | "all_available";
export type RefinementProgressPhase = "starting" | "answering" | "confirming";
export type RecencyUnit = "days" | "months";

export type SourceStatus = {
  label: string;
  enabled: boolean;
  setup_required: boolean;
  reason: string | null;
  configured_source_count?: number;
  quota_units_used?: number;
  root_path?: string;
  collection_count?: number;
  indexed_count?: number;
  unsupported_count?: number;
  failed_count?: number;
  mode?: string;
  max_core_companies?: number;
  max_related_companies?: number;
};

export type SourceStatusResponse = {
  sources: Record<SourceKey, SourceStatus>;
};

export type TopicProfile = {
  topic_id: string;
  statement: string;
  scope: string;
  subtopics?: string[];
  keywords?: string[];
  search_queries?: string[];
  source_queries?: Record<string, string[]>;
  foreign_language_plan?: Array<{ code: string; name: string; native_query: string; reason?: string }>;
  foreign_regions?: string[];
  direct_episode_queries?: string[];
  related_episode_queries?: string[];
  negative_constraints?: string[];
  priority_terms?: string[];
  depth?: string;
  recency_weighting?: string;
  lookback_hours?: number | null;
  exclusions?: string[];
  must_have_terms?: string[];
  must_have_aliases?: Record<string, string[]>;
  source_selection: Record<string, boolean>;
  requested_sources?: Array<{ adapter: string; ref: string }>;
  promoted_sources?: Array<{ adapter: string; ref: string; has_feed: boolean; feed_url: string | null }>;
  gmail_rules?: {
    intent?: string;
    lookback_hours?: number;
    include_senders?: string[];
    candidates?: Array<{ sender: string; sender_name?: string; subject?: string; message_count?: number; latest_at?: string | null }>;
  };
  schedule?: string | null;
  schedule_config?: Record<string, unknown>;
  delivery_config?: Record<string, unknown>;
  content_limits?: Partial<ContentLimitsDraft>;
  pipeline_limits?: Partial<PipelineLimitsDraft>;
  status?: string;
  archived?: boolean;
  deleted?: boolean;
};

export type TopicProfileResponse = {
  topic_id: string;
  statement: string;
  schedule: string | null;
  created_at?: string;
  updated_at?: string;
  profile: TopicProfile;
  latest_exploration?: Exploration | null;
  next_run_at?: string | null;
};

export type StrategyPreview = {
  statement: string;
  scope: string;
  looks_at: string[];
  ignores: string[];
  search_queries: string[];
  per_source: Array<{
    source: string;
    key: string;
    queries: string[];
    approved_senders?: string[];
    tickers?: string[];
    direct_episode_queries?: string[];
    related_episode_queries?: string[];
    negative_constraints?: string[];
    priority_terms?: string[];
    note?: string;
  }>;
  lookback_hours: number | null;
  recency_weighting: string;
  exclusions: string[];
  must_have_terms?: string[];
  must_have_aliases?: Record<string, string[]>;
  reasoning_summary: string;
};

export type PendingStrategyRefinement = {
  instruction: string;
  assistant_response: string;
  reasoning_summary?: string;
  profile_patch?: Record<string, unknown>;
  proposed_profile: TopicProfile;
  strategy_preview?: StrategyPreview;
  created_at?: string;
  findings?: string[];
  review_mode?: string;
  conversation?: Array<{ role: string; content: string }>;
};

export type StrategyReview = {
  status: "passed" | "proposed" | "unavailable" | string;
  assistant_response?: string;
  findings?: string[];
  reviewed_at?: string;
};

export type RefinementSession = {
  session_id: string;
  statement: string;
  status: "active" | "finalized";
  turn_count: number;
  messages: ChatMessage[];
  profile: TopicProfile;
  topic_id: string | null;
  topic_profile?: TopicProfileResponse;
  reasoning_summary?: string;
  strategy_preview?: StrategyPreview;
  pending_strategy_refinement?: PendingStrategyRefinement | null;
  strategy_review?: StrategyReview | null;
};

export type ChatMessage = { role: "assistant" | "user"; content: string };

export type ConfirmedProfilePayload = {
  topic_id?: string;
  refinement_session_id?: string;
  statement: string;
  scope: string;
  depth: ConfirmationDraft["depth"];
  recency_weighting: SourceScope;
  lookback_hours?: number | null;
  exclusions: string[];
  source_selection: Record<string, boolean>;
  requested_sources: Array<{ adapter: string; ref: string }>;
  subtopics: string[];
  keywords: string[];
  foreign_regions?: string[];
  search_queries: string[];
  source_queries: Record<string, string[]>;
  direct_episode_queries?: string[];
  related_episode_queries?: string[];
  negative_constraints?: string[];
  priority_terms?: string[];
  must_have_terms?: string[];
  must_have_aliases?: Record<string, string[]>;
  gmail_rules?: TopicProfile["gmail_rules"];
  models: Record<string, never>;
  schedule?: string | null;
  schedule_config?: Record<string, unknown>;
  delivery_config?: Record<string, unknown>;
  candidate_limit?: number;
  content_limits?: ContentLimitsDraft;
};

export type ExplorationIssue = {
  source_name: string;
  reason: string;
  source?: string;
  item?: string;
  item_url?: string;
};

export type Exploration = {
  exploration_id: string;
  topic_id: string;
  mode: "show_now" | "scheduled";
  source_selection: Record<string, boolean>;
  progress: {
    pipeline?: Record<string, string>;
    sources?: Record<string, { status: string; candidate_count: number; message?: string | null }>;
    candidate_count?: number;
    requested_source_issues?: ExplorationIssue[];
    source_audit_issues?: ExplorationIssue[];
    source_filter_notes?: ExplorationIssue[];
    built_with_issues?: boolean;
    reasoning?: { editorial?: string; critic?: string };
    queue?: { status?: string; message?: string };
    source_audit?: { status?: string; message?: string; summary?: string };
    model_health?: {
      status?: "ok" | "degraded";
      message?: string;
      model_call_count?: number;
      model_success_count?: number;
      model_failure_count?: number;
      included_article_count?: number;
    };
    brief?: {
      title: string;
      html_path?: string;
      snapshot?: string;
      stats?: {
        stage_seconds?: Record<string, number>;
        model_call_count?: number;
        model_success_count?: number;
        model_failure_count?: number;
        included_article_count?: number;
      };
      candidate_count?: number;
    };
    error?: string;
  };
  status: "queued" | "running" | "complete" | "failed";
  brief_ref: string | null;
  emailed: boolean;
  started_at: string;
  finished_at: string | null;
  deleted_at?: string | null;
  delete_after?: string | null;
  purged_at?: string | null;
};

export type Digest = {
  id: string;
  name: string;
  interest: string;
  schedule: string;
  sources: Array<Record<string, string>>;
  status: string;
  updated_at?: string;
  created_at?: string;
  next_run_at?: string | null;
};

export type AdminStatus = {
  system?: {
    environment?: string;
    release?: {
      timestamp?: string | null;
      revision?: string | null;
      source?: string | null;
    };
  };
  health?: {
    headline: string;
    safe_for_overnight: boolean;
    problem_count: number;
    warning_count: number;
    checks: Array<{ name: string; status: string; message: string }>;
  };
  gmail?: {
    configured: boolean;
    connected: boolean;
    requires_reconnect: boolean;
    network: string;
    oauth_redirect_ready: boolean;
    redirect_warning: string | null;
  };
  scheduler?: {
    running: boolean;
    enabled: boolean;
    daily_run_time: string;
    timezone: string;
    last_error: string | null;
  };
  model?: {
    model: string | null;
    local_model?: string | null;
    enabled: boolean;
    api_key_configured: boolean;
    catalog: {
      available: boolean;
      models: Array<{ id: string }>;
      selected_model: string | null;
      selected_local_model?: string | null;
      error: string | null;
      providers?: {
        local?: { available: boolean; models: Array<{ id: string }>; error: string | null; selected_model?: string | null };
      };
    };
    routing?: {
      agents: Array<{ id: string; label: string; description: string }>;
      providers: Array<{ id: string; label: string; configured: boolean; privacy: string }>;
      routes: Record<string, { provider: string; model: string | null; effective_model?: string | null; label?: string }>;
      local: { configured: boolean; base_url: string | null; key_path: string; default_model?: string | null };
      defaults?: { local?: string | null };
    };
    selection_sources?: { local?: string };
  };
  delivery?: {
    email: {
      recipient_email: string | null;
      enabled: boolean;
      gmail_send_ready?: boolean;
      last_delivery_status?: string | null;
      last_delivered_at?: string | null;
      last_error?: string | null;
    };
    scheduled_failures?: ScheduledDeliveryFailure[];
  };
  digests?: Digest[];
  inference_metrics?: {
    record_count: number;
    success_count: number;
    failure_count: number;
    latest_ts: string | null;
    ttft_available?: boolean;
    models?: Array<{
      model: string;
      backend: string | null;
      record_count: number;
      avg_total_ms: number | null;
      p95_total_ms: number | null;
      avg_prompt_tokens?: number | null;
      avg_completion_tokens?: number | null;
      avg_tokens_per_sec?: number | null;
      fallback_rate?: number | null;
    }>;
    routes?: Array<{
      route_name: string;
      model: string;
      backend: string | null;
      record_count: number;
      avg_total_ms: number | null;
      p95_total_ms?: number | null;
      avg_queue_wait_ms?: number | null;
      avg_prompt_tokens?: number | null;
      avg_completion_tokens?: number | null;
      avg_total_tokens?: number | null;
      avg_tokens_per_sec?: number | null;
      fallback_rate: number | null;
    }>;
  };
  model_cache?: {
    record_count: number;
    latest_updated_at: string | null;
  };
  model_jobs?: Array<{
    id: string;
    model_name: string;
    status: string;
    processed_count: number;
    limit_count: number;
    created_at: string;
  }>;
  podcasts?: {
    aggregator_configured: boolean;
    transcription_configured: boolean;
    sources: Array<Record<string, string | null>>;
  };
  secret_health?: {
    secrets_dir: string;
    directory_permissions: { status: string; mode: string | null; expected?: string };
    summary: { configured_count: number; missing_count: number; warning_count: number };
    items: Array<{
      id: string;
      label: string;
      configured: boolean;
      status: "ok" | "warning" | "missing" | string;
      storage: string;
      path: string | null;
      message: string;
      permissions?: { status: string; mode: string | null; expected?: string };
    }>;
    external_plaintext: Array<{ server: string; location: string; key: string; path: string }>;
  };
};

export type ScheduledDeliveryFailure = {
  topic_id: string;
  name: string;
  schedule?: string | null;
  error: string;
  last_attempted_at?: string | null;
  latest_exploration_id?: string | null;
};

export type ModelRouteDraft = Record<string, { model: string }>;

export type EditingDigestDraft = {
  topicId: string;
  preset: SchedulePreset;
  time: string;
  emailEnabled: boolean;
  recipients: string[];
  newRecipient: string;
};

export type EditingRecencyDraft = {
  topicId: string;
  lookbackHours: number | null;
};

export type LibraryResponse = {
  explorations: Exploration[];
  deleted_explorations: Exploration[];
  topics: TopicProfileResponse[];
  digests: TopicProfileResponse[];
  legacy_digests: Digest[];
};

export type ExplorationLibraryItem =
  | { kind: "exploration"; exploration: Exploration; topic: TopicProfileResponse | null }
  | { kind: "topic"; topic: TopicProfileResponse };

export type DigestLibraryItem =
  | { kind: "topic"; topic: TopicProfileResponse }
  | { kind: "legacy"; digest: Digest };

export type ConfirmationDraft = {
  scope: string;
  depth: "practitioner" | "informed-generalist";
  recency_weighting: SourceScope;
  lookback_hours: number | null;
  exclusions: string;
  must_have: string;
  content_limits: ContentLimitsDraft;
  sourceScopeTouched?: boolean;
  recency_scope_confirmed?: boolean;
};

export type ContentLimitsDraft = {
  total_items: number;
  target_items: number;
  lead_items: number;
  per_source: Partial<Record<SourceKey, number>>;
  quality_floor: "standard" | "strong";
};

export type BriefControlsDraft = {
  lookback_hours: number | null;
  content_limits: ContentLimitsDraft;
  youtube_presets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
  podcast_presets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
  gmail_presets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
};

export type SystemLimitGroup = {
  group: string;
  items: Array<{ label: string; value: string; note?: string }>;
};

export type PipelineLimitsDraft = {
  article_fetches: number;
  article_fetch_concurrency: number;
  model_refinement_items: number;
  date_adjudication_candidates: number;
  source_audit_candidates: number;
  editorial_candidates: number;
  critic_articles: number;
  critic_newsletter_records: number;
};

export type BriefSettingsResponse = {
  defaults: BriefControlsDraft;
  pipeline_limits: PipelineLimitsDraft;
  system_limits: SystemLimitGroup[];
  youtube_presets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
  podcast_presets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
  gmail_presets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
};

export type RefinementProgress = {
  phase: RefinementProgressPhase;
  startedAt: number;
  label: string;
};

export type AdminTab = "status" | "sources" | "library" | "settings" | "models" | "metrics" | "reporting";

export const sourceOptions: Array<{ key: SourceKey; label: string; icon: string }> = [
  { key: "web_search", label: "Web", icon: "🌐" },
  { key: "foreign_media", label: "Foreign Media", icon: "🌍" },
  { key: "gmail", label: "Gmail", icon: "✉️" },
  { key: "podcasts", label: "Podcast", icon: "🎙️" },
  { key: "youtube", label: "YouTube", icon: "▶" },
  { key: "collections", label: "Collections", icon: "▣" },
  { key: "markets", label: "Markets", icon: "$" },
  { key: "reddit", label: "Reddit", icon: "👽" },
  { key: "google_news", label: "Google News", icon: "📰" },
  { key: "academic", label: "Academic", icon: "📚" },
  { key: "regulatory", label: "Regulatory", icon: "⚖️" },
  { key: "hacker_news", label: "Hacker News", icon: "🟧" },
];

export interface ForeignRegionOption {
  key: string;
  label: string;
}

export interface ForeignRegionGroup {
  continent: string;
  regions: ForeignRegionOption[];
}

// Regions a brief can focus on, grouped by continent. Selecting any region feeds
// the foreign-media lane (and boosts its caps/limits by 50% on the backend).
export const foreignRegionGroups: ForeignRegionGroup[] = [
  {
    continent: "Americas",
    regions: [
      { key: "north_america", label: "North America" },
      { key: "south_america", label: "South America" },
    ],
  },
  {
    continent: "Europe",
    regions: [{ key: "europe", label: "Europe" }],
  },
  {
    continent: "Asia",
    regions: [
      { key: "asia", label: "Asia" },
      { key: "east_asia", label: "East Asia" },
    ],
  },
  {
    continent: "Middle East & Africa",
    regions: [
      { key: "middle_east", label: "Middle East" },
      { key: "africa", label: "Africa" },
    ],
  },
  {
    continent: "Oceania",
    regions: [{ key: "oceania", label: "Oceania" }],
  },
];

// Flat list of every valid region key (derived from the grouped taxonomy).
export const foreignRegionOptions: ForeignRegionOption[] = foreignRegionGroups.flatMap(
  (group) => group.regions,
);

export const defaultSourceSelection: Record<SourceKey, boolean> = {
  web_search: true,
  foreign_media: false,
  gmail: false,
  podcasts: false,
  youtube: false,
  collections: false,
  markets: false,
  reddit: false,
  google_news: false,
  academic: false,
  regulatory: false,
  hacker_news: false,
};
export const defaultSourceSelectionForControls: Record<SourceKey, boolean> = {
  web_search: true,
  foreign_media: true,
  gmail: true,
  podcasts: true,
  youtube: true,
  collections: true,
  markets: true,
  reddit: true,
  google_news: true,
  academic: true,
  regulatory: true,
  hacker_news: true,
};

export const defaultContentLimits: ContentLimitsDraft = {
  total_items: 1000,
  target_items: 50,
  lead_items: 5,
  per_source: {
    web_search: 80,
    foreign_media: 80,
    gmail: 80,
    podcasts: 40,
    youtube: 40,
    collections: 50,
    markets: 80,
    reddit: 60,
    google_news: 80,
    academic: 50,
    regulatory: 50,
    hacker_news: 40,
  },
  quality_floor: "standard",
};
export const defaultMediumContentLimits: ContentLimitsDraft = {
  total_items: 600,
  target_items: 30,
  lead_items: 3,
  per_source: {
    web_search: 48,
    foreign_media: 48,
    gmail: 48,
    podcasts: 24,
    youtube: 24,
    collections: 30,
    markets: 48,
    reddit: 36,
    google_news: 48,
    academic: 30,
    regulatory: 30,
    hacker_news: 24,
  },
  quality_floor: "standard",
};
export const defaultBriefControls: BriefControlsDraft = {
  lookback_hours: 168,
  content_limits: defaultMediumContentLimits,
  youtube_presets: {
    max: 40,
    large: 32,
    medium: 24,
    focused: 16,
  },
  podcast_presets: {
    max: 40,
    large: 32,
    medium: 24,
    focused: 16,
  },
  gmail_presets: {
    max: 80,
    large: 64,
    medium: 48,
    focused: 32,
  },
};
export const briefControlBounds = {
  source_window_days: { min: 0, max: 10950 },
  total_items: { min: 1, max: 1000 },
  target_items: { min: 1, max: 1000 },
  lead_items: { min: 0, max: 20 },
  per_source: { min: 1, max: 80 },
};

function clampContentLimitValue(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min;
  return Math.max(min, Math.min(max, Math.round(value)));
}

export function scaleContentLimits(limits: ContentLimitsDraft, scale: number): ContentLimitsDraft {
  const scaleValue = (value: number, min: number, max: number) => (
    clampContentLimitValue(Math.ceil(value * scale), min, max)
  );
  const perSource: Partial<Record<SourceKey, number>> = {};
  for (const source of sourceOptions) {
    const value = limits.per_source[source.key] ?? briefControlBounds.per_source.max;
    perSource[source.key] = scaleValue(value, briefControlBounds.per_source.min, briefControlBounds.per_source.max);
  }
  return {
    ...limits,
    total_items: scaleValue(limits.total_items, briefControlBounds.total_items.min, briefControlBounds.total_items.max),
    target_items: scaleValue(limits.target_items, briefControlBounds.target_items.min, briefControlBounds.target_items.max),
    lead_items: scaleValue(limits.lead_items, briefControlBounds.lead_items.min, briefControlBounds.lead_items.max),
    per_source: perSource,
  };
}
export const defaultPipelineLimits: PipelineLimitsDraft = {
  article_fetches: 1000,
  article_fetch_concurrency: 35,
  model_refinement_items: 250,
  date_adjudication_candidates: 100,
  source_audit_candidates: 150,
  editorial_candidates: 500,
  critic_articles: 250,
  critic_newsletter_records: 20,
};
export const pipelineLimitFields: Array<{
  key: keyof PipelineLimitsDraft;
  label: string;
  min: number;
  max: number;
  note: string;
}> = [
  {
    key: "article_fetches",
    label: "Article fetches",
    min: 1,
    max: 1000,
    note: "Maximum article URLs the fetch step will retrieve.",
  },
  {
    key: "article_fetch_concurrency",
    label: "Fetch concurrency",
    min: 1,
    max: 40,
    note: "Parallel article fetches during extraction.",
  },
  {
    key: "model_refinement_items",
    label: "Model-enriched items",
    min: 0,
    max: 250,
    note: "Candidate summaries/refinements sent through the model.",
  },
  {
    key: "date_adjudication_candidates",
    label: "Date adjudication candidates",
    min: 1,
    max: 100,
    note: "Candidates reviewed for publication-date ambiguity before strict recency filtering.",
  },
  {
    key: "source_audit_candidates",
    label: "Source audit candidates",
    min: 1,
    max: 150,
    note: "Candidates reviewed in the pre-ranking source audit.",
  },
  {
    key: "editorial_candidates",
    label: "Editorial candidates",
    min: 1,
    max: 500,
    note: "Candidates the editorial model can sort and include.",
  },
  {
    key: "critic_articles",
    label: "Critic articles",
    min: 1,
    max: 250,
    note: "Draft articles reviewed by the critic pass.",
  },
  {
    key: "critic_newsletter_records",
    label: "Newsletter samples",
    min: 0,
    max: 20,
    note: "Gmail newsletter samples visible to the critic pass.",
  },
];

export const schedulePresets: Array<{ value: SchedulePreset; label: string }> = [
  { value: "daily", label: "Daily" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];
export const adminTabOptions: AdminTab[] = ["status", "sources", "library", "settings", "models", "metrics", "reporting"];

export type GmailAllowlistAction = "approve" | "reject" | "remove";

export type GmailSenderRecord = {
  sender: string;
  sender_name?: string | null;
  state: "approved" | "candidate" | "rejected";
  reason?: string | null;
  source?: string | null;
  message_count?: number;
  last_seen_at?: string | null;
};

export type GmailAllowlistResponse = {
  summary: { sender_count: number; approved_count: number; candidate_count: number; rejected_count: number };
  approved: GmailSenderRecord[];
  candidates: GmailSenderRecord[];
  rejected: GmailSenderRecord[];
};
