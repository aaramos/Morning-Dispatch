import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent, ReactNode } from "react";

type SourceKey = "web_search" | "foreign_media" | "gmail" | "podcasts" | "youtube" | "collections" | "markets";
type FlowState = "idle" | "refining" | "confirm" | "building" | "ready" | "schedule";
type SortMode = "recent" | "name";
type SchedulePreset = "daily" | "weekdays" | "weekly" | "monthly";
type SourceScope = "breaking" | "recent" | "last_year" | "all_available";
type RefinementProgressPhase = "starting" | "answering" | "confirming";

type SourceStatus = {
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

type SourceStatusResponse = {
  sources: Record<SourceKey, SourceStatus>;
};

type TopicProfile = {
  topic_id: string;
  statement: string;
  scope: string;
  subtopics?: string[];
  keywords?: string[];
  search_queries?: string[];
  source_queries?: Record<string, string[]>;
  foreign_language_plan?: Array<{ code: string; name: string; native_query: string; reason?: string }>;
  depth?: string;
  recency_weighting?: string;
  lookback_hours?: number | null;
  exclusions?: string[];
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

type TopicProfileResponse = {
  topic_id: string;
  statement: string;
  schedule: string | null;
  created_at?: string;
  updated_at?: string;
  profile: TopicProfile;
  latest_exploration?: Exploration | null;
  next_run_at?: string | null;
};

type StrategyPreview = {
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
    note?: string;
  }>;
  lookback_hours: number | null;
  recency_weighting: string;
  exclusions: string[];
  reasoning_summary: string;
};

type PendingStrategyRefinement = {
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

type StrategyReview = {
  status: "passed" | "proposed" | "unavailable" | string;
  assistant_response?: string;
  findings?: string[];
  reviewed_at?: string;
};

type RefinementSession = {
  session_id: string;
  statement: string;
  status: "active" | "finalized";
  turn_count: number;
  messages: Array<{ role: "assistant" | "user"; content: string }>;
  profile: TopicProfile;
  topic_id: string | null;
  topic_profile?: TopicProfileResponse;
  reasoning_summary?: string;
  strategy_preview?: StrategyPreview;
  pending_strategy_refinement?: PendingStrategyRefinement | null;
  strategy_review?: StrategyReview | null;
};

type ConfirmedProfilePayload = {
  topic_id?: string;
  refinement_session_id?: string;
  statement: string;
  scope: string;
  depth: ConfirmationDraft["depth"];
  recency_weighting: SourceScope;
  lookback_hours?: number;
  exclusions: string[];
  source_selection: Record<string, boolean>;
  requested_sources: Array<{ adapter: string; ref: string }>;
  subtopics: string[];
  keywords: string[];
  search_queries: string[];
  source_queries: Record<string, string[]>;
  gmail_rules?: TopicProfile["gmail_rules"];
  models: Record<string, never>;
  schedule?: string | null;
  schedule_config?: Record<string, unknown>;
  delivery_config?: Record<string, unknown>;
  candidate_limit?: number;
  content_limits?: ContentLimitsDraft;
};

type ExplorationIssue = {
  source_name: string;
  reason: string;
  source?: string;
  item?: string;
  item_url?: string;
};

type Exploration = {
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

type Digest = {
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

type AdminStatus = {
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
    ollama_cloud_model?: string | null;
    enabled: boolean;
    api_key_configured: boolean;
    catalog: {
      available: boolean;
      models: Array<{ id: string }>;
      selected_model: string | null;
      selected_local_model?: string | null;
      selected_ollama_cloud_model?: string | null;
      error: string | null;
      providers?: {
        local?: { available: boolean; models: Array<{ id: string }>; error: string | null; selected_model?: string | null };
        ollama_cloud?: { available: boolean; configured: boolean; models: Array<{ id: string }>; error: string | null; selected_model?: string | null };
      };
    };
    routing?: {
      agents: Array<{ id: string; label: string; description: string }>;
      providers: Array<{ id: string; label: string; configured: boolean; privacy: string }>;
      routes: Record<string, { provider: string; model: string | null; allow_private_cloud: boolean; effective_model?: string | null; label?: string }>;
      ollama_cloud: { configured: boolean; base_url: string; key_path: string; default_model?: string | null };
      defaults?: { local?: string | null; ollama_cloud?: string | null };
      privacy: { rule: string; private_sources: string[] };
    };
    selection_sources?: { local?: string; ollama_cloud?: string };
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

type ModelRouteDraft = Record<string, { provider: string; model: string; allow_private_cloud: boolean }>;

type LibraryResponse = {
  explorations: Exploration[];
  deleted_explorations: Exploration[];
  topics: TopicProfileResponse[];
  digests: TopicProfileResponse[];
  legacy_digests: Digest[];
};

type ExplorationLibraryItem =
  | { kind: "exploration"; exploration: Exploration; topic: TopicProfileResponse | null }
  | { kind: "topic"; topic: TopicProfileResponse };

type DigestLibraryItem =
  | { kind: "topic"; topic: TopicProfileResponse }
  | { kind: "legacy"; digest: Digest };

type HomeRecentItem =
  | { kind: "exploration"; exploration: Exploration; topic: TopicProfileResponse | null; digest: boolean }
  | { kind: "topic"; topic: TopicProfileResponse; digest: boolean };

type ConfirmationDraft = {
  scope: string;
  depth: "practitioner" | "informed-generalist";
  recency_weighting: SourceScope;
  lookback_hours: number;
  exclusions: string;
  content_limits: ContentLimitsDraft;
  sourceScopeTouched?: boolean;
  recency_scope_confirmed?: boolean;
};

type ContentLimitsDraft = {
  total_items: number;
  target_items: number;
  lead_items: number;
  per_source: Partial<Record<SourceKey, number>>;
  quality_floor: "standard" | "strong";
};

type BriefControlsDraft = {
  lookback_hours: number;
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
};

type SystemLimitGroup = {
  group: string;
  items: Array<{ label: string; value: string; note?: string }>;
};

type PipelineLimitsDraft = {
  article_fetches: number;
  article_fetch_concurrency: number;
  model_refinement_items: number;
  source_audit_candidates: number;
  editorial_candidates: number;
  critic_articles: number;
  critic_newsletter_records: number;
};

type BriefSettingsResponse = {
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
};

type RefinementProgress = {
  phase: RefinementProgressPhase;
  startedAt: number;
  label: string;
};

type AdminTab = "status" | "sources" | "library" | "settings" | "models" | "metrics";

const sourceOptions: Array<{ key: SourceKey; label: string; icon: string }> = [
  { key: "web_search", label: "Web", icon: "🌐" },
  { key: "foreign_media", label: "Foreign Media", icon: "🌍" },
  { key: "gmail", label: "Gmail", icon: "✉️" },
  { key: "podcasts", label: "Podcast", icon: "🎙️" },
  { key: "youtube", label: "YouTube", icon: "▶" },
  { key: "collections", label: "Collections", icon: "▣" },
  { key: "markets", label: "Markets", icon: "$" },
];

const defaultSourceSelection: Record<SourceKey, boolean> = {
  web_search: true,
  foreign_media: false,
  gmail: false,
  podcasts: false,
  youtube: false,
  collections: false,
  markets: false,
};
const defaultSourceSelectionForControls: Record<SourceKey, boolean> = {
  web_search: true,
  foreign_media: true,
  gmail: true,
  podcasts: true,
  youtube: true,
  collections: true,
  markets: true,
};

const defaultContentLimits: ContentLimitsDraft = {
  total_items: 250,
  target_items: 25,
  lead_items: 5,
  per_source: {
    web_search: 25,
    foreign_media: 25,
    gmail: 25,
    podcasts: 25,
    youtube: 25,
    collections: 25,
    markets: 50,
  },
  quality_floor: "standard",
};
const defaultBriefControls: BriefControlsDraft = {
  lookback_hours: 336,
  content_limits: defaultContentLimits,
  youtube_presets: {
    max: 25,
    large: 20,
    medium: 15,
    focused: 10,
  },
  podcast_presets: {
    max: 25,
    large: 20,
    medium: 15,
    focused: 10,
  },
};
const briefControlBounds = {
  source_window_days: { min: 1, max: 365 },
  total_items: { min: 1, max: 250 },
  target_items: { min: 1, max: 250 },
  lead_items: { min: 0, max: 20 },
  per_source: { min: 1, max: 50 },
};
const defaultPipelineLimits: PipelineLimitsDraft = {
  article_fetches: 250,
  article_fetch_concurrency: 10,
  model_refinement_items: 150,
  source_audit_candidates: 28,
  editorial_candidates: 150,
  critic_articles: 50,
  critic_newsletter_records: 20,
};
const pipelineLimitFields: Array<{
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
    max: 250,
    note: "Maximum article URLs the fetch step will retrieve.",
  },
  {
    key: "article_fetch_concurrency",
    label: "Fetch concurrency",
    min: 1,
    max: 20,
    note: "Parallel article fetches during extraction.",
  },
  {
    key: "model_refinement_items",
    label: "Model-enriched items",
    min: 0,
    max: 150,
    note: "Candidate summaries/refinements sent through the model.",
  },
  {
    key: "source_audit_candidates",
    label: "Source audit candidates",
    min: 1,
    max: 28,
    note: "Candidates reviewed in the pre-ranking source audit.",
  },
  {
    key: "editorial_candidates",
    label: "Editorial candidates",
    min: 1,
    max: 150,
    note: "Candidates the editorial model can sort and include.",
  },
  {
    key: "critic_articles",
    label: "Critic articles",
    min: 1,
    max: 50,
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

const schedulePresets: Array<{ value: SchedulePreset; label: string }> = [
  { value: "daily", label: "Daily" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];
const interestDraftCookieName = "morning_dispatch_interest_draft";
const interestDraftTtlSeconds = 60 * 60;
const interestDraftTtlMs = interestDraftTtlSeconds * 1000;
const adminTabOptions: AdminTab[] = ["status", "sources", "library", "settings", "models", "metrics"];

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

function loadInterestDraft(): string {
  const rawCookie = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith(`${interestDraftCookieName}=`));
  if (!rawCookie) return "";
  try {
    const payload = JSON.parse(decodeURIComponent(rawCookie.split("=").slice(1).join("="))) as {
      statement?: string;
      expires_at?: number;
    };
    if (!payload.expires_at || payload.expires_at <= Date.now()) {
      clearInterestDraft();
      return "";
    }
    return typeof payload.statement === "string" ? payload.statement : "";
  } catch {
    clearInterestDraft();
    return "";
  }
}

function saveInterestDraft(statement: string): void {
  const cleanStatement = statement.trim() ? statement : "";
  if (!cleanStatement) {
    clearInterestDraft();
    return;
  }
  const payload = encodeURIComponent(JSON.stringify({
    statement: cleanStatement,
    expires_at: Date.now() + interestDraftTtlMs,
  }));
  document.cookie = `${interestDraftCookieName}=${payload}; Max-Age=${interestDraftTtlSeconds}; Path=/; SameSite=Lax`;
}

function clearInterestDraft(): void {
  document.cookie = `${interestDraftCookieName}=; Max-Age=0; Path=/; SameSite=Lax`;
}

function loadSessionValue<T>(key: string, fallback: T): T {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export default function App() {
  if (window.location.pathname === "/admin") {
    return <AdminApp />;
  }
  return <DispatchApp />;
}

function DispatchApp() {
  const [sourceStatus, setSourceStatus] = useState<SourceStatusResponse | null>(null);
  const [sourceSelection, setSourceSelection] = useState<Record<SourceKey, boolean>>(defaultSourceSelection);
  const [statement, setStatement] = useState(() => loadInterestDraft());
  const [submittedInterest, setSubmittedInterest] = useState("");
  const [session, setSession] = useState<RefinementSession | null>(null);
  const [answer, setAnswer] = useState("");
  const [topicProfile, setTopicProfile] = useState<TopicProfileResponse | null>(null);
  const [draft, setDraft] = useState<ConfirmationDraft>(emptyDraft());
  const [flow, setFlow] = useState<FlowState>("idle");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Ready");
  const [enableSource, setEnableSource] = useState<SourceKey | null>(null);
  const [webKey, setWebKey] = useState("");
  const [gmailSecret, setGmailSecret] = useState("");
  const [podcastKey, setPodcastKey] = useState("");
  const [podcastSecret, setPodcastSecret] = useState("");
  const [youtubeKey, setYoutubeKey] = useState("");
  const [fredKey, setFredKey] = useState("");
  const [exploration, setExploration] = useState<Exploration | null>(null);
  const [briefHtml, setBriefHtml] = useState("");
  const [recentExplorations, setRecentExplorations] = useState<Exploration[]>([]);
  const [scheduledTopics, setScheduledTopics] = useState<TopicProfileResponse[]>([]);
  const [allTopics, setAllTopics] = useState<TopicProfileResponse[]>([]);
  const [deliveryConfigured, setDeliveryConfigured] = useState(false);
  const [emailSendReady, setEmailSendReady] = useState(false);
  const [briefEmailRecipient, setBriefEmailRecipient] = useState("");
  const [homeDeleteUndo, setHomeDeleteUndo] = useState<{ explorationId: string; title: string; until: string | null } | null>(null);
  const [recentExpanded, setRecentExpanded] = useState(() => loadSessionValue("dispatch.recentExpanded", false));
  const [schedulePreset, setSchedulePreset] = useState<SchedulePreset>("daily");
  const [scheduleTime, setScheduleTime] = useState("08:00");
  const [emailOnSchedule, setEmailOnSchedule] = useState(false);
  const [refinementProgress, setRefinementProgress] = useState<RefinementProgress | null>(null);
  const [refinementFallbackStartedAt, setRefinementFallbackStartedAt] = useState(0);
  const [refinementTargetExplorationId, setRefinementTargetExplorationId] = useState<string | null>(null);
  const [briefSettings, setBriefSettings] = useState<BriefSettingsResponse | null>(null);
  const [adminStatus, setAdminStatus] = useState<AdminStatus | null>(null);
  const [strategyConfirmation, setStrategyConfirmation] = useState("");
  const [initialRefineExplorationId] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const refineExplorationId = params.get("refine_exploration");
    if (refineExplorationId) {
      params.delete("refine_exploration");
      const nextUrl = `${window.location.pathname}${params.toString() ? `?${params}` : ""}`;
      window.history.replaceState(null, "", nextUrl);
    }
    return refineExplorationId;
  });
  const [progressNow, setProgressNow] = useState(0);

  const topicById = useMemo(() => new Map(allTopics.map((topic) => [topic.topic_id, topic])), [allTopics]);
  const activeDigest = scheduledTopics[0] ?? null;
  const homeRecentItems = useMemo<HomeRecentItem[]>(() => {
    const topicIdsWithExplorations = new Set(recentExplorations.map((item) => item.topic_id));
    const digestTopicIds = new Set(scheduledTopics.map((topic) => topic.topic_id));
    const explorationItems: HomeRecentItem[] = recentExplorations.map((exploration) => ({
      kind: "exploration",
      exploration,
      topic: topicById.get(exploration.topic_id) ?? null,
      digest: digestTopicIds.has(exploration.topic_id),
    }));
    const unbuiltTopicItems: HomeRecentItem[] = allTopics
      .filter((topic) => !topic.profile.archived && !topic.profile.deleted)
      .filter((topic) => !topicIdsWithExplorations.has(topic.topic_id))
      .map((topic) => ({
        kind: "topic",
        topic,
        digest: digestTopicIds.has(topic.topic_id),
      }));
    return [...explorationItems, ...unbuiltTopicItems]
      .sort((a, b) => homeRecentDate(b) - homeRecentDate(a))
      .slice(0, 5);
  }, [allTopics, recentExplorations, scheduledTopics, topicById]);
  const selectedEnabledSources = useMemo(
    () => enabledSourceSelection(sourceSelection, sourceStatus),
    [sourceSelection, sourceStatus],
  );
  const defaultControls = briefSettings?.defaults ?? defaultBriefControls;
  const activeInterest = (submittedInterest || statement).trim();
  const buildInterest = (
    activeInterest
    || draft.scope
    || session?.profile?.statement
    || topicProfile?.statement
    || ""
  ).trim();
  const sourceLocked = flow === "building";
  const canSubmitInterest = (flow === "idle" || flow === "ready") && statement.trim().length > 0 && !busy;
  const canBuild = buildInterest.length > 0 && !busy;
  const currentIssues = buildAttentionIssues(exploration);
  const backgroundBuild = useMemo(
    () => recentExplorations.find((item) => item.status === "queued" || item.status === "running") ?? null,
    [recentExplorations],
  );
  const visibleBuild = flow === "building"
    ? exploration
    : flow === "ready"
      ? exploration
      : flow === "idle" ? backgroundBuild : null;
  const refinementWorking = busy && !enableSource && !exploration && flow === "refining";
  const activeRefinementProgress = useMemo<RefinementProgress | null>(() => {
    if (refinementProgress) return refinementProgress;
    if (!refinementWorking || !refinementFallbackStartedAt) return null;
    return { phase: "starting", startedAt: refinementFallbackStartedAt, label: "Refining" };
  }, [refinementFallbackStartedAt, refinementProgress, refinementWorking]);

  const loadHome = useCallback(async () => {
    const [sources, explorations, scheduled, topics, admin, settings] = await Promise.all([
      api<SourceStatusResponse>("/api/explore/source-status").catch(() => null),
      api<Exploration[]>("/api/explore/explorations?limit=25").catch(() => []),
      api<TopicProfileResponse[]>("/api/explore/scheduled-topic-profiles").catch(() => []),
      api<TopicProfileResponse[]>("/api/explore/topic-profiles").catch(() => []),
      api<AdminStatus>("/api/admin/status").catch(() => null),
      api<BriefSettingsResponse>("/api/admin/brief-settings").catch(() => null),
    ]);
    if (sources) setSourceStatus(sources);
    if (admin) setAdminStatus(admin);
    if (settings) setBriefSettings(settings);
    setRecentExplorations(explorations);
    setScheduledTopics(scheduled);
    setAllTopics(topics);
    const email = admin?.delivery?.email;
    const configured = Boolean(email?.enabled && email.recipient_email && email.gmail_send_ready !== false);
    const sendReady = Boolean(email?.gmail_send_ready);
    setDeliveryConfigured(configured);
    setEmailSendReady(sendReady);
    if (email?.recipient_email) setBriefEmailRecipient((current) => current || String(email.recipient_email));
    setEmailOnSchedule(configured);
  }, []);

  useEffect(() => {
    void loadHome();
  }, [loadHome]);

  useEffect(() => {
    if (!backgroundBuild) return;
    const timer = window.setInterval(() => {
      void loadHome();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [backgroundBuild, loadHome]);

  useEffect(() => {
    if (flow !== "idle") return;
    if (!statement.trim()) {
      clearInterestDraft();
      return;
    }
    const timer = window.setTimeout(() => {
      clearInterestDraft();
      setStatement("");
      setMessage("Draft cleared after one hour of inactivity");
    }, interestDraftTtlMs);
    saveInterestDraft(statement);
    return () => window.clearTimeout(timer);
  }, [flow, statement]);

  useEffect(() => {
    window.sessionStorage.setItem("dispatch.recentExpanded", JSON.stringify(recentExpanded));
  }, [recentExpanded]);

  useEffect(() => {
    if (!session) return;
    setDraft(draftFromProfile(session.profile, defaultControls.content_limits));
  }, [defaultControls.content_limits, session]);

  useEffect(() => {
    if (!activeRefinementProgress) return;
    setProgressNow(Date.now());
    const timer = window.setInterval(() => setProgressNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [activeRefinementProgress]);

  const beginRefinementProgress = useCallback((phase: RefinementProgressPhase, label: string) => {
    const now = Date.now();
    setRefinementFallbackStartedAt(now);
    setProgressNow(now);
    setRefinementProgress({ phase, label, startedAt: now });
  }, []);

  const endRefinementProgress = useCallback(() => {
    setRefinementProgress(null);
    setRefinementFallbackStartedAt(0);
    setProgressNow(Date.now());
  }, []);

  async function startFlow(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    if (flow !== "idle" && flow !== "ready") return;
    if (!statement.trim()) return;
    const blocked = firstBlockedSelectedSource(sourceSelection, sourceStatus);
    if (blocked) {
      setEnableSource(blocked);
      return;
    }
    if (!hasEnabledSource(selectedEnabledSources)) {
      setEnableSource("web_search");
      return;
    }
    const interest = statement.trim();
    clearInterestDraft();
    setStrategyConfirmation("");
    setSubmittedInterest(interest);
    setRefinementTargetExplorationId(null);
    setStatement("");
    setFlow("refining");
    setBusy(true);
    setMessage("Refining your interest...");
    beginRefinementProgress("starting", "Starting refinement");
    setBriefHtml("");
    setExploration(null);
    try {
      const nextSession = await api<RefinementSession>("/api/explore/refinement-sessions", {
        method: "POST",
        body: JSON.stringify({
          statement: interest,
          source_selection: selectedEnabledSources,
          models: {},
        }),
      });
      setSession(nextSession);
      setDraft(draftFromProfile(nextSession.profile, defaultControls.content_limits));
      setFlow(nextSession.status === "finalized" ? "confirm" : "refining");
      if (nextSession.topic_profile) setTopicProfile(nextSession.topic_profile);
      setMessage(nextSession.status === "finalized" ? "Confirm the brief setup" : "Answer a few quick questions");
    } catch (error) {
      setStatement(interest);
      saveInterestDraft(interest);
      setSubmittedInterest("");
      setFlow("idle");
      setMessage(errorMessage(error, "Could not start refinement"));
    } finally {
      setBusy(false);
      endRefinementProgress();
    }
  }

  async function answerRefinement(justGoNow = false) {
    if (!activeInterest) return;
    if (!session && !justGoNow) return;
    if (!justGoNow && !answer.trim()) return;
    setBusy(true);
    setMessage(justGoNow ? "Preparing confirmation..." : "Refining...");
    beginRefinementProgress(justGoNow ? "confirming" : "answering", justGoNow ? "Preparing confirmation" : "Refining answer");
    try {
      const currentSession = session ?? await api<RefinementSession>("/api/explore/refinement-sessions", {
        method: "POST",
        body: JSON.stringify({
          statement: activeInterest,
          source_selection: selectedEnabledSources,
          models: {},
        }),
      });
      const updated = await api<RefinementSession>(`/api/explore/refinement-sessions/${currentSession.session_id}/messages`, {
        method: "POST",
        body: JSON.stringify({
          answer: answer.trim(),
          just_go_now: justGoNow,
          models: {},
        }),
      });
      setAnswer("");
      setSession(updated);
      setDraft(draftFromProfile(updated.profile, defaultControls.content_limits));
      if (updated.topic_profile) setTopicProfile(updated.topic_profile);
      setFlow(updated.status === "finalized" ? "confirm" : "refining");
      setMessage(updated.status === "finalized" ? "Confirm the brief setup" : "Refinement updated");
    } catch (error) {
      setMessage(errorMessage(error, "Could not update refinement"));
    } finally {
      setBusy(false);
      endRefinementProgress();
    }
  }

  async function refineSearchStrategy(instruction: string) {
    const cleanInstruction = instruction.trim();
    if (!cleanInstruction) return;
    const baseStatement = activeInterest || topicProfile?.statement || session?.statement || "";
    if (!baseStatement) return;
    setBusy(true);
    setMessage("Asking AI to review your strategy feedback...");
    beginRefinementProgress("answering", "Reviewing strategy feedback");
    try {
      const currentSession = session ?? await api<RefinementSession>("/api/explore/refinement-sessions", {
        method: "POST",
        body: JSON.stringify({
          statement: baseStatement,
          topic_id: topicProfile?.topic_id,
          revisit: Boolean(topicProfile?.topic_id),
          source_selection: selectedEnabledSources,
          models: {},
        }),
      });
      const updated = await api<RefinementSession>(`/api/explore/refinement-sessions/${currentSession.session_id}/strategy`, {
        method: "POST",
        body: JSON.stringify({
          instruction: cleanInstruction,
          models: {},
        }),
      });
      setSession(updated);
      const proposal = updated.pending_strategy_refinement?.proposed_profile;
      if (proposal) setDraft(draftFromProfile(proposal, defaultControls.content_limits));
      if (updated.topic_profile) setTopicProfile(updated.topic_profile);
      setFlow("confirm");
      setStrategyConfirmation(updated.pending_strategy_refinement?.assistant_response || "AI prepared a proposed strategy update for review.");
      setMessage("Review the proposed strategy update");
    } catch (error) {
      setMessage(errorMessage(error, "Could not update search strategy"));
    } finally {
      setBusy(false);
      endRefinementProgress();
    }
  }

  async function confirmStrategyRefinement(apply: boolean) {
    if (!session?.session_id) return;
    setBusy(true);
    setMessage(apply ? "Applying strategy update..." : "Discarding strategy update...");
    beginRefinementProgress("confirming", apply ? "Applying strategy update" : "Discarding strategy update");
    try {
      const updated = await api<RefinementSession>(`/api/explore/refinement-sessions/${session.session_id}/strategy/confirm`, {
        method: "POST",
        body: JSON.stringify({ apply }),
      });
      setSession(updated);
      setDraft(draftFromProfile(updated.profile, defaultControls.content_limits));
      if (updated.topic_profile) setTopicProfile(updated.topic_profile);
      if (!updated.pending_strategy_refinement) setFlow("confirm");
      setStrategyConfirmation(apply ? strategyUpdateConfirmation("Search strategy updated.", updated.profile) : "Discarded the proposed strategy update.");
      setMessage(apply ? "Search strategy updated" : "Strategy proposal discarded");
    } catch (error) {
      setMessage(errorMessage(error, "Could not confirm search strategy update"));
    } finally {
      setBusy(false);
      endRefinementProgress();
    }
  }

  function confirmedProfilePayload(draftOverride: ConfirmationDraft = draft): ConfirmedProfilePayload {
    const baseProfile = session?.pending_strategy_refinement?.proposed_profile ?? session?.profile ?? topicProfile?.profile;
    const topicId = topicProfile?.topic_id ?? session?.topic_id ?? baseProfile?.topic_id;
    const interest = buildInterest || baseProfile?.statement || "";
    const lookbackHours = draftOverride.recency_weighting === "all_available"
      ? null
      : lookbackHoursForConfirmedDraft(baseProfile, draftOverride, defaultControls.lookback_hours);
    return {
      ...(topicId ? { topic_id: topicId } : {}),
      ...(session?.session_id ? { refinement_session_id: session.session_id } : {}),
      statement: interest,
      scope: draftOverride.scope.trim() || interest,
      depth: draftOverride.depth,
      recency_weighting: draftOverride.recency_weighting,
      ...(lookbackHours ? { lookback_hours: lookbackHours } : {}),
      exclusions: splitList(draftOverride.exclusions),
      source_selection: selectedEnabledSources,
      requested_sources: baseProfile?.requested_sources ?? [],
      subtopics: baseProfile?.subtopics ?? [],
      keywords: baseProfile?.keywords ?? [],
      search_queries: baseProfile?.search_queries ?? [],
      source_queries: baseProfile?.source_queries ?? {},
      gmail_rules: baseProfile?.gmail_rules ?? {},
      models: {},
      schedule: baseProfile?.schedule ?? null,
      schedule_config: baseProfile?.schedule_config ?? {},
      delivery_config: baseProfile?.delivery_config ?? {},
      candidate_limit: draftOverride.content_limits.total_items,
      content_limits: draftOverride.content_limits,
    };
  }

  async function reviewSearchStrategyBeforeBuild(profilePayload: ConfirmedProfilePayload): Promise<boolean> {
    const currentSession = session ?? await api<RefinementSession>("/api/explore/refinement-sessions", {
      method: "POST",
      body: JSON.stringify({
        statement: profilePayload.statement,
        topic_id: profilePayload.topic_id ?? topicProfile?.topic_id,
        revisit: Boolean(profilePayload.topic_id ?? topicProfile?.topic_id),
        source_selection: selectedEnabledSources,
        models: {},
      }),
    });
    const reviewed = await api<RefinementSession>(`/api/explore/refinement-sessions/${currentSession.session_id}/strategy/review`, {
      method: "POST",
      body: JSON.stringify({
        profile: profilePayload,
        models: {},
      }),
    });
    profilePayload.refinement_session_id = reviewed.session_id;
    setSession(reviewed);
    const proposal = reviewed.pending_strategy_refinement?.proposed_profile;
    if (proposal) {
      setDraft(draftFromProfile(proposal, defaultControls.content_limits));
      if (reviewed.topic_profile) setTopicProfile(reviewed.topic_profile);
      setFlow("confirm");
      setStrategyConfirmation(
        reviewed.pending_strategy_refinement?.assistant_response
          || "AI found strategy changes to review before building."
      );
      setMessage("Review the AI strategy proposal before building");
      return false;
    }
    if (reviewed.strategy_review?.assistant_response) {
      setStrategyConfirmation(reviewed.strategy_review.assistant_response);
    }
    if (reviewed.topic_profile) setTopicProfile(reviewed.topic_profile);
    return true;
  }

  async function buildBrief() {
    if (!canBuild) return;
    if (session?.pending_strategy_refinement) {
      setFlow("confirm");
      setMessage("Apply or discard the proposed strategy update before building.");
      return;
    }
    const buildDraft = draft;
    const blocked = firstBlockedSelectedSource(sourceSelection, sourceStatus);
    if (blocked) {
      setEnableSource(blocked);
      return;
    }
    const profilePayload = {
      ...confirmedProfilePayload(buildDraft),
      source_selection: selectedEnabledSources,
    };
    setBusy(true);
    setMessage("AI is checking the search strategy...");
    beginRefinementProgress("confirming", "Reviewing search strategy");
    let startedExploration: Exploration | null = null;
    try {
      const strategyReady = await reviewSearchStrategyBeforeBuild(profilePayload);
      endRefinementProgress();
      if (!strategyReady) return;
      setFlow("building");
      setMessage("Building the brief...");
      setBriefHtml("");
      const started = refinementTargetExplorationId
        ? await api<{ exploration: Exploration }>(`/api/explore/explorations/${refinementTargetExplorationId}/rebuild`, {
          method: "POST",
          body: JSON.stringify({
            topic_profile: profilePayload,
            refinement_session_id: session?.session_id,
            source_selection: selectedEnabledSources,
            candidate_limit: profilePayload.candidate_limit,
            lookback_hours: profilePayload.lookback_hours,
          }),
        })
        : await api<{ topic_profile: TopicProfileResponse; exploration: Exploration }>("/api/explore/topic-profiles/build", {
          method: "POST",
          body: JSON.stringify(profilePayload),
        });
      const returnedTopic = (started as { topic_profile?: TopicProfileResponse }).topic_profile;
      if (returnedTopic) setTopicProfile(returnedTopic);
      setSession(null);
      setAnswer("");
      startedExploration = started.exploration;
      setExploration(started.exploration);
      const { exploration: finished, html } = await waitForBriefReady(started.exploration.exploration_id);
      setExploration(finished);
      setBriefHtml(html);
      await loadHome();
      setFlow("ready");
      setRefinementTargetExplorationId(null);
      setMessage(finished.progress.built_with_issues ? "Brief ready with issues" : refinementTargetExplorationId ? "Refined brief rebuilt" : "Brief ready");
      openBrief(finished);
    } catch (error) {
      setFlow(startedExploration ? "building" : "confirm");
      setMessage(errorMessage(error, "Could not build brief"));
    } finally {
      setBusy(false);
      endRefinementProgress();
    }
  }

  async function rebuildBrief() {
    if (!exploration || !hasEnabledSource(selectedEnabledSources)) return;
    setBusy(true);
    setFlow("building");
    setMessage("Rebuilding the brief...");
    setBriefHtml("");
    let startedExploration: Exploration | null = null;
    try {
      const started = await api<{ exploration: Exploration }>(`/api/explore/explorations/${exploration.exploration_id}/rebuild`, {
        method: "POST",
        body: JSON.stringify({
          source_selection: selectedEnabledSources,
          candidate_limit: draft.content_limits.total_items,
          lookback_hours: lookbackHoursForBuild(topicProfile?.profile ?? session?.profile, draft, defaultControls.lookback_hours),
        }),
      });
      startedExploration = started.exploration;
      setExploration(started.exploration);
      const { exploration: finished, html } = await waitForBriefReady(started.exploration.exploration_id);
      setExploration(finished);
      setBriefHtml(html);
      await loadHome();
      setFlow("ready");
      setMessage(finished.progress.built_with_issues ? "Brief rebuilt with issues" : "Brief rebuilt");
      openBrief(finished);
    } catch (error) {
      setFlow(startedExploration ? "building" : "ready");
      setMessage(errorMessage(error, "Could not rebuild brief"));
    } finally {
      setBusy(false);
    }
  }

  const startRefineExisting = useCallback(async (targetExploration = exploration) => {
    if (!targetExploration) return;
    setBusy(true);
    setFlow("refining");
    setMessage("Reopening refinement...");
    beginRefinementProgress("starting", "Reopening refinement");
    try {
      const topic = topicProfile?.topic_id === targetExploration.topic_id
        ? topicProfile
        : await api<TopicProfileResponse>(`/api/explore/topic-profiles/${targetExploration.topic_id}`);
      const nextSession = await api<RefinementSession>("/api/explore/refinement-sessions", {
        method: "POST",
        body: JSON.stringify({
          statement: topic.statement,
          topic_id: topic.topic_id,
          revisit: true,
          source_selection: targetExploration.source_selection,
          models: {},
        }),
      });
      clearInterestDraft();
      setRefinementTargetExplorationId(targetExploration.exploration_id);
      setTopicProfile(topic);
      setSubmittedInterest(topic.statement);
      setStatement("");
      setSourceSelection(sourceSelectionFromRecord(targetExploration.source_selection));
      setSession(nextSession);
      setDraft(draftFromProfile(nextSession.profile, defaultControls.content_limits));
      setAnswer("");
      setBriefHtml("");
      if (nextSession.topic_profile) setTopicProfile(nextSession.topic_profile);
      setFlow(nextSession.status === "finalized" ? "confirm" : "refining");
      setMessage(nextSession.status === "finalized" ? "Confirm the refined setup" : "Refine the brief before rebuilding");
    } catch (error) {
      setFlow(targetExploration.status === "complete" ? "ready" : "idle");
      setMessage(errorMessage(error, "Could not reopen refinement"));
    } finally {
      setBusy(false);
      endRefinementProgress();
    }
  }, [beginRefinementProgress, defaultControls.content_limits, endRefinementProgress, exploration, topicProfile]);

  async function waitForBriefReady(explorationId: string): Promise<{ exploration: Exploration; html: string }> {
    for (let attempt = 0; attempt < 667; attempt += 1) {
      const next = await api<Exploration>(`/api/explore/explorations/${explorationId}`);
      setExploration(next);
      if (next.status === "failed") {
        throw new Error(next.progress.error || "Brief build failed");
      }
      if (next.status === "complete") {
        const html = await fetchBriefHtml(next);
        if (html) return { exploration: next, html };
      }
      await sleep(1800);
    }
    throw new Error("Brief build timed out while waiting for the finished brief");
  }

  async function fetchBriefHtml(record: Exploration): Promise<string | null> {
    const path = briefPath(record);
    if (!path) return null;
    const response = await fetch(path);
    if (response.ok) {
      const html = await response.text();
      return html.trim() ? html : null;
    }
    return null;
  }

  async function scheduleBrief() {
    if (!topicProfile || !exploration || exploration.status !== "complete") return;
    setBusy(true);
    setMessage("Scheduling digest...");
    try {
      const scheduled = await api<TopicProfileResponse>(`/api/explore/topic-profiles/${topicProfile.topic_id}/schedule`, {
        method: "POST",
        body: JSON.stringify({
          schedule: schedulePreset,
          time_of_day: scheduleTime,
          timezone: "America/Los_Angeles",
          email_enabled: emailOnSchedule,
        }),
      });
      setTopicProfile(scheduled);
      await loadHome();
      setFlow("ready");
      setMessage("Digest scheduled");
    } catch (error) {
      setMessage(errorMessage(error, "Could not schedule digest"));
    } finally {
      setBusy(false);
    }
  }

  async function sendToInbox(recipientEmail: string) {
    if (!exploration) return;
    const recipient = recipientEmail.trim();
    if (!recipient || !recipient.includes("@")) {
      setMessage("Enter a valid email address");
      return;
    }
    setBusy(true);
    setMessage("Sending brief...");
    try {
      const result = await api<{ status: string; error?: string }>(`/api/explore/explorations/${exploration.exploration_id}/email`, {
        method: "POST",
        body: JSON.stringify({ recipient_email: recipient }),
      });
      if (result.status !== "sent") {
        setMessage(result.error ?? "Email delivery skipped");
      } else {
        setExploration({ ...exploration, emailed: true });
        setMessage(`Sent to ${recipient}`);
      }
    } catch (error) {
      setMessage(errorMessage(error, "Could not send brief"));
    } finally {
      setBusy(false);
    }
  }

  function openBrief(record = exploration) {
    const path = record ? briefPath(record) : null;
    openPath(path);
  }

  async function deleteHomeExploration(item: Extract<HomeRecentItem, { kind: "exploration" }>) {
    const title = homeRecentTitle(item);
    setBusy(true);
    try {
      const result = await api<{ exploration: Exploration; undo_available_until?: string | null }>(
        `/api/explore/explorations/${item.exploration.exploration_id}`,
        { method: "DELETE" },
      );
      setRecentExplorations((current) => current.filter((record) => record.exploration_id !== item.exploration.exploration_id));
      if (exploration?.exploration_id === item.exploration.exploration_id) {
        resetForNewBrief();
      }
      await loadHome();
      setHomeDeleteUndo({
        explorationId: item.exploration.exploration_id,
        title,
        until: result.undo_available_until ?? result.exploration.delete_after ?? null,
      });
      setMessage("Brief deleted. You can undo for 7 days.");
    } catch (error) {
      setMessage(errorMessage(error, "Could not delete brief"));
    } finally {
      setBusy(false);
    }
  }

  async function restoreHomeExploration() {
    if (!homeDeleteUndo) return;
    setBusy(true);
    try {
      await api(`/api/explore/explorations/${homeDeleteUndo.explorationId}/restore`, { method: "POST" });
      setHomeDeleteUndo(null);
      await loadHome();
      setMessage("Brief restored");
    } catch (error) {
      setMessage(errorMessage(error, "Could not restore brief"));
    } finally {
      setBusy(false);
    }
  }

  async function openHomeRecentItem(item: HomeRecentItem) {
    if (item.kind === "topic") {
      loadTopicForConfirmation(item.topic);
      return;
    }
    if (item.exploration.status === "queued" || item.exploration.status === "running") {
      setExploration(item.exploration);
      setTopicProfile(item.topic);
      if (item.topic) {
        setStatement(item.topic.statement);
        setDraft(draftFromProfile(item.topic.profile, defaultControls.content_limits));
        setSourceSelection(sourceSelectionFromRecord(item.topic.profile.source_selection));
      }
      setFlow("building");
      setMessage(item.exploration.status === "queued" ? "Brief is queued..." : "Brief is still building...");
      try {
        const { exploration: finished, html } = await waitForBriefReady(item.exploration.exploration_id);
        setExploration(finished);
        setBriefHtml(html);
        await loadHome();
        setFlow("ready");
        setMessage(finished.progress.built_with_issues ? "Brief ready with issues" : "Brief ready");
        openBrief(finished);
      } catch (error) {
        setMessage(errorMessage(error, "Could not refresh the building brief"));
      }
      return;
    }
    if (briefPath(item.exploration)) {
      openBrief(item.exploration);
      return;
    }
    if (item.topic) {
      loadTopicForConfirmation(item.topic);
      return;
    }
    setMessage("This brief is not ready yet");
  }

  function loadTopicForConfirmation(topic: TopicProfileResponse) {
    clearInterestDraft();
    setRefinementTargetExplorationId(null);
    setTopicProfile(topic);
    setSession(null);
    setExploration(null);
    setBriefHtml("");
    setAnswer("");
    setStatement(topic.statement);
    setSubmittedInterest(topic.statement);
    setDraft(draftFromProfile(topic.profile, defaultControls.content_limits));
    setSourceSelection(sourceSelectionFromRecord(topic.profile.source_selection));
    setFlow("confirm");
    setMessage("Saved brief plan loaded");
  }

  function resetForNewBrief() {
    clearInterestDraft();
    setStrategyConfirmation("");
    setRefinementTargetExplorationId(null);
    setStatement("");
    setSubmittedInterest("");
    setSession(null);
    setTopicProfile(null);
    setDraft(emptyDraft());
    setExploration(null);
    setBriefHtml("");
    setAnswer("");
    endRefinementProgress();
    setFlow("idle");
    setSourceSelection(defaultSourceSelection);
    setMessage("Ready");
  }

  function updateSource(key: SourceKey) {
    if (sourceLocked) return;
    const status = sourceStatus?.sources[key];
    if (status && !status.enabled) {
      setEnableSource(key);
      return;
    }
    setSourceSelection((current) => ({ ...current, [key]: !current[key] }));
  }

  async function refreshSourcesAndSelect(key: SourceKey) {
    const status = await api<SourceStatusResponse>("/api/explore/source-status");
    setSourceStatus(status);
    if (status.sources[key]?.enabled) {
      setSourceSelection((current) => ({ ...current, [key]: true }));
      setEnableSource(null);
    }
  }

  async function saveWebKey() {
    if (!webKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/web-search/credentials", {
        method: "POST",
        body: JSON.stringify({ provider: "tavily", api_key: webKey.trim() }),
      });
      setWebKey("");
      await refreshSourcesAndSelect(enableSource === "foreign_media" ? "foreign_media" : "web_search");
      setMessage(enableSource === "foreign_media" ? "Foreign Media connected" : "Web Search connected");
    } catch (error) {
      setMessage(errorMessage(error, "Could not connect Web Search"));
    } finally {
      setBusy(false);
    }
  }

  async function saveGmailClientSecret() {
    if (!gmailSecret.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/gmail/client-secret", {
        method: "POST",
        body: JSON.stringify({ client_secret_json: gmailSecret.trim() }),
      });
      setGmailSecret("");
      setMessage("Gmail OAuth client saved");
      await refreshSourcesAndSelect("gmail");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save Gmail setup"));
    } finally {
      setBusy(false);
    }
  }

  async function loadGmailClientFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      setGmailSecret(await file.text());
      setMessage("Gmail OAuth client file loaded");
    } catch (error) {
      setMessage(errorMessage(error, "Could not read Gmail OAuth file"));
    } finally {
      event.target.value = "";
    }
  }

  async function connectGmail() {
    setBusy(true);
    try {
      const result = await api<{ authorization_url: string }>("/api/admin/gmail/oauth/start", { method: "POST" });
      window.location.href = result.authorization_url;
    } catch (error) {
      setMessage(errorMessage(error, "Could not start Gmail connection"));
      setBusy(false);
    }
  }

  async function savePodcastCredentials() {
    if (!podcastKey.trim() || !podcastSecret.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/podcasts/credentials", {
        method: "POST",
        body: JSON.stringify({ api_key: podcastKey.trim(), api_secret: podcastSecret.trim() }),
      });
      setPodcastKey("");
      setPodcastSecret("");
      await refreshSourcesAndSelect("podcasts");
      setMessage("Podcast directory connected");
    } catch (error) {
      setMessage(errorMessage(error, "Could not connect podcasts"));
    } finally {
      setBusy(false);
    }
  }

  async function saveYoutubeCredentials() {
    if (!youtubeKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/youtube/credentials", {
        method: "POST",
        body: JSON.stringify({ api_key: youtubeKey.trim() }),
      });
      setYoutubeKey("");
      await refreshSourcesAndSelect("youtube");
      setMessage("YouTube connected");
    } catch (error) {
      setMessage(errorMessage(error, "Could not connect YouTube"));
    } finally {
      setBusy(false);
    }
  }

  async function saveFredCredentials() {
    if (!fredKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/fred/credentials", {
        method: "POST",
        body: JSON.stringify({ api_key: fredKey.trim() }),
      });
      setFredKey("");
      await refreshSourcesAndSelect("markets");
      setMessage("FRED connected");
    } catch (error) {
      setMessage(errorMessage(error, "Could not connect FRED"));
    } finally {
      setBusy(false);
    }
  }

  async function setupCollectionsSource() {
    setBusy(true);
    try {
      await api("/api/admin/collections/setup", { method: "POST" });
      await refreshSourcesAndSelect("collections");
      setMessage("Collections folder ready");
    } catch (error) {
      setMessage(errorMessage(error, "Could not set up Collections"));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!initialRefineExplorationId) return;
    const explorationId = initialRefineExplorationId;
    let cancelled = false;
    setBusy(true);
    setFlow("refining");
    setMessage("Loading brief to refine...");
    const now = Date.now();
    setRefinementFallbackStartedAt(now);
    setProgressNow(now);
    setRefinementProgress({ phase: "starting", label: "Reopening refinement", startedAt: now });
    void (async () => {
      try {
        const target = await api<Exploration>(`/api/explore/explorations/${explorationId}`);
        const topic = await api<TopicProfileResponse>(`/api/explore/topic-profiles/${target.topic_id}`);
        const nextSession = await api<RefinementSession>("/api/explore/refinement-sessions", {
          method: "POST",
          body: JSON.stringify({
            statement: topic.statement,
            topic_id: topic.topic_id,
            revisit: true,
            source_selection: target.source_selection,
            models: {},
          }),
        });
        if (cancelled) return;
        clearInterestDraft();
        setExploration(target);
        setRefinementTargetExplorationId(target.exploration_id);
        setTopicProfile(topic);
        setSubmittedInterest(topic.statement);
        setStatement("");
        setSourceSelection(sourceSelectionFromRecord(target.source_selection));
        setSession(nextSession);
        setDraft(draftFromProfile(nextSession.profile));
        setAnswer("");
        setBriefHtml("");
        setFlow(nextSession.status === "finalized" ? "confirm" : "refining");
        setMessage(nextSession.status === "finalized" ? "Confirm the refined setup" : "Refine the brief before rebuilding");
      } catch (error) {
        if (!cancelled) setMessage(errorMessage(error, "Could not load brief to refine"));
      } finally {
        if (!cancelled) {
          setBusy(false);
          setRefinementProgress(null);
          setRefinementFallbackStartedAt(0);
          setProgressNow(Date.now());
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [initialRefineExplorationId]);

  return (
    <main className="dispatch-page">
      <section className="dispatch-frame">
        <header className="dispatch-header">
          <a className="brand-lockup" href="/" aria-label="Dispatch home">
            <span className="brand-mark">◔</span>
            <span>Dispatch</span>
          </a>
          <span className="release-stamp">{releaseStamp(adminStatus)}</span>
          <a className="icon-menu" href="/admin" aria-label="Open Admin">•••</a>
        </header>

        <section className="dispatch-body">
          {homeRecentItems.length || activeDigest ? (
            <div className="recent-block">
              <div className="section-header-row">
                <p className="section-kicker">Recent Briefs</p>
                <DisclosureButton
                  expanded={recentExpanded}
                  label={recentExpanded ? "Hide" : "Show"}
                  onToggle={() => setRecentExpanded((current) => !current)}
                />
              </div>
              {recentExpanded ? (
                <>
                  {activeDigest ? (
                    <div className="active-digest-row">
                      <span className="live-dot" />
                      <strong>{profileName(activeDigest)}</strong>
                      <button
                        type="button"
                        className="active-digest-link"
                        onClick={() => activeDigest.latest_exploration && openBrief(activeDigest.latest_exploration)}
                        disabled={!activeDigest.latest_exploration}
                      >
                        Last ran: {formatDateTime(activeDigest.latest_exploration?.finished_at ?? activeDigest.latest_exploration?.started_at)}
                      </button>
                    </div>
                  ) : null}
                  <div className="recent-list">
                    {homeRecentItems.map((item) => (
                      <div className="recent-pill-row" key={homeRecentKey(item)}>
                        <button className="recent-pill" onClick={() => void openHomeRecentItem(item)}>
                          <span>{homeRecentIcon(item)}</span>
                          <strong>{homeRecentTitle(item)}</strong>
                          <em>{homeRecentMeta(item)}</em>
                          {item.digest ? <b>digest</b> : homeRecentBadge(item) ? <b>{homeRecentBadge(item)}</b> : null}
                        </button>
                        {item.kind === "exploration" ? (
                          <button
                            type="button"
                            className="recent-delete"
                            onClick={() => void deleteHomeExploration(item)}
                            disabled={busy}
                            aria-label={`Delete ${homeRecentTitle(item)}`}
                          >
                            Delete
                          </button>
                        ) : null}
                      </div>
                    ))}
                  </div>
                  {homeDeleteUndo ? (
                    <div className="undo-note">
                      <span>
                        Deleted "{homeDeleteUndo.title}".
                        {homeDeleteUndo.until ? ` Undo until ${formatDateTime(homeDeleteUndo.until)}.` : " Undo is available for 7 days."}
                      </span>
                      <button type="button" className="secondary-action" onClick={() => void restoreHomeExploration()} disabled={busy}>Undo</button>
                    </div>
                  ) : null}
                </>
              ) : null}
            </div>
          ) : null}

          {flow === "refining" || refinementProgress ? (
            <RefinementPanel
              session={session}
              interest={submittedInterest || statement}
              profile={session?.profile ?? topicProfile?.profile ?? null}
              sourceSelection={selectedEnabledSources}
              answer={answer}
              busy={busy}
              progress={activeRefinementProgress}
              now={progressNow}
              onAnswerChange={setAnswer}
              onSend={() => void answerRefinement(false)}
              onJustGo={() => void answerRefinement(true)}
            />
          ) : null}

          {flow === "confirm" ? (
            <ConfirmationPanel
              draft={draft}
              profile={session?.profile ?? topicProfile?.profile ?? null}
              strategyPreview={session?.strategy_preview ?? null}
              pendingStrategy={session?.pending_strategy_refinement ?? null}
              strategyConfirmation={strategyConfirmation}
              sources={sourceSelection}
              sourceStatus={sourceStatus}
              defaultContentLimits={defaultControls.content_limits}
              busy={busy}
              onDraftChange={setDraft}
              onSourceClick={updateSource}
              onStrategyRefine={(instruction) => void refineSearchStrategy(instruction)}
              onStrategyConfirm={(apply) => void confirmStrategyRefinement(apply)}
              onBuild={() => void buildBrief()}
              youtubePresets={briefSettings?.youtube_presets ?? defaultBriefControls.youtube_presets}
            />
          ) : null}

          {flow === "building" && !exploration ? (
            <BuildStartingPanel />
          ) : null}

          {visibleBuild ? (
            <ProgressPanel exploration={visibleBuild} sourceSelection={visibleBuild.source_selection ?? selectedEnabledSources} />
          ) : null}

          {flow === "ready" && exploration ? (
            <BriefReadyPanel
              exploration={exploration}
              issues={currentIssues}
              html={briefHtml}
              emailSendReady={emailSendReady}
              emailRecipient={briefEmailRecipient}
              busy={busy}
              onOpen={() => openBrief()}
              onEditSources={() => setFlow("confirm")}
              onRefine={() => void startRefineExisting()}
              onRebuild={() => void rebuildBrief()}
              onSchedule={() => setFlow("schedule")}
              onEmailRecipientChange={setBriefEmailRecipient}
              onSend={(recipient) => void sendToInbox(recipient)}
              onNew={resetForNewBrief}
            />
          ) : null}

          {flow === "schedule" ? (
            <SchedulePanel
              preset={schedulePreset}
              time={scheduleTime}
              emailEnabled={emailOnSchedule}
              deliveryConfigured={deliveryConfigured}
              busy={busy}
              onPresetChange={setSchedulePreset}
              onTimeChange={setScheduleTime}
              onEmailChange={setEmailOnSchedule}
              onCancel={() => setFlow("ready")}
              onSchedule={() => void scheduleBrief()}
            />
          ) : null}
        </section>

        {flow === "idle" || flow === "ready" ? (
        <form className="composer" onSubmit={startFlow}>
          <textarea
            value={statement}
            onChange={(event) => {
              setStatement(event.target.value);
              if (flow === "ready") {
                setFlow("idle");
                setExploration(null);
                setBriefHtml("");
                setRefinementTargetExplorationId(null);
              }
            }}
            onFocus={() => {
              if (flow === "idle" && statement.trim()) saveInterestDraft(statement);
            }}
            placeholder="Describe what you're interested in?"
            rows={4}
            disabled={busy}
          />
          {activeRefinementProgress ? (
            <div className="composer-progress">
              <RefinementStatusIndicator progress={activeRefinementProgress} now={progressNow} />
            </div>
          ) : null}
          <div className="composer-footer">
            <SourceChips
              selection={sourceSelection}
              status={sourceStatus}
              locked={sourceLocked}
              onToggle={updateSource}
            />
            <button className="primary-action" type="submit" disabled={!canSubmitInterest}>
              Submit
            </button>
          </div>
        </form>
        ) : null}
      </section>
      <p className="screen-reader-status" aria-live="polite">{message}</p>
      {enableSource ? (
        <EnableSourceModal
          source={enableSource}
          status={sourceStatus?.sources[enableSource]}
          webKey={webKey}
          gmailSecret={gmailSecret}
          podcastKey={podcastKey}
          podcastSecret={podcastSecret}
          youtubeKey={youtubeKey}
          fredKey={fredKey}
          busy={busy}
          onClose={() => setEnableSource(null)}
          onWebKeyChange={setWebKey}
          onGmailSecretChange={setGmailSecret}
          onGmailFileChange={(event) => void loadGmailClientFile(event)}
          onPodcastKeyChange={setPodcastKey}
          onPodcastSecretChange={setPodcastSecret}
          onYoutubeKeyChange={setYoutubeKey}
          onFredKeyChange={setFredKey}
          onSaveWeb={() => void saveWebKey()}
          onSaveGmailSecret={() => void saveGmailClientSecret()}
          onConnectGmail={() => void connectGmail()}
          onSavePodcast={() => void savePodcastCredentials()}
          onSaveYoutube={() => void saveYoutubeCredentials()}
          onSaveFred={() => void saveFredCredentials()}
          onSetupCollections={() => void setupCollectionsSource()}
          onRetry={() => void refreshSourcesAndSelect(enableSource)}
        />
      ) : null}
      {activeRefinementProgress ? (
        <RefinementProgressOverlay
          progress={activeRefinementProgress}
          now={progressNow}
          summary={progressIntentSummary(session?.profile ?? topicProfile?.profile ?? null, submittedInterest || statement)}
        />
      ) : null}
    </main>
  );
}

function RefinementPanel(props: {
  session: RefinementSession | null;
  interest: string;
  profile: TopicProfile | null;
  sourceSelection: Record<string, boolean>;
  answer: string;
  busy: boolean;
  progress: RefinementProgress | null;
  now: number;
  onAnswerChange: (value: string) => void;
  onSend: () => void;
  onJustGo: () => void;
}) {
  return (
    <section className="conversation-panel">
      <div className="refinement-workspace-header">
        <div>
          <p className="section-kicker">Refining brief</p>
          <h2>{props.profile?.scope || "Turning your interest into a search plan"}</h2>
        </div>
        <span className="status-pill good">{props.session?.status === "finalized" ? "Ready to confirm" : "In progress"}</span>
      </div>
      <div className="refinement-request-card">
        <strong>You asked</strong>
        <p>{props.interest}</p>
        <small>{sourcePlan(props.sourceSelection)}</small>
      </div>
      <RefinementStatusIndicator progress={props.progress} now={props.now} />
      <RefinementPlanPreview profile={props.profile} />
      <div className="chat-list">
        {(props.session?.messages ?? []).map((message, index) => (
          <div className={`chat-bubble ${message.role}`} key={`${message.role}-${index}`}>
            <ChatMessageContent content={message.content} />
          </div>
        ))}
        {!props.session ? (
          <div className="chat-bubble assistant">
            I’m preparing the first question and search strategy.
          </div>
        ) : null}
      </div>
      <div className="refinement-input">
        <input
          value={props.answer}
          onChange={(event) => props.onAnswerChange(event.target.value)}
          placeholder="Answer..."
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              props.onSend();
            }
          }}
        />
        <button type="button" onClick={props.onSend} disabled={props.busy || !props.answer.trim()}>
          Send
        </button>
        <button type="button" className="secondary-action" onClick={props.onJustGo} disabled={props.busy}>
          Just go now
        </button>
      </div>
    </section>
  );
}

