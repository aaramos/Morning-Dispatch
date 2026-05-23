import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useState } from "react";

type Digest = {
  id: string;
  name: string;
  interest: string;
  schedule: "hourly" | "daily" | "weekly" | "monthly";
  sources: Array<Record<string, string>>;
  status: string;
  threshold: number;
};

type Health = {
  status: string;
  database_path: string;
  data_dir: string;
  secrets_dir: string;
};

type Issue = {
  id: string;
  title: string;
  snapshot: string;
};

type GmailAdminStatus = {
  configured: boolean;
  connected: boolean;
  client_secret_path: string;
  credentials_path: string;
  redirect_uri: string;
  oauth_redirect_ready: boolean;
  redirect_warning: string | null;
  network: string;
};

type SchedulerStatus = {
  enabled: boolean;
  running: boolean;
  interval_seconds: number;
  daily_run_time: string;
  timezone: string;
  last_check_at: string | null;
  last_started_count: number;
  last_error: string | null;
};

type ModelCacheStatus = {
  record_count: number;
  latest_updated_at: string | null;
  models: Array<{ model_name: string; record_count: number; latest_updated_at: string | null }>;
};

type AvailableModel = {
  id: string;
  owned_by: string | null;
  created: number | null;
};

type ModelCatalogStatus = {
  available: boolean;
  models: AvailableModel[];
  error: string | null;
  selected_model: string | null;
  base_url: string | null;
};

type McpStatus = {
  available: boolean;
  error: string | null;
  server_count: number;
  tool_count: number;
  gmail: {
    connected: boolean;
    server_state: string;
    tools_count: number;
    fetch_tool_present: boolean;
    error: string | null;
  };
  reddit: {
    connected: boolean;
    server_state: string;
    tools_count: number;
    browse_tool_present: boolean;
    search_tool_present: boolean;
    error: string | null;
  };
};

type AdminHealthStatus = {
  status: "ready" | "needs_attention";
  safe_for_overnight: boolean;
  headline: string;
  problem_count: number;
  warning_count: number;
  checks: Array<{
    name: string;
    status: "ok" | "warning" | "problem";
    message: string;
  }>;
};

type InferenceModelSummary = {
  model: string;
  backend: string | null;
  model_tag: string | null;
  quantization: string | null;
  record_count: number;
  success_count: number;
  failure_count: number;
  avg_total_ms: number | null;
  p50_total_ms: number | null;
  p95_total_ms: number | null;
  avg_queue_wait_ms?: number | null;
  avg_prompt_tokens: number | null;
  avg_completion_tokens: number | null;
  avg_tokens_per_sec: number | null;
  schema_valid_rate: number | null;
  fallback_rate: number | null;
  articles_per_minute: number | null;
  estimated_100_seconds: number | null;
  estimated_500_seconds: number | null;
};

type InferenceMetricsStatus = {
  record_count: number;
  success_count: number;
  failure_count: number;
  latest_ts: string | null;
  status_counts: Record<string, number>;
  models: InferenceModelSummary[];
  ttft_available: boolean;
};

type PodcastMetricsStatus = {
  record_count: number;
  latest_ts: string | null;
  status_counts: Record<string, number>;
  transcript_source_counts: Record<string, number>;
  cache_hit_count: number;
  audio_bytes: number;
  transcript_words: number;
  avg_download_ms: number | null;
  avg_transcription_ms: number | null;
  avg_total_ms: number | null;
};

type AgentDecisionsStatus = {
  record_count: number;
  latest_created_at: string | null;
  latest_model_name: string | null;
  agent_counts: Record<string, number>;
  action_counts: Record<string, number>;
  decision_counts: Record<string, number>;
};

type FetchFailureBreakdown = {
  run_id: string | null;
  run_at?: string | null;
  digest_id?: string | null;
  total_count: number;
  groups: Array<{
    status: string;
    count: number;
    fixability: string;
  }>;
  examples: Array<{
    title: string;
    url: string | null;
    domain: string | null;
    status: string;
    reason: string;
    fixability: string;
    context: string | null;
  }>;
};

type BriefReview = {
  run_id: string | null;
  issue_id: string | null;
  generated_at: string | null;
  counts: {
    included: number;
    unresolved: number;
    dropped: number;
    duplicate: number;
    repaired: number;
  };
  included: ReviewItem[];
  unresolved: ReviewItem[];
  dropped: ReviewDecision[];
  duplicates: ReviewDecision[];
  repaired: ReviewDecision[];
};

type DigestStats = {
  run_id: string | null;
  generated_at: string | null;
  source_count: number;
  newsletter_count: number;
  link_count: number;
  podcast_episode_count: number;
  article_candidate_count: number;
  included_article_count: number;
  unresolved_count: number;
  dropped_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  model_call_count: number;
  processing_seconds: number | null;
  stage_seconds: Record<string, number>;
};

type PodcastSource = {
  key?: string;
  digest_id?: string;
  digest_name?: string;
  type: "podcast_rss" | "podcast_search";
  title?: string | null;
  feed_url?: string | null;
  site_url?: string | null;
  author?: string | null;
  query?: string | null;
  aggregator?: string | null;
  itunes_id?: string | null;
  apple_podcasts_url?: string | null;
  transcription?: string | null;
};

type PodcastStatus = {
  aggregator_configured: boolean;
  transcription_configured: boolean;
  sources: PodcastSource[];
  audio_cache_dir: string;
  transcript_cache_dir: string;
};

type PodcastDiscoveryResponse = {
  configured: boolean;
  results: PodcastSource[];
  message: string | null;
};

type EmailDeliveryStatus = {
  digest_id: string | null;
  recipient_email: string;
  enabled: boolean;
  last_delivery_status: string | null;
  last_delivered_at: string | null;
  last_error: string | null;
  updated_at: string | null;
  gmail_send_ready: boolean;
  requires_gmail_reconnect: boolean;
  token_scopes: string[];
};

type ReviewItem = {
  title: string;
  url: string | null;
  domain: string | null;
  tier: string | null;
  section: string | null;
  status: string | null;
  reason: string | null;
  score: number | null;
  summary: string | null;
};

type ReviewDecision = {
  agent: string;
  target: string;
  decision: string;
  action: string;
  reason: string | null;
  confidence: number | null;
  created_at: string;
};

type SourceScoutRun = {
  id: string;
  digest_id: string;
  run_at: string;
  status: "completed" | "partial" | "failed";
  sampled_count: number;
  active_count: number;
  candidate_count: number;
  retired_count: number;
  summary: string | null;
  error_detail: string | null;
};

type SourceScoutStatus = {
  source_count: number;
  active_count: number;
  search_only_count: number;
  candidate_count: number;
  retired_count: number;
  latest_run: SourceScoutRun | null;
};

type RedditSource = {
  id: string;
  digest_id: string;
  subreddit: string;
  state: "active" | "search_only" | "candidate" | "retired";
  category: string | null;
  score: number;
  reason: string | null;
  last_reviewed_at: string | null;
  last_seen_post_at: string | null;
  consecutive_stale_runs: number;
  metadata: Record<string, unknown>;
};

type SourceScoutDecision = {
  id: string;
  scout_run_id: string;
  digest_id: string;
  agent: string;
  subreddit: string;
  decision: string;
  action: string;
  confidence: number | null;
  reason: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
};

type SourceScoutResponse = SourceScoutRun & {
  sources: RedditSource[];
  decisions: SourceScoutDecision[];
};

type VerificationRunResult = {
  status: string;
  mode?: string;
  published?: boolean;
  source_run_id?: string;
  published_run_id?: string | null;
  published_issue_id?: string | null;
  reviewed_article_count?: number;
  active_before_count?: number;
  active_after_count?: number;
  dropped_count?: number;
  lead_title?: string | null;
  decision_count?: number;
  stored_decision_count?: number;
  reused_verified_decisions?: boolean;
  action_counts?: Record<string, number>;
  agent_counts?: Record<string, number>;
  message?: string;
};

