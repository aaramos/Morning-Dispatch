import { useCallback, useEffect, useMemo, useState } from "react";
import type { ChangeEvent, FormEvent, ReactNode } from "react";

type SourceKey = "web_search" | "foreign_media" | "gmail" | "reddit" | "podcasts" | "youtube" | "collections" | "markets";
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
  schedule?: string | null;
  schedule_config?: Record<string, unknown>;
  delivery_config?: Record<string, unknown>;
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

type RefinementSession = {
  session_id: string;
  statement: string;
  status: "active" | "finalized";
  turn_count: number;
  messages: Array<{ role: "assistant" | "user"; content: string }>;
  profile: TopicProfile;
  topic_id: string | null;
  topic_profile?: TopicProfileResponse;
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
  models: Record<string, never>;
  schedule?: string | null;
  schedule_config?: Record<string, unknown>;
  delivery_config?: Record<string, unknown>;
};

type ExplorationIssue = {
  source_name: string;
  reason: string;
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
  exclusions: string;
  sourceScopeTouched?: boolean;
};

type RefinementProgress = {
  phase: RefinementProgressPhase;
  startedAt: number;
  label: string;
};

type AdminTab = "status" | "sources" | "library" | "models" | "metrics";

const sourceOptions: Array<{ key: SourceKey; label: string; icon: string }> = [
  { key: "web_search", label: "Web", icon: "🌐" },
  { key: "foreign_media", label: "Foreign Media", icon: "🌍" },
  { key: "gmail", label: "Gmail", icon: "✉️" },
  { key: "reddit", label: "Reddit", icon: "🟠" },
  { key: "podcasts", label: "Podcast", icon: "🎙️" },
  { key: "youtube", label: "YouTube", icon: "▶" },
  { key: "collections", label: "Collections", icon: "▣" },
  { key: "markets", label: "Markets", icon: "$" },
];

const defaultSourceSelection: Record<SourceKey, boolean> = {
  web_search: true,
  foreign_media: false,
  gmail: false,
  reddit: false,
  podcasts: false,
  youtube: false,
  collections: false,
  markets: false,
};