type GmailCandidateLine = {
  index: string;
  name: string;
  sender: string;
  count: string;
  subject: string;
  rationale?: string;
};

function ChatMessageContent(props: { content: string }) {
  const gmailCandidateMessage = parseGmailCandidateMessage(props.content);
  if (gmailCandidateMessage) {
    return (
      <div className="gmail-candidate-message">
        <p>{gmailCandidateMessage.intro}</p>
        <ol className="gmail-candidate-list">
          {gmailCandidateMessage.candidates.map((candidate) => (
            <li key={`${candidate.index}-${candidate.sender}`}>
              <div>
                <strong>{candidate.name}</strong>
                <span>{candidate.sender}</span>
              </div>
              <small>{candidate.count} found · Latest: {candidate.subject}</small>
              {candidate.rationale ? <small>{candidate.rationale}</small> : null}
            </li>
          ))}
        </ol>
        <p>{gmailCandidateMessage.prompt}</p>
      </div>
    );
  }
  return <>{props.content}</>;
}

function parseGmailCandidateMessage(content: string): { intro: string; candidates: GmailCandidateLine[]; prompt: string } | null {
  const lines = content.split("\n").map((line) => line.trim()).filter(Boolean);
  const firstCandidateIndex = lines.findIndex((line) => /^\d+\.\s/.test(line));
  if (firstCandidateIndex < 1) return null;
  const candidates: GmailCandidateLine[] = [];
  let promptStart = lines.length;
  for (let index = firstCandidateIndex; index < lines.length; index += 1) {
    const match = lines[index].match(
      /^(\d+)\.\s+(.+?)\s+<([^>]+)>\s+\((\d+)\s+found;\s+latest subject:\s+(.+?)\)(?:\s+[—-]\s+(.+))?$/i,
    );
    if (!match) {
      promptStart = index;
      break;
    }
    candidates.push({
      index: match[1],
      name: match[2],
      sender: match[3],
      count: match[4],
      subject: match[5],
      rationale: match[6],
    });
  }
  if (!candidates.length || !lines[0].includes("found newsletter candidates")) return null;
  return {
    intro: lines.slice(0, firstCandidateIndex).join(" "),
    candidates,
    prompt: lines.slice(promptStart).join(" "),
  };
}