type AgentDecisionRecord = {
  id: string;
  agent: string;
  decision: string;
  action: string;
  reason: string | null;
  confidence: number | null;
  target: string;
  model_name: string | null;
  created_at: string;
};

type ModelJob = {
  id: string;
  model_name: string;
  status: "queued" | "running" | "completed" | "failed";
  limit_count: number;
  include_cached: number;
  processed_count: number;
  success_count: number;
  cache_hit_count: number;
  failure_count: number;
  avg_total_ms: number | null;
  estimated_100_seconds: number | null;
  error_detail: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

type DigestOverview = {
  id: string;
  name: string;
  schedule: string;
  status: string;
  source_count: number;
  latest_run_id: string | null;
  latest_inference_run_id: string | null;
  latest_run_at: string | null;
  latest_completed_at: string | null;
  latest_item_count: number | null;
  latest_failed_count: number | null;
  latest_fallback_count: number | null;
  latest_newsletter_count: number | null;
  latest_link_count: number | null;
  latest_fetched_article_count: number | null;
  latest_model_cache_hit_count: number | null;
  latest_model_cache_miss_count: number | null;
  latest_model_cache_write_count: number | null;
  latest_duration_seconds: number | null;
  latest_trigger: string | null;
  latest_issue_id: string | null;
  latest_issue_title: string | null;
  next_run_at: string | null;
  due: boolean;
};

type AdminPipelineStatus = {
  system: {
    environment: string;
    database_path: string;
    data_dir: string;
    secrets_dir: string;
    public_base_url: string | null;
  };
  delivery: {
    latest_brief_path: string;
    latest_brief_url: string;
    email: EmailDeliveryStatus;
  };
  podcasts: PodcastStatus;
  health: AdminHealthStatus;
  gmail: GmailAdminStatus;
  mcp: McpStatus;
  model: {
    enabled: boolean;
    model: string | null;
    base_url: string | null;
    api_key_configured: boolean;
    max_items: number;
    selection_source: "admin" | "environment";
    settings_path: string;
    catalog: ModelCatalogStatus;
  };
  scheduler: SchedulerStatus;
  digests: DigestOverview[];
  model_cache: ModelCacheStatus;
  inference_metrics: InferenceMetricsStatus;
  podcast_metrics: PodcastMetricsStatus;
  agent_decisions: AgentDecisionsStatus;
  source_scout: SourceScoutStatus;
  fetch_failures: FetchFailureBreakdown;
  brief_review: BriefReview;
  digest_stats: DigestStats;
  model_jobs: ModelJob[];
};

const defaultInterest = "AI model releases, local AI infrastructure, investing signals, and practical product strategy.";

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

export default function App() {
  if (window.location.pathname === "/admin") {
    return <AdminApp />;
  }

  return <MorningDispatchApp />;
}

function MorningDispatchApp() {

  const [health, setHealth] = useState<Health | null>(null);
  const [digests, setDigests] = useState<Digest[]>([]);
  const [selectedDigestId, setSelectedDigestId] = useState<string | null>(null);
  const [issue, setIssue] = useState<Issue | null>(null);
  const [issueHtml, setIssueHtml] = useState("");
  const [name, setName] = useState("AI Morning Brief");
  const [interest, setInterest] = useState(defaultInterest);
  const [sourceSender, setSourceSender] = useState("");
  const [status, setStatus] = useState("Loading local app...");

  const selectedDigest = useMemo(
    () => digests.find((digest) => digest.id === selectedDigestId) ?? digests[0],
    [digests, selectedDigestId],
  );
  const selectedDigestIssueId = selectedDigest?.id ?? null;

  const refresh = useCallback(async () => {
    const [healthResult, digestResult] = await Promise.all([
      api<Health>("/api/health"),
      api<Digest[]>("/api/digests"),
    ]);
    setHealth(healthResult);
    setDigests(digestResult);
    setSelectedDigestId((current) => current ?? digestResult[0]?.id ?? null);
    setStatus("Ready");
  }, []);

  const loadLatestIssue = useCallback(async (digestId: string) => {
    try {
      const latest = await api<Issue>(`/api/digests/${digestId}/issues/latest`);
      const html = await fetch(`/api/issues/${latest.id}/html`).then((response) => response.text());
      setIssue(latest);
      setIssueHtml(html);
    } catch {
      setIssue(null);
      setIssueHtml("");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selectedDigestIssueId) return;
    void loadLatestIssue(selectedDigestIssueId);
  }, [loadLatestIssue, selectedDigestIssueId]);

  async function createDigest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStatus("Creating digest...");
    const sources = sourceSender.trim()
      ? [{ type: "gmail_newsletter", sender: sourceSender.trim() }]
      : [];
    const digest = await api<Digest>("/api/digests", {
      method: "POST",
      body: JSON.stringify({ name, interest, schedule: "daily", sources }),
    });
    setDigests((current) => [digest, ...current]);
    setSelectedDigestId(digest.id);
    setStatus("Digest created");
  }

  async function runSelectedDigest() {
    if (!selectedDigest) return;
    setStatus("Creating preview issue...");
    await api(`/api/digests/${selectedDigest.id}/run`, { method: "POST" });
    await loadLatestIssue(selectedDigest.id);
    setStatus("Preview issue ready");
  }

  function openIssuePreview() {
    if (!issue) return;
    window.open(`/api/issues/${issue.id}/html`, "_blank", "noopener,noreferrer");
  }

  function openIssuePreviewFromKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openIssuePreview();
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div>
          <p className="eyebrow">Morning Dispatch</p>
          <h1>Local newspaper digests from curated newsletters.</h1>
        </div>

        <nav className="sidebar-nav">
          <a href="/">Digests</a>
          <a href="/admin">Admin</a>
        </nav>

        <section className="status-panel">
          <span className="status-dot" />
          <div>
            <strong>{status}</strong>
            <p>{health ? `Data: ${health.data_dir}` : "Starting backend connection"}</p>
          </div>
        </section>

        <form className="create-form" onSubmit={createDigest}>
          <label>
            Digest name
            <input value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <label>
            Interest profile
            <textarea value={interest} onChange={(event) => setInterest(event.target.value)} rows={5} />
          </label>
          <label>
            First newsletter sender
            <input
              value={sourceSender}
              onChange={(event) => setSourceSender(event.target.value)}
              placeholder="newsletter@example.com"
            />
          </label>
          <button type="submit">Create Digest</button>
        </form>
      </aside>

      <section className="workspace">
        <header className="toolbar">
          <div>
            <p className="eyebrow">Control Panel</p>
            <h2>{selectedDigest?.name ?? "No digest yet"}</h2>
          </div>
          <button onClick={runSelectedDigest} disabled={!selectedDigest}>
            Run Preview
          </button>
        </header>

        <div className="content-grid">
          <section className="panel">
            <h3>Digests</h3>
            {digests.length === 0 ? (
              <p className="muted">Create your first digest to begin.</p>
            ) : (
              <div className="digest-list">
                {digests.map((digest) => (
                  <button
                    className={digest.id === selectedDigest?.id ? "digest-row selected" : "digest-row"}
                    key={digest.id}
                    onClick={() => setSelectedDigestId(digest.id)}
                  >
                    <span>{digest.name}</span>
                    <small>{digest.schedule} · {digest.sources.length} source(s)</small>
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className="panel issue-panel">
            <div className="panel-heading">
              <div>
                <h3>Issue Preview</h3>
                <p className="muted">{issue?.snapshot ?? "Run a digest to generate a local HTML issue."}</p>
              </div>
              {issue ? (
                <button className="secondary-button" onClick={openIssuePreview}>
                  Open Issue
                </button>
              ) : null}
            </div>
            {issueHtml ? (
              <div
                className="issue-preview-launcher"
                role="button"
                tabIndex={0}
                aria-label="Open full issue preview in a new page"
                onClick={openIssuePreview}
                onKeyDown={openIssuePreviewFromKeyboard}
              >
                <iframe title={issue?.title ?? "Digest issue"} srcDoc={issueHtml} />
              </div>
            ) : (
              <div className="empty-preview">No preview issue yet.</div>
            )}
          </section>
        </div>
      </section>
    </main>
  );
}

function AdminApp() {
  const [status, setStatus] = useState<GmailAdminStatus | null>(null);
  const [pipeline, setPipeline] = useState<AdminPipelineStatus | null>(null);
  const [clientSecretJson, setClientSecretJson] = useState("");
  const [callbackUrl, setCallbackUrl] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [jobModel, setJobModel] = useState("");
  const [jobLimit, setJobLimit] = useState(100);
  const [jobIncludeCached, setJobIncludeCached] = useState(false);
  const [deliveryEmail, setDeliveryEmail] = useState("");
  const [deliveryEnabled, setDeliveryEnabled] = useState(false);
  const [podcastQuery, setPodcastQuery] = useState("AI daily brief");
  const [podcastFeedUrl, setPodcastFeedUrl] = useState("");
  const [podcastTitle, setPodcastTitle] = useState("");
  const [podcastApiKey, setPodcastApiKey] = useState("");
  const [podcastApiSecret, setPodcastApiSecret] = useState("");
  const [podcastResults, setPodcastResults] = useState<PodcastSource[]>([]);
  const [verificationResult, setVerificationResult] = useState<VerificationRunResult | null>(null);
  const [agentDecisions, setAgentDecisions] = useState<AgentDecisionRecord[]>([]);
  const [sourceScoutSources, setSourceScoutSources] = useState<RedditSource[]>([]);
  const [sourceScoutDecisions, setSourceScoutDecisions] = useState<SourceScoutDecision[]>([]);
  const [message, setMessage] = useState("Loading admin status...");
  const [busy, setBusy] = useState(false);
  const modelOptions = pipeline?.model.catalog.models ?? [];
  const modelCatalogReady = Boolean(pipeline?.model.catalog.available && modelOptions.length > 0);
  const modelSelectionChanged = Boolean(selectedModel && selectedModel !== pipeline?.model.model);

  const loadAgentDecisions = useCallback(async () => {
    try {
      const result = await api<{ decisions: AgentDecisionRecord[] }>("/api/admin/agent-decisions");
      setAgentDecisions(result.decisions);
    } catch {
      setAgentDecisions([]);
    }
  }, []);

  const loadSourceScout = useCallback(async () => {
    try {
      const result = await api<{ sources: RedditSource[]; decisions: SourceScoutDecision[] }>("/api/admin/source-scout");
      setSourceScoutSources(result.sources);
      setSourceScoutDecisions(result.decisions);
    } catch {
      setSourceScoutSources([]);
      setSourceScoutDecisions([]);
    }
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      const result = await api<AdminPipelineStatus>("/api/admin/status");
      setPipeline(result);
      setStatus(result.gmail);
      const preferredModel = result.model.model || result.model.catalog.models[0]?.id || "";
      setSelectedModel(preferredModel);
      setJobModel((current) => current || preferredModel);
      setDeliveryEmail(result.delivery.email.recipient_email ?? "");
      setDeliveryEnabled(Boolean(result.delivery.email.enabled));
      setMessage(result.gmail.connected ? "Gmail connected" : "Gmail not connected");
      await Promise.all([loadAgentDecisions(), loadSourceScout()]);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Admin status unavailable");
    }
  }, [loadAgentDecisions, loadSourceScout]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function saveClientSecret() {
    setBusy(true);
    setMessage("Saving Google OAuth client...");
    try {
      await api("/api/admin/gmail/client-secret", {
        method: "POST",
        body: JSON.stringify({ client_secret_json: clientSecretJson }),
      });
      setClientSecretJson("");
      await loadStatus();
      setMessage("Google OAuth client saved");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save client secret");
    } finally {
      setBusy(false);
    }
  }

  async function connectGmail() {
    setBusy(true);
    setMessage("Starting Google login...");
    try {
      const result = await api<{ authorization_url: string }>("/api/admin/gmail/oauth/start", {
        method: "POST",
      });
      window.location.href = result.authorization_url;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not start Google login");
      setBusy(false);
    }
  }

  async function disconnectGmail() {
    setBusy(true);
    setMessage("Disconnecting Gmail...");
    try {
      await api("/api/admin/gmail/disconnect", { method: "POST" });
      await loadStatus();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not disconnect Gmail");
    } finally {
      setBusy(false);
    }
  }

  async function completeGmailFromRedirect() {
    setBusy(true);
    setMessage("Completing Gmail connection...");
    try {
      await api("/api/admin/gmail/oauth/complete", {
        method: "POST",
        body: JSON.stringify({ callback_url: callbackUrl.trim() }),
      });
      setCallbackUrl("");
      await loadStatus();
      setMessage("Gmail connected");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not complete Google login");
    } finally {
      setBusy(false);
    }
  }

  async function startModelJob() {
    setBusy(true);
    setMessage("Starting model batch...");
    try {
      await api<ModelJob>("/api/admin/model/jobs", {
        method: "POST",
        body: JSON.stringify({
          model_name: jobModel.trim(),
          limit_count: jobLimit,
          include_cached: jobIncludeCached,
        }),
      });
      await loadStatus();
      setMessage("Model batch started");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not start model batch");
    } finally {
      setBusy(false);
    }
  }

  async function saveSelectedModel() {
    if (!selectedModel.trim()) return;
    setBusy(true);
    setMessage("Saving model selection...");
    try {
      const result = await api<{ model: string }>("/api/admin/model/selection", {
        method: "POST",
        body: JSON.stringify({ model_name: selectedModel.trim() }),
      });
      setJobModel(result.model);
      await loadStatus();
      setMessage("Model selection saved");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save model selection");
    } finally {
      setBusy(false);
    }
  }

  async function runControlledVerification(publish = false) {
    const digest = pipeline?.digests[0];
    if (!digest) return;
    setBusy(true);
    setMessage(publish ? "Publishing verified brief..." : "Running controlled verification...");
    try {
      const result = await api<VerificationRunResult>(`/api/admin/digests/${digest.id}/verification-run${publish ? "?publish=true" : ""}`, {
        method: "POST",
      });
      setVerificationResult(result);
      await loadStatus();
      setMessage(
        result.status === "completed"
          ? result.published
            ? "Verified brief published"
            : "Controlled verification completed"
          : result.message ?? result.status,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Controlled verification failed");
    } finally {
      setBusy(false);
    }
  }

  async function runSourceScout(liveSample = true) {
    const digest = pipeline?.digests[0];
    if (!digest) return;
    setBusy(true);
    setMessage(liveSample ? "Running Reddit Source Scout..." : "Seeding Reddit Source Scout...");
    try {
      const result = await api<SourceScoutResponse>(
        `/api/admin/digests/${digest.id}/source-scout?live_sample=${liveSample ? "true" : "false"}`,
        { method: "POST" },
      );
      setSourceScoutSources(result.sources);
      setSourceScoutDecisions(result.decisions);
      await loadStatus();
      setMessage(result.summary ?? "Reddit Source Scout completed");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Source Scout failed");
    } finally {
      setBusy(false);
    }
  }

  async function saveDeliverySettings() {
    const digest = pipeline?.digests[0];
    if (!digest) return;
    setBusy(true);
    setMessage("Saving delivery settings...");
    try {
      await api(`/api/admin/digests/${digest.id}/delivery`, {
        method: "PATCH",
        body: JSON.stringify({
          recipient_email: deliveryEmail.trim(),
          enabled: deliveryEnabled,
        }),
      });
      await loadStatus();
      setMessage("Delivery settings saved");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save delivery settings");
    } finally {
      setBusy(false);
    }
  }

  async function discoverPodcasts() {
    if (!podcastQuery.trim()) return;
    setBusy(true);
    setMessage("Searching podcast directory...");
    try {
      const result = await api<PodcastDiscoveryResponse>(
        `/api/admin/podcasts/discover?query=${encodeURIComponent(podcastQuery.trim())}&limit=8`,
      );
      setPodcastResults(result.results);
      setMessage(result.message ?? `Found ${result.results.length} podcast candidate(s)`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Podcast search failed");
    } finally {
      setBusy(false);
    }
  }

  async function savePodcastCredentials() {
    setBusy(true);
    setMessage("Saving Podcast Index credentials...");
    try {
      await api<PodcastStatus>("/api/admin/podcasts/credentials", {
        method: "POST",
        body: JSON.stringify({
          api_key: podcastApiKey.trim(),
          api_secret: podcastApiSecret.trim(),
        }),
      });
      setPodcastApiKey("");
      setPodcastApiSecret("");
      await loadStatus();
      setMessage("Podcast Index credentials saved");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save podcast credentials");
    } finally {
      setBusy(false);
    }
  }

  async function addPodcastSource(source: Partial<PodcastSource>) {
    const digest = pipeline?.digests[0];
    if (!digest) return;
    setBusy(true);
    setMessage("Adding podcast source...");
    try {
      await api(`/api/admin/digests/${digest.id}/podcast-sources`, {
        method: "POST",
        body: JSON.stringify(source),
      });
      setPodcastFeedUrl("");
      setPodcastTitle("");
      await loadStatus();
      setMessage("Podcast source added");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not add podcast source");
    } finally {
      setBusy(false);
    }
  }

  async function addManualPodcastFeed() {
    if (!podcastFeedUrl.trim()) return;
    await addPodcastSource({
      type: "podcast_rss",
      title: podcastTitle.trim() || undefined,
      feed_url: podcastFeedUrl.trim(),
      transcription: "auto",
    });
  }

  async function addPodcastSearch() {
    if (!podcastQuery.trim()) return;
    await addPodcastSource({
      type: "podcast_search",
      title: `Search: ${podcastQuery.trim()}`,
      query: podcastQuery.trim(),
      aggregator: "podcastindex",
      transcription: "auto",
    });
  }

  async function removePodcastSource(source: PodcastSource) {
    const digest = pipeline?.digests[0];
    if (!digest || !source.key) return;
    setBusy(true);
    setMessage("Removing podcast source...");
    try {
      await api(`/api/admin/digests/${digest.id}/podcast-sources/${encodeURIComponent(source.key)}`, {
        method: "DELETE",
      });
      await loadStatus();
      setMessage("Podcast source removed");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not remove podcast source");
    } finally {
      setBusy(false);
    }
  }

  async function sendLatestDigestEmail() {
    const digest = pipeline?.digests[0];
    if (!digest) return;
    setBusy(true);
    setMessage("Sending latest digest...");
    try {
      await api(`/api/admin/digests/${digest.id}/delivery/send-test`, {
        method: "POST",
      });
      await loadStatus();
      setMessage("Latest digest sent");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not send latest digest");
    } finally {
      setBusy(false);
    }
  }

  async function readClientSecretFile(file: File | undefined) {
    if (!file) return;
    setClientSecretJson(await file.text());
  }

  return (
    <main className="admin-shell">
      <aside className="sidebar admin-sidebar">
        <div>
          <p className="eyebrow">Admin</p>
          <h1>Operations</h1>
        </div>
        <nav className="sidebar-nav">
          <a href="/">Digests</a>
          <a href="/admin">Admin</a>
        </nav>
        <section className="status-panel">
          <span className={status?.connected ? "status-dot" : "status-dot warning"} />
          <div>
            <strong>{message}</strong>
            <p>{status ? `Admin API: ${status.network}` : "Checking access"}</p>
          </div>
        </section>
      </aside>

      <section className="admin-workspace">
        <header className="toolbar">
          <div>
            <p className="eyebrow">Google OAuth</p>
            <h2>Connect Gmail</h2>
          </div>
          <button onClick={loadStatus} disabled={busy}>
            Refresh
          </button>
        </header>

        <div className="admin-grid">
          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Pipeline Status</h3>
                <p className="muted">{pipeline?.system.public_base_url ?? "Local runtime"}</p>
              </div>
              <span className={pipeline?.scheduler.running ? "status-pill good" : "status-pill"}>
                {pipeline?.scheduler.enabled ? "Scheduled" : "Manual"}
              </span>
            </div>
            {pipeline?.health ? (
              <div className={`admin-health ${pipeline.health.safe_for_overnight ? "ready" : "needs-attention"}`}>
                <div>
                  <span>{pipeline.health.safe_for_overnight ? "Safe" : "Attention"}</span>
                  <strong>{pipeline.health.headline}</strong>
                  <small>
                    {pipeline.health.problem_count} problem(s) · {pipeline.health.warning_count} warning(s)
                  </small>
                </div>
                <div className="health-checks">
                  {pipeline.health.checks.map((check) => (
                    <article className={`health-check ${check.status}`} key={check.name}>
                      <strong>{check.name}</strong>
                      <span>{check.message}</span>
                    </article>
                  ))}
                </div>
              </div>
            ) : null}
            <div className="metric-strip">
              <div>
                <span>Scheduler</span>
                <strong>{pipeline?.scheduler.running ? "Running" : pipeline?.scheduler.enabled ? "Starting" : "Off"}</strong>
                <small>{formatSchedulerTime(pipeline?.scheduler.daily_run_time, pipeline?.scheduler.timezone)}</small>
              </div>
              <div>
                <span>Model</span>
                <strong>{pipeline?.model.enabled ? pipeline.model.model : "Fallback"}</strong>
                <small>{pipeline?.model.enabled ? `${pipeline.model.max_items}/run · cache reused` : "Deterministic summaries"}</small>
              </div>
              <div>
                <span>Gmail MCP</span>
                <strong>{pipeline?.mcp.gmail.connected ? "Connected" : "Offline"}</strong>
                <small>{formatMcpStatus(pipeline?.mcp)}</small>
              </div>
              <div>
                <span>Delivery</span>
                <strong>
                  <a href={pipeline?.delivery.latest_brief_url ?? "/brief"} target="_blank" rel="noreferrer">
                    Latest Brief
                  </a>
                </strong>
                <small>{formatDeliveryUrl(pipeline?.delivery.latest_brief_url)}</small>
              </div>
              <div>
                <span>Agentic Flow</span>
                <strong>{pipeline?.agent_decisions.record_count ?? 0}</strong>
                <small>{formatAgentDecisionSummary(pipeline?.agent_decisions)}</small>
              </div>
              <div>
                <span>Reddit Scout</span>
                <strong>{pipeline?.source_scout.active_count ?? 0} active</strong>
                <small>{formatSourceScoutSummary(pipeline?.source_scout)}</small>
              </div>
              <div>
                <span>Podcasts</span>
                <strong>{pipeline?.podcasts.sources.length ?? 0}</strong>
                <small>{pipeline?.podcasts.transcription_configured ? "Transcript cache on" : "Show notes fallback"}</small>
              </div>
              <div>
                <span>Cache</span>
                <strong>{pipeline?.model_cache.record_count ?? 0}</strong>
              </div>
              <div>
                <span>Last Check</span>
                <strong>{formatDateTime(pipeline?.scheduler.last_check_at)}</strong>
              </div>
            </div>
            {pipeline?.scheduler.last_error ? <p className="error-text">{pipeline.scheduler.last_error}</p> : null}
            <div className="panel-actions">
              <button onClick={() => runControlledVerification(false)} disabled={busy || !pipeline?.digests.length}>
                Verify Only
              </button>
              <button onClick={() => runControlledVerification(true)} disabled={busy || !pipeline?.digests.length}>
                Publish Verified Brief
              </button>
              {verificationResult ? (
                <p className="muted">
                  {formatVerificationResult(verificationResult)}
                </p>
              ) : (
                <p className="muted">Exercises the agentic editor and critic without publishing over the live brief.</p>
              )}
            </div>
            {agentDecisions.length ? (
              <div className="decision-list">
                {agentDecisions.slice(0, 6).map((decision) => (
                  <article key={decision.id}>
                    <strong>
                      {decision.agent} · {decision.action || decision.decision}
                    </strong>
                    <span>{decision.reason || decision.decision}</span>
                  </article>
                ))}
              </div>
            ) : null}
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Digest Stats</h3>
                <p className="muted">Clean run summary shown in the digest; unresolved details stay in Admin.</p>
              </div>
              <span className="status-pill">{formatDateTime(pipeline?.digest_stats.generated_at)}</span>
            </div>
            <div className="metric-strip">
              <div>
                <span>Sources</span>
                <strong>{formatNumber(pipeline?.digest_stats.source_count)}</strong>
                <small>Configured inputs</small>
              </div>
              <div>
                <span>Newsletters</span>
                <strong>{formatNumber(pipeline?.digest_stats.newsletter_count)}</strong>
                <small>{formatNumber(pipeline?.digest_stats.link_count)} links extracted</small>
              </div>
              <div>
                <span>Podcasts</span>
                <strong>{formatNumber(pipeline?.digest_stats.podcast_episode_count)}</strong>
                <small>Episodes added</small>
              </div>
              <div>
                <span>Included</span>
                <strong>{formatNumber(pipeline?.digest_stats.included_article_count)}</strong>
                <small>{formatNumber(pipeline?.digest_stats.article_candidate_count)} candidates reviewed</small>
              </div>
              <div>
                <span>Filtered</span>
                <strong>
                  {formatNumber((pipeline?.digest_stats.dropped_count ?? 0) + (pipeline?.digest_stats.unresolved_count ?? 0))}
                </strong>
                <small>{formatNumber(pipeline?.digest_stats.unresolved_count)} unresolved, admin-only</small>
              </div>
              <div>
                <span>Model tokens</span>
                <strong>{formatNumber(pipeline?.digest_stats.total_tokens)}</strong>
                <small>
                  {formatNumber(pipeline?.digest_stats.prompt_tokens)} prompt · {formatNumber(pipeline?.digest_stats.completion_tokens)} output
                </small>
              </div>
              <div>
                <span>Processing</span>
                <strong>{formatSeconds(pipeline?.digest_stats.processing_seconds)}</strong>
                <small>{formatNumber(pipeline?.digest_stats.model_call_count)} model call(s)</small>
              </div>
            </div>
            <div className="stage-strip">
              {Object.entries(pipeline?.digest_stats.stage_seconds ?? {}).map(([stage, seconds]) => (
                <span key={stage}>
                  {formatStageLabel(stage)}: <strong>{formatSeconds(seconds)}</strong>
                </span>
              ))}
            </div>
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Podcast Sources</h3>
                <p className="muted">Episodes are summarized into the digest with a link back to the original episode.</p>
              </div>
              <span className={pipeline?.podcasts.aggregator_configured ? "status-pill good" : "status-pill"}>
                {pipeline?.podcasts.aggregator_configured ? "Directory ready" : "RSS only"}
              </span>
            </div>
            <div className="podcast-credentials">
              <label>
                Podcast Index API key
                <input
                  type="password"
                  value={podcastApiKey}
                  onChange={(event) => setPodcastApiKey(event.target.value)}
                  placeholder={pipeline?.podcasts.aggregator_configured ? "Saved" : "Paste API key"}
                  autoComplete="off"
                />
              </label>
              <label>
                Podcast Index API secret
                <input
                  type="password"
                  value={podcastApiSecret}
                  onChange={(event) => setPodcastApiSecret(event.target.value)}
                  placeholder={pipeline?.podcasts.aggregator_configured ? "Saved" : "Paste API secret"}
                  autoComplete="off"
                />
              </label>
              <button
                onClick={savePodcastCredentials}
                disabled={busy || !podcastApiKey.trim() || !podcastApiSecret.trim()}
              >
                Save Directory Access
              </button>
            </div>
            <div className="podcast-tools">
              <label>
                Search topic
                <input
                  value={podcastQuery}
                  onChange={(event) => setPodcastQuery(event.target.value)}
                  placeholder="AI daily brief"
                />
              </label>
              <div className="button-row podcast-buttons">
                <button onClick={discoverPodcasts} disabled={busy || !podcastQuery.trim() || !pipeline?.podcasts.aggregator_configured}>
                  Search Directory
                </button>
                <button
                  className="secondary-button"
                  onClick={addPodcastSearch}
                  disabled={busy || !podcastQuery.trim() || !pipeline?.podcasts.aggregator_configured}
                >
                  Watch Search
                </button>
              </div>
              <label>
                RSS feed
                <input
                  value={podcastFeedUrl}
                  onChange={(event) => setPodcastFeedUrl(event.target.value)}
                  placeholder="https://example.com/feed.xml"
                />
              </label>
              <label>
                Feed name
                <input
                  value={podcastTitle}
                  onChange={(event) => setPodcastTitle(event.target.value)}
                  placeholder="Optional"
                />
              </label>
              <button onClick={addManualPodcastFeed} disabled={busy || !podcastFeedUrl.trim()}>
                Add RSS
              </button>
            </div>
            <div className="metric-strip">
              <div>
                <span>Tracked</span>
                <strong>{pipeline?.podcasts.sources.length ?? 0}</strong>
                <small>{pipeline?.podcasts.aggregator_configured ? "Podcast Index enabled" : "Manual feeds enabled"}</small>
              </div>
              <div>
                <span>Transcription</span>
                <strong>{pipeline?.podcasts.transcription_configured ? "Configured" : "Show notes"}</strong>
                <small>Cached once available</small>
              </div>
              <div>
                <span>Transcript cache</span>
                <strong>{formatPathTail(pipeline?.podcasts.transcript_cache_dir)}</strong>
                <small>{formatPathTail(pipeline?.podcasts.audio_cache_dir)}</small>
              </div>
            </div>
            <div className="metric-strip">
              <div>
                <span>Telemetry rows</span>
                <strong>{formatNumber(pipeline?.podcast_metrics.record_count)}</strong>
                <small>{formatNumber(pipeline?.podcast_metrics.cache_hit_count)} cache hit(s)</small>
              </div>
              <div>
                <span>Avg transcript</span>
                <strong>{formatMs(pipeline?.podcast_metrics.avg_transcription_ms)}</strong>
                <small>{formatNumber(pipeline?.podcast_metrics.transcript_words)} transcript words</small>
              </div>
              <div>
                <span>Avg download</span>
                <strong>{formatMs(pipeline?.podcast_metrics.avg_download_ms)}</strong>
                <small>{formatBytes(pipeline?.podcast_metrics.audio_bytes)} cached audio</small>
              </div>
              <div>
                <span>Latest</span>
                <strong>{formatDateTime(pipeline?.podcast_metrics.latest_ts)}</strong>
                <small>{formatPodcastMetricCounts(pipeline?.podcast_metrics)}</small>
              </div>
            </div>
            {podcastResults.length ? (
              <>
                <p className="section-label">Directory results</p>
                <div className="source-list">
                  {podcastResults.map((source) => (
                    <article key={source.feed_url ?? source.key}>
                      <div>
                        <strong>{source.title ?? "Podcast"}</strong>
                        <small>{source.author ?? source.site_url ?? source.feed_url}</small>
                      </div>
                      <button className="source-action" onClick={() => addPodcastSource(source)} disabled={busy}>
                        Add
                      </button>
                      <p>{source.feed_url}</p>
                    </article>
                  ))}
                </div>
              </>
            ) : null}
            <p className="section-label">Tracked sources</p>
            <div className="source-list">
              {(pipeline?.podcasts.sources ?? []).map((source) => (
                <article key={source.key ?? source.feed_url ?? source.query}>
                  <div>
                    <strong>{source.title ?? source.query ?? source.feed_url ?? "Podcast source"}</strong>
                    <small>
                      {source.type === "podcast_search" ? `Search: ${source.query}` : source.feed_url}
                    </small>
                  </div>
                  <button className="source-action" onClick={() => removePodcastSource(source)} disabled={busy}>
                    Remove
                  </button>
                  <p>{source.aggregator ?? "rss"} · {source.transcription ?? "auto"}</p>
                </article>
              ))}
              {(pipeline?.podcasts.sources.length ?? 0) === 0 ? (
                <article>
                  <div>
                    <strong>No podcast sources yet</strong>
                    <small>Add a directory search or RSS feed.</small>
                  </div>
                  <span className="source-state candidate">New</span>
                </article>
              ) : null}
            </div>
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Digest Delivery</h3>
                <p className="muted">Sends the finished morning brief to your inbox after the scheduled run completes.</p>
              </div>
              <span className={pipeline?.delivery.email.enabled ? "status-pill good" : "status-pill"}>
                {pipeline?.delivery.email.enabled ? "Enabled" : "Off"}
              </span>
            </div>
            <div className="delivery-form">
              <label>
                Recipient email
                <input
                  value={deliveryEmail}
                  onChange={(event) => setDeliveryEmail(event.target.value)}
                  placeholder="you@example.com"
                />
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={deliveryEnabled}
                  onChange={(event) => setDeliveryEnabled(event.target.checked)}
                />
                Send each morning
              </label>
              <div className="button-row">
                <button onClick={saveDeliverySettings} disabled={busy || !pipeline?.digests.length}>
                  Save Delivery
                </button>
                <button
                  className="secondary-button"
                  onClick={sendLatestDigestEmail}
                  disabled={busy || !pipeline?.delivery.email.enabled || !pipeline.delivery.email.recipient_email}
                >
                  Send Latest Now
                </button>
              </div>
            </div>
            <dl className="status-list inline">
              <div>
                <dt>Gmail send</dt>
                <dd>{pipeline?.delivery.email.gmail_send_ready ? "Ready" : "Reconnect Gmail"}</dd>
              </div>
              <div>
                <dt>Last sent</dt>
                <dd>{formatDateTime(pipeline?.delivery.email.last_delivered_at)}</dd>
              </div>
              <div>
                <dt>Last status</dt>
                <dd>{pipeline?.delivery.email.last_delivery_status ?? "Never"}</dd>
              </div>
            </dl>
            {pipeline?.delivery.email.requires_gmail_reconnect ? (
              <p className="error-text">Reconnect Gmail once so Morning Dispatch can send the brief, not just read newsletters.</p>
            ) : null}
            {pipeline?.delivery.email.last_error ? <p className="error-text">{pipeline.delivery.email.last_error}</p> : null}
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Unresolved Links</h3>
                <p className="muted">Admin-only detail. These no longer appear in the digest itself.</p>
              </div>
              <span className={pipeline?.fetch_failures.total_count ? "status-pill" : "status-pill good"}>
                {pipeline?.fetch_failures.total_count ?? 0} unresolved
              </span>
            </div>
            <div className="metric-strip">
              {(pipeline?.fetch_failures.groups ?? []).slice(0, 5).map((group) => (
                <div key={group.status}>
                  <span>{formatStatusLabel(group.status)}</span>
                  <strong>{group.count}</strong>
                  <small>{group.fixability}</small>
                </div>
              ))}
              {(pipeline?.fetch_failures.groups.length ?? 0) === 0 ? (
                <div>
                  <span>Failures</span>
                  <strong>0</strong>
                  <small>No failed article fetches in the latest run.</small>
                </div>
              ) : null}
            </div>
            <div className="review-list">
              {(pipeline?.fetch_failures.examples ?? []).map((item) => (
                <article key={`${item.url ?? item.title}-${item.status}`}>
                  <div>
                    <strong>{item.title}</strong>
                    <small>{item.domain ?? "unknown source"} · {formatStatusLabel(item.status)}</small>
                  </div>
                  <p>{item.reason}</p>
                  {item.context ? <span>{item.context}</span> : null}
                </article>
              ))}
            </div>
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Brief Review</h3>
                <p className="muted">A compact audit of what the agents included, dropped, repaired, or deduplicated.</p>
              </div>
              <span className="status-pill">{formatDateTime(pipeline?.brief_review.generated_at)}</span>
            </div>
            <div className="metric-strip">
              <div>
                <span>Included</span>
                <strong>{pipeline?.brief_review.counts.included ?? 0}</strong>
              </div>
              <div>
                <span>Unresolved</span>
                <strong>{pipeline?.brief_review.counts.unresolved ?? 0}</strong>
              </div>
              <div>
                <span>Dropped</span>
                <strong>{pipeline?.brief_review.counts.dropped ?? 0}</strong>
              </div>
              <div>
                <span>Duplicate</span>
                <strong>{pipeline?.brief_review.counts.duplicate ?? 0}</strong>
              </div>
              <div>
                <span>Repaired</span>
                <strong>{pipeline?.brief_review.counts.repaired ?? 0}</strong>
              </div>
            </div>
            <div className="review-columns">
              <div>
                <p className="section-label">Included</p>
                <div className="review-list compact">
                  {(pipeline?.brief_review.included ?? []).slice(0, 5).map((item) => (
                    <article key={`${item.url ?? item.title}-included`}>
                      <strong>{item.title}</strong>
                      <small>{item.section ?? item.tier ?? "included"} · {formatPercent(item.score)}</small>
                    </article>
                  ))}
                </div>
              </div>
              <div>
                <p className="section-label">Dropped / Repaired</p>
                <div className="review-list compact">
                  {[...(pipeline?.brief_review.dropped ?? []), ...(pipeline?.brief_review.repaired ?? [])]
                    .slice(0, 5)
                    .map((item) => (
                      <article key={`${item.target}-${item.action}-${item.created_at}`}>
                        <strong>{item.action || item.decision}</strong>
                        <small>{item.target}</small>
                        <p>{item.reason ?? item.decision}</p>
                      </article>
                    ))}
                </div>
              </div>
            </div>
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Reddit Source Scout</h3>
                <p className="muted">
                  Keeps Reddit communities aligned with the digest interest by promoting fresh sources and retiring stale ones.
                </p>
              </div>
              <span className={pipeline?.source_scout.latest_run?.status === "completed" ? "status-pill good" : "status-pill"}>
                {pipeline?.source_scout.latest_run?.status ?? "Not run"}
              </span>
            </div>
            <div className="metric-strip">
              <div>
                <span>Tracked</span>
                <strong>{pipeline?.source_scout.source_count ?? sourceScoutSources.length}</strong>
                <small>{formatDateTime(pipeline?.source_scout.latest_run?.run_at)}</small>
              </div>
              <div>
                <span>Active</span>
                <strong>{pipeline?.source_scout.active_count ?? 0}</strong>
                <small>Browsed every run</small>
              </div>
              <div>
                <span>Search-only</span>
                <strong>{pipeline?.source_scout.search_only_count ?? 0}</strong>
                <small>Queried when keywords match</small>
              </div>
              <div>
                <span>Candidate</span>
                <strong>{pipeline?.source_scout.candidate_count ?? 0}</strong>
                <small>Proving signal</small>
              </div>
              <div>
                <span>Retired</span>
                <strong>{pipeline?.source_scout.retired_count ?? 0}</strong>
                <small>Kept for audit</small>
              </div>
            </div>
            <div className="panel-actions">
              <button onClick={() => runSourceScout(true)} disabled={busy || !pipeline?.digests.length}>
                Run Scout
              </button>
              <button onClick={() => runSourceScout(false)} disabled={busy || !pipeline?.digests.length}>
                Seed Only
              </button>
              <p className="muted">{pipeline?.source_scout.latest_run?.summary ?? "No Reddit source review has run yet."}</p>
            </div>
            {sourceScoutSources.length ? (
              <div className="source-list">
                {sourceScoutSources.slice(0, 18).map((source) => (
                  <article key={source.id}>
                    <div>
                      <strong>r/{source.subreddit}</strong>
                      <small>{source.category ?? "Uncategorized"} · score {formatPercent(source.score)}</small>
                    </div>
                    <span className={`source-state ${source.state}`}>{formatSourceState(source.state)}</span>
                    <p>{source.reason ?? "No review note yet."}</p>
                  </article>
                ))}
              </div>
            ) : (
              <p className="muted">Run the scout to seed Reddit communities.</p>
            )}
            {sourceScoutDecisions.length ? (
              <>
                <p className="section-label">Recent scout decisions</p>
                <div className="decision-list">
                  {sourceScoutDecisions.slice(0, 5).map((decision) => (
                    <article key={decision.id}>
                      <strong>
                        r/{decision.subreddit} · {decision.action || decision.decision}
                      </strong>
                      <span>{decision.reason || decision.decision}</span>
                    </article>
                  ))}
                </div>
              </>
            ) : null}
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Librarian Model</h3>
                <p className="muted">
                  {pipeline?.model.catalog.error ??
                    `Using ${pipeline?.model.catalog.base_url ?? "the configured local model server"}`}
                </p>
              </div>
              <span className={modelCatalogReady ? "status-pill good" : "status-pill"}>
                {modelCatalogReady ? `${modelOptions.length} available` : "Unavailable"}
              </span>
            </div>
            <div className="model-picker">
              <label>
                Active model
                <select
                  value={selectedModel}
                  onChange={(event) => {
                    setSelectedModel(event.target.value);
                    setJobModel(event.target.value);
                  }}
                  disabled={busy || !modelCatalogReady}
                >
                  {modelOptions.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.id}
                    </option>
                  ))}
                </select>
              </label>
              <dl className="compact-status-list">
                <div>
                  <dt>Current</dt>
                  <dd>{pipeline?.model.model ?? "Fallback only"}</dd>
                </div>
                <div>
                  <dt>Source</dt>
                  <dd>{pipeline?.model.selection_source === "admin" ? "Admin setting" : "Launch setting"}</dd>
                </div>
                <div>
                  <dt>API key</dt>
                  <dd>{pipeline?.model.api_key_configured ? "Configured" : "Missing"}</dd>
                </div>
              </dl>
              <button onClick={saveSelectedModel} disabled={busy || !modelCatalogReady || !modelSelectionChanged}>
                Save Model
              </button>
            </div>
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Inference Metrics</h3>
                <p className="muted">
                  Model averages exclude queue wait; queue wait is stored separately. TTFT needs streaming.
                </p>
              </div>
              <span className="status-pill">{pipeline?.inference_metrics.ttft_available ? "Streaming" : "Non-streaming"}</span>
            </div>
            <div className="metric-strip">
              <div>
                <span>Attempts</span>
                <strong>{pipeline?.inference_metrics.record_count ?? 0}</strong>
              </div>
              <div>
                <span>Successful</span>
                <strong>{pipeline?.inference_metrics.success_count ?? 0}</strong>
              </div>
              <div>
                <span>Fallbacks</span>
                <strong>{pipeline?.inference_metrics.failure_count ?? 0}</strong>
              </div>
              <div>
                <span>Latest</span>
                <strong>{formatDateTime(pipeline?.inference_metrics.latest_ts)}</strong>
              </div>
            </div>
            <div className="run-list metrics-list">
              {(pipeline?.inference_metrics.models ?? []).map((model) => (
                <article className="run-row model-row" key={`${model.model}-${model.backend ?? ""}-${model.model_tag ?? ""}`}>
                  <div>
                    <strong>{model.model}</strong>
                    <small>
                      {model.backend ?? "backend unknown"} · {model.quantization ?? model.model_tag ?? "tag unknown"} ·{" "}
                      {model.record_count} attempt(s)
                    </small>
                  </div>
                  <dl>
                    <div>
                      <dt>Model Avg</dt>
                      <dd>{formatMs(model.avg_total_ms)}</dd>
                    </div>
                    <div>
                      <dt>P95</dt>
                      <dd>{formatMs(model.p95_total_ms)}</dd>
                    </div>
                    <div>
                      <dt>Rate</dt>
                      <dd>{model.articles_per_minute ? `${model.articles_per_minute}/min` : "Unknown"}</dd>
                    </div>
                    <div>
                      <dt>500 Est.</dt>
                      <dd>{formatSeconds(model.estimated_500_seconds)}</dd>
                    </div>
                    <div>
                      <dt>Schema</dt>
                      <dd>{formatPercent(model.schema_valid_rate)}</dd>
                    </div>
                  </dl>
                </article>
              ))}
              {(pipeline?.inference_metrics.models.length ?? 0) === 0 ? (
                <p className="muted">No model attempts have been recorded yet.</p>
              ) : null}
            </div>
          </section>

          <section className="panel wide-panel">
            <div className="panel-heading">
              <div>
                <h3>Model Batch</h3>
                <p className="muted">Run stored articles through a selected model and cache successful enrichments.</p>
              </div>
            </div>
            <div className="job-form">
              <label>
                Model name
                {modelCatalogReady ? (
                  <select value={jobModel} onChange={(event) => setJobModel(event.target.value)}>
                    {modelOptions.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.id}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input value={jobModel} onChange={(event) => setJobModel(event.target.value)} />
                )}
              </label>
              <label>
                Article count
                <input
                  type="number"
                  min={1}
                  max={1000}
                  value={jobLimit}
                  onChange={(event) => setJobLimit(Number(event.target.value))}
                />
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={jobIncludeCached}
                  onChange={(event) => setJobIncludeCached(event.target.checked)}
                />
                Re-run cached articles
              </label>
              <button onClick={startModelJob} disabled={busy || !jobModel.trim()}>
                Start Batch
              </button>
            </div>
            <div className="run-list">
              {(pipeline?.model_jobs ?? []).map((job) => (
                <article className="run-row model-row" key={job.id}>
                  <div>
                    <strong>{job.model_name}</strong>
                    <small>
                      {job.status} · {formatDateTime(job.completed_at ?? job.started_at ?? job.created_at)}
                    </small>
                  </div>
                  <dl>
                    <div>
                      <dt>Progress</dt>
                      <dd>{job.processed_count}/{job.limit_count}</dd>
                    </div>
                    <div>
                      <dt>Success</dt>
                      <dd>{job.success_count}</dd>
                    </div>
                    <div>
                      <dt>Fallback</dt>
                      <dd>{job.failure_count}</dd>
                    </div>
                    <div>
                      <dt>Throughput</dt>
                      <dd>{formatMs(job.avg_total_ms)}</dd>
                    </div>
                    <div>
                      <dt>100 Est.</dt>
                      <dd>{formatSeconds(job.estimated_100_seconds)}</dd>
                    </div>
                  </dl>
                  {job.error_detail ? <p className="error-text">{job.error_detail}</p> : null}
                </article>
              ))}
              {(pipeline?.model_jobs.length ?? 0) === 0 ? <p className="muted">No model batches have run yet.</p> : null}
            </div>
          </section>

          <section className="panel wide-panel">
            <h3>Digest Runs</h3>
            <div className="run-list">
              {(pipeline?.digests ?? []).map((digest) => (
                <article className="run-row" key={digest.id}>
                  <div>
                    <strong>{digest.name}</strong>
                    <small>
                      {digest.schedule} · {digest.source_count} source(s) · next {formatDateTime(digest.next_run_at)}
                    </small>
                  </div>
                  <dl>
                    <div>
                      <dt>Last Run</dt>
                      <dd>{formatDateTime(digest.latest_completed_at ?? digest.latest_run_at)}</dd>
                    </div>
                    <div>
                      <dt>Items</dt>
                      <dd>{digest.latest_item_count ?? 0}</dd>
                    </div>
                    <div>
                      <dt>Articles</dt>
                      <dd>{digest.latest_fetched_article_count ?? 0}</dd>
                    </div>
                    <div>
                      <dt>Cache</dt>
                      <dd>{digest.latest_model_cache_hit_count ?? 0}/{digest.latest_model_cache_miss_count ?? 0}</dd>
                    </div>
                    <div>
                      <dt>Duration</dt>
                      <dd>{formatSeconds(digest.latest_duration_seconds)}</dd>
                    </div>
                  </dl>
                </article>
              ))}
              {pipeline?.digests.length === 0 ? <p className="muted">No digests configured.</p> : null}
            </div>
          </section>

          <section className="panel">
            <h3>Model Cache</h3>
            <dl className="status-list">
              <div>
                <dt>Records</dt>
                <dd>{pipeline?.model_cache.record_count ?? 0}</dd>
              </div>
              <div>
                <dt>Latest update</dt>
                <dd>{formatDateTime(pipeline?.model_cache.latest_updated_at)}</dd>
              </div>
              <div>
                <dt>Model</dt>
                <dd>{pipeline?.model.model ?? "Unavailable"}</dd>
              </div>
              <div>
                <dt>API key</dt>
                <dd>{pipeline?.model.api_key_configured ? "Configured" : "Missing"}</dd>
              </div>
            </dl>
          </section>

          <section className="panel">
            <h3>Status</h3>
            <dl className="status-list">
              <div>
                <dt>OAuth client</dt>
                <dd>{status?.configured ? "Configured" : "Missing"}</dd>
              </div>
              <div>
                <dt>Gmail token</dt>
                <dd>{status?.connected ? "Connected" : "Not connected"}</dd>
              </div>
              <div>
                <dt>Redirect URI</dt>
                <dd>{status?.redirect_uri ?? "Unavailable"}</dd>
              </div>
              <div>
                <dt>Redirect readiness</dt>
                <dd>{status?.redirect_warning ?? "Ready"}</dd>
              </div>
              <div>
                <dt>Token path</dt>
                <dd>{status?.credentials_path ?? "Unavailable"}</dd>
              </div>
            </dl>
            <div className="button-row">
              <button onClick={connectGmail} disabled={busy || !status?.configured || !status?.oauth_redirect_ready}>
                Connect Gmail
              </button>
              <button className="secondary-button" onClick={disconnectGmail} disabled={busy || !status?.connected}>
                Disconnect
              </button>
            </div>
          </section>

          <section className="panel">
            <h3>OAuth Client Secret</h3>
            <div className="create-form">
              <label>
                Upload JSON
                <input
                  type="file"
                  accept="application/json,.json"
                  onChange={(event) => void readClientSecretFile(event.target.files?.[0])}
                />
              </label>
              <label>
                Client secret JSON
                <textarea
                  value={clientSecretJson}
                  onChange={(event) => setClientSecretJson(event.target.value)}
                  rows={8}
                  placeholder='{"installed": ... }'
                />
              </label>
              <button onClick={saveClientSecret} disabled={busy || clientSecretJson.trim().length === 0}>
                Save OAuth Client
              </button>
            </div>
          </section>

          <section className="panel">
            <h3>Complete Redirect</h3>
            <div className="create-form">
              <label>
                Failed Google redirect URL
                <textarea
                  value={callbackUrl}
                  onChange={(event) => setCallbackUrl(event.target.value)}
                  rows={5}
                  placeholder="https://ultras-mac-studio-2.tail4aeef0.ts.net/api/admin/gmail/oauth/callback?..."
                />
              </label>
              <button onClick={completeGmailFromRedirect} disabled={busy || callbackUrl.trim().length === 0}>
                Complete Gmail Login
              </button>
            </div>
          </section>
        </div>
      </section>
    </main>
  );
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function formatSchedulerTime(timeValue: string | null | undefined, timezone: string | null | undefined): string {
  if (!timeValue) return "No fixed time";
  const [hourText, minuteText = "00"] = timeValue.split(":");
  const hour = Number(hourText);
  const minute = Number(minuteText);
  if (Number.isNaN(hour) || Number.isNaN(minute)) return timeValue;
  const period = hour >= 12 ? "PM" : "AM";
  const displayHour = hour % 12 || 12;
  const zone = timezone === "America/Los_Angeles" ? "Pacific" : timezone;
  return `${displayHour}:${String(minute).padStart(2, "0")} ${period}${zone ? ` ${zone}` : ""}`;
}

function formatMcpStatus(status: McpStatus | null | undefined): string {
  if (!status) return "Checking...";
  if (!status.available) return status.error ?? "oMLX MCP unavailable";
  const gmailTools = `${status.gmail.tools_count} Gmail tool${status.gmail.tools_count === 1 ? "" : "s"}`;
  return `${gmailTools} · ${status.tool_count} total`;
}

function formatStatusLabel(value: string | null | undefined): string {
  if (!value) return "Unknown";
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function formatDeliveryUrl(value: string | null | undefined): string {
  if (!value) return "Available after status refresh";
  try {
    const url = new URL(value);
    return `${url.host}${url.pathname}`;
  } catch {
    return value;
  }
}

function formatAgentDecisionSummary(status: AgentDecisionsStatus | null | undefined): string {
  if (!status || status.record_count === 0) return "No agent reviews yet";
  const editorial = status.agent_counts.editorial ?? 0;
  const critic = status.agent_counts.critic ?? 0;
  const podcast = status.agent_counts.podcast_scout ?? 0;
  return `${editorial} editorial · ${critic} critic · ${podcast} podcast`;
}

function formatSourceScoutSummary(status: SourceScoutStatus | null | undefined): string {
  if (!status || status.source_count === 0) return "Not seeded yet";
  return `${status.search_only_count} search-only · ${status.candidate_count} candidate`;
}

function formatSourceState(value: RedditSource["state"]): string {
  if (value === "search_only") return "Search-only";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function formatVerificationResult(result: VerificationRunResult): string {
  if (result.status !== "completed") return result.message ?? result.status;
  const reviewed = result.reviewed_article_count ?? 0;
  const decisions = result.decision_count ?? 0;
  const dropped = result.dropped_count ?? 0;
  const lead = result.lead_title ? ` · lead: ${result.lead_title}` : "";
  const prefix = result.published ? "Published" : "Reviewed";
  return `${prefix} ${reviewed} article(s), saved ${decisions} decision(s), dropped ${dropped}${lead}`;
}

function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return "0s";
  if (value < 60) return `${Math.round(value)}s`;
  return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
}

function formatNumber(value: number | null | undefined): string {
  return new Intl.NumberFormat().format(value ?? 0);
}

function formatStageLabel(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Unknown";
  if (value < 1000) return `${Math.round(value)}ms`;
  return formatSeconds(value / 1000);
}

function formatBytes(value: number | null | undefined): string {
  const bytes = value ?? 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatPodcastMetricCounts(status: PodcastMetricsStatus | null | undefined): string {
  if (!status || status.record_count === 0) return "No podcast telemetry yet";
  const success = status.status_counts.success ?? 0;
  const failed = Object.entries(status.status_counts)
    .filter(([key]) => key !== "success")
    .reduce((total, [, count]) => total + count, 0);
  return `${success} success · ${failed} issue(s)`;
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Unknown";
  return `${Math.round(value * 100)}%`;
}

function formatPathTail(value: string | null | undefined): string {
  if (!value) return "Not set";
  const parts = value.split("/").filter(Boolean);
  return parts.slice(-2).join("/") || value;
}