const schedulePresets: Array<{ value: SchedulePreset; label: string }> = [
  { value: "daily", label: "Daily" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];
const interestDraftCookieName = "morning_dispatch_interest_draft";
const interestDraftTtlSeconds = 60 * 60;
const interestDraftTtlMs = interestDraftTtlSeconds * 1000;
const adminTabOptions: AdminTab[] = ["status", "sources", "library", "models", "metrics"];

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
  const [initialRefineExplorationId, setInitialRefineExplorationId] = useState(() => {
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
  const activeInterest = (submittedInterest || statement).trim();
  const sourceLocked = flow === "building";
  const canSubmitInterest = (flow === "idle" || flow === "ready") && statement.trim().length > 0 && !busy;
  const canBuild = activeInterest.length > 0 && !busy;
  const currentIssues = buildAttentionIssues(exploration);
  const refinementWorking = busy && !enableSource && !exploration && flow === "refining";
  const activeRefinementProgress = useMemo<RefinementProgress | null>(() => {
    if (refinementProgress) return refinementProgress;
    if (!refinementWorking || !refinementFallbackStartedAt) return null;
    return { phase: "starting", startedAt: refinementFallbackStartedAt, label: "Refining" };
  }, [refinementFallbackStartedAt, refinementProgress, refinementWorking]);

  const loadHome = useCallback(async () => {
    const [sources, explorations, scheduled, topics, admin] = await Promise.all([
      api<SourceStatusResponse>("/api/explore/source-status").catch(() => null),
      api<Exploration[]>("/api/explore/explorations?limit=25").catch(() => []),
      api<TopicProfileResponse[]>("/api/explore/scheduled-topic-profiles").catch(() => []),
      api<TopicProfileResponse[]>("/api/explore/topic-profiles").catch(() => []),
      api<AdminStatus>("/api/admin/status").catch(() => null),
    ]);
    if (sources) setSourceStatus(sources);
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
    setDraft(draftFromProfile(session.profile));
  }, [session]);

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
      setDraft(draftFromProfile(nextSession.profile));
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
      setDraft(draftFromProfile(updated.profile));
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

  function confirmedProfilePayload(): ConfirmedProfilePayload {
    const baseProfile = session?.profile ?? topicProfile?.profile;
    const topicId = topicProfile?.topic_id ?? session?.topic_id ?? baseProfile?.topic_id;
    const interest = activeInterest || baseProfile?.statement || "";
    const lookbackHours = lookbackHoursForConfirmedDraft(baseProfile, draft);
    return {
      ...(topicId ? { topic_id: topicId } : {}),
      ...(session?.session_id ? { refinement_session_id: session.session_id } : {}),
      statement: interest,
      scope: draft.scope.trim() || interest,
      depth: draft.depth,
      recency_weighting: draft.recency_weighting,
      ...(lookbackHours ? { lookback_hours: lookbackHours } : {}),
      exclusions: splitList(draft.exclusions),
      source_selection: selectedEnabledSources,
      requested_sources: baseProfile?.requested_sources ?? [],
      subtopics: baseProfile?.subtopics ?? [],
      keywords: baseProfile?.keywords ?? [],
      search_queries: baseProfile?.search_queries ?? [],
      source_queries: baseProfile?.source_queries ?? {},
      models: {},
      schedule: baseProfile?.schedule ?? null,
      schedule_config: baseProfile?.schedule_config ?? {},
      delivery_config: baseProfile?.delivery_config ?? {},
    };
  }

  async function buildBrief() {
    if (!canBuild) return;
    const blocked = firstBlockedSelectedSource(sourceSelection, sourceStatus);
    if (blocked) {
      setEnableSource(blocked);
      return;
    }
    setBusy(true);
    setFlow("building");
    setMessage("Building the brief...");
    setBriefHtml("");
    try {
      const profilePayload = {
        ...confirmedProfilePayload(),
        source_selection: selectedEnabledSources,
      };
      const started = refinementTargetExplorationId
        ? await api<{ exploration: Exploration }>(`/api/explore/explorations/${refinementTargetExplorationId}/rebuild`, {
          method: "POST",
          body: JSON.stringify({
            topic_profile: profilePayload,
            refinement_session_id: session?.session_id,
            source_selection: selectedEnabledSources,
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
      setExploration(started.exploration);
      const finished = await pollExploration(started.exploration.exploration_id);
      setExploration(finished);
      await loadBriefHtml(finished);
      await loadHome();
      setFlow("ready");
      setRefinementTargetExplorationId(null);
      setMessage(finished.progress.built_with_issues ? "Brief ready with issues" : refinementTargetExplorationId ? "Refined brief rebuilt" : "Brief ready");
    } catch (error) {
      setFlow("confirm");
      setMessage(errorMessage(error, "Could not build brief"));
    } finally {
      setBusy(false);
    }
  }

  async function rebuildBrief() {
    if (!exploration || !hasEnabledSource(selectedEnabledSources)) return;
    setBusy(true);
    setFlow("building");
    setMessage("Rebuilding the brief...");
    setBriefHtml("");
    try {
      const started = await api<{ exploration: Exploration }>(`/api/explore/explorations/${exploration.exploration_id}/rebuild`, {
        method: "POST",
        body: JSON.stringify({
          source_selection: selectedEnabledSources,
          lookback_hours: lookbackHoursForBuild(topicProfile?.profile ?? session?.profile, draft),
        }),
      });
      setExploration(started.exploration);
      const finished = await pollExploration(started.exploration.exploration_id);
      setExploration(finished);
      await loadBriefHtml(finished);
      await loadHome();
      setFlow("ready");
      setMessage(finished.progress.built_with_issues ? "Brief rebuilt with issues" : "Brief rebuilt");
    } catch (error) {
      setFlow("ready");
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
      setDraft(draftFromProfile(nextSession.profile));
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
  }, [beginRefinementProgress, endRefinementProgress, exploration, topicProfile]);

  async function pollExploration(explorationId: string): Promise<Exploration> {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const next = await api<Exploration>(`/api/explore/explorations/${explorationId}`);
      setExploration(next);
      if (next.status !== "queued" && next.status !== "running") return next;
      await sleep(1800);
    }
    throw new Error("Brief build timed out while waiting for results");
  }

  async function loadBriefHtml(record: Exploration) {
    const path = briefPath(record);
    if (!path) return;
    const response = await fetch(path);
    if (response.ok) {
      setBriefHtml(await response.text());
    }
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
    if (path) window.open(path, "_blank", "noopener,noreferrer");
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
        setDraft(draftFromProfile(item.topic.profile));
        setSourceSelection(sourceSelectionFromRecord(item.topic.profile.source_selection));
      }
      setFlow("building");
      setMessage(item.exploration.status === "queued" ? "Brief is queued..." : "Brief is still building...");
      try {
        const finished = await pollExploration(item.exploration.exploration_id);
        setExploration(finished);
        await loadBriefHtml(finished);
        await loadHome();
        setFlow("ready");
        setMessage(finished.progress.built_with_issues ? "Brief ready with issues" : "Brief ready");
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
    setDraft(draftFromProfile(topic.profile));
    setSourceSelection(sourceSelectionFromRecord(topic.profile.source_selection));
    setFlow("confirm");
    setMessage("Saved brief plan loaded");
  }

  function resetForNewBrief() {
    clearInterestDraft();
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
    let cancelled = false;
    setInitialRefineExplorationId(null);
    setBusy(true);
    setFlow("refining");
    setMessage("Loading brief to refine...");
    const now = Date.now();
    setRefinementFallbackStartedAt(now);
    setProgressNow(now);
    setRefinementProgress({ phase: "starting", label: "Reopening refinement", startedAt: now });
    void (async () => {
      try {
        const target = await api<Exploration>(`/api/explore/explorations/${initialRefineExplorationId}`);
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
          <a className="icon-menu" href="/admin" aria-label="Open Admin">•••</a>
        </header>

        <section className="dispatch-body">
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

          {homeRecentItems.length ? (
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
              sources={sourceSelection}
              sourceStatus={sourceStatus}
              busy={busy}
              onDraftChange={setDraft}
              onSourceClick={updateSource}
              onBuild={() => void buildBrief()}
            />
          ) : null}

          {flow === "building" && exploration ? (
            <ProgressPanel exploration={exploration} sourceSelection={selectedEnabledSources} />
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
      <p className="app-status">{message}</p>

      {enableSource ? (
        <EnableSourceModal
          source={enableSource}
          status={sourceStatus?.sources[enableSource]}
          webKey={webKey}
          gmailSecret={gmailSecret}
          podcastKey={podcastKey}
          podcastSecret={podcastSecret}
          youtubeKey={youtubeKey}
          busy={busy}
          onClose={() => setEnableSource(null)}
          onWebKeyChange={setWebKey}
          onGmailSecretChange={setGmailSecret}
          onGmailFileChange={(event) => void loadGmailClientFile(event)}
          onPodcastKeyChange={setPodcastKey}
          onPodcastSecretChange={setPodcastSecret}
          onYoutubeKeyChange={setYoutubeKey}
          onSaveWeb={() => void saveWebKey()}
          onSaveGmailSecret={() => void saveGmailClientSecret()}
          onConnectGmail={() => void connectGmail()}
          onSavePodcast={() => void savePodcastCredentials()}
          onSaveYoutube={() => void saveYoutubeCredentials()}
          onSetupCollections={() => void setupCollectionsSource()}
          onRetry={() => void refreshSourcesAndSelect(enableSource)}
        />
      ) : null}
      {activeRefinementProgress ? (
        <RefinementProgressOverlay
          progress={activeRefinementProgress}
          now={progressNow}
          interest={submittedInterest || statement}
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
            {message.content}
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
      <div className="progress-track" aria-label={`Refinement progress ${state.percent}%`}>
        <span style={{ width: `${state.percent}%` }} />
      </div>
      <div className="refinement-activity-row">
        <span className="activity-pulse" />
        <small>{state.activity}</small>
      </div>
    </div>
  );
}

function RefinementProgressOverlay(props: { progress: RefinementProgress; now: number; interest: string }) {
  return (
    <div className="refinement-progress-backdrop" role="status" aria-live="polite">
      <div className="refinement-progress-modal">
        <p className="section-kicker">Refinement running</p>
        <h2>Working on your brief plan</h2>
        <p className="muted">{props.interest}</p>
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

function ConfirmationPanel(props: {
  draft: ConfirmationDraft;
  profile: TopicProfile | null;
  sources: Record<SourceKey, boolean>;
  sourceStatus: SourceStatusResponse | null;
  busy: boolean;
  onDraftChange: (draft: ConfirmationDraft) => void;
  onSourceClick: (source: SourceKey) => void;
  onBuild: () => void;
}) {
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
              sourceScopeTouched: true,
            })}
          >
            <option value="breaking">Breaking News</option>
            <option value="recent">Recent Time</option>
            <option value="last_year">Within Last Year</option>
            <option value="all_available">As Much as possible</option>
          </select>
          <small>{sourceScopeConfirmation(props.draft.recency_weighting)}</small>
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
      {props.profile?.requested_sources?.length ? (
        <div className="requested-source-list">
          <strong>Requested sources</strong>
          {props.profile.requested_sources.map((source) => (
            <span key={`${source.adapter}-${source.ref}`}>{formatSourceLabel(source.adapter)}: {source.ref}</span>
          ))}
        </div>
      ) : null}
      {searchPlanItems(props.profile).length ? (
        <div className="search-plan-list">
          <strong>Search plan</strong>
          {searchPlanItems(props.profile).map((query) => (
            <span key={query}>{query}</span>
          ))}
        </div>
      ) : null}
      <div className="confirmation-actions">
        <button type="button" className="primary-action build-brief-action" onClick={props.onBuild} disabled={props.busy}>
          Build brief
        </button>
      </div>
    </section>
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
          {filterNotes.slice(0, 20).map((issue) => (
            <p key={`${issue.source_name}-${issue.reason}`}>{issue.source_name}: {issue.reason}</p>
          ))}
        </details>
      ) : null}
      {pipeline.length ? <p className="muted">{formatPipeline(pipeline)}</p> : null}
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
      <div className="ready-footer">
        <strong>{isModelDegraded(props.exploration) ? "Brief ready with AI issues" : "Brief ready"}</strong>
        <button type="button" onClick={props.onOpen}>Open brief</button>
      </div>
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
  busy: boolean;
  onClose: () => void;
  onWebKeyChange: (value: string) => void;
  onGmailSecretChange: (value: string) => void;
  onGmailFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onPodcastKeyChange: (value: string) => void;
  onPodcastSecretChange: (value: string) => void;
  onYoutubeKeyChange: (value: string) => void;
  onSaveWeb: () => void;
  onSaveGmailSecret: () => void;
  onConnectGmail: () => void;
  onSavePodcast: () => void;
  onSaveYoutube: () => void;
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
            <p>Markets uses free public-market data. No API key is required for Simple mode.</p>
            <button type="button" onClick={props.onRetry} disabled={props.busy}>Retry Markets</button>
          </div>
        ) : null}
        {props.source === "reddit" ? (
          <div className="enable-stack">
            <p>Reddit is enabled through the local MCP connection. Retry after the connector is running.</p>
            <button type="button" onClick={props.onRetry} disabled={props.busy}>Retry Reddit</button>
          </div>
        ) : null}
      </section>
    </div>
  );
}

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
  const [issueDetails, setIssueDetails] = useState<{ built_with_issues: boolean; issues: ExplorationIssue[] } | null>(null);
  const [webProvider, setWebProvider] = useState<"tavily" | "brave" | "serpapi">("tavily");
  const [webKey, setWebKey] = useState("");
  const [adminGmailSecret, setAdminGmailSecret] = useState("");
  const [adminPodcastKey, setAdminPodcastKey] = useState("");
  const [adminPodcastSecret, setAdminPodcastSecret] = useState("");
  const [youtubeKey, setYoutubeKey] = useState("");
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

  const topicById = useMemo(() => new Map(library.topics.map((topic) => [topic.topic_id, topic])), [library.topics]);
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
    const [nextStatus, nextSources, nextLibrary] = await Promise.all([
      api<AdminStatus>("/api/admin/status").catch(() => null),
      api<SourceStatusResponse>("/api/explore/source-status").catch(() => null),
      api<LibraryResponse>("/api/admin/library").catch(() => ({ explorations: [], deleted_explorations: [], topics: [], digests: [], legacy_digests: [] })),
    ]);
    setStatus(nextStatus);
    if (nextSources) setSources(nextSources);
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
    setBusy(true);
    try {
      await api(`/api/explore/explorations/${exploration.exploration_id}/rebuild`, {
        method: "POST",
        body: JSON.stringify({ source_selection: exploration.source_selection }),
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

  async function buildTopicFromAdmin(topic: TopicProfileResponse) {
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${topic.topic_id}/run`, {
        method: "POST",
        body: JSON.stringify({
          mode: "show_now",
          source_selection: topic.profile.source_selection,
          lookback_hours: lookbackHoursForBuild(topic.profile),
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
    setBusy(true);
    try {
      if (topic.latest_exploration) {
        await api(`/api/explore/explorations/${topic.latest_exploration.exploration_id}/rebuild`, {
          method: "POST",
          body: JSON.stringify({
            source_selection: topic.profile.source_selection,
            lookback_hours: lookbackHoursForBuild(topic.profile),
          }),
        });
      } else {
        await api(`/api/explore/topic-profiles/${topic.topic_id}/run`, {
          method: "POST",
          body: JSON.stringify({
            mode: "scheduled",
            source_selection: topic.profile.source_selection,
            lookback_hours: lookbackHoursForBuild(topic.profile),
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
                  <p>Simple mode. No API key required.</p>
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
                      <button type="button" className="secondary-action" onClick={() => void buildTopicFromAdmin(item.topic)} disabled={busy}>Build brief</button>
                    </div>
                  </article>
                );
              }
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
                  </div>
                  <div className="button-row">
                    <button type="button" className="secondary-action" onClick={() => openPath(briefPath(item.exploration))} disabled={!briefPath(item.exploration)}>Open</button>
                    <button type="button" className="secondary-action" onClick={() => refineFromAdmin(item.exploration)} disabled={busy || item.exploration.status === "queued" || item.exploration.status === "running"}>Refine</button>
                    <button type="button" className="secondary-action" onClick={() => void rebuildFromAdmin(item.exploration)} disabled={busy}>Rebuild</button>
                    <button type="button" className="secondary-action" onClick={() => void scheduleExploration(item.exploration)} disabled={busy || item.exploration.status !== "complete"}>Schedule</button>
                    <button type="button" className="secondary-action destructive" onClick={() => void deleteExplorationFromAdmin(item.exploration)} disabled={busy}>Delete</button>
                  </div>
                  {item.exploration.status === "queued" || item.exploration.status === "running" || isModelDegraded(item.exploration) ? (
                    <LibraryBuildProgress exploration={item.exploration} />
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
                    <strong>{route.model}</strong>
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

function emptyDraft(): ConfirmationDraft {
  return {
    scope: "",
    depth: "informed-generalist",
    recency_weighting: "recent",
    exclusions: "",
    sourceScopeTouched: false,
  };
}

function draftFromProfile(profile: TopicProfile): ConfirmationDraft {
  return {
    scope: profile.scope || profile.statement || "",
    depth: profile.depth === "practitioner" ? "practitioner" : "informed-generalist",
    recency_weighting: sourceScopeFromProfile(profile),
    exclusions: (profile.exclusions ?? []).join(", "),
    sourceScopeTouched: false,
  };
}

function lookbackHoursForConfirmedDraft(profile: TopicProfile | null | undefined, draft: ConfirmationDraft): number {
  if (draft.sourceScopeTouched) return lookbackHoursFromSourceScope(draft.recency_weighting);
  return lookbackHoursForBuild(profile, draft);
}

function lookbackHoursForBuild(profile: TopicProfile | null | undefined, draft?: ConfirmationDraft): number {
  const explicit = Number(profile?.lookback_hours ?? 0);
  if (Number.isFinite(explicit) && explicit >= 1) return Math.min(8760, Math.floor(explicit));
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
  if (sourceScope === "recent") return 72;
  return 24;
}

function sourceScopeConfirmation(sourceScope: SourceScope): string {
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

function splitList(value: string): string[] {
  return value.split(/[,;\n]/).map((item) => item.trim()).filter(Boolean);
}

function enabledSourceSelection(selection: Record<SourceKey, boolean>, status: SourceStatusResponse | null): Record<SourceKey, boolean> {
  return {
    web_search: Boolean(selection.web_search && status?.sources.web_search?.enabled),
    foreign_media: Boolean(selection.foreign_media && status?.sources.foreign_media?.enabled),
    gmail: Boolean(selection.gmail && status?.sources.gmail?.enabled),
    reddit: Boolean(selection.reddit && status?.sources.reddit?.enabled),
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
  if (path) window.open(path, "_blank", "noopener,noreferrer");
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
  if (source === "reddit") return "Reddit";
  if (source === "podcasts") return "Podcast";
  if (source === "youtube") return "YouTube";
  if (source === "collections") return "Collections";
  if (source === "markets") return "Markets";
  return source.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
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
  let activity = "Model request is active.";
  let percent = 14;

  if (seconds >= 1.5) {
    stage = "Calling model";
    detail = "Sending the refinement prompt to the configured model provider.";
    percent = 30;
  }
  if (seconds >= 4) {
    stage = "Waiting for model";
    detail = "The selected provider may be loading the model or allocating memory.";
    activity = "Waiting for the model response.";
    percent = 48;
  }
  if (seconds >= 9) {
    stage = "Generating response";
    detail = "Waiting for the model to return the refined profile and next question.";
    activity = "Token-level progress is not exposed for this blocking call yet.";
    percent = 68;
  }
  if (seconds >= 20) {
    stage = "Still waiting on oMLX";
    detail = "No response yet. The model may still be loading, but this is the point to watch.";
    activity = "No server response yet; elapsed time is still updating.";
    percent = 82;
  }
  if (seconds >= 45) {
    stage = "Possibly hung";
    detail = "This is taking longer than expected. You can keep waiting, retry, or refresh.";
    percent = 90;
  }
  if (progress.phase === "answering" && seconds < 4) {
    stage = "Updating search strategy";
    detail = "Applying your answer and deciding the next refinement question.";
    percent = Math.max(percent, 34);
  }
  if (progress.phase === "confirming" && seconds < 4) {
    stage = "Preparing confirmation";
    detail = "Finalizing the profile so you can review it before building.";
    percent = Math.max(percent, 38);
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