function RefinementStatusIndicator(props: { progress: RefinementProgress | null; now: number }) {
  if (!props.progress) {
    return (
      <div className="refinement-status-card idle">
        <div>
          <strong>Waiting for your next answer</strong>
          <p>The current search strategy is saved in this workspace.</p>
        </div>
      </div>
    );
  }
  const state = refinementProgressState(props.progress, props.now);
  return (
    <div className={`refinement-status-card ${state.alert ? "alert" : ""}`}>
      <div className="refinement-status-top">
        <div>
          <strong>{state.stage}</strong>
          <p>{state.detail}</p>
        </div>
        <span>{formatElapsed(state.elapsedMs)}</span>
      </div>
      <div className="refinement-activity-row">
        <span className="activity-pulse" />
        <small>{state.activity}</small>
      </div>
    </div>
  );
}

function RefinementProgressOverlay(props: { progress: RefinementProgress; now: number; summary: string }) {
  return (
    <div className="refinement-progress-backdrop" role="status" aria-live="polite">
      <div className="refinement-progress-modal">
        <p className="section-kicker">Refinement running</p>
        <h2>Working on your brief plan</h2>
        <p className="muted">{props.summary}</p>
        <RefinementStatusIndicator progress={props.progress} now={props.now} />
      </div>
    </div>
  );
}

function RefinementPlanPreview(props: { profile: TopicProfile | null }) {
  const plan = searchPlanItems(props.profile);
  const subtopics = props.profile?.subtopics ?? [];
  if (!props.profile && !plan.length) return null;
  return (
    <div className="refinement-plan-preview">
      <div>
        <strong>What I’ve understood</strong>
        <p>{props.profile?.scope || "I’m extracting the angle, sources, and search terms."}</p>
      </div>
      {subtopics.length ? (
        <div className="mini-chip-row">
          {subtopics.slice(0, 5).map((subtopic) => <span key={subtopic}>{subtopic}</span>)}
        </div>
      ) : null}
      {plan.length ? (
        <div className="mini-chip-row">
          {plan.slice(0, 6).map((query) => <span key={query}>{query}</span>)}
        </div>
      ) : null}
    </div>
  );
}

function StrategyReviewCard(props: { preview: StrategyPreview }) {
  const { preview } = props;
  const looksAt = preview.looks_at.filter((label) => label.toLowerCase() !== "reddit");
  const ignores = preview.ignores.filter((label) => label.toLowerCase() !== "reddit");
  return (
    <div className="strategy-review-card">
      {preview.reasoning_summary ? (
        <p className="strategy-review-summary">{preview.reasoning_summary}</p>
      ) : null}
      <div className="strategy-review-row">
        {looksAt.length ? (
          <div className="strategy-review-block">
            <strong>Looks at</strong>
            <span>{looksAt.join(", ")}</span>
          </div>
        ) : null}
        {ignores.length ? (
          <div className="strategy-review-block">
            <strong>Ignores</strong>
            <span>{ignores.join(", ")}</span>
          </div>
        ) : null}
        {preview.exclusions.length ? (
          <div className="strategy-review-block">
            <strong>Avoids</strong>
            <span>{preview.exclusions.join(", ")}</span>
          </div>
        ) : null}
      </div>
      {preview.search_queries.length ? (
        <div className="strategy-review-block">
          <strong>Searches it will run</strong>
          <ul className="strategy-review-queries">
            {preview.search_queries.map((query) => (
              <li key={query}>{query}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {preview.per_source.some((entry) => entry.approved_senders?.length) ? (
        <div className="strategy-review-block">
          <strong>Approved Gmail newsletters</strong>
          {preview.per_source
            .filter((entry) => entry.approved_senders?.length)
            .map((entry) => (
              <span key={entry.key}>{entry.approved_senders!.join(", ")}</span>
            ))}
        </div>
      ) : null}
      {preview.per_source.some((entry) => entry.tickers?.length) ? (
        <div className="strategy-review-block">
          <strong>Market tickers</strong>
          <div className="strategy-review-tickers">
            {preview.per_source
              .filter((entry) => entry.tickers?.length)
              .flatMap((entry) => entry.tickers!)
              .map((ticker) => (
                <span key={ticker} className="strategy-ticker-chip">{ticker}</span>
              ))}
          </div>
          {preview.per_source
            .filter((entry) => entry.tickers?.length && entry.note)
            .map((entry) => (
              <span key={entry.key} className="strategy-review-note">{entry.note}</span>
            ))}
        </div>
      ) : null}
    </div>
  );
}

function ConfirmationPanel(props: {
  draft: ConfirmationDraft;
  profile: TopicProfile | null;
  strategyPreview: StrategyPreview | null;
  pendingStrategy: PendingStrategyRefinement | null;
  strategyConfirmation: string;
  sources: Record<SourceKey, boolean>;
  sourceStatus: SourceStatusResponse | null;
  defaultContentLimits: ContentLimitsDraft;
  busy: boolean;
  onDraftChange: (draft: ConfirmationDraft) => void;
  onSourceClick: (source: SourceKey) => void;
  onStrategyRefine: (instruction: string) => void;
  onStrategyConfirm: (apply: boolean) => void;
  onBuild: () => void;
  youtubePresets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [strategyInstruction, setStrategyInstruction] = useState("");
  const strategyTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const reviewProfile = props.pendingStrategy?.proposed_profile ?? props.profile;
  const reviewPreview = props.pendingStrategy?.strategy_preview ?? props.strategyPreview;
  const contentLimitErrors = validateContentLimits(props.draft.content_limits, props.sources);
  const searchPlanGroups = sourceSearchPlanGroups(reviewProfile);
  const readinessItems = sourceReadinessItems(props.sources, props.sourceStatus, reviewProfile);
  const buildBlockedByProposal = Boolean(props.pendingStrategy);

  function updateContentLimits(next: ContentLimitsDraft) {
    props.onDraftChange({ ...props.draft, content_limits: next });
  }

  return (
    <section className="confirmation-panel">
      <div className="panel-title-row">
        <div>
          <p className="section-kicker">Confirm Setup</p>
          <h2>{props.draft.scope || props.profile?.statement || "Brief setup"}</h2>
        </div>
      </div>
      <div className="confirm-grid">
        <label>
          Scope
          <input
            value={props.draft.scope}
            onChange={(event) => props.onDraftChange({ ...props.draft, scope: event.target.value })}
          />
        </label>
        <label>
          Depth
          <select
            value={props.draft.depth}
            onChange={(event) => props.onDraftChange({ ...props.draft, depth: event.target.value as ConfirmationDraft["depth"] })}
          >
            <option value="informed-generalist">Informed generalist</option>
            <option value="practitioner">Practitioner</option>
          </select>
        </label>
        <label>
          Source Scope
          <select
            value={props.draft.recency_weighting}
            onChange={(event) => props.onDraftChange({
              ...props.draft,
              recency_weighting: event.target.value as ConfirmationDraft["recency_weighting"],
              lookback_hours: lookbackHoursFromSourceScope(event.target.value as SourceScope),
              sourceScopeTouched: true,
            })}
          >
            <option value="breaking">Breaking News</option>
            <option value="recent">Recent Time</option>
            <option value="last_year">Within Last Year</option>
            <option value="all_available">As Much as possible</option>
          </select>
          <small>{sourceScopeConfirmation(props.draft.recency_weighting, props.draft.lookback_hours)}</small>
        </label>
        <label>
          Exclusions
          <input
            value={props.draft.exclusions}
            onChange={(event) => props.onDraftChange({ ...props.draft, exclusions: event.target.value })}
            placeholder="Anything to avoid"
          />
        </label>
      </div>
      <SourceChips selection={props.sources} status={props.sourceStatus} locked={false} onToggle={props.onSourceClick} />
      <div className="source-readiness-list">
        <strong>Source readiness</strong>
        {readinessItems.map((item) => (
          <span className={item.ready ? "ready" : "warning"} key={item.key}>{item.label}: {item.message}</span>
        ))}
      </div>
      {reviewProfile?.requested_sources?.length ? (
        <div className="requested-source-list">
          <strong>Requested sources</strong>
          {reviewProfile.requested_sources.map((source) => (
            <span key={`${source.adapter}-${source.ref}`}>{formatSourceLabel(source.adapter)}: {source.ref}</span>
          ))}
        </div>
      ) : null}
      {reviewProfile?.gmail_rules?.include_senders?.length ? (
        <div className="requested-source-list">
          <strong>Gmail rules</strong>
          <span>{reviewProfile.gmail_rules.intent || "Selected newsletters"}</span>
          <span>{gmailLookbackLabel(reviewProfile.gmail_rules.lookback_hours)} · {reviewProfile.gmail_rules.include_senders.join(", ")}</span>
        </div>
      ) : null}
      {searchPlanGroups.length ? (
        <div className="search-plan-list">
          <strong>Search plan</strong>
          {searchPlanGroups.map((group) => (
            <div className="search-plan-group" key={group.key}>
              <b>{group.label}</b>
              <div>
                {group.queries.map((query) => (
                  <span key={`${group.key}-${query}`}>{query}</span>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : null}
      <div className="strategy-refine-box">
        {reviewPreview ? <StrategyReviewCard preview={reviewPreview} /> : null}
        {props.strategyConfirmation ? (
          <div className={`strategy-confirmation ${props.pendingStrategy ? "pending" : ""}`}>
            <strong>{props.pendingStrategy ? "AI proposed update" : "AI update"}</strong>
            <p>{props.strategyConfirmation}</p>
            {props.pendingStrategy?.conversation?.length ? (
              <div className="strategy-conversation">
                {props.pendingStrategy.conversation.slice(-4).map((turn, index) => (
                  <div className={`strategy-turn ${turn.role === "user" ? "user" : "assistant"}`} key={`${turn.role}-${index}-${turn.content.slice(0, 20)}`}>
                    <b>{turn.role === "user" ? "You" : "AI"}</b>
                    <span>{turn.content}</span>
                  </div>
                ))}
              </div>
            ) : null}
            {props.pendingStrategy?.findings?.length ? (
              <ul className="strategy-findings">
                {props.pendingStrategy.findings.map((finding) => (
                  <li key={finding}>{finding}</li>
                ))}
              </ul>
            ) : null}
            {props.pendingStrategy ? (
              <div className="strategy-proposal-actions">
                <button
                  type="button"
                  className="primary-action"
                  onClick={() => props.onStrategyConfirm(true)}
                  disabled={props.busy}
                >
                  Apply proposed strategy
                </button>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => strategyTextareaRef.current?.focus()}
                  disabled={props.busy}
                >
                  Keep refining
                </button>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => props.onStrategyConfirm(false)}
                  disabled={props.busy}
                >
                  Discard
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        <label>
          {props.pendingStrategy ? "Keep refining this proposal" : "Refine search strategy"}
          <textarea
            ref={strategyTextareaRef}
            value={strategyInstruction}
            onChange={(event) => setStrategyInstruction(event.target.value)}
            placeholder={props.pendingStrategy ? "Add your next comment about this proposed update..." : "Add missing sources, entities, recency, countries, channels, podcasts, or search terms..."}
          />
        </label>
        <button
          type="button"
          className="secondary-action"
          onClick={() => {
            if (!strategyInstruction.trim()) {
              strategyTextareaRef.current?.focus();
              return;
            }
            props.onStrategyRefine(strategyInstruction);
            setStrategyInstruction("");
          }}
          disabled={props.busy}
        >
          Update strategy
        </button>
      </div>
      <div className="advanced-settings-shell">
        <DisclosureButton
          expanded={advancedOpen}
          label="Advanced Settings"
          onToggle={() => setAdvancedOpen((open) => !open)}
        />
        {advancedOpen ? (
          <>
            <ContentLimitsPanel
              limits={props.draft.content_limits}
              defaults={props.defaultContentLimits}
              sourceSelection={props.sources}
              resetLabel="Use system defaults"
              onChange={updateContentLimits}
              youtubePresets={props.youtubePresets}
            />
            <SettingsErrorList errors={contentLimitErrors} />
          </>
        ) : null}
      </div>
      <div className="confirmation-actions">
        {props.draft.recency_weighting !== "all_available" ? (
          <NumberStepper
            label="Recency window (days)"
            value={Math.max(1, Math.round(props.draft.lookback_hours / 24))}
            min={briefControlBounds.source_window_days.min}
            max={briefControlBounds.source_window_days.max}
            compact
            onChange={(days) => props.onDraftChange({
              ...props.draft,
              lookback_hours: clampContentLimit(days, 1, 365) * 24,
              sourceScopeTouched: true,
            })}
          />
        ) : null}
        <button
          type="button"
          className="primary-action build-brief-action"
          onClick={props.onBuild}
          disabled={props.busy || contentLimitErrors.length > 0 || buildBlockedByProposal}
          title={
            buildBlockedByProposal
              ? "Apply or discard the proposed strategy update before building."
              : undefined
          }
        >
          {props.pendingStrategy ? "Review proposal first" : "Build brief"}
        </button>
      </div>
    </section>
  );
}

function ContentLimitsPanel(props: {
  limits: ContentLimitsDraft;
  sourceSelection: Record<SourceKey, boolean>;
  defaults?: ContentLimitsDraft;
  resetLabel?: string;
  showReset?: boolean;
  onChange: (limits: ContentLimitsDraft) => void;
  youtubePresets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
  podcastPresets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
}) {
  const selectedSources = sourceOptions.filter((source) => props.sourceSelection[source.key]);
  const defaults = props.defaults ?? defaultContentLimits;

  function updateNumber(key: "total_items" | "target_items" | "lead_items", value: number) {
    props.onChange({ ...props.limits, [key]: value });
  }

  function updateSourceLimit(source: SourceKey, value: number) {
    props.onChange({
      ...props.limits,
      per_source: {
        ...props.limits.per_source,
        [source]: value,
      },
    });
  }

  function applyPreset(scale: number) {
    const scaled = (value: number, min: number, max: number) => clampContentLimit(Math.round(value * scale), min, max);
    const nextPerSource: Partial<Record<SourceKey, number>> = {};
    const presets = props.youtubePresets ?? { max: 25, large: 20, medium: 15, focused: 10 };
    const podcastPresets = props.podcastPresets ?? { max: 25, large: 20, medium: 15, focused: 10 };
    const systemMax = defaultContentLimits;
    
    for (const source of sourceOptions) {
      if (source.key === "youtube") {
        let ytVal = presets.medium;
        if (scale === 1.0) ytVal = presets.max;
        else if (scale === 0.8) ytVal = presets.large;
        else if (scale === 0.6) ytVal = presets.medium;
        else if (scale === 0.4) ytVal = presets.focused;
        nextPerSource[source.key] = ytVal;
      } else if (source.key === "podcasts") {
        let podcastVal = podcastPresets.medium;
        if (scale === 1.0) podcastVal = podcastPresets.max;
        else if (scale === 0.8) podcastVal = podcastPresets.large;
        else if (scale === 0.6) podcastVal = podcastPresets.medium;
        else if (scale === 0.4) podcastVal = podcastPresets.focused;
        nextPerSource[source.key] = podcastVal;
      } else {
        const maxValue = systemMax.per_source[source.key] ?? briefControlBounds.per_source.max;
        nextPerSource[source.key] = scaled(maxValue, briefControlBounds.per_source.min, briefControlBounds.per_source.max);
      }
    }
    props.onChange({
      ...props.limits,
      total_items: scaled(systemMax.total_items, briefControlBounds.total_items.min, briefControlBounds.total_items.max),
      target_items: scaled(systemMax.target_items, briefControlBounds.target_items.min, briefControlBounds.target_items.max),
      lead_items: scaled(systemMax.lead_items, briefControlBounds.lead_items.min, briefControlBounds.lead_items.max),
      per_source: nextPerSource,
    });
  }

  return (
    <div className="content-limits-panel">
      <div className="preset-control-row">
        <strong>Load preset</strong>
        <button type="button" onClick={() => applyPreset(1)}>Max</button>
        <button type="button" onClick={() => applyPreset(0.8)}>Large</button>
        <button type="button" onClick={() => applyPreset(0.6)}>Medium</button>
        <button type="button" onClick={() => applyPreset(0.4)}>Focused</button>
      </div>
      <div className="content-limit-grid">
        <NumberStepper
          label="Candidate budget"
          value={props.limits.total_items}
          min={briefControlBounds.total_items.min}
          max={briefControlBounds.total_items.max}
          onChange={(value) => updateNumber("total_items", value)}
        />
        <NumberStepper
          label="Target visible stories"
          value={props.limits.target_items}
          min={briefControlBounds.target_items.min}
          max={briefControlBounds.target_items.max}
          onChange={(value) => updateNumber("target_items", value)}
        />
        <NumberStepper
          label="Lead stories"
          value={props.limits.lead_items}
          min={briefControlBounds.lead_items.min}
          max={briefControlBounds.lead_items.max}
          onChange={(value) => updateNumber("lead_items", value)}
        />
        <label>
          Quality floor
          <select
            value={props.limits.quality_floor}
            onChange={(event) => props.onChange({ ...props.limits, quality_floor: event.target.value as ContentLimitsDraft["quality_floor"] })}
          >
            <option value="standard">Standard signal</option>
            <option value="strong">Strong signal only</option>
          </select>
        </label>
      </div>
      {selectedSources.length ? (
        <div className="source-limit-list">
          <strong>Per-source maximums</strong>
          {selectedSources.map((source) => (
            <NumberStepper
              key={source.key}
              label={source.label}
              value={props.limits.per_source[source.key] ?? defaults.per_source[source.key] ?? 3}
              min={briefControlBounds.per_source.min}
              max={briefControlBounds.per_source.max}
              compact
              onChange={(value) => updateSourceLimit(source.key, value)}
            />
          ))}
        </div>
      ) : null}
      {props.showReset !== false ? (
        <button type="button" className="ghost-action reset-limits-action" onClick={() => props.onChange(defaultContentLimits)}>
          {props.resetLabel ?? "Reset to defaults"}
        </button>
      ) : null}
    </div>
  );
}

function BriefControlsPanel(props: {
  controls: BriefControlsDraft;
  defaults: BriefControlsDraft;
  sourceSelection: Record<SourceKey, boolean>;
  showReset?: boolean;
  onChange: (controls: BriefControlsDraft) => void;
}) {
  const sourceWindowDays = Math.max(0, Math.round((Number(props.controls.lookback_hours) || 0) / 24));
  const presets = props.controls.youtube_presets ?? { max: 25, large: 20, medium: 15, focused: 10 };
  const podcastPresets = props.controls.podcast_presets ?? { max: 25, large: 20, medium: 15, focused: 10 };

  return (
    <div className="brief-controls-panel">
      <div className="content-limit-grid">
        <NumberStepper
          label="Source window (days)"
          value={sourceWindowDays}
          min={briefControlBounds.source_window_days.min}
          max={briefControlBounds.source_window_days.max}
          onChange={(days) => props.onChange({ ...props.controls, lookback_hours: days * 24 })}
        />
      </div>
      <ContentLimitsPanel
        limits={props.controls.content_limits}
        defaults={props.defaults.content_limits}
        sourceSelection={props.sourceSelection}
        showReset={false}
        onChange={(content_limits) => props.onChange({ ...props.controls, content_limits })}
        youtubePresets={props.controls.youtube_presets}
        podcastPresets={props.controls.podcast_presets}
      />
      <div className="settings-youtube-presets" style={{ marginTop: "24px", paddingTop: "18px", borderTop: "1px solid var(--line)" }}>
        <strong>YouTube scale presets</strong>
        <p className="muted" style={{ margin: "4px 0 12px", fontSize: "0.85rem" }}>Configure per-source video limits for YouTube for each profile scale (Max 25).</p>
        <div className="content-limit-grid">
          <NumberStepper
            label="Max profile"
            value={presets.max}
            min={1}
            max={25}
            onChange={(val) => props.onChange({
              ...props.controls,
              youtube_presets: { ...presets, max: val }
            })}
          />
          <NumberStepper
            label="Large profile"
            value={presets.large}
            min={1}
            max={25}
            onChange={(val) => props.onChange({
              ...props.controls,
              youtube_presets: { ...presets, large: val }
            })}
          />
          <NumberStepper
            label="Medium profile"
            value={presets.medium}
            min={1}
            max={25}
            onChange={(val) => props.onChange({
              ...props.controls,
              youtube_presets: { ...presets, medium: val }
            })}
          />
          <NumberStepper
            label="Focused profile"
            value={presets.focused}
            min={1}
            max={25}
            onChange={(val) => props.onChange({
              ...props.controls,
              youtube_presets: { ...presets, focused: val }
            })}
          />
        </div>
      </div>
      <div className="settings-youtube-presets" style={{ marginTop: "24px", paddingTop: "18px", borderTop: "1px solid var(--line)" }}>
        <strong>Podcast scale presets</strong>
        <p className="muted" style={{ margin: "4px 0 12px", fontSize: "0.85rem" }}>Configure per-source limits for podcast items for each profile scale (Max 25).</p>
        <div className="content-limit-grid">
          <NumberStepper
            label="Max profile"
            value={podcastPresets.max}
            min={1}
            max={25}
            onChange={(val) => props.onChange({
              ...props.controls,
              podcast_presets: { ...podcastPresets, max: val }
            })}
          />
          <NumberStepper
            label="Large profile"
            value={podcastPresets.large}
            min={1}
            max={25}
            onChange={(val) => props.onChange({
              ...props.controls,
              podcast_presets: { ...podcastPresets, large: val }
            })}
          />
          <NumberStepper
            label="Medium profile"
            value={podcastPresets.medium}
            min={1}
            max={25}
            onChange={(val) => props.onChange({
              ...props.controls,
              podcast_presets: { ...podcastPresets, medium: val }
            })}
          />
          <NumberStepper
            label="Focused profile"
            value={podcastPresets.focused}
            min={1}
            max={25}
            onChange={(val) => props.onChange({
              ...props.controls,
              podcast_presets: { ...podcastPresets, focused: val }
            })}
          />
        </div>
      </div>
      {props.showReset !== false ? (
        <button type="button" className="ghost-action reset-limits-action" onClick={() => props.onChange(props.defaults)}>
          Reset to defaults
        </button>
      ) : null}
    </div>
  );
}

function SystemLimitsPanel(props: { groups: SystemLimitGroup[] }) {
  return (
    <div className="system-limits-panel">
      {props.groups.map((group) => (
        <section className="system-limit-group" key={group.group}>
          <h3>{group.group}</h3>
          <div className="system-limit-grid">
            {group.items.map((item) => (
              <article className="system-limit-card" key={`${group.group}-${item.label}`}>
                <span>{item.label}</span>
                <strong>{item.value}</strong>
                {item.note ? <p>{item.note}</p> : null}
              </article>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function SettingsErrorList(props: { errors: string[] }) {
  if (!props.errors.length) return null;
  return (
    <div className="settings-error-box" role="alert">
      <strong>Fix these values before saving:</strong>
      <ul>
        {props.errors.map((error) => (
          <li key={error}>{error}</li>
        ))}
      </ul>
    </div>
  );
}

function PipelineLimitsPanel(props: {
  limits: PipelineLimitsDraft;
  defaults?: PipelineLimitsDraft;
  onChange?: (limits: PipelineLimitsDraft) => void;
  showReset?: boolean;
}) {
  const defaults = props.defaults ?? defaultPipelineLimits;
  const editable = Boolean(props.onChange);
  const updateLimit = (key: keyof PipelineLimitsDraft, value: number, min: number, max: number) => {
    if (!props.onChange) return;
    props.onChange({ ...props.limits, [key]: clampNumber(value, min, max) });
  };
  return (
    <div className="pipeline-limits-panel">
      <div className="pipeline-limit-grid">
        {pipelineLimitFields.map((field) => (
          <article className={editable ? "pipeline-limit-card editable" : "pipeline-limit-card"} key={field.key}>
            {editable ? (
              <NumberStepper
                label={field.label}
                value={props.limits[field.key] ?? defaults[field.key]}
                min={field.min}
                max={field.max}
                onChange={(value) => updateLimit(field.key, value, field.min, field.max)}
              />
            ) : (
              <div>
                <span>{field.label}</span>
                <strong>{props.limits[field.key] ?? defaults[field.key]}</strong>
              </div>
            )}
            <p>{field.note}</p>
          </article>
        ))}
      </div>
      {editable && props.showReset !== false ? (
        <button type="button" className="ghost-action reset-limits-action" onClick={() => props.onChange?.(defaults)}>
          Reset to system limits
        </button>
      ) : null}
    </div>
  );
}

function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

function NumberStepper(props: {
  label: string;
  value: number;
  min: number;
  max: number;
  compact?: boolean;
  onChange: (value: number) => void;
}) {
  const changeValue = (rawValue: string) => {
    const digits = rawValue.replace(/\D/g, "");
    props.onChange(digits ? Number(digits) : 0);
  };
  return (
    <label className={props.compact ? "number-stepper compact" : "number-stepper"}>
      {props.label}
      <span>
        <input
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          aria-invalid={props.value < props.min || props.value > props.max}
          value={props.value}
          onChange={(event) => changeValue(event.target.value)}
          onBlur={() => {
            if (!Number.isFinite(props.value)) props.onChange(0);
          }}
        />
      </span>
    </label>
  );
}

function ProgressPanel(props: { exploration: Exploration; sourceSelection: Record<string, boolean> }) {
  const pipeline = Object.entries(props.exploration.progress.pipeline ?? {});
  const sources = Object.entries(props.exploration.progress.sources ?? {});
  const filterNotes = filterDecisionNotes(props.exploration);
  const auditIssues = actionableIssues(props.exploration.progress.source_audit_issues);
  const queuedMessage = props.exploration.status === "queued"
    ? props.exploration.progress.queue?.message ?? "Waiting for the current brief build to finish."
    : null;
  return (
    <section className="progress-panel">
      <div className="progress-heading">
        <div>
          <p className="section-kicker">{props.exploration.status === "queued" ? "Queued" : "Full pipeline running"}</p>
          <h2>{progressHeadline(props.exploration)}</h2>
        </div>
        <span className={`status-pill ${props.exploration.status === "running" ? "good" : ""} ${isModelDegraded(props.exploration) ? "warning" : ""}`}>
          {isModelDegraded(props.exploration) ? "Needs attention" : formatStage(props.exploration.status)}
        </span>
      </div>
      <p className="queue-note">{progressDetail(props.exploration)}</p>
      <p className="section-kicker">{sourcePlan(props.sourceSelection)}</p>
      {queuedMessage ? <p className="queue-note">{queuedMessage}</p> : null}
      <div className="pipeline-row">
        {["discovery", "fetch", "summarize", "audit", "rank", "review", "done"].map((stage) => (
          <span className={`pipeline-pill ${props.exploration.progress.pipeline?.[stage] ?? "pending"}`} key={stage}>
            {formatStage(stage)}
          </span>
        ))}
      </div>
      {props.exploration.progress.source_audit?.message ? (
        <p className="queue-note">{props.exploration.progress.source_audit.message}</p>
      ) : props.exploration.progress.source_audit?.summary ? (
        <p className="queue-note">{props.exploration.progress.source_audit.summary}</p>
      ) : null}
      {isModelDegraded(props.exploration) ? (
        <div className="issue-note strong">
          <p>{modelDegradedMessage(props.exploration)}</p>
        </div>
      ) : null}
      <div className="source-progress-grid">
        {sources.map(([source, data]) => (
          <article className={`source-progress ${data.status}`} key={source}>
            <strong>{formatSourceLabel(source)}</strong>
            <span>{formatStage(data.status)}</span>
            <small>{data.candidate_count ? `${data.candidate_count} item(s)` : data.message ?? "Waiting"}</small>
          </article>
        ))}
      </div>
      {props.exploration.progress.requested_source_issues?.length ? (
        <div className="issue-note">
          {props.exploration.progress.requested_source_issues.map((issue) => (
            <p key={`${issue.source_name}-${issue.reason}`}>
              {issue.source_name}: {issue.reason}
            </p>
          ))}
        </div>
      ) : null}
      {auditIssues.length ? (
        <div className="issue-note">
          {auditIssues.map((issue) => (
            <p key={`${issue.source_name}-${issue.reason}`}>
              {issue.source_name}: {issue.reason}
            </p>
          ))}
        </div>
      ) : null}
      {filterNotes.length ? (
        <details className="filter-note">
          <summary>{filterNotes.length} item(s) filtered out</summary>
          <div className="filter-matrix" role="table" aria-label="Filtered source items">
            <div className="filter-matrix-row header" role="row">
              <strong>Source</strong>
              <strong>Item</strong>
              <strong>Reject reason</strong>
            </div>
            {filterNotes.slice(0, 40).map((issue) => (
              <div className="filter-matrix-row" role="row" key={`${issue.source_name}-${issue.item ?? ""}-${issue.reason}`}>
                <span>{issue.source || sourceFromIssueName(issue.source_name)}</span>
                <span>
                  {issue.item_url ? (
                    <a href={issue.item_url} target="_blank" rel="noreferrer">{issue.item || issue.source_name}</a>
                  ) : (
                    issue.item || issue.source_name
                  )}
                </span>
                <span>{issue.reason}</span>
              </div>
            ))}
          </div>
        </details>
      ) : null}
      {pipeline.length ? <p className="muted">{formatPipeline(pipeline)}</p> : null}
    </section>
  );
}

function BuildStartingPanel() {
  return (
    <section className="progress-panel" role="status" aria-live="polite">
      <div className="progress-heading">
        <div>
          <p className="section-kicker">Starting build</p>
          <h2>Starting the newsletter build</h2>
        </div>
        <span className="status-pill good">Starting</span>
      </div>
      <p className="queue-note">Creating the build job. Progress will appear here as soon as the server accepts it.</p>
      <div className="pipeline-row">
        {["discovery", "fetch", "summarize", "audit", "rank", "review", "done"].map((stage) => (
          <span className="pipeline-pill pending" key={stage}>
            {formatStage(stage)}
          </span>
        ))}
      </div>
    </section>
  );
}

function LibraryBuildProgress(props: { exploration: Exploration }) {
  const sources = Object.entries(props.exploration.progress.sources ?? {})
    .filter(([, data]) => data.status !== "disabled")
    .slice(0, 6);
  return (
    <div className="library-progress">
      <div className="library-progress-top">
        <strong>{progressHeadline(props.exploration)}</strong>
        <span>{isModelDegraded(props.exploration) ? "Needs attention" : formatStage(props.exploration.status)}</span>
      </div>
      <p>{progressDetail(props.exploration)}</p>
      <div className="pipeline-row compact">
        {["discovery", "fetch", "summarize", "audit", "rank", "review", "done"].map((stage) => (
          <span className={`pipeline-pill ${props.exploration.progress.pipeline?.[stage] ?? "pending"}`} key={stage}>
            {formatStage(stage)}
          </span>
        ))}
      </div>
      {sources.length ? (
        <div className="library-source-row">
          {sources.map(([source, data]) => (
            <span key={source}>{formatSourceLabel(source)}: {formatStage(data.status)}</span>
          ))}
        </div>
      ) : null}
      {isModelDegraded(props.exploration) ? (
        <p className="warning-text">{modelDegradedMessage(props.exploration)}</p>
      ) : null}
    </div>
  );
}

function BriefReadyPanel(props: {
  exploration: Exploration;
  issues: ExplorationIssue[];
  html: string;
  emailSendReady: boolean;
  emailRecipient: string;
  busy: boolean;
  onOpen: () => void;
  onEditSources: () => void;
  onRefine: () => void;
  onRebuild: () => void;
  onSchedule: () => void;
  onEmailRecipientChange: (value: string) => void;
  onSend: (recipient: string) => void;
  onNew: () => void;
}) {
  return (
    <section className="brief-ready-panel">
      {props.issues.length ? (
        <a className="brief-issue-link" href={`/admin?tab=library&issue_run=${props.exploration.exploration_id}`}>
          Issue Built without request sources; click here for details
        </a>
      ) : null}
      <div className="ready-actions">
        <button type="button" className="secondary-action" onClick={props.onEditSources}>Edit sources</button>
        <button type="button" className="secondary-action" onClick={props.onRefine} disabled={props.busy}>Refine</button>
        <button type="button" className="secondary-action" onClick={props.onRebuild} disabled={props.busy}>Rebuild</button>
        <button type="button" className="secondary-action" onClick={props.onSchedule}>Schedule as digest</button>
        <button type="button" className="ghost-action" onClick={props.onNew}>New brief</button>
      </div>
      {props.emailSendReady ? (
        <div className="email-send-box">
          <label>
            Email this brief
            <input
              type="email"
              value={props.emailRecipient}
              onChange={(event) => props.onEmailRecipientChange(event.target.value)}
              placeholder="name@example.com"
            />
          </label>
          <button
            type="button"
            className="secondary-action"
            onClick={() => props.onSend(props.emailRecipient)}
            disabled={props.busy || !props.emailRecipient.trim()}
          >
            Send brief
          </button>
          {props.exploration.emailed ? <span>Sent at least once</span> : null}
        </div>
      ) : (
        <p className="muted">Email sending needs Gmail send access in Admin before briefs can be sent.</p>
      )}
      {props.html ? (
        <button className="brief-preview" type="button" onClick={props.onOpen} aria-label="Open generated brief">
          <iframe title="Brief preview" srcDoc={props.html} />
        </button>
      ) : null}
    </section>
  );
}

function SchedulePanel(props: {
  preset: SchedulePreset;
  time: string;
  emailEnabled: boolean;
  deliveryConfigured: boolean;
  busy: boolean;
  onPresetChange: (preset: SchedulePreset) => void;
  onTimeChange: (time: string) => void;
  onEmailChange: (enabled: boolean) => void;
  onCancel: () => void;
  onSchedule: () => void;
}) {
  return (
    <section className="schedule-panel">
      <div className="panel-title-row">
        <div>
          <p className="section-kicker">Schedule</p>
          <h2>Make this a digest</h2>
        </div>
      </div>
      <div className="schedule-controls">
        <div className="segmented-control">
          {schedulePresets.map((option) => (
            <button
              key={option.value}
              type="button"
              className={props.preset === option.value ? "active" : ""}
              onClick={() => props.onPresetChange(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <label>
          Time
          <input type="time" value={props.time} onChange={(event) => props.onTimeChange(event.target.value)} />
        </label>
        {props.deliveryConfigured ? (
          <label className="checkbox-row">
            <input type="checkbox" checked={props.emailEnabled} onChange={(event) => props.onEmailChange(event.target.checked)} />
            Send by email
          </label>
        ) : (
          <p className="muted">Email can be enabled later in Admin.</p>
        )}
      </div>
      <div className="button-row">
        <button type="button" className="secondary-action" onClick={props.onCancel}>Cancel</button>
        <button type="button" className="primary-action" onClick={props.onSchedule} disabled={props.busy}>Schedule</button>
      </div>
    </section>
  );
}

function SourceChips(props: {
  selection: Record<SourceKey, boolean>;
  status: SourceStatusResponse | null;
  locked: boolean;
  onToggle: (source: SourceKey) => void;
}) {
  return (
    <div className="source-chips">
      {sourceOptions.map((source) => {
        const status = props.status?.sources[source.key];
        const enabled = status?.enabled ?? false;
        const selected = Boolean(props.selection[source.key] && enabled);
        return (
          <button
            type="button"
            key={source.key}
            className={`source-chip ${selected ? "selected" : ""} ${enabled ? "" : "disabled"}`}
            onClick={() => props.onToggle(source.key)}
            disabled={props.locked}
            aria-pressed={selected}
            data-source-state={selected ? "selected" : enabled ? "available" : "disabled"}
            title={enabled ? source.label : status?.reason ?? "Setup required"}
          >
            <span>{source.icon}</span>
            {source.label}
          </button>
        );
      })}
    </div>
  );
}

function EnableSourceModal(props: {
  source: SourceKey;
  status?: SourceStatus;
  webKey: string;
  gmailSecret: string;
  podcastKey: string;
  podcastSecret: string;
  youtubeKey: string;
  fredKey: string;
  busy: boolean;
  onClose: () => void;
  onWebKeyChange: (value: string) => void;
  onGmailSecretChange: (value: string) => void;
  onGmailFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onPodcastKeyChange: (value: string) => void;
  onPodcastSecretChange: (value: string) => void;
  onYoutubeKeyChange: (value: string) => void;
  onFredKeyChange: (value: string) => void;
  onSaveWeb: () => void;
  onSaveGmailSecret: () => void;
  onConnectGmail: () => void;
  onSavePodcast: () => void;
  onSaveYoutube: () => void;
  onSaveFred: () => void;
  onSetupCollections: () => void;
  onRetry: () => void;
}) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="enable-card">
        <button type="button" className="modal-close" onClick={props.onClose} aria-label="Close">×</button>
        <p className="section-kicker">Enable Source</p>
        <h2>Connect {props.status?.label ?? formatSourceLabel(props.source)}</h2>
        <p>{props.status?.reason ?? "This source needs setup before it can be selected."}</p>
        {props.source === "web_search" || props.source === "foreign_media" ? (
          <label>
            Web Search API key
            <input
              type="password"
              value={props.webKey}
              onChange={(event) => props.onWebKeyChange(event.target.value)}
              placeholder="Paste API key"
            />
            <button type="button" onClick={props.onSaveWeb} disabled={props.busy || !props.webKey.trim()}>
              Connect {props.source === "foreign_media" ? "Foreign Media" : "Web Search"}
            </button>
          </label>
        ) : null}
        {props.source === "gmail" ? (
          <div className="enable-stack">
            <button type="button" onClick={props.onConnectGmail} disabled={props.busy}>Connect Gmail</button>
            <label>
              OAuth client JSON file
              <input type="file" accept=".json,application/json" onChange={props.onGmailFileChange} />
            </label>
            <label>
              OAuth client JSON
              <textarea
                value={props.gmailSecret}
                onChange={(event) => props.onGmailSecretChange(event.target.value)}
                rows={5}
                placeholder='{"installed": ... }'
              />
              <button type="button" onClick={props.onSaveGmailSecret} disabled={props.busy || !props.gmailSecret.trim()}>
                Save OAuth Client
              </button>
            </label>
          </div>
        ) : null}
        {props.source === "podcasts" ? (
          <div className="enable-stack">
            <label>
              Podcast Index API key
              <input type="password" value={props.podcastKey} onChange={(event) => props.onPodcastKeyChange(event.target.value)} />
            </label>
            <label>
              Podcast Index API secret
              <input type="password" value={props.podcastSecret} onChange={(event) => props.onPodcastSecretChange(event.target.value)} />
            </label>
            <button type="button" onClick={props.onSavePodcast} disabled={props.busy || !props.podcastKey.trim() || !props.podcastSecret.trim()}>
              Connect Podcasts
            </button>
          </div>
        ) : null}
        {props.source === "youtube" ? (
          <label>
            YouTube Data API key
            <input
              type="password"
              value={props.youtubeKey}
              onChange={(event) => props.onYoutubeKeyChange(event.target.value)}
              placeholder="Paste API key"
            />
            <button type="button" onClick={props.onSaveYoutube} disabled={props.busy || !props.youtubeKey.trim()}>
              Connect YouTube
            </button>
          </label>
        ) : null}
        {props.source === "collections" ? (
          <div className="enable-stack">
            <p>{props.status?.root_path ? `Folder: ${props.status.root_path}` : "Collections uses local folders on this Mac."}</p>
            <button type="button" onClick={props.onSetupCollections} disabled={props.busy}>
              Create Collections Folder
            </button>
          </div>
        ) : null}
        {props.source === "markets" ? (
          <div className="enable-stack">
            <p>Markets uses free public-market data. For rich macroeconomic indicators (yield curve, interest rates, inflation, etc.), you can optionally provide a free FRED API key.</p>
            <label style={{ display: "flex", flexDirection: "column", gap: "6px", width: "100%", boxSizing: "border-box" }}>
              FRED API Key (optional)
              <input
                type="password"
                value={props.fredKey}
                onChange={(event) => props.onFredKeyChange(event.target.value)}
                placeholder="Paste FRED API key"
              />
            </label>
            <button type="button" onClick={props.onSaveFred} disabled={props.busy || !props.fredKey.trim()}>
              Save FRED Key
            </button>
            <div style={{ marginTop: "12px", borderTop: "1px solid var(--line)", paddingTop: "12px", display: "flex", justifyContent: "flex-end" }}>
              <button type="button" onClick={props.onRetry} disabled={props.busy}>Retry Markets</button>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}

type GmailAllowlistAction = "approve" | "reject" | "remove";

function GmailAllowlistGroup(props: {
  title: string;
  senders: GmailSenderRecord[];
  busy: boolean;
  actions: { label: string; action: GmailAllowlistAction }[];
  onAction: (sender: string, action: GmailAllowlistAction) => void;
  emptyLabel: string;
  collapsible?: boolean;
  defaultCollapsed?: boolean;
}) {
  const [collapsed, setCollapsed] = useState(Boolean(props.defaultCollapsed));
  const contentId = `gmail-allowlist-${props.title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  const content = props.senders.length === 0 ? (
    <p className="muted gmail-allowlist-empty">{props.emptyLabel}</p>
  ) : (
    <ul className="gmail-allowlist-list">
      {props.senders.map((record) => (
        <li key={record.sender} className="gmail-allowlist-item">
          <div className="gmail-allowlist-sender">
            <span className="gmail-allowlist-name">{record.sender_name || record.sender}</span>
            {record.sender_name ? <span className="muted gmail-allowlist-email">{record.sender}</span> : null}
          </div>
          <div className="button-row">
            {props.actions.map((entry) => (
              <button
                key={entry.action}
                type="button"
                className="secondary-action"
                disabled={props.busy}
                onClick={() => props.onAction(record.sender, entry.action)}
              >
                {entry.label}
              </button>
            ))}
          </div>
        </li>
      ))}
    </ul>
  );

  return (
    <div className="gmail-allowlist-group">
      {props.collapsible ? (
        <>
          <button
            type="button"
            className="gmail-allowlist-group-toggle"
            onClick={() => setCollapsed((value) => !value)}
            aria-expanded={!collapsed}
            aria-controls={contentId}
          >
            <span>
              {props.title} <span className="muted">({props.senders.length})</span>
            </span>
            <span className="muted">{collapsed ? "Show" : "Hide"}</span>
          </button>
          {collapsed ? null : <div id={contentId}>{content}</div>}
        </>
      ) : (
        <>
          <p className="gmail-allowlist-group-title">
            {props.title} <span className="muted">({props.senders.length})</span>
          </p>
          {content}
        </>
      )}
    </div>
  );
}

type GmailSenderRecord = {
  sender: string;
  sender_name?: string | null;
  state: "approved" | "candidate" | "rejected";
  reason?: string | null;
  source?: string | null;
  message_count?: number;
  last_seen_at?: string | null;
};

type GmailAllowlistResponse = {
  summary: { sender_count: number; approved_count: number; candidate_count: number; rejected_count: number };
  approved: GmailSenderRecord[];
  candidates: GmailSenderRecord[];
  rejected: GmailSenderRecord[];
};

function AdminApp() {
  const requestedTab = new URLSearchParams(window.location.search).get("tab") ?? "status";
  const initialTab = adminTabOptions.includes(requestedTab as AdminTab) ? (requestedTab as AdminTab) : "status";
  const issueRun = new URLSearchParams(window.location.search).get("issue_run");
  const [tab, setTab] = useState(initialTab);
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [sources, setSources] = useState<SourceStatusResponse | null>(null);
  const [library, setLibrary] = useState<LibraryResponse>({ explorations: [], deleted_explorations: [], topics: [], digests: [], legacy_digests: [] });
  const [message, setMessage] = useState("Loading Admin...");
  const [busy, setBusy] = useState(false);
  const [explorationSort, setExplorationSort] = useState<SortMode>(() => loadSessionValue("admin.explorationSort", "recent"));
  const [digestSort, setDigestSort] = useState<SortMode>(() => loadSessionValue("admin.digestSort", "recent"));
  const [editingDigest, setEditingDigest] = useState<{ topicId: string; preset: SchedulePreset; time: string } | null>(null);
  const [editingAdvancedSettings, setEditingAdvancedSettings] = useState<{
    topic: TopicProfileResponse;
    controls: BriefControlsDraft;
    pipelineLimits: PipelineLimitsDraft;
    tab: "brief" | "system";
  } | null>(null);
  const [briefSettings, setBriefSettings] = useState<BriefSettingsResponse | null>(null);
  const [defaultControlsDraft, setDefaultControlsDraft] = useState<BriefControlsDraft>(defaultBriefControls);
  const [pipelineLimitsDraft, setPipelineLimitsDraft] = useState<PipelineLimitsDraft>(defaultPipelineLimits);
  const [issueDetails, setIssueDetails] = useState<{ built_with_issues: boolean; issues: ExplorationIssue[] } | null>(null);
  const [webProvider, setWebProvider] = useState<"tavily" | "brave" | "serpapi">("tavily");
  const [webKey, setWebKey] = useState("");
  const [adminGmailSecret, setAdminGmailSecret] = useState("");
  const [gmailAllowlist, setGmailAllowlist] = useState<GmailAllowlistResponse | null>(null);
  const [newGmailSender, setNewGmailSender] = useState("");
  const [newGmailSenderName, setNewGmailSenderName] = useState("");
  const [adminPodcastKey, setAdminPodcastKey] = useState("");
  const [adminPodcastSecret, setAdminPodcastSecret] = useState("");
  const [youtubeKey, setYoutubeKey] = useState("");
  const [adminFredKey, setAdminFredKey] = useState("");
  const [adminEmailRecipients, setAdminEmailRecipients] = useState<Record<string, string>>({});
  const [selectedLocalModel, setSelectedLocalModel] = useState("");
  const [selectedCloudModel, setSelectedCloudModel] = useState("");
  const [jobModel, setJobModel] = useState("");
  const [jobLimit, setJobLimit] = useState(100);
  const [ollamaKey, setOllamaKey] = useState("");
  const [modelRoutes, setModelRoutes] = useState<ModelRouteDraft>({});
  const [secretsExpanded, setSecretsExpanded] = useState(() => loadSessionValue("admin.secretsExpanded", false));
  const [sourceConfigExpanded, setSourceConfigExpanded] = useState(() => loadSessionValue("admin.sourceConfigExpanded", false));
  const [explorationsExpanded, setExplorationsExpanded] = useState(() => loadSessionValue("admin.explorationsExpanded", false));
  const [deletedExpanded, setDeletedExpanded] = useState(() => loadSessionValue("admin.deletedExpanded", false));
  const [digestsExpanded, setDigestsExpanded] = useState(() => loadSessionValue("admin.digestsExpanded", false));

  const topicById = useMemo(
    () => new Map([...library.topics, ...library.digests].map((topic) => [topic.topic_id, topic])),
    [library.digests, library.topics],
  );
  const sortedExplorations = useMemo<ExplorationLibraryItem[]>(() => {
    const explorationTopicIds = new Set(library.explorations.map((exploration) => exploration.topic_id));
    const explorationRows: ExplorationLibraryItem[] = library.explorations.map((exploration) => ({
      kind: "exploration",
      exploration,
      topic: topicById.get(exploration.topic_id) ?? null,
    }));
    const unbuiltTopicRows: ExplorationLibraryItem[] = library.topics
      .filter((topic) => !topic.schedule && !explorationTopicIds.has(topic.topic_id))
      .map((topic) => ({ kind: "topic", topic }));
    return [...explorationRows, ...unbuiltTopicRows].sort((a, b) => {
      if (explorationSort === "name") {
        return explorationLibraryName(a).localeCompare(explorationLibraryName(b));
      }
      return explorationLibraryDate(b) - explorationLibraryDate(a);
    });
  }, [explorationSort, library.explorations, library.topics, topicById]);
  const sortedDigests = useMemo<DigestLibraryItem[]>(() => {
    const rows: DigestLibraryItem[] = [
      ...library.digests.map((topic) => ({ kind: "topic" as const, topic })),
      ...library.legacy_digests.map((digest) => ({ kind: "legacy" as const, digest })),
    ];
    return rows.sort((a, b) => {
      if (digestSort === "name") return digestLibraryName(a).localeCompare(digestLibraryName(b));
      return digestLibraryDate(b) - digestLibraryDate(a);
    });
  }, [digestSort, library.digests, library.legacy_digests]);
  const modelOptions = status?.model?.catalog.models ?? [];
  const cloudModelOptions = status?.model?.catalog.providers?.ollama_cloud?.models ?? [];
  const hasActiveLibraryBuilds = useMemo(() => {
    const activeExploration = library.explorations.some((item) => item.status === "queued" || item.status === "running");
    const activeDigest = library.digests.some((topic) => {
      const latest = topic.latest_exploration;
      return latest?.status === "queued" || latest?.status === "running";
    });
    return activeExploration || activeDigest;
  }, [library.digests, library.explorations]);

  const loadAdmin = useCallback(async () => {
    const [nextStatus, nextSources, nextLibrary, nextBriefSettings, nextAllowlist] = await Promise.all([
      api<AdminStatus>("/api/admin/status").catch(() => null),
      api<SourceStatusResponse>("/api/explore/source-status").catch(() => null),
      api<LibraryResponse>("/api/admin/library").catch(() => ({ explorations: [], deleted_explorations: [], topics: [], digests: [], legacy_digests: [] })),
      api<BriefSettingsResponse>("/api/admin/brief-settings").catch(() => null),
      api<GmailAllowlistResponse>("/api/admin/gmail/allowlist").catch(() => null),
    ]);
    setStatus(nextStatus);
    if (nextSources) setSources(nextSources);
    if (nextAllowlist) setGmailAllowlist(nextAllowlist);
    if (nextBriefSettings) {
      setBriefSettings(nextBriefSettings);
      setDefaultControlsDraft(nextBriefSettings.defaults);
      setPipelineLimitsDraft(nextBriefSettings.pipeline_limits ?? defaultPipelineLimits);
    }
    setLibrary(nextLibrary);
    const preferredLocalModel = nextStatus?.model?.catalog.selected_local_model
      ?? nextStatus?.model?.local_model
      ?? nextStatus?.model?.catalog.models[0]?.id
      ?? "";
    const preferredCloudModel = nextStatus?.model?.catalog.selected_ollama_cloud_model
      ?? nextStatus?.model?.ollama_cloud_model
      ?? nextStatus?.model?.catalog.providers?.ollama_cloud?.models?.[0]?.id
      ?? "";
    setSelectedLocalModel(preferredLocalModel);
    setSelectedCloudModel(preferredCloudModel);
    setJobModel((current) => current || preferredLocalModel);
    setModelRoutes(routeDraftFromStatus(nextStatus));
    setMessage(nextStatus?.health?.headline ?? "Admin ready");
  }, []);

  useEffect(() => {
    void loadAdmin();
  }, [loadAdmin]);

  useEffect(() => {
    if (!hasActiveLibraryBuilds) return;
    const timer = window.setInterval(() => {
      void loadAdmin();
    }, 2200);
    return () => window.clearInterval(timer);
  }, [hasActiveLibraryBuilds, loadAdmin]);

  useEffect(() => {
    if (!issueRun) return;
    setTab("library");
    void api<{ built_with_issues: boolean; issues: ExplorationIssue[] }>(`/api/admin/explorations/${issueRun}/issues`)
      .then(setIssueDetails)
      .catch(() => setIssueDetails(null));
  }, [issueRun]);

  useEffect(() => {
    window.sessionStorage.setItem("admin.explorationSort", JSON.stringify(explorationSort));
  }, [explorationSort]);

  useEffect(() => {
    window.sessionStorage.setItem("admin.digestSort", JSON.stringify(digestSort));
  }, [digestSort]);

  useEffect(() => {
    window.sessionStorage.setItem("admin.secretsExpanded", JSON.stringify(secretsExpanded));
    window.sessionStorage.setItem("admin.sourceConfigExpanded", JSON.stringify(sourceConfigExpanded));
    window.sessionStorage.setItem("admin.explorationsExpanded", JSON.stringify(explorationsExpanded));
    window.sessionStorage.setItem("admin.deletedExpanded", JSON.stringify(deletedExpanded));
    window.sessionStorage.setItem("admin.digestsExpanded", JSON.stringify(digestsExpanded));
  }, [deletedExpanded, digestsExpanded, explorationsExpanded, secretsExpanded, sourceConfigExpanded]);

  function changeTab(nextTab: AdminTab) {
    setTab(nextTab);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", nextTab);
    window.history.replaceState(null, "", url);
  }

  async function runVerification(publish = false, forcePodcastRefresh = false) {
    const digest = status?.digests?.[0];
    if (!digest) return;
    setBusy(true);
    try {
      const params = new URLSearchParams();
      if (publish) params.set("publish", "true");
      if (forcePodcastRefresh) params.set("force_podcast_refresh", "true");
      await api(`/api/admin/digests/${digest.id}/verification-run${params.toString() ? `?${params}` : ""}`, { method: "POST" });
      await loadAdmin();
      setMessage(publish ? "Published verified brief" : "Verification complete");
    } catch (error) {
      setMessage(errorMessage(error, "Verification failed"));
    } finally {
      setBusy(false);
    }
  }

  async function saveWebSearch() {
    if (!webKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/web-search/credentials", {
        method: "POST",
        body: JSON.stringify({ provider: webProvider, api_key: webKey.trim() }),
      });
      setWebKey("");
      await loadAdmin();
      setMessage("Web Search saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save Web Search"));
    } finally {
      setBusy(false);
    }
  }

  async function saveAdminGmailClientSecret() {
    if (!adminGmailSecret.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/gmail/client-secret", {
        method: "POST",
        body: JSON.stringify({ client_secret_json: adminGmailSecret.trim() }),
      });
      setAdminGmailSecret("");
      await loadAdmin();
      setMessage("Gmail OAuth client saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save Gmail setup"));
    } finally {
      setBusy(false);
    }
  }

  async function loadAdminGmailClientFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      setAdminGmailSecret(await file.text());
      setMessage("Gmail OAuth client file loaded");
    } catch (error) {
      setMessage(errorMessage(error, "Could not read Gmail OAuth file"));
    } finally {
      event.target.value = "";
    }
  }

  async function connectAdminGmail() {
    setBusy(true);
    try {
      const result = await api<{ authorization_url: string }>("/api/admin/gmail/oauth/start", { method: "POST" });
      window.location.href = result.authorization_url;
    } catch (error) {
      setMessage(errorMessage(error, "Could not start Gmail connection"));
      setBusy(false);
    }
  }

  async function addGmailAllowlistSender() {
    const sender = newGmailSender.trim();
    if (!sender) return;
    setBusy(true);
    try {
      const next = await api<GmailAllowlistResponse>("/api/admin/gmail/allowlist", {
        method: "POST",
        body: JSON.stringify({ sender, sender_name: newGmailSenderName.trim() || null }),
      });
      setGmailAllowlist(next);
      setNewGmailSender("");
      setNewGmailSenderName("");
      setMessage(`Approved ${sender}`);
    } catch (error) {
      setMessage(errorMessage(error, "Could not add sender"));
    } finally {
      setBusy(false);
    }
  }

  async function updateGmailAllowlistSender(sender: string, action: "approve" | "reject" | "remove") {
    setBusy(true);
    try {
      const path = `/api/admin/gmail/allowlist/${encodeURIComponent(sender)}${action === "remove" ? "" : `/${action}`}`;
      const next = await api<GmailAllowlistResponse>(path, { method: action === "remove" ? "DELETE" : "POST" });
      setGmailAllowlist(next);
      setMessage(action === "remove" ? `Removed ${sender}` : `${action === "approve" ? "Approved" : "Rejected"} ${sender}`);
    } catch (error) {
      setMessage(errorMessage(error, "Could not update sender"));
    } finally {
      setBusy(false);
    }
  }

  async function saveAdminPodcastCredentials() {
    if (!adminPodcastKey.trim() || !adminPodcastSecret.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/podcasts/credentials", {
        method: "POST",
        body: JSON.stringify({ api_key: adminPodcastKey.trim(), api_secret: adminPodcastSecret.trim() }),
      });
      setAdminPodcastKey("");
      setAdminPodcastSecret("");
      await loadAdmin();
      setMessage("Podcast Index saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save Podcast Index"));
    } finally {
      setBusy(false);
    }
  }

  async function saveYoutube() {
    if (!youtubeKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/youtube/credentials", {
        method: "POST",
        body: JSON.stringify({ api_key: youtubeKey.trim() }),
      });
      setYoutubeKey("");
      await loadAdmin();
      setMessage("YouTube saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save YouTube"));
    } finally {
      setBusy(false);
    }
  }

  async function saveAdminFred() {
    if (!adminFredKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/fred/credentials", {
        method: "POST",
        body: JSON.stringify({ api_key: adminFredKey.trim() }),
      });
      setAdminFredKey("");
      await loadAdmin();
      setMessage("FRED saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save FRED"));
    } finally {
      setBusy(false);
    }
  }

  async function setupCollections() {
    setBusy(true);
    try {
      await api("/api/admin/collections/setup", { method: "POST" });
      await loadAdmin();
      setMessage("Collections folder ready");
    } catch (error) {
      setMessage(errorMessage(error, "Could not set up Collections"));
    } finally {
      setBusy(false);
    }
  }

  async function saveModel(provider: "local" | "ollama_cloud") {
    const modelName = (provider === "ollama_cloud" ? selectedCloudModel : selectedLocalModel).trim();
    if (!modelName) return;
    setBusy(true);
    try {
      await api("/api/admin/model/selection", {
        method: "POST",
        body: JSON.stringify({ provider, model_name: modelName }),
      });
      await loadAdmin();
      setMessage(provider === "ollama_cloud" ? "Cloud default saved" : "Local default saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save model"));
    } finally {
      setBusy(false);
    }
  }

  async function restoreModelDefaults() {
    setBusy(true);
    try {
      await api("/api/admin/model/defaults/restore", { method: "POST" });
      await loadAdmin();
      setMessage("Model defaults restored to Gemma4-MTP-26B-BF16");
    } catch (error) {
      setMessage(errorMessage(error, "Could not restore model defaults"));
    } finally {
      setBusy(false);
    }
  }

  async function saveOllamaCloud() {
    if (!ollamaKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/model/ollama-cloud/credentials", {
        method: "POST",
        body: JSON.stringify({ api_key: ollamaKey.trim() }),
      });
      setOllamaKey("");
      await loadAdmin();
      setMessage("Ollama Cloud saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save Ollama Cloud"));
    } finally {
      setBusy(false);
    }
  }

  async function saveModelRoutes() {
    setBusy(true);
    try {
      await api("/api/admin/model/routes", {
        method: "POST",
        body: JSON.stringify({ routes: modelRoutes }),
      });
      await loadAdmin();
      setMessage("Model routes saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save model routes"));
    } finally {
      setBusy(false);
    }
  }

  function updateModelRoute(agent: string, patch: Partial<ModelRouteDraft[string]>) {
    setModelRoutes((current) => ({
      ...current,
      [agent]: {
        provider: current[agent]?.provider ?? "local",
        model: current[agent]?.model ?? "",
        allow_private_cloud: current[agent]?.allow_private_cloud ?? false,
        ...patch,
      },
    }));
  }

  async function startModelJob() {
    if (!jobModel.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/model/jobs", {
        method: "POST",
        body: JSON.stringify({ model_name: jobModel.trim(), limit_count: jobLimit, include_cached: false }),
      });
      await loadAdmin();
      setMessage("Model batch started");
    } catch (error) {
      setMessage(errorMessage(error, "Could not start model batch"));
    } finally {
      setBusy(false);
    }
  }

  async function rebuildFromAdmin(exploration: Exploration) {
    const topic = topicById.get(exploration.topic_id);
    setBusy(true);
    try {
      await api(`/api/explore/explorations/${exploration.exploration_id}/rebuild`, {
        method: "POST",
        body: JSON.stringify({
          source_selection: exploration.source_selection,
          candidate_limit: topic ? contentLimitsFromProfile(topic.profile, defaultControlsDraft.content_limits).total_items : undefined,
        }),
      });
      await loadAdmin();
      setMessage("Rebuild queued. Progress is shown in the row.");
    } catch (error) {
      setMessage(errorMessage(error, "Could not rebuild"));
    } finally {
      setBusy(false);
    }
  }

  function refineFromAdmin(exploration: Exploration) {
    window.location.href = `/?refine_exploration=${encodeURIComponent(exploration.exploration_id)}`;
  }

  function openAdvancedSettings(topic: TopicProfileResponse) {
    const systemDefaults = briefSettings?.pipeline_limits ?? defaultPipelineLimits;
    setEditingAdvancedSettings({
      topic,
      controls: briefControlsFromProfile(topic.profile, defaultControlsDraft),
      pipelineLimits: pipelineLimitsFromProfile(topic.profile, systemDefaults),
      tab: "brief",
    });
  }

  async function saveAdvancedSettings() {
    if (!editingAdvancedSettings) return;
    const errors = validateBriefControls(
      editingAdvancedSettings.controls,
      sourceSelectionFromRecord(editingAdvancedSettings.topic.profile.source_selection),
    );
    if (errors.length) {
      setMessage("Fix the advanced settings errors before saving.");
      return;
    }
    setBusy(true);
    try {
      const saved = await api<TopicProfileResponse>(`/api/explore/topic-profiles/${editingAdvancedSettings.topic.topic_id}/content-limits`, {
        method: "POST",
        body: JSON.stringify({
          content_limits: editingAdvancedSettings.controls.content_limits,
          lookback_hours: editingAdvancedSettings.controls.lookback_hours,
          pipeline_limits: editingAdvancedSettings.pipelineLimits,
        }),
      });
      setEditingAdvancedSettings(null);
      await loadAdmin();
      setMessage(`Advanced settings saved for ${profileName(saved)}`);
    } catch (error) {
      setMessage(errorMessage(error, "Could not save advanced settings"));
    } finally {
      setBusy(false);
    }
  }

  async function saveDefaultBriefSettings() {
    const errors = validateBriefControls(defaultControlsDraft, defaultSourceSelectionForControls);
    if (errors.length) {
      setMessage("Fix the default brief settings errors before saving.");
      return;
    }
    setBusy(true);
    try {
      const saved = await api<BriefSettingsResponse>("/api/admin/brief-settings/defaults", {
        method: "PUT",
        body: JSON.stringify(defaultControlsDraft),
      });
      setBriefSettings(saved);
      setDefaultControlsDraft(saved.defaults);
      await loadAdmin();
      setMessage("Default brief settings saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save default brief settings"));
    } finally {
      setBusy(false);
    }
  }

  async function savePipelineLimits() {
    setBusy(true);
    try {
      const saved = await api<BriefSettingsResponse>("/api/admin/brief-settings/pipeline-limits", {
        method: "PUT",
        body: JSON.stringify(pipelineLimitsDraft),
      });
      setBriefSettings(saved);
      setPipelineLimitsDraft(saved.pipeline_limits);
      await loadAdmin();
      setMessage("Pipeline limits saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save pipeline limits"));
    } finally {
      setBusy(false);
    }
  }

  async function buildTopicFromAdmin(topic: TopicProfileResponse) {
    const limits = contentLimitsFromProfile(topic.profile, defaultControlsDraft.content_limits);
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${topic.topic_id}/run`, {
        method: "POST",
        body: JSON.stringify({
          mode: "show_now",
          source_selection: topic.profile.source_selection,
          candidate_limit: limits.total_items,
          lookback_hours: lookbackHoursForBuild(topic.profile, undefined, defaultControlsDraft.lookback_hours),
        }),
      });
      await loadAdmin();
      setMessage("Brief build started");
    } catch (error) {
      setMessage(errorMessage(error, "Could not build brief"));
    } finally {
      setBusy(false);
    }
  }

  async function rebuildLegacyDigest(digest: Digest) {
    setBusy(true);
    try {
      await api(`/api/admin/digests/${digest.id}/verification-run?publish=true`, { method: "POST" });
      await loadAdmin();
      setMessage("Digest rebuild queued. Progress is shown in the row.");
    } catch (error) {
      setMessage(errorMessage(error, "Could not rebuild digest"));
    } finally {
      setBusy(false);
    }
  }

  async function scheduleExploration(exploration: Exploration) {
    const topic = topicById.get(exploration.topic_id);
    if (topic?.schedule) {
      startEditingDigest(topic);
      return;
    }
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${exploration.topic_id}/schedule`, {
        method: "POST",
        body: JSON.stringify({ schedule: "daily", time_of_day: "08:00", timezone: "America/Los_Angeles" }),
      });
      await loadAdmin();
      setMessage("Digest scheduled");
    } catch (error) {
      setMessage(errorMessage(error, "Could not schedule digest"));
    } finally {
      setBusy(false);
    }
  }

  async function sendExplorationFromAdmin(exploration: Exploration) {
    const fallback = status?.delivery?.email.recipient_email ?? "";
    const recipient = (adminEmailRecipients[exploration.exploration_id] || fallback || "").trim();
    if (!recipient || !recipient.includes("@")) {
      setMessage("Enter a valid email address");
      return;
    }
    setBusy(true);
    try {
      const result = await api<{ status: string; error?: string; recipient_email?: string }>(`/api/explore/explorations/${exploration.exploration_id}/email`, {
        method: "POST",
        body: JSON.stringify({ recipient_email: recipient }),
      });
      if (result.status !== "sent") {
        setMessage(result.error ?? "Email delivery skipped");
      } else {
        await loadAdmin();
        setMessage(`Sent to ${result.recipient_email ?? recipient}`);
      }
    } catch (error) {
      setMessage(errorMessage(error, "Could not send brief"));
    } finally {
      setBusy(false);
    }
  }

  async function deleteExplorationFromAdmin(exploration: Exploration) {
    setBusy(true);
    try {
      await api(`/api/explore/explorations/${exploration.exploration_id}`, { method: "DELETE" });
      await loadAdmin();
      setMessage("Brief deleted. Undo is available for 7 days.");
    } catch (error) {
      setMessage(errorMessage(error, "Could not delete brief"));
    } finally {
      setBusy(false);
    }
  }

  async function restoreExplorationFromAdmin(exploration: Exploration) {
    setBusy(true);
    try {
      await api(`/api/explore/explorations/${exploration.exploration_id}/restore`, { method: "POST" });
      await loadAdmin();
      setMessage("Brief restored");
    } catch (error) {
      setMessage(errorMessage(error, "Could not restore brief"));
    } finally {
      setBusy(false);
    }
  }

  function startEditingDigest(topic: TopicProfileResponse) {
    const config = topic.profile.schedule_config ?? {};
    setEditingDigest({
      topicId: topic.topic_id,
      preset: ((topic.schedule ?? "daily") as SchedulePreset),
      time: typeof config.time_of_day === "string" ? config.time_of_day : "08:00",
    });
    setMessage("Editing schedule");
  }

  async function saveDigestSchedule(topic: TopicProfileResponse) {
    if (!editingDigest || editingDigest.topicId !== topic.topic_id) return;
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${topic.topic_id}/schedule`, {
        method: "POST",
        body: JSON.stringify({
          schedule: editingDigest.preset,
          time_of_day: editingDigest.time || "08:00",
          timezone: "America/Los_Angeles",
        }),
      });
      setEditingDigest(null);
      await loadAdmin();
      setMessage("Schedule updated");
    } catch (error) {
      setMessage(errorMessage(error, "Could not update schedule"));
    } finally {
      setBusy(false);
    }
  }

  async function rebuildDigest(topic: TopicProfileResponse) {
    const limits = contentLimitsFromProfile(topic.profile, defaultControlsDraft.content_limits);
    setBusy(true);
    try {
      if (topic.latest_exploration) {
        await api(`/api/explore/explorations/${topic.latest_exploration.exploration_id}/rebuild`, {
          method: "POST",
          body: JSON.stringify({
            source_selection: topic.profile.source_selection,
            candidate_limit: limits.total_items,
            lookback_hours: lookbackHoursForBuild(topic.profile, undefined, defaultControlsDraft.lookback_hours),
          }),
        });
      } else {
        await api(`/api/explore/topic-profiles/${topic.topic_id}/run`, {
          method: "POST",
          body: JSON.stringify({
            mode: "scheduled",
            source_selection: topic.profile.source_selection,
            candidate_limit: limits.total_items,
            lookback_hours: lookbackHoursForBuild(topic.profile, undefined, defaultControlsDraft.lookback_hours),
          }),
        });
      }
      await loadAdmin();
      setMessage("Digest rebuild queued. Progress is shown in the row.");
    } catch (error) {
      setMessage(errorMessage(error, "Could not rebuild digest"));
    } finally {
      setBusy(false);
    }
  }

  async function pauseDigest(topic: TopicProfileResponse) {
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${topic.topic_id}/pause`, { method: "POST" });
      await loadAdmin();
      setMessage("Digest paused");
    } catch (error) {
      setMessage(errorMessage(error, "Could not pause digest"));
    } finally {
      setBusy(false);
    }
  }

  async function archiveDigest(topic: TopicProfileResponse) {
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${topic.topic_id}/archive`, { method: "POST" });
      await loadAdmin();
      setMessage("Digest archived");
    } catch (error) {
      setMessage(errorMessage(error, "Could not archive digest"));
    } finally {
      setBusy(false);
    }
  }

  async function deleteDigest(topic: TopicProfileResponse) {
    if (!window.confirm(`Delete "${profileName(topic)}" from the library?`)) return;
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${topic.topic_id}`, { method: "DELETE" });
      await loadAdmin();
      setMessage("Digest deleted");
    } catch (error) {
      setMessage(errorMessage(error, "Could not delete digest"));
    } finally {
      setBusy(false);
    }
  }

  const defaultControlsErrors = validateBriefControls(defaultControlsDraft, defaultSourceSelectionForControls);
  const advancedControlsErrors = editingAdvancedSettings
    ? validateBriefControls(
        editingAdvancedSettings.controls,
        sourceSelectionFromRecord(editingAdvancedSettings.topic.profile.source_selection),
      )
    : [];

  return (
    <main className="admin-page">
      <header className="admin-header">
        <a className="brand-lockup" href="/">
          <span className="brand-mark">◔</span>
          <span>Dispatch Admin</span>
        </a>
        <a className="secondary-action" href="/">Back to Dispatch</a>
      </header>
      <nav className="admin-tabs">
        {adminTabOptions.map((item) => (
          <button type="button" className={tab === item ? "active" : ""} key={item} onClick={() => changeTab(item)}>
            {formatStage(item)}
          </button>
        ))}
      </nav>
      <p className="app-status">{message}</p>

      {tab === "status" ? (
        <section className="admin-panel">
          <div className="panel-title-row">
            <div>
              <p className="section-kicker">Status</p>
              <h1>{status?.health?.headline ?? "Runtime status"}</h1>
            </div>
            <button type="button" onClick={() => void loadAdmin()} disabled={busy}>Refresh</button>
          </div>
          <div className="health-grid">
            {(status?.health?.checks ?? []).map((check) => (
              <article className={`health-card ${check.status}`} key={check.name}>
                <strong>{check.name}</strong>
                <p>{check.message}</p>
              </article>
            ))}
          </div>
          <SecretHealthPanel
            health={status?.secret_health}
            expanded={secretsExpanded}
            onToggle={() => setSecretsExpanded((current) => !current)}
          />
          <div className="button-row">
            <a className="secondary-action" href="/brief" target="_blank" rel="noreferrer">View latest brief</a>
            <button type="button" onClick={() => void runVerification(false)} disabled={busy}>Verify only</button>
            <button type="button" onClick={() => void runVerification(true)} disabled={busy}>Publish verified brief</button>
            <button type="button" onClick={() => void runVerification(false, true)} disabled={busy}>Refresh podcasts</button>
          </div>
        </section>
      ) : null}

      {tab === "sources" ? (
        <section className="admin-panel">
          <div className="panel-title-row">
            <div>
              <p className="section-kicker">Sources</p>
              <h1>Connections</h1>
            </div>
          </div>
          <div className="source-admin-grid">
            {sourceOptions.map((source) => {
              const item = sources?.sources[source.key];
              return (
                <article className="source-admin-card" key={source.key}>
                  <strong>{source.icon} {source.label}</strong>
                  <span className={item?.enabled ? "status-pill good" : "status-pill"}>{item?.enabled ? "Enabled" : "Needs setup"}</span>
                  <p>{item?.reason ?? "Ready for brief runs."}</p>
                </article>
              );
            })}
          </div>
          <section className="collapsible-panel">
            <div className="library-section-header">
              <div>
                <p className="section-kicker">Setup forms</p>
                <h2>Source Configuration</h2>
              </div>
              <DisclosureButton
                expanded={sourceConfigExpanded}
                label={sourceConfigExpanded ? "Hide" : "Show"}
                onToggle={() => setSourceConfigExpanded((current) => !current)}
              />
            </div>
            {sourceConfigExpanded ? (
              <div className="source-setup-grid">
                <section className="source-setup-card">
                  <h2>Web</h2>
                  <p>{sources?.sources.web_search?.enabled ? "Connected." : sources?.sources.web_search?.reason}</p>
                  <label>
                    Provider
                    <select value={webProvider} onChange={(event) => setWebProvider(event.target.value as "tavily" | "brave" | "serpapi")}>
                      <option value="tavily">Tavily</option>
                      <option value="brave">Brave</option>
                      <option value="serpapi">SerpAPI</option>
                    </select>
                  </label>
                  <label>
                    API key
                    <input type="password" value={webKey} onChange={(event) => setWebKey(event.target.value)} />
                  </label>
                  <button type="button" onClick={() => void saveWebSearch()} disabled={busy || !webKey.trim()}>Save Web Search</button>
                </section>

                <section className="source-setup-card">
                  <h2>Gmail</h2>
                  <p>
                    {status?.gmail?.connected
                      ? "Connected."
                      : status?.gmail?.configured
                        ? "OAuth client saved. Finish the Gmail connection."
                        : "Upload a Gmail OAuth client, then connect Gmail."}
                  </p>
                  <label>
                    OAuth client JSON file
                    <input type="file" accept=".json,application/json" onChange={(event) => void loadAdminGmailClientFile(event)} />
                  </label>
                  <label>
                    OAuth client JSON
                    <textarea
                      value={adminGmailSecret}
                      onChange={(event) => setAdminGmailSecret(event.target.value)}
                      rows={5}
                      placeholder='{"installed": ... }'
                    />
                  </label>
                  <div className="button-row">
                    <button type="button" onClick={() => void saveAdminGmailClientSecret()} disabled={busy || !adminGmailSecret.trim()}>
                      Save OAuth Client
                    </button>
                    <button type="button" className="secondary-action" onClick={() => void connectAdminGmail()} disabled={busy || !status?.gmail?.configured}>
                      Connect Gmail
                    </button>
                  </div>
                </section>

                <section className="source-setup-card gmail-allowlist-card">
                  <h2>Gmail Allowlist</h2>
                  <p>
                    Only approved senders are ever read into a brief. Newsletters suggested during topic
                    refinement land here as candidates for you to approve.
                  </p>
                  <div className="gmail-allowlist-add">
                    <label>
                      Sender email
                      <input
                        type="email"
                        value={newGmailSender}
                        onChange={(event) => setNewGmailSender(event.target.value)}
                        placeholder="newsletter@example.com"
                      />
                    </label>
                    <label>
                      Name (optional)
                      <input
                        type="text"
                        value={newGmailSenderName}
                        onChange={(event) => setNewGmailSenderName(event.target.value)}
                        placeholder="Example Weekly"
                      />
                    </label>
                    <button type="button" onClick={() => void addGmailAllowlistSender()} disabled={busy || !newGmailSender.trim()}>
                      Approve Sender
                    </button>
                  </div>
                  <GmailAllowlistGroup
                    title="Approved"
                    senders={gmailAllowlist?.approved ?? []}
                    busy={busy}
                    actions={[
                      { label: "Reject", action: "reject" },
                      { label: "Remove", action: "remove" },
                    ]}
                    onAction={(sender, action) => void updateGmailAllowlistSender(sender, action)}
                    emptyLabel="No approved senders yet."
                    collapsible
                    defaultCollapsed
                  />
                  <GmailAllowlistGroup
                    title="Pending approval"
                    senders={gmailAllowlist?.candidates ?? []}
                    busy={busy}
                    actions={[
                      { label: "Approve", action: "approve" },
                      { label: "Reject", action: "reject" },
                    ]}
                    onAction={(sender, action) => void updateGmailAllowlistSender(sender, action)}
                    emptyLabel="No candidates waiting."
                    collapsible
                    defaultCollapsed
                  />
                  <GmailAllowlistGroup
                    title="Rejected"
                    senders={gmailAllowlist?.rejected ?? []}
                    busy={busy}
                    actions={[
                      { label: "Approve", action: "approve" },
                      { label: "Remove", action: "remove" },
                    ]}
                    onAction={(sender, action) => void updateGmailAllowlistSender(sender, action)}
                    emptyLabel="Nothing rejected."
                  />
                </section>

                <section className="source-setup-card">
                  <h2>Podcast</h2>
                  <p>{status?.podcasts?.aggregator_configured ? "Podcast Index connected." : "Add Podcast Index credentials."}</p>
                  <label>
                    Podcast Index API key
                    <input type="password" value={adminPodcastKey} onChange={(event) => setAdminPodcastKey(event.target.value)} />
                  </label>
                  <label>
                    Podcast Index API secret
                    <input type="password" value={adminPodcastSecret} onChange={(event) => setAdminPodcastSecret(event.target.value)} />
                  </label>
                  <button type="button" onClick={() => void saveAdminPodcastCredentials()} disabled={busy || !adminPodcastKey.trim() || !adminPodcastSecret.trim()}>
                    Save Podcast Index
                  </button>
                </section>

                <section className="source-setup-card">
                  <h2>YouTube</h2>
                  <p>{sources?.sources.youtube?.enabled ? "Connected." : sources?.sources.youtube?.reason}</p>
                  <label>
                    YouTube Data API key
                    <input type="password" value={youtubeKey} onChange={(event) => setYoutubeKey(event.target.value)} />
                  </label>
                  <button type="button" onClick={() => void saveYoutube()} disabled={busy || !youtubeKey.trim()}>Save YouTube</button>
                </section>

                <section className="source-setup-card">
                  <h2>Collections</h2>
                  <p>{sources?.sources.collections?.root_path ?? "Local folder source"}</p>
                  <button type="button" onClick={() => void setupCollections()} disabled={busy}>Create Collections Folder</button>
                </section>

                <section className="source-setup-card">
                  <h2>Markets</h2>
                  <p>
                    Price and filing data works without a key. Add a free FRED API key here to enable macro indicators
                    such as rates, inflation, unemployment, and the yield curve.
                  </p>
                  <label>
                    FRED API key
                    <input
                      type="password"
                      value={adminFredKey}
                      onChange={(event) => setAdminFredKey(event.target.value)}
                      placeholder="Paste FRED API key"
                    />
                  </label>
                  <button type="button" onClick={() => void saveAdminFred()} disabled={busy || !adminFredKey.trim()}>
                    Save FRED
                  </button>
                </section>
              </div>
            ) : null}
          </section>
        </section>
      ) : null}

      {tab === "library" ? (
        <section className="admin-panel">
          {issueDetails?.built_with_issues ? (
            <div className="issue-note admin-issue-note">
              <strong>Built with issues</strong>
              {issueDetails.issues.map((issue) => (
                <p key={`${issue.source_name}-${issue.reason}`}>{issue.source_name}: {issue.reason}</p>
              ))}
            </div>
          ) : null}
          <LibrarySection
            title="Explorations"
            sort={explorationSort}
            onSort={setExplorationSort}
            count={sortedExplorations.length}
            expanded={explorationsExpanded}
            onToggle={() => setExplorationsExpanded((current) => !current)}
          >
            {sortedExplorations.map((item) => {
              if (item.kind === "topic") {
                return (
                  <article className="library-row" key={`topic-${item.topic.topic_id}`}>
                    <div>
                      <strong>{profileName(item.topic)}</strong>
                      <small>Ready to build · {formatDateTime(item.topic.updated_at ?? item.topic.created_at)} · {formatSourceSelection(item.topic.profile.source_selection)}</small>
                    </div>
                    <div className="button-row">
                      <button type="button" className="secondary-action" onClick={() => openAdvancedSettings(item.topic)} disabled={busy}>Advanced Settings</button>
                      <button type="button" className="secondary-action" onClick={() => void buildTopicFromAdmin(item.topic)} disabled={busy}>Build brief</button>
                    </div>
                  </article>
                );
              }
              const isScheduledDigest = Boolean(item.topic?.schedule);
              return (
                <article className="library-row" key={item.exploration.exploration_id}>
                  <div>
                    <strong>{explorationLibraryName(item)}</strong>
                    <small>{formatDateTime(item.exploration.finished_at ?? item.exploration.started_at)} · {formatSourceSelection(item.exploration.source_selection)}</small>
                    {isModelDegraded(item.exploration) ? (
                      <p className="warning-text">Built with AI issues.</p>
                    ) : hasActionableBuildIssues(item.exploration) && item.exploration.status === "complete" ? (
                      <p className="warning-text">Built with source issues.</p>
                    ) : hasActionableBuildIssues(item.exploration) ? (
                      <p className="warning-text">Source issues detected so far.</p>
                    ) : null}
                    {isScheduledDigest ? (
                      <p className="muted">Scheduled digest · {formatStage(item.topic?.schedule ?? "daily")}</p>
                    ) : null}
                  </div>
                  <div className="button-row">
                    <button type="button" className="secondary-action" onClick={() => openPath(briefPath(item.exploration))} disabled={!briefPath(item.exploration)}>Open</button>
                    <button type="button" className="secondary-action" onClick={() => item.topic && openAdvancedSettings(item.topic)} disabled={busy || !item.topic}>Advanced Settings</button>
                    <button type="button" className="secondary-action" onClick={() => refineFromAdmin(item.exploration)} disabled={busy || item.exploration.status === "queued" || item.exploration.status === "running"}>Refine</button>
                    <button type="button" className="secondary-action" onClick={() => void rebuildFromAdmin(item.exploration)} disabled={busy}>Rebuild</button>
                    <button
                      type="button"
                      className="secondary-action"
                      onClick={() => {
                        if (isScheduledDigest && item.topic) {
                          startEditingDigest(item.topic);
                          return;
                        }
                        void scheduleExploration(item.exploration);
                      }}
                      disabled={busy || item.exploration.status !== "complete" || !item.topic}
                    >
                      {isScheduledDigest ? "Edit schedule" : "Schedule"}
                    </button>
                    <button type="button" className="secondary-action destructive" onClick={() => void deleteExplorationFromAdmin(item.exploration)} disabled={busy}>Delete</button>
                  </div>
                  {item.exploration.status === "queued" || item.exploration.status === "running" || isModelDegraded(item.exploration) ? (
                    <LibraryBuildProgress exploration={item.exploration} />
                  ) : null}
                  {item.topic && editingDigest?.topicId === item.topic.topic_id ? (
                    <div className="inline-schedule-editor">
                      <select
                        value={editingDigest.preset}
                        onChange={(event) => setEditingDigest({ ...editingDigest, preset: event.target.value as SchedulePreset })}
                      >
                        {schedulePresets.map((preset) => (
                          <option value={preset.value} key={preset.value}>{preset.label}</option>
                        ))}
                      </select>
                      <input
                        type="time"
                        value={editingDigest.time}
                        onChange={(event) => setEditingDigest({ ...editingDigest, time: event.target.value })}
                      />
                      <button type="button" onClick={() => void saveDigestSchedule(item.topic!)} disabled={busy}>Save</button>
                      <button type="button" className="ghost-action" onClick={() => setEditingDigest(null)} disabled={busy}>Cancel</button>
                    </div>
                  ) : null}
                  {item.exploration.status === "complete" ? (
                    <div className="inline-email-editor">
                      <input
                        type="email"
                        value={adminEmailRecipients[item.exploration.exploration_id] ?? status?.delivery?.email.recipient_email ?? ""}
                        onChange={(event) => setAdminEmailRecipients({
                          ...adminEmailRecipients,
                          [item.exploration.exploration_id]: event.target.value,
                        })}
                        placeholder="name@example.com"
                        aria-label="Email recipient"
                      />
                      <button
                        type="button"
                        className="secondary-action"
                        onClick={() => void sendExplorationFromAdmin(item.exploration)}
                        disabled={busy || !status?.delivery?.email.gmail_send_ready}
                      >
                        Email brief
                      </button>
                    </div>
                  ) : null}
                </article>
              );
            })}
          </LibrarySection>
          {library.deleted_explorations.length ? (
            <section className="library-section deleted-library-section">
              <div className="library-section-header">
                <div>
                  <p className="section-kicker">{library.deleted_explorations.length} restorable</p>
                  <h2>Recently Deleted</h2>
                </div>
                <DisclosureButton
                  expanded={deletedExpanded}
                  label={deletedExpanded ? "Hide" : "Show"}
                  onToggle={() => setDeletedExpanded((current) => !current)}
                />
              </div>
              {deletedExpanded ? (
                <div className="library-list">
                  {library.deleted_explorations.map((deleted) => {
                    const item: ExplorationLibraryItem = {
                      kind: "exploration",
                      exploration: deleted,
                      topic: topicById.get(deleted.topic_id) ?? null,
                    };
                    return (
                      <article className="library-row deleted-row" key={`deleted-${deleted.exploration_id}`}>
                        <div>
                          <strong>{explorationLibraryName(item)}</strong>
                          <small>
                            Deleted {formatDateTime(deleted.deleted_at)}
                            {deleted.delete_after ? ` · undo until ${formatDateTime(deleted.delete_after)}` : ""}
                          </small>
                        </div>
                        <div className="button-row">
                          <button type="button" className="secondary-action" onClick={() => void restoreExplorationFromAdmin(deleted)} disabled={busy}>Restore</button>
                        </div>
                      </article>
                    );
                  })}
                </div>
              ) : null}
            </section>
          ) : null}
          <LibrarySection
            title="Digests"
            sort={digestSort}
            onSort={setDigestSort}
            count={sortedDigests.length}
            expanded={digestsExpanded}
            onToggle={() => setDigestsExpanded((current) => !current)}
          >
            {sortedDigests.map((item) => {
              if (item.kind === "legacy") {
                return (
                  <article className="library-row" key={`legacy-${item.digest.id}`}>
                    <div>
                      <strong>{item.digest.name}</strong>
                      <small>Legacy digest · {formatStage(item.digest.schedule)} · {item.digest.status}</small>
                    </div>
                    <div className="button-row">
                      <button type="button" className="secondary-action" onClick={() => openPath("/brief")}>Open latest</button>
                      <button type="button" className="secondary-action" onClick={() => void rebuildLegacyDigest(item.digest)} disabled={busy}>Rebuild</button>
                    </div>
                  </article>
                );
              }
              const topic = item.topic;
              return (
                <article className="library-row" key={topic.topic_id}>
                  <div>
                    <strong>{profileName(topic)}</strong>
                    <small>
                      {topic.profile.status === "paused" ? "Paused" : formatStage(topic.schedule ?? "daily")}
                      {topic.profile.status === "paused" ? "" : ` · next ${formatDateTime(topic.next_run_at)}`}
                    </small>
                  </div>
                  <div className="button-row">
                    <button type="button" className="secondary-action" onClick={() => topic.latest_exploration && openPath(briefPath(topic.latest_exploration))} disabled={!topic.latest_exploration}>Open latest</button>
                    <button type="button" className="secondary-action" onClick={() => openAdvancedSettings(topic)} disabled={busy}>Advanced Settings</button>
                    <button type="button" className="secondary-action" onClick={() => topic.latest_exploration && refineFromAdmin(topic.latest_exploration)} disabled={busy || !topic.latest_exploration || topic.latest_exploration.status === "queued" || topic.latest_exploration.status === "running"}>Refine</button>
                    <button type="button" className="secondary-action" onClick={() => void rebuildDigest(topic)} disabled={busy}>Rebuild</button>
                    <button type="button" className="secondary-action" onClick={() => startEditingDigest(topic)} disabled={busy}>Edit schedule</button>
                    <button type="button" className="secondary-action" onClick={() => void pauseDigest(topic)} disabled={busy || topic.profile.status === "paused"}>Pause</button>
                    <button type="button" className="secondary-action" onClick={() => void archiveDigest(topic)} disabled={busy}>Archive</button>
                    <button type="button" className="secondary-action destructive" onClick={() => void deleteDigest(topic)} disabled={busy}>Delete</button>
                  </div>
                  {topic.latest_exploration?.status === "queued" || topic.latest_exploration?.status === "running" ? (
                    <LibraryBuildProgress exploration={topic.latest_exploration} />
                  ) : null}
                  {editingDigest?.topicId === topic.topic_id ? (
                    <div className="inline-schedule-editor">
                      <select
                        value={editingDigest.preset}
                        onChange={(event) => setEditingDigest({ ...editingDigest, preset: event.target.value as SchedulePreset })}
                      >
                        {schedulePresets.map((preset) => (
                          <option value={preset.value} key={preset.value}>{preset.label}</option>
                        ))}
                      </select>
                      <input
                        type="time"
                        value={editingDigest.time}
                        onChange={(event) => setEditingDigest({ ...editingDigest, time: event.target.value })}
                      />
                      <button type="button" onClick={() => void saveDigestSchedule(topic)} disabled={busy}>Save</button>
                      <button type="button" className="ghost-action" onClick={() => setEditingDigest(null)} disabled={busy}>Cancel</button>
                    </div>
                  ) : null}
                </article>
              );
            })}
          </LibrarySection>
        </section>
      ) : null}

      {tab === "settings" ? (
        <section className="admin-panel">
          <div className="panel-title-row">
            <div>
              <p className="section-kicker">Settings</p>
              <h1>Brief defaults</h1>
              <p className="muted">These defaults apply to new briefs and to any saved brief that is reset to system defaults.</p>
            </div>
            <button type="button" className="primary-action" onClick={() => void saveDefaultBriefSettings()} disabled={busy || defaultControlsErrors.length > 0}>
              Save defaults
            </button>
          </div>
          <BriefControlsPanel
            controls={defaultControlsDraft}
            defaults={defaultBriefControls}
            sourceSelection={defaultSourceSelectionForControls}
            onChange={setDefaultControlsDraft}
          />
          <SettingsErrorList errors={defaultControlsErrors} />
          <section className="settings-subsection">
            <div className="panel-title-row compact-title-row">
              <div>
                <p className="section-kicker">System limits</p>
                <h2>Configurable pipeline limits</h2>
                <p className="muted">These defaults apply to every brief build unless a lower per-brief content limit is set.</p>
              </div>
              <button type="button" className="primary-action" onClick={() => void savePipelineLimits()} disabled={busy}>
                Save pipeline limits
              </button>
            </div>
            <PipelineLimitsPanel
              limits={pipelineLimitsDraft}
              defaults={defaultPipelineLimits}
              onChange={setPipelineLimitsDraft}
            />
          </section>
          <section className="settings-subsection">
            <div className="panel-title-row compact-title-row">
              <div>
                <p className="section-kicker">Hard caps</p>
                <h2>System ceilings</h2>
                <p className="muted">These are the built-in maximums the app will not exceed.</p>
              </div>
            </div>
            <SystemLimitsPanel groups={briefSettings?.system_limits ?? []} />
          </section>
        </section>
      ) : null}

      {tab === "models" ? (
        <section className="admin-panel">
          <div className="panel-title-row">
            <div>
              <p className="section-kicker">Models</p>
              <h1>Model settings</h1>
            </div>
            <button type="button" className="secondary-action" onClick={() => void restoreModelDefaults()} disabled={busy}>
              Restore defaults
            </button>
          </div>
          <div className="admin-form-grid">
            <label>
              Local default model
              <select value={selectedLocalModel} onChange={(event) => setSelectedLocalModel(event.target.value)} disabled={!modelOptions.length}>
                {modelOptions.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}
              </select>
            </label>
            <button type="button" onClick={() => void saveModel("local")} disabled={busy || !selectedLocalModel}>Save local default</button>
            <label>
              Cloud default model
              <select value={selectedCloudModel} onChange={(event) => setSelectedCloudModel(event.target.value)} disabled={!cloudModelOptions.length}>
                {cloudModelOptions.length ? cloudModelOptions.map((model) => <option key={model.id} value={model.id}>{model.id}</option>) : <option value="">No cloud models</option>}
              </select>
            </label>
            <button type="button" onClick={() => void saveModel("ollama_cloud")} disabled={busy || !selectedCloudModel || !cloudModelOptions.length}>
              Save cloud default
            </button>
            <div className="admin-form-note">
              <strong>Current defaults</strong>
              <span>Local: {status?.model?.local_model ?? "Not set"}</span>
              <span>Cloud: {status?.model?.ollama_cloud_model ?? "Not set"}</span>
            </div>
            <div className="admin-form-note">
              <strong>Source</strong>
              <span>Local: {status?.model?.selection_sources?.local ?? "environment"}</span>
              <span>Cloud: {status?.model?.selection_sources?.ollama_cloud ?? "environment"}</span>
            </div>
            <label>
              Batch model
              <input value={jobModel} onChange={(event) => setJobModel(event.target.value)} />
            </label>
            <label>
              Article count
              <input type="number" min={1} max={1000} value={jobLimit} onChange={(event) => setJobLimit(Number(event.target.value))} />
            </label>
            <button type="button" onClick={() => void startModelJob()} disabled={busy || !jobModel.trim()}>Start batch</button>
          </div>
          <div className="source-setup-grid model-routing-grid">
            <section className="source-setup-card">
              <h2>Ollama Cloud</h2>
              <p>{status?.model?.routing?.ollama_cloud.configured ? "Connected." : "Add an Ollama API key to use cloud routes."}</p>
              <label>
                Ollama API key
                <input type="password" value={ollamaKey} onChange={(event) => setOllamaKey(event.target.value)} />
              </label>
              <button type="button" onClick={() => void saveOllamaCloud()} disabled={busy || !ollamaKey.trim()}>Save Ollama Cloud</button>
            </section>
            <section className="source-setup-card model-routing-card">
              <div className="library-section-header">
                <div>
                  <p className="section-kicker">Per-agent routes</p>
                  <h2>Model routing</h2>
                </div>
                <button type="button" onClick={() => void saveModelRoutes()} disabled={busy}>Save routes</button>
              </div>
              <p className="muted">{status?.model?.routing?.privacy.rule}</p>
              <div className="model-route-list">
                {(status?.model?.routing?.agents ?? []).map((agent) => {
                  const route = modelRoutes[agent.id] ?? { provider: "local", model: "", allow_private_cloud: false };
                  const routeModels = route.provider === "ollama_cloud" ? cloudModelOptions : modelOptions;
                  return (
                    <article className="model-route-row" key={agent.id}>
                      <div>
                        <strong>{agent.label}</strong>
                        <p>{agent.description}</p>
                      </div>
                      <label>
                        Provider
                        <select
                          value={route.provider}
                          onChange={(event) => updateModelRoute(agent.id, {
                            provider: event.target.value,
                            model: event.target.value === "ollama_cloud" ? (cloudModelOptions[0]?.id ?? "") : "",
                          })}
                        >
                          <option value="local">Local</option>
                          <option value="ollama_cloud">Ollama Cloud</option>
                        </select>
                      </label>
                      <label>
                        Model
                        <select
                          value={route.model}
                          onChange={(event) => updateModelRoute(agent.id, { model: event.target.value })}
                        >
                          <option value="">Default</option>
                          {routeModels.map((model) => <option key={`${agent.id}-${route.provider}-${model.id}`} value={model.id}>{model.id}</option>)}
                        </select>
                      </label>
                      {route.provider === "ollama_cloud" ? (
                        <span className="status-pill">Default: {status?.model?.routing?.defaults?.ollama_cloud ?? "Cloud default"}</span>
                      ) : (
                        <span className="status-pill good">Default: {status?.model?.routing?.defaults?.local ?? "Local default"}</span>
                      )}
                    </article>
                  );
                })}
              </div>
            </section>
          </div>
        </section>
      ) : null}

      {tab === "metrics" ? (
        <section className="admin-panel">
          <div className="panel-title-row">
            <div>
              <p className="section-kicker">Metrics</p>
              <h1>Inference performance</h1>
            </div>
            <button type="button" onClick={() => void loadAdmin()} disabled={busy}>Refresh</button>
          </div>
          <div className="metric-grid">
            <article><span>Attempts</span><strong>{status?.inference_metrics?.record_count ?? 0}</strong></article>
            <article><span>Success</span><strong>{status?.inference_metrics?.success_count ?? 0}</strong></article>
            <article><span>Failures</span><strong>{status?.inference_metrics?.failure_count ?? 0}</strong></article>
            <article><span>Cache entries</span><strong>{status?.model_cache?.record_count ?? 0}</strong></article>
          </div>
          {status?.inference_metrics?.routes?.length ? (
            <section className="metrics-section">
              <div className="library-section-header">
                <div>
                  <p className="section-kicker">By route and model</p>
                  <h2>Route performance</h2>
                </div>
              </div>
              <div className="metrics-table">
                <div className="metrics-table-header">
                  <span>Route</span>
                  <span>Model</span>
                  <span>Calls</span>
                  <span>Avg time</span>
                  <span>P95</span>
                  <span>Avg tokens</span>
                  <span>Fallbacks</span>
                </div>
                {status.inference_metrics.routes.map((route) => (
                  <article className="metrics-table-row" key={`${route.route_name}-${route.model}-${route.backend ?? "unknown"}`}>
                    <span>{formatStage(route.route_name)}</span>
                    <strong>
                      {route.model}
                      {currentRouteModel(status, route.route_name) === route.model ? <em>Current</em> : <em>Historical</em>}
                    </strong>
                    <span>{route.record_count}</span>
                    <span>{formatMetricMs(route.avg_total_ms)}</span>
                    <span>{formatMetricMs(route.p95_total_ms ?? null)}</span>
                    <span>{formatMetricNumber(route.avg_total_tokens ?? null)}</span>
                    <span>{formatRate(route.fallback_rate)}</span>
                  </article>
                ))}
              </div>
            </section>
          ) : null}
          {status?.inference_metrics?.models?.length ? (
            <section className="metrics-section">
              <div className="library-section-header">
                <div>
                  <p className="section-kicker">By model</p>
                  <h2>Model averages</h2>
                </div>
              </div>
              <div className="metrics-table">
                <div className="metrics-table-header">
                  <span>Model</span>
                  <span>Backend</span>
                  <span>Calls</span>
                  <span>Avg time</span>
                  <span>P95</span>
                  <span>Avg prompt</span>
                  <span>Avg completion</span>
                </div>
                {status.inference_metrics.models.map((model) => (
                  <article className="metrics-table-row" key={`${model.model}-${model.backend ?? "unknown"}`}>
                    <strong>{model.model}</strong>
                    <span>{model.backend ?? "unknown"}</span>
                    <span>{model.record_count}</span>
                    <span>{formatMetricMs(model.avg_total_ms)}</span>
                    <span>{formatMetricMs(model.p95_total_ms)}</span>
                    <span>{formatMetricNumber(model.avg_prompt_tokens ?? null)}</span>
                    <span>{formatMetricNumber(model.avg_completion_tokens ?? null)}</span>
                  </article>
                ))}
              </div>
            </section>
          ) : null}
        </section>
      ) : null}
      {editingAdvancedSettings ? (
        <div className="modal-backdrop" role="presentation">
          <section className="advanced-settings-modal" role="dialog" aria-modal="true" aria-labelledby="advanced-settings-title">
            <button
              type="button"
              className="modal-close"
              onClick={() => setEditingAdvancedSettings(null)}
              aria-label="Close advanced settings"
              disabled={busy}
            >
              ×
            </button>
            <div>
              <p className="section-kicker">Brief settings</p>
              <h2 id="advanced-settings-title">{profileName(editingAdvancedSettings.topic)}</h2>
            </div>
            <div className="settings-tabs">
              <button
                type="button"
                className={editingAdvancedSettings.tab === "brief" ? "active" : ""}
                onClick={() => setEditingAdvancedSettings({ ...editingAdvancedSettings, tab: "brief" })}
              >
                Brief controls
              </button>
              <button
                type="button"
                className={editingAdvancedSettings.tab === "system" ? "active" : ""}
                onClick={() => setEditingAdvancedSettings({ ...editingAdvancedSettings, tab: "system" })}
              >
                System limits
              </button>
            </div>
            {editingAdvancedSettings.tab === "brief" ? (
              <>
                <BriefControlsPanel
                  controls={editingAdvancedSettings.controls}
                  defaults={defaultControlsDraft}
                  sourceSelection={sourceSelectionFromRecord(editingAdvancedSettings.topic.profile.source_selection)}
                  showReset={false}
                  onChange={(controls) => setEditingAdvancedSettings({ ...editingAdvancedSettings, controls })}
                />
                <SettingsErrorList errors={advancedControlsErrors} />
              </>
            ) : (
              <div className="advanced-system-limits">
                <section>
                  <div className="compact-title-row">
                    <p className="section-kicker">Configured pipeline limits</p>
                    <h3>This brief</h3>
                    <p className="muted">Use these when this saved brief runs.</p>
                  </div>
                  <PipelineLimitsPanel
                    limits={editingAdvancedSettings.pipelineLimits}
                    defaults={briefSettings?.pipeline_limits ?? defaultPipelineLimits}
                    onChange={(pipelineLimits) => setEditingAdvancedSettings({ ...editingAdvancedSettings, pipelineLimits })}
                  />
                </section>
                <section>
                  <div className="compact-title-row">
                    <p className="section-kicker">Hard caps</p>
                    <h3>System ceilings</h3>
                  </div>
                  <SystemLimitsPanel groups={briefSettings?.system_limits ?? []} />
                </section>
              </div>
            )}
            <div className="modal-actions">
              <button type="button" className="ghost-action" onClick={() => setEditingAdvancedSettings(null)} disabled={busy}>Cancel</button>
              <button
                type="button"
                className="secondary-action"
                onClick={() => setEditingAdvancedSettings({
                  ...editingAdvancedSettings,
                  controls: defaultControlsDraft,
                  pipelineLimits: briefSettings?.pipeline_limits ?? defaultPipelineLimits,
                  tab: "brief",
                })}
                disabled={busy}
              >
                Use system defaults
              </button>
              <button type="button" className="primary-action" onClick={() => void saveAdvancedSettings()} disabled={busy || advancedControlsErrors.length > 0}>
                Save settings
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </main>
  );
}

function SecretHealthPanel(props: {
  health: AdminStatus["secret_health"] | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  if (!props.health) return null;
  return (
    <section className="secret-health-panel">
      <div className="library-section-header">
        <div>
          <p className="section-kicker">
            {props.health.summary.configured_count} configured · {props.health.summary.warning_count} warning(s)
          </p>
          <h2>Secret health</h2>
        </div>
        <span className={props.health.summary.warning_count ? "status-pill" : "status-pill good"}>
          {props.health.summary.warning_count ? "Review" : "Owner-only"}
        </span>
        <DisclosureButton expanded={props.expanded} label={props.expanded ? "Hide" : "Show"} onToggle={props.onToggle} />
      </div>
      {props.expanded ? (
        <>
          <p className="muted">Secrets folder: {props.health.secrets_dir}</p>
          <div className="health-grid secret-health-grid">
            <article className={`health-card ${props.health.directory_permissions.status === "ok" ? "ok" : "warning"}`}>
              <strong>Folder permissions</strong>
              <p>
                {props.health.directory_permissions.status === "ok"
                  ? "Owner-only access."
                  : `Review folder mode ${props.health.directory_permissions.mode ?? "unknown"}.`}
              </p>
            </article>
            {props.health.items.map((item) => (
              <article className={`health-card ${item.status}`} key={item.id}>
                <strong>{item.label}</strong>
                <p>{item.configured ? item.storage : item.message}</p>
                {item.path ? <small>{item.path}</small> : null}
              </article>
            ))}
          </div>
          {props.health.external_plaintext.length ? (
            <div className="issue-note">
              <strong>Plaintext MCP config to review</strong>
              {props.health.external_plaintext.map((item) => (
                <p key={`${item.server}-${item.location}-${item.key}`}>
                  {item.server}: {item.location}.{item.key} in {item.path}
                </p>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function LibrarySection(props: {
  title: string;
  sort: SortMode;
  onSort: (sort: SortMode) => void;
  count: number;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className="library-section">
      <div className="library-section-header">
        <div>
          <p className="section-kicker">{props.count} total</p>
          <h2>{props.title}</h2>
        </div>
        <div className="segmented-control">
          <button type="button" className={props.sort === "recent" ? "active" : ""} onClick={() => props.onSort("recent")}>Recent</button>
          <button type="button" className={props.sort === "name" ? "active" : ""} onClick={() => props.onSort("name")}>Name</button>
        </div>
        <DisclosureButton expanded={props.expanded} label={props.expanded ? "Hide" : "Show"} onToggle={props.onToggle} />
      </div>
      {props.expanded ? <div className="library-list">{props.children}</div> : null}
    </section>
  );
}

function DisclosureButton(props: { expanded: boolean; label: string; onToggle: () => void }) {
  return (
    <button type="button" className="disclosure-button" onClick={props.onToggle} aria-expanded={props.expanded}>
      <span>{props.expanded ? "▾" : "▸"}</span>
      {props.label}
    </button>
  );
}

function emptyDraft(defaults = defaultContentLimits): ConfirmationDraft {
  return {
    scope: "",
    depth: "informed-generalist",
    recency_weighting: "recent",
    lookback_hours: defaultBriefControls.lookback_hours,
    exclusions: "",
    content_limits: defaults,
    recency_scope_confirmed: false,
    sourceScopeTouched: false,
  };
}

function draftFromProfile(profile: TopicProfile, defaults = defaultContentLimits): ConfirmationDraft {
  return {
    scope: profile.scope || profile.statement || "",
    depth: profile.depth === "practitioner" ? "practitioner" : "informed-generalist",
    recency_weighting: sourceScopeFromProfile(profile),
    lookback_hours: lookbackHoursForBuild(profile, undefined, defaultBriefControls.lookback_hours),
    exclusions: (profile.exclusions ?? []).join(", "),
    content_limits: contentLimitsFromProfile(profile, defaults),
    recency_scope_confirmed: false,
    sourceScopeTouched: false,
  };
}

function briefControlsFromProfile(profile: TopicProfile, defaults = defaultBriefControls): BriefControlsDraft {
  const explicit = Number(profile.lookback_hours ?? 0);
  return {
    lookback_hours: Number.isFinite(explicit) && explicit >= 1
      ? Math.min(8760, Math.floor(explicit))
      : defaults.lookback_hours,
    content_limits: contentLimitsFromProfile(profile, defaults.content_limits),
  };
}

function contentLimitsFromProfile(profile: TopicProfile, defaults = defaultContentLimits): ContentLimitsDraft {
  const saved = profile.content_limits ?? {};
  return {
    total_items: clampContentLimit(Number(saved.total_items ?? defaults.total_items), 1, 250),
    target_items: clampContentLimit(Number(saved.target_items ?? defaults.target_items), 1, 250),
    lead_items: clampContentLimit(Number(saved.lead_items ?? defaults.lead_items), 0, 20),
    per_source: {
      ...defaults.per_source,
      ...(saved.per_source ?? {}),
    },
    quality_floor: saved.quality_floor === "strong" ? "strong" : "standard",
  };
}

function pipelineLimitsFromProfile(profile: TopicProfile, defaults = defaultPipelineLimits): PipelineLimitsDraft {
  const saved = profile.pipeline_limits ?? {};
  return {
    article_fetches: clampContentLimit(Number(saved.article_fetches ?? defaults.article_fetches), 1, 250),
    article_fetch_concurrency: clampContentLimit(Number(saved.article_fetch_concurrency ?? defaults.article_fetch_concurrency), 1, 20),
    model_refinement_items: clampContentLimit(Number(saved.model_refinement_items ?? defaults.model_refinement_items), 0, 150),
    source_audit_candidates: clampContentLimit(Number(saved.source_audit_candidates ?? defaults.source_audit_candidates), 1, 28),
    editorial_candidates: clampContentLimit(Number(saved.editorial_candidates ?? defaults.editorial_candidates), 1, 150),
    critic_articles: clampContentLimit(Number(saved.critic_articles ?? defaults.critic_articles), 1, 50),
    critic_newsletter_records: clampContentLimit(Number(saved.critic_newsletter_records ?? defaults.critic_newsletter_records), 0, 20),
  };
}

function clampContentLimit(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, Math.round(value)));
}

function validateBriefControls(controls: BriefControlsDraft, sourceSelection: Record<SourceKey, boolean>): string[] {
  const errors: string[] = [];
  const sourceWindowDays = Number(controls.lookback_hours) / 24;
  addBoundsError(errors, "Source window", sourceWindowDays, briefControlBounds.source_window_days.min, briefControlBounds.source_window_days.max, "days");
  errors.push(...validateContentLimits(controls.content_limits, sourceSelection));
  return errors;
}

function validateContentLimits(contentLimits: ContentLimitsDraft, sourceSelection: Record<SourceKey, boolean>): string[] {
  const errors: string[] = [];
  addBoundsError(errors, "Candidate budget", contentLimits.total_items, briefControlBounds.total_items.min, briefControlBounds.total_items.max);
  addBoundsError(errors, "Target visible stories", contentLimits.target_items, briefControlBounds.target_items.min, briefControlBounds.target_items.max);
  addBoundsError(errors, "Lead stories", contentLimits.lead_items, briefControlBounds.lead_items.min, briefControlBounds.lead_items.max);
  sourceOptions
    .filter((source) => sourceSelection[source.key])
    .forEach((source) => {
      const value = contentLimits.per_source[source.key] ?? 0;
      addBoundsError(errors, `${source.label} maximum`, value, briefControlBounds.per_source.min, briefControlBounds.per_source.max);
    });
  return errors;
}

function addBoundsError(errors: string[], label: string, value: number, min: number, max: number, suffix = "") {
  const valueLabel = suffix ? `${min}-${max} ${suffix}` : `${min}-${max}`;
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < min || value > max) {
    errors.push(`${label} must be a whole number from ${valueLabel}.`);
  }
}

function lookbackHoursForConfirmedDraft(profile: TopicProfile | null | undefined, draft: ConfirmationDraft, defaultLookbackHours = defaultBriefControls.lookback_hours): number {
  if (draft.sourceScopeTouched) return clampContentLimit(Number(draft.lookback_hours || 0), 1, 8760);
  return lookbackHoursForBuild(profile, draft, defaultLookbackHours);
}

function lookbackHoursForBuild(profile: TopicProfile | null | undefined, draft?: ConfirmationDraft, defaultLookbackHours = defaultBriefControls.lookback_hours): number {
  if (draft?.sourceScopeTouched && Number.isFinite(Number(draft.lookback_hours)) && Number(draft.lookback_hours) >= 1) {
    return Math.min(8760, Math.floor(Number(draft.lookback_hours)));
  }
  const explicit = Number(profile?.lookback_hours ?? 0);
  if (Number.isFinite(explicit) && explicit >= 1) return Math.min(8760, Math.floor(explicit));
  if (!draft && Number.isFinite(defaultLookbackHours) && defaultLookbackHours >= 1) {
    return Math.min(8760, Math.floor(defaultLookbackHours));
  }
  return lookbackHoursFromSourceScope(draft?.recency_weighting ?? normalizeSourceScope(profile?.recency_weighting));
}

function sourceScopeFromProfile(profile: TopicProfile): SourceScope {
  const explicit = Number(profile.lookback_hours ?? 0);
  if (Number.isFinite(explicit) && explicit >= 1) {
    if (explicit <= 48) return "breaking";
    if (explicit >= 365 * 24) return "last_year";
    return "recent";
  }
  return normalizeSourceScope(profile.recency_weighting);
}

function lookbackHoursFromSourceScope(sourceScope: SourceScope): number {
  if (sourceScope === "last_year" || sourceScope === "all_available") return 8760;
  if (sourceScope === "recent") return 336;
  return 24;
}

function sourceScopeConfirmation(sourceScope: SourceScope, lookbackHours?: number): string {
  if (lookbackHours && sourceScope !== "all_available") {
    const days = Math.max(1, Math.round(lookbackHours / 24));
    return `I’ll look for sources dated within the last ${days === 1 ? "day" : `${days} days`}.`;
  }
  if (sourceScope === "breaking") return "I’ll look for sources dated within the last 24 hours.";
  if (sourceScope === "recent") return "I’ll look for sources dated within the last 3 days.";
  if (sourceScope === "last_year") return "I’ll look for sources dated within the last year.";
  return "I’ll use the best available sources, even when older context is useful.";
}

function normalizeSourceScope(value: string | undefined): SourceScope {
  if (value === "breaking") return "breaking";
  if (value === "last_year") return "last_year";
  if (value === "all_available" || value === "evergreen") return "all_available";
  return "recent";
}

function sourceReadinessItems(
  selection: Record<SourceKey, boolean>,
  status: SourceStatusResponse | null,
  profile: TopicProfile | null,
): Array<{ key: SourceKey; label: string; ready: boolean; message: string }> {
  return sourceOptions
    .filter((source) => selection[source.key])
    .map((source) => {
      const sourceStatus = status?.sources[source.key];
      const queries = profile?.source_queries?.[source.key] ?? [];
      if (!sourceStatus?.enabled) {
        return { key: source.key, label: source.label, ready: false, message: sourceStatus?.reason || "not configured" };
      }
      if (source.key === "podcasts" && !queries.length && !sourceStatus.configured_source_count) {
        return { key: source.key, label: source.label, ready: false, message: "no show or search targets yet" };
      }
      if (source.key === "youtube" && sourceStatus.quota_units_used && sourceStatus.quota_units_used >= 8000) {
        return { key: source.key, label: source.label, ready: false, message: "quota is high today" };
      }
      if (source.key === "foreign_media" && !queries.length && !profile?.foreign_language_plan?.length) {
        return { key: source.key, label: source.label, ready: false, message: "no native-language plan yet" };
      }
      return { key: source.key, label: source.label, ready: true, message: queries.length ? `${queries.length} planned query(s)` : "ready" };
    });
}

function searchPlanItems(profile: TopicProfile | null): string[] {
  if (!profile) return [];
  const items: string[] = [];
  const sourceSelection = profile.source_selection ?? {};
  const hasSourceSelection = Object.keys(sourceSelection).length > 0;
  for (const query of profile.search_queries ?? []) {
    if (query.trim()) items.push(query.trim());
  }
  for (const [source, queries] of Object.entries(profile.source_queries ?? {})) {
    if (hasSourceSelection && !sourceSelection[source]) continue;
    for (const query of queries) {
      const cleaned = query.trim();
      if (cleaned) items.push(`${formatSourceLabel(source)}: ${cleaned}`);
    }
  }
  for (const item of profile.foreign_language_plan ?? []) {
    if (item.native_query?.trim()) items.push(`${item.name || item.code}: ${item.native_query.trim()}`);
  }
  return Array.from(new Set(items)).slice(0, 8);
}

type SearchPlanGroup = {
  key: string;
  label: string;
  queries: string[];
};

function sourceSearchPlanGroups(profile: TopicProfile | null): SearchPlanGroup[] {
  if (!profile) return [];
  const groups: SearchPlanGroup[] = [];
  const generalQueries = uniqueCleanList(profile.search_queries ?? []);
  if (generalQueries.length) {
    groups.push({ key: "general", label: "General", queries: generalQueries });
  }

  const sourceSelection = profile.source_selection ?? {};
  const hasSourceSelection = Object.keys(sourceSelection).length > 0;
  for (const source of sourceOptions) {
    if (hasSourceSelection && !sourceSelection[source.key]) continue;
    const queries = uniqueCleanList(profile.source_queries?.[source.key] ?? []);
    groups.push({
      key: source.key,
      label: source.label,
      queries: queries.length ? queries : [emptySourcePlanLabel(source.key)],
    });
  }

  for (const item of profile.foreign_language_plan ?? []) {
    const nativeQuery = item.native_query?.trim();
    if (nativeQuery) {
      groups.push({
        key: `foreign-${item.code || item.name || nativeQuery}`,
        label: item.name || item.code || "Foreign Media",
        queries: [nativeQuery],
      });
    }
  }
  return groups;
}

function uniqueCleanList(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function emptySourcePlanLabel(source: SourceKey): string {
  if (source === "markets") return "No ticker resolved yet";
  if (source === "foreign_media") return "No native-language query set yet";
  if (source === "gmail") return "Uses approved newsletter rules";
  return "Uses general search terms";
}

function splitList(value: string): string[] {
  return value.split(/[,;\n]/).map((item) => item.trim()).filter(Boolean);
}

function enabledSourceSelection(selection: Record<SourceKey, boolean>, status: SourceStatusResponse | null): Record<SourceKey, boolean> {
  return {
    web_search: Boolean(selection.web_search && status?.sources.web_search?.enabled),
    foreign_media: Boolean(selection.foreign_media && status?.sources.foreign_media?.enabled),
    gmail: Boolean(selection.gmail && status?.sources.gmail?.enabled),
    podcasts: Boolean(selection.podcasts && status?.sources.podcasts?.enabled),
    youtube: Boolean(selection.youtube && status?.sources.youtube?.enabled),
    collections: Boolean(selection.collections && status?.sources.collections?.enabled),
    markets: Boolean(selection.markets && status?.sources.markets?.enabled),
  };
}

function firstBlockedSelectedSource(selection: Record<SourceKey, boolean>, status: SourceStatusResponse | null): SourceKey | null {
  for (const source of sourceOptions) {
    if (selection[source.key] && status && !status.sources[source.key]?.enabled) return source.key;
  }
  return null;
}

function hasEnabledSource(selection: Record<string, boolean>): boolean {
  return Object.values(selection).some(Boolean);
}

function briefPath(record: Exploration | null): string | null {
  if (!record) return null;
  if (record.progress.brief?.html_path) return record.progress.brief.html_path;
  if (record.brief_ref) return `/api/explore/explorations/${record.exploration_id}/brief/html`;
  return null;
}

function openPath(path: string | null) {
  if (path) window.location.assign(path);
}

function sourceSelectionFromRecord(selection: Record<string, boolean> | undefined): Record<SourceKey, boolean> {
  return sourceOptions.reduce<Record<SourceKey, boolean>>((result, source) => {
    result[source.key] = Boolean(selection?.[source.key]);
    return result;
  }, { ...defaultSourceSelection });
}

function profileName(topic: TopicProfileResponse): string {
  return topic.profile.scope || topic.statement || "Untitled brief";
}

function homeRecentKey(item: HomeRecentItem): string {
  if (item.kind === "topic") return `topic-${item.topic.topic_id}`;
  return `exploration-${item.exploration.exploration_id}`;
}

function homeRecentTitle(item: HomeRecentItem): string {
  if (item.kind === "topic") return profileName(item.topic);
  return item.topic ? profileName(item.topic) : item.exploration.progress.brief?.title ?? "Brief";
}

function homeRecentDate(item: HomeRecentItem): number {
  if (item.kind === "topic") return dateValue(item.topic.updated_at ?? item.topic.created_at);
  return dateValue(item.exploration.finished_at ?? item.exploration.started_at);
}

function homeRecentMeta(item: HomeRecentItem): string {
  if (item.kind === "topic") return relativeDate(item.topic.updated_at ?? item.topic.created_at);
  if (item.exploration.status === "queued") return "queued";
  if (item.exploration.status === "running") return "building";
  if (item.exploration.status === "failed") return "failed";
  return relativeDate(item.exploration.finished_at ?? item.exploration.started_at);
}

function homeRecentBadge(item: HomeRecentItem): string | null {
  if (item.kind === "topic") return "plan";
  if (item.exploration.status === "queued") return "queued";
  if (item.exploration.status === "running") return "building";
  if (item.exploration.status === "failed") return "failed";
  return null;
}

function homeRecentIcon(item: HomeRecentItem): string {
  if (item.kind === "topic") return "◇";
  if (item.exploration.status === "queued") return "◌";
  if (item.exploration.status === "running") return "◌";
  if (item.exploration.status === "failed") return "!";
  return "⌕";
}

function explorationLibraryName(item: ExplorationLibraryItem): string {
  if (item.kind === "topic") return profileName(item.topic);
  return item.topic?.profile.scope
    ?? item.topic?.statement
    ?? item.exploration.progress.brief?.title
    ?? "Brief";
}

function explorationLibraryDate(item: ExplorationLibraryItem): number {
  if (item.kind === "topic") return dateValue(item.topic.updated_at ?? item.topic.created_at);
  return dateValue(item.exploration.finished_at ?? item.exploration.started_at);
}

function digestLibraryName(item: DigestLibraryItem): string {
  if (item.kind === "topic") return profileName(item.topic);
  return item.digest.name || item.digest.interest || "Digest";
}

function digestLibraryDate(item: DigestLibraryItem): number {
  if (item.kind === "topic") return dateValue(item.topic.updated_at ?? item.topic.created_at);
  return dateValue(item.digest.updated_at ?? item.digest.created_at);
}

function formatSourceLabel(source: string): string {
  if (source === "web_search") return "Web";
  if (source === "foreign_media") return "Foreign Media";
  if (source === "gmail") return "Gmail";
  if (source === "podcasts") return "Podcast";
  if (source === "youtube") return "YouTube";
  if (source === "collections") return "Collections";
  if (source === "markets") return "Markets";
  return source.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function gmailLookbackLabel(hours: number | undefined): string {
  const value = Number(hours || 0);
  if (!Number.isFinite(value) || value < 1) return "Default window";
  if (value % 168 === 0 && value >= 168) {
    const weeks = value / 168;
    return `Last ${weeks} week${weeks === 1 ? "" : "s"}`;
  }
  if (value % 24 === 0 && value >= 24) {
    const days = value / 24;
    return `Last ${days} day${days === 1 ? "" : "s"}`;
  }
  return `Last ${value} hour${value === 1 ? "" : "s"}`;
}

function formatSourceSelection(selection: Record<string, boolean>): string {
  const enabled = sourceOptions.filter((source) => selection[source.key]).map((source) => source.label);
  return enabled.length ? enabled.join(", ") : "No sources";
}

function sourcePlan(selection: Record<string, boolean>): string {
  const enabled = sourceOptions.filter((source) => selection[source.key]).map((source) => source.label);
  const disabled = sourceOptions.filter((source) => !selection[source.key]).map((source) => source.label);
  if (!enabled.length) return "No sources selected";
  return disabled.length ? `Running: ${enabled.join(", ")} (${disabled.join(", ")} excluded)` : `Running: ${enabled.join(", ")}`;
}

function formatPipeline(pipeline: Array<[string, string]>): string {
  const running = pipeline.find(([, status]) => status === "running");
  if (running) return `${formatStage(running[0])} running`;
  const failed = pipeline.find(([, status]) => status === "failed");
  if (failed) return `${formatStage(failed[0])} failed`;
  return "Ready";
}

function progressHeadline(exploration: Exploration): string {
  if (exploration.status === "queued") return "Waiting its turn";
  if (exploration.status === "failed") return "Build failed";
  if (isModelDegraded(exploration)) return "Brief built with AI issues";
  const running = Object.entries(exploration.progress.pipeline ?? {}).find(([, status]) => status === "running");
  if (!running) return exploration.status === "complete" ? "Brief ready" : "Preparing build";
  const labels: Record<string, string> = {
    discovery: "Discovering sources",
    fetch: "Fetching source content",
    summarize: "Enriching and translating items",
    audit: "Auditing source fit",
    rank: "Ranking the complete set",
    review: "Reviewing the brief",
    done: "Rendering the brief",
  };
  return labels[running[0]] ?? `${formatStage(running[0])} running`;
}

function progressDetail(exploration: Exploration): string {
  if (exploration.status === "queued") {
    return exploration.progress.queue?.message ?? "Queued behind another brief. It will start automatically.";
  }
  if (exploration.status === "failed") return exploration.progress.error ?? "The build stopped before the brief was ready.";
  if (isModelDegraded(exploration)) return modelDegradedMessage(exploration);
  if (exploration.progress.source_audit?.message) return exploration.progress.source_audit.message;
  if (exploration.progress.source_audit?.summary) return exploration.progress.source_audit.summary;
  const candidateCount = exploration.progress.candidate_count ?? 0;
  const running = Object.entries(exploration.progress.pipeline ?? {}).find(([, status]) => status === "running")?.[0];
  if (running === "discovery") return "Searching every selected source from scratch.";
  if (running === "fetch") return `Fetching full content for ${candidateCount || "the discovered"} candidate items.`;
  if (running === "summarize") return "Cleaning, summarizing, and translating usable source material.";
  if (running === "audit") return "Checking whether the retrieved sources match the requested strategy.";
  if (running === "rank") return "Comparing all candidates together before choosing the lead stories.";
  if (running === "review") return "The critic is checking quality, cuts, and adherence before publish.";
  if (running === "done") return "Writing the finished brief HTML.";
  return "Preparing the full rebuild pipeline.";
}

function isModelDegraded(exploration: Exploration): boolean {
  if (exploration.progress.model_health?.status === "degraded") return true;
  const stats = exploration.progress.brief?.stats;
  const modelCalls = Number(stats?.model_call_count ?? 0);
  const modelSuccesses = Number(stats?.model_success_count ?? 0);
  const modelFailures = Number(stats?.model_failure_count ?? 0);
  const includedArticles = Number(stats?.included_article_count ?? 0);
  return modelCalls > 0 && (modelSuccesses === 0 || (modelFailures > 0 && includedArticles === 0));
}

function hasActionableBuildIssues(exploration: Exploration): boolean {
  return buildAttentionIssues(exploration).length > 0;
}

function buildAttentionIssues(exploration: Exploration | null): ExplorationIssue[] {
  if (!exploration) return [];
  return [
    ...(exploration.progress.requested_source_issues ?? []),
    ...actionableIssues(exploration.progress.source_audit_issues),
  ];
}

function actionableIssues(issues: ExplorationIssue[] | undefined): ExplorationIssue[] {
  return (issues ?? []).filter((issue) => isActionableIssue(issue));
}

function filterDecisionNotes(exploration: Exploration): ExplorationIssue[] {
  return [
    ...(exploration.progress.source_filter_notes ?? []),
    ...(exploration.progress.source_audit_issues ?? []).filter((issue) => !isActionableIssue(issue)),
  ];
}

function sourceFromIssueName(sourceName: string): string {
  const lowered = sourceName.toLowerCase();
  if (lowered.includes("gmail") || lowered.includes("@")) return "Gmail";
  if (lowered.includes("podcast")) return "Podcast";
  if (lowered.includes("youtube")) return "YouTube";
  if (lowered.includes("market") || /^[A-Z0-9.=-]{1,12}$/.test(sourceName.trim())) return "Markets";
  if (/[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/.test(sourceName)) return "Foreign Media";
  return "Web";
}

function isActionableIssue(issue: ExplorationIssue): boolean {
  const source = issue.source_name.trim().toLowerCase();
  const reason = issue.reason.trim().toLowerCase();
  return source === "source audit" || source === "ai review" || reason.startsWith("audit could not complete");
}

function modelDegradedMessage(exploration: Exploration): string {
  if (exploration.progress.model_health?.message) return exploration.progress.model_health.message;
  const stats = exploration.progress.brief?.stats;
  const modelCalls = Number(stats?.model_call_count ?? 0);
  const modelSuccesses = Number(stats?.model_success_count ?? 0);
  if (modelCalls > 0 && modelSuccesses === 0) {
    return "AI review did not complete; the brief was built with fallback checks.";
  }
  return "The brief finished, but AI review had failures. Rebuild after the model service is healthy.";
}

function formatStage(value: string): string {
  return value.split("_").filter(Boolean).map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`).join(" ");
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    month: "numeric",
    day: "numeric",
    year: "2-digit",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function releaseStamp(status: AdminStatus | null): string {
  const release = status?.system?.release;
  const timestamp = release?.timestamp ? formatDateTime(release.timestamp) : "";
  const revision = release?.revision ? release.revision : "";
  if (timestamp && revision) return `Release ${timestamp} · ${revision}`;
  if (timestamp) return `Release ${timestamp}`;
  if (revision) return `Release ${revision}`;
  return "Release unknown";
}

function progressIntentSummary(profile: TopicProfile | null, fallback: string): string {
  if (!profile) {
    const cleaned = fallback.trim();
    return cleaned ? truncateSentence(cleaned, 160) : "Reviewing your brief plan and source strategy.";
  }
  const scope = profile.scope || profile.statement || fallback || "Reviewing your brief plan";
  const sources = Object.entries(profile.source_selection ?? {})
    .filter(([, enabled]) => enabled)
    .map(([source]) => formatSourceLabel(source))
    .filter((source) => source !== "Collections")
    .slice(0, 5);
  const lookback = profile.lookback_hours
    ? `over the last ${Math.max(1, Math.round(Number(profile.lookback_hours) / 24))} days`
    : "";
  const exclusions = profile.exclusions?.length ? ` while avoiding ${profile.exclusions.slice(0, 2).join(" and ")}` : "";
  const sourceText = sources.length ? ` across ${sources.join(", ")}` : "";
  return truncateSentence(`${scope}${sourceText}${lookback ? ` ${lookback}` : ""}${exclusions}.`, 160);
}

function truncateSentence(value: string, maxLength: number): string {
  const cleaned = value.split(/\s+/).join(" ").trim();
  if (cleaned.length <= maxLength) return cleaned;
  return `${cleaned.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
}

function strategyUpdateConfirmation(note: string | undefined, profile: TopicProfile): string {
  const sourceQueries = profile.source_queries ?? {};
  const changedSources = Object.entries(sourceQueries)
    .filter(([, queries]) => Array.isArray(queries) && queries.length > 0)
    .map(([source]) => formatSourceLabel(source))
    .slice(0, 5);
  const summary = (note ?? "").trim();
  if (summary && summary !== "Search strategy updated.") return summary;
  if (changedSources.length) {
    return `Updated the strategy and refreshed the visible plan for ${changedSources.join(", ")}.`;
  }
  return "I applied the instruction, but it did not add a visible source query. Review the plan before building.";
}

function relativeDate(value: string | null | undefined): string {
  if (!value) return "never";
  const delta = Date.now() - new Date(value).valueOf();
  if (Number.isNaN(delta)) return "unknown";
  const days = Math.floor(delta / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "1d ago";
  return `${days}d ago`;
}

function dateValue(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = new Date(value).valueOf();
  return Number.isNaN(parsed) ? 0 : parsed;
}

function routeDraftFromStatus(status: AdminStatus | null): ModelRouteDraft {
  const routes = status?.model?.routing?.routes ?? {};
  const draft: ModelRouteDraft = {};
  Object.entries(routes).forEach(([agent, route]) => {
    draft[agent] = {
      provider: route.provider || "local",
      model: route.model ?? "",
      allow_private_cloud: Boolean(route.allow_private_cloud),
    };
  });
  return draft;
}

function formatMetricMs(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  return `${Math.round(value)} ms`;
}

function formatMetricNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  return `${Math.round(value)}`;
}

function currentRouteModel(status: AdminStatus | null, routeName: string): string | null {
  const route = status?.model?.routing?.routes?.[routeName];
  return route?.effective_model ?? route?.model ?? status?.model?.routing?.defaults?.local ?? null;
}

function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  return `${Math.round(value * 100)}%`;
}

type RefinementProgressState = {
  stage: string;
  detail: string;
  activity: string;
  percent: number;
  elapsedMs: number;
  alert: boolean;
};

function refinementProgressState(progress: RefinementProgress, now: number): RefinementProgressState {
  const elapsedMs = Math.max(0, now - progress.startedAt);
  const seconds = elapsedMs / 1000;
  let stage = "Preparing request";
  let detail = "Packaging your interest, selected sources, and the current refinement state.";
  let activity = "Still working.";
  const percent = 0;

  if (seconds >= 1.5) {
    stage = "Calling model";
    detail = "The model is reviewing your plan.";
  }
  if (seconds >= 4) {
    stage = "Still working";
    detail = "The model is reviewing your brief plan. This step can take a minute.";
    activity = "Still working.";
  }
  if (seconds >= 9) {
    stage = "Still working";
    detail = "The model is still working on the strategy update.";
    activity = "Still working.";
  }
  if (seconds >= 20) {
    stage = "Still working";
    detail = "This is taking longer than usual, but the request is still running.";
    activity = "Still working.";
  }
  if (seconds >= 75) {
    stage = "Taking longer than usual";
    detail = "The request has not finished yet. You can keep waiting or retry.";
  }
  if (progress.phase === "answering" && seconds < 4) {
    stage = "Updating search strategy";
    detail = "The model is reviewing your feedback.";
  }
  if (progress.phase === "confirming" && seconds < 4) {
    stage = "Reviewing strategy";
    detail = "The model is checking the plan before the brief is built.";
  }

  return {
    stage,
    detail,
    activity,
    percent,
    elapsedMs,
    alert: seconds >= 20,
  };
}

function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}m ${seconds}s`;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
