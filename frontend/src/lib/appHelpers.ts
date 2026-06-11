import {
  briefControlBounds,
  defaultBriefControls,
  defaultContentLimits,
  defaultPipelineLimits,
  defaultSourceSelection,
  sourceOptions,
} from "./types";
import { formatSourceLabel, formatStage } from "./display";
import type {
  AdminStatus,
  BriefControlsDraft,
  ConfirmationDraft,
  ContentLimitsDraft,
  DigestLibraryItem,
  Exploration,
  ExplorationIssue,
  ExplorationLibraryItem,
  ModelRouteDraft,
  PendingStrategyRefinement,
  PipelineLimitsDraft,
  RefinementProgress,
  ScheduledDeliveryFailure,
  SourceKey,
  SourceScope,
  SourceStatusResponse,
  StrategyPreview,
  TopicProfile,
  TopicProfileResponse,
} from "./types";

export function recencyText(weighting?: string, lookbackHours?: number | null): string {
  if (lookbackHours === null || weighting === "all_available") return "Unlimited";
  if (lookbackHours && lookbackHours > 0) {
    if (lookbackHours <= 48) return `Last ${lookbackHours} hours`;
    const days = Math.round(lookbackHours / 24);
    return `Last ${days} day${days === 1 ? "" : "s"}`;
  }
  const map: Record<string, string> = {
    breaking: "Breaking / latest",
    recent: "Recent",
    last_year: "Past year",
    all_available: "Best available",
    balanced: "Balanced",
    evergreen: "Evergreen",
  };
  return weighting ? map[weighting] ?? weighting : "";
}

export function sourceScopeFromLookbackHours(lookbackHours: number | null): SourceScope {
  if (lookbackHours === null) return "all_available";
  if (lookbackHours <= 48) return "breaking";
  if (lookbackHours >= 365 * 24) return "last_year";
  return "recent";
}

export function strategyConversationTurns(
  pendingStrategy: PendingStrategyRefinement | null,
  strategyConfirmation: string,
): Array<{ role: string; content: string }> {
  const turns = (pendingStrategy?.conversation ?? [])
    .filter((turn) => turn && typeof turn.content === "string" && turn.content.trim())
    .map((turn) => ({ role: turn.role === "user" ? "user" : "assistant", content: turn.content.trim() }));
  if (turns.length) return turns;
  const confirmation = strategyConfirmation.trim();
  return confirmation ? [{ role: "assistant", content: confirmation }] : [];
}

export function strategyIntentSummary(profile: TopicProfile | null, preview: StrategyPreview | null): string {
  const scope = profile?.scope || preview?.scope || profile?.statement || preview?.statement || "Current brief strategy";
  const sourceLabels = Object.entries(profile?.source_selection ?? {})
    .filter(([, enabled]) => enabled)
    .map(([source]) => formatSourceLabel(source))
    .filter((source) => source !== "Collections")
    .slice(0, 5);
  const lookback = profile?.lookback_hours ? ` over ${gmailLookbackLabel(profile.lookback_hours).toLowerCase()}` : "";
  const sources = sourceLabels.length ? ` across ${sourceLabels.join(", ")}` : "";
  return truncateSentence(`${scope}${sources}${lookback}.`, 220);
}

export function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

export function deliveryFailuresFromStatus(
  status: AdminStatus | null,
  topics: TopicProfileResponse[] = [],
): ScheduledDeliveryFailure[] {
  const failures = new Map<string, ScheduledDeliveryFailure>();
  for (const failure of status?.delivery?.scheduled_failures ?? []) {
    failures.set(failure.topic_id, failure);
  }
  for (const topic of topics) {
    const config = topic.profile.delivery_config ?? {};
    const failed = config.delivery_disabled_after_failure === true || config.last_delivery_status === "failed";
    if (!failed) continue;
    const error = typeof config.last_error === "string"
      ? config.last_error
      : typeof config.last_delivery_error === "string"
        ? config.last_delivery_error
        : "Email delivery failed.";
    failures.set(topic.topic_id, {
      topic_id: topic.topic_id,
      name: profileName(topic),
      schedule: topic.schedule,
      error,
      last_attempted_at: typeof config.last_delivery_attempted_at === "string" ? config.last_delivery_attempted_at : null,
      latest_exploration_id: topic.latest_exploration?.exploration_id ?? null,
    });
  }
  return [...failures.values()];
}

export function emptyDraft(defaults = defaultContentLimits): ConfirmationDraft {
  return {
    scope: "",
    depth: "informed-generalist",
    recency_weighting: "recent",
    lookback_hours: defaultBriefControls.lookback_hours,
    exclusions: "",
    must_have: "",
    content_limits: defaults,
    recency_scope_confirmed: false,
    sourceScopeTouched: false,
  };
}

export function draftFromProfile(profile: TopicProfile, defaults = defaultContentLimits, preserve?: ConfirmationDraft): ConfirmationDraft {
  const nextDraft: ConfirmationDraft = {
    scope: profile.scope || profile.statement || "",
    depth: profile.depth === "practitioner" ? "practitioner" : "informed-generalist",
    recency_weighting: sourceScopeFromProfile(profile),
    lookback_hours: lookbackHoursForBuild(profile, undefined, defaultBriefControls.lookback_hours),
    exclusions: (profile.exclusions ?? []).join(", "),
    must_have: (profile.must_have_terms ?? []).join(", "),
    content_limits: contentLimitsFromProfile(profile, defaults),
    recency_scope_confirmed: false,
    sourceScopeTouched: false,
  };
  if (!preserve?.sourceScopeTouched) return nextDraft;
  return {
    ...nextDraft,
    recency_weighting: preserve.recency_weighting,
    lookback_hours: preserve.lookback_hours,
    sourceScopeTouched: true,
  };
}

export function mergeSourceSelections(
  incoming: Record<SourceKey, boolean>,
  sticky: Record<SourceKey, boolean>,
): Record<SourceKey, boolean> {
  return { ...incoming, ...sticky };
}

export function briefControlsFromProfile(profile: TopicProfile, defaults = defaultBriefControls): BriefControlsDraft {
  return {
    lookback_hours: normalizeLookbackHours(profile.lookback_hours, defaults.lookback_hours),
    content_limits: contentLimitsFromProfile(profile, defaults.content_limits),
  };
}

export function contentLimitsFromProfile(profile: TopicProfile, defaults = defaultContentLimits): ContentLimitsDraft {
  const saved = profile.content_limits ?? {};
  const savedPerSource = saved.per_source ?? {};
  const perSource: Partial<Record<SourceKey, number>> = {};
  for (const source of sourceOptions) {
    const sourceMax = defaultContentLimits.per_source[source.key] ?? briefControlBounds.per_source.max;
    perSource[source.key] = clampContentLimit(
      Number(savedPerSource[source.key] ?? defaults.per_source[source.key] ?? sourceMax),
      briefControlBounds.per_source.min,
      sourceMax,
    );
  }
  return {
    total_items: clampContentLimit(Number(saved.total_items ?? defaults.total_items), briefControlBounds.total_items.min, briefControlBounds.total_items.max),
    target_items: clampContentLimit(Number(saved.target_items ?? defaults.target_items), briefControlBounds.target_items.min, briefControlBounds.target_items.max),
    lead_items: clampContentLimit(Number(saved.lead_items ?? defaults.lead_items), 0, 20),
    per_source: perSource,
    quality_floor: saved.quality_floor === "strong" ? "strong" : "standard",
  };
}

export function pipelineLimitsFromProfile(profile: TopicProfile, defaults = defaultPipelineLimits): PipelineLimitsDraft {
  const saved = profile.pipeline_limits ?? {};
  return {
    article_fetches: clampContentLimit(Number(saved.article_fetches ?? defaults.article_fetches), 1, 1000),
    article_fetch_concurrency: clampContentLimit(Number(saved.article_fetch_concurrency ?? defaults.article_fetch_concurrency), 1, 40),
    model_refinement_items: clampContentLimit(Number(saved.model_refinement_items ?? defaults.model_refinement_items), 0, 250),
    date_adjudication_candidates: clampContentLimit(Number(saved.date_adjudication_candidates ?? defaults.date_adjudication_candidates), 1, 100),
    source_audit_candidates: clampContentLimit(Number(saved.source_audit_candidates ?? defaults.source_audit_candidates), 1, 150),
    editorial_candidates: clampContentLimit(Number(saved.editorial_candidates ?? defaults.editorial_candidates), 1, 500),
    critic_articles: clampContentLimit(Number(saved.critic_articles ?? defaults.critic_articles), 1, 250),
    critic_newsletter_records: clampContentLimit(Number(saved.critic_newsletter_records ?? defaults.critic_newsletter_records), 0, 20),
  };
}

export function clampContentLimit(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, Math.round(value)));
}

export function validateBriefControls(controls: BriefControlsDraft, sourceSelection: Record<SourceKey, boolean>): string[] {
  const errors: string[] = [];
  if (controls.lookback_hours !== null) {
    const sourceWindowDays = Number(controls.lookback_hours) <= 24 ? 0 : Number(controls.lookback_hours) / 24;
    addBoundsError(errors, "Source window", sourceWindowDays, briefControlBounds.source_window_days.min, briefControlBounds.source_window_days.max, "days");
  }
  errors.push(...validateContentLimits(controls.content_limits, sourceSelection));
  return errors;
}

export function validateContentLimits(contentLimits: ContentLimitsDraft, sourceSelection: Record<SourceKey, boolean>): string[] {
  const errors: string[] = [];
  addBoundsError(errors, "Candidate budget", contentLimits.total_items, briefControlBounds.total_items.min, briefControlBounds.total_items.max);
  addBoundsError(errors, "Target visible stories", contentLimits.target_items, briefControlBounds.target_items.min, briefControlBounds.target_items.max);
  addBoundsError(errors, "Lead stories", contentLimits.lead_items, briefControlBounds.lead_items.min, briefControlBounds.lead_items.max);
  sourceOptions
    .filter((source) => sourceSelection[source.key])
    .forEach((source) => {
      const value = contentLimits.per_source[source.key] ?? 0;
      const sourceMax = defaultContentLimits.per_source[source.key] ?? briefControlBounds.per_source.max;
      addBoundsError(errors, `${source.label} maximum`, value, briefControlBounds.per_source.min, sourceMax);
    });
  return errors;
}

export function addBoundsError(errors: string[], label: string, value: number, min: number, max: number, suffix = "") {
  const valueLabel = suffix ? `${min}-${max} ${suffix}` : `${min}-${max}`;
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < min || value > max) {
    errors.push(`${label} must be a whole number from ${valueLabel}.`);
  }
}

export function lookbackHoursForConfirmedDraft(profile: TopicProfile | null | undefined, draft: ConfirmationDraft, defaultLookbackHours = defaultBriefControls.lookback_hours): number | null {
  if (draft.sourceScopeTouched) return normalizeLookbackHours(draft.lookback_hours, defaultLookbackHours);
  return lookbackHoursForBuild(profile, draft, defaultLookbackHours);
}

export function lookbackHoursForBuild(profile: TopicProfile | null | undefined, draft?: ConfirmationDraft, defaultLookbackHours = defaultBriefControls.lookback_hours): number | null {
  if (draft?.sourceScopeTouched) return normalizeLookbackHours(draft.lookback_hours, defaultLookbackHours);
  if (profile && "lookback_hours" in profile) return normalizeLookbackHours(profile.lookback_hours ?? null, defaultLookbackHours);
  if (!draft) return normalizeLookbackHours(defaultLookbackHours, defaultBriefControls.lookback_hours);
  return lookbackHoursFromSourceScope(draft?.recency_weighting ?? normalizeSourceScope(profile?.recency_weighting));
}

export function sourceScopeFromProfile(profile: TopicProfile): SourceScope {
  if (profile.lookback_hours === null) return "all_available";
  const explicit = Number(profile.lookback_hours ?? 0);
  if (Number.isFinite(explicit) && explicit >= 1) {
    if (explicit <= 48) return "breaking";
    if (explicit >= 365 * 24) return "last_year";
    return "recent";
  }
  return normalizeSourceScope(profile.recency_weighting);
}

export function topicRecencyLabel(topic: TopicProfileResponse, defaultLookbackHours = defaultBriefControls.lookback_hours): string {
  const lookback = lookbackHoursForBuild(topic.profile, undefined, defaultLookbackHours);
  return recencyText(sourceScopeFromLookbackHours(lookback), lookback);
}

export function lookbackHoursFromSourceScope(sourceScope: SourceScope): number | null {
  if (sourceScope === "all_available") return null;
  if (sourceScope === "last_year") return 8760;
  if (sourceScope === "recent") return 168;
  return 24;
}

export function sourceScopeConfirmation(sourceScope: SourceScope, lookbackHours?: number | null): string {
  if (lookbackHours === null || sourceScope === "all_available") return "I’ll use all available dates; no source-window filter will run.";
  if (lookbackHours) {
    const days = Math.max(1, Math.round(lookbackHours / 24));
    return `I’ll look for sources dated within the last ${days === 1 ? "day" : `${days} days`}.`;
  }
  if (sourceScope === "breaking") return "I’ll look for sources dated within the last 24 hours.";
  if (sourceScope === "recent") return "I’ll look for sources dated within the last 3 days.";
  if (sourceScope === "last_year") return "I’ll look for sources dated within the last year.";
  return "I’ll use the best available sources, even when older context is useful.";
}

export function normalizeLookbackHours(value: number | null | undefined, fallback: number | null = 168): number | null {
  if (value === null) return null;
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric >= 0) {
    if (numeric === 0) return 24;
    return Math.min(262800, Math.floor(numeric));
  }
  return fallback === undefined ? 168 : fallback;
}

export function normalizeSourceScope(value: string | undefined): SourceScope {
  if (value === "breaking") return "breaking";
  if (value === "last_year") return "last_year";
  if (value === "all_available" || value === "evergreen") return "all_available";
  return "recent";
}

export function sourceReadinessItems(
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
        const semanticCount = uniqueCleanList([
          ...(profile?.direct_episode_queries ?? []),
          ...(profile?.related_episode_queries ?? []),
          ...(profile?.priority_terms ?? []),
        ]).length;
        if (!semanticCount) {
          return { key: source.key, label: source.label, ready: false, message: "no show or search targets yet" };
        }
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

export type SearchPlanGroup = {
  key: string;
  label: string;
  queries: string[];
};

export function sourceSearchPlanGroups(profile: TopicProfile | null): SearchPlanGroup[] {
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
    if (source.key === "podcasts") {
      const direct = uniqueCleanList(profile.direct_episode_queries ?? []);
      const related = uniqueCleanList(profile.related_episode_queries ?? []);
      const boosted = uniqueCleanList(profile.priority_terms ?? []);
      const avoided = uniqueCleanList(profile.negative_constraints ?? []);
      if (direct.length) groups.push({ key: "podcast-direct", label: "Podcast direct", queries: direct });
      if (related.length) groups.push({ key: "podcast-related", label: "Podcast related", queries: related });
      if (boosted.length) groups.push({ key: "podcast-boost", label: "Podcast boost", queries: boosted });
      if (avoided.length) groups.push({ key: "podcast-avoid", label: "Podcast avoid", queries: avoided });
    }
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

export function uniqueCleanList(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

export function parseEmailEntries(value: string): string[] {
  return uniqueCleanList(value.split(/[\s,;]+/));
}

export function digestEmailEnabled(topic: TopicProfileResponse): boolean {
  const config = topic.profile.delivery_config ?? {};
  return Boolean(config.email_enabled);
}

export function digestRecipients(topic: TopicProfileResponse, fallback = ""): string[] {
  const config = topic.profile.delivery_config ?? {};
  if (Array.isArray(config.recipient_emails)) {
    return uniqueCleanList(config.recipient_emails.map((value) => String(value || "")));
  }
  if (typeof config.recipient_email === "string" && config.recipient_email.trim()) {
    return uniqueCleanList([config.recipient_email]);
  }
  if (digestEmailEnabled(topic) && fallback.trim()) {
    return uniqueCleanList([fallback]);
  }
  return [];
}

export function cleanSourceQueryRecord(value: Record<string, string[]> | undefined): Record<string, string[]> {
  const cleaned: Record<string, string[]> = {};
  for (const [source, queries] of Object.entries(value ?? {})) {
    const nextQueries = uniqueCleanList(Array.isArray(queries) ? queries : []);
    if (nextQueries.length) cleaned[source] = nextQueries;
  }
  return cleaned;
}

export function emptySourcePlanLabel(source: SourceKey): string {
  if (source === "markets") return "No ticker resolved yet";
  if (source === "foreign_media") return "No native-language query set yet";
  if (source === "gmail") return "Uses approved newsletter rules";
  return "Uses general search terms";
}

export function splitList(value: string): string[] {
  return value.split(/[,;\n]/).map((item) => item.trim()).filter(Boolean);
}

export function enabledSourceSelection(selection: Record<SourceKey, boolean>, status: SourceStatusResponse | null): Record<SourceKey, boolean> {
  return {
    web_search: Boolean(selection.web_search && status?.sources.web_search?.enabled),
    foreign_media: Boolean(selection.foreign_media && status?.sources.foreign_media?.enabled),
    gmail: Boolean(selection.gmail && status?.sources.gmail?.enabled),
    podcasts: Boolean(selection.podcasts && status?.sources.podcasts?.enabled),
    youtube: Boolean(selection.youtube && status?.sources.youtube?.enabled),
    collections: Boolean(selection.collections && status?.sources.collections?.enabled),
    markets: Boolean(selection.markets && status?.sources.markets?.enabled),
    reddit: Boolean(selection.reddit && status?.sources.reddit?.enabled),
    google_news: Boolean(selection.google_news && status?.sources.google_news?.enabled),
  };
}

export function firstBlockedSelectedSource(selection: Record<SourceKey, boolean>, status: SourceStatusResponse | null): SourceKey | null {
  for (const source of sourceOptions) {
    if (selection[source.key] && status && !status.sources[source.key]?.enabled) return source.key;
  }
  return null;
}

export function hasEnabledSource(selection: Record<string, boolean>): boolean {
  return Object.values(selection).some(Boolean);
}

export function briefPath(record: Exploration | null): string | null {
  if (!record) return null;
  if (record.progress.brief?.html_path) return record.progress.brief.html_path;
  if (record.brief_ref) return `/api/explore/explorations/${record.exploration_id}/brief/html`;
  return null;
}

export function openPath(path: string | null) {
  if (path) window.location.assign(path);
}

export function sourceSelectionFromRecord(selection: Record<string, boolean> | undefined): Record<SourceKey, boolean> {
  return sourceOptions.reduce<Record<SourceKey, boolean>>((result, source) => {
    result[source.key] = Boolean(selection?.[source.key]);
    return result;
  }, { ...defaultSourceSelection });
}

export function profileName(topic: TopicProfileResponse): string {
  return topic.profile.scope || topic.statement || "Untitled brief";
}

export function explorationLibraryName(item: ExplorationLibraryItem): string {
  if (item.kind === "topic") return profileName(item.topic);
  return item.topic?.profile.scope
    ?? item.topic?.statement
    ?? item.exploration.progress.brief?.title
    ?? "Brief";
}

export function explorationLibraryDate(item: ExplorationLibraryItem): number {
  if (item.kind === "topic") return dateValue(item.topic.updated_at ?? item.topic.created_at);
  return dateValue(item.exploration.finished_at ?? item.exploration.started_at);
}

export function digestLibraryName(item: DigestLibraryItem): string {
  if (item.kind === "topic") return profileName(item.topic);
  return item.digest.name || item.digest.interest || "Digest";
}

export function digestLibraryDate(item: DigestLibraryItem): number {
  if (item.kind === "topic") return dateValue(item.topic.updated_at ?? item.topic.created_at);
  return dateValue(item.digest.updated_at ?? item.digest.created_at);
}

export function gmailLookbackLabel(hours: number | undefined): string {
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

export function formatSourceSelection(selection: Record<string, boolean>): string {
  const enabled = sourceOptions.filter((source) => selection[source.key]).map((source) => source.label);
  return enabled.length ? enabled.join(", ") : "No sources";
}

export function sourcePlan(selection: Record<string, boolean>): string {
  const enabled = sourceOptions.filter((source) => selection[source.key]).map((source) => source.label);
  if (!enabled.length) return "No sources selected";
  return `Running: ${enabled.join(", ")}`;
}

export function formatPipeline(pipeline: Array<[string, string]>): string {
  const running = pipeline.find(([, status]) => status === "running");
  if (running) return `${formatStage(running[0])} running`;
  const failed = pipeline.find(([, status]) => status === "failed");
  if (failed) return `${formatStage(failed[0])} failed`;
  return "Ready";
}

export function hasActionableBuildIssues(exploration: Exploration): boolean {
  return buildAttentionIssues(exploration).length > 0;
}

export function buildAttentionIssues(exploration: Exploration | null): ExplorationIssue[] {
  if (!exploration) return [];
  return [
    ...(exploration.progress.requested_source_issues ?? []),
    ...actionableIssues(exploration.progress.source_audit_issues),
  ];
}

export function actionableIssues(issues: ExplorationIssue[] | undefined): ExplorationIssue[] {
  return (issues ?? []).filter((issue) => isActionableIssue(issue));
}

export function filterDecisionNotes(exploration: Exploration): ExplorationIssue[] {
  return [
    ...(exploration.progress.source_filter_notes ?? []),
    ...(exploration.progress.source_audit_issues ?? []).filter((issue) => !isActionableIssue(issue)),
  ];
}

export function sourceFromIssueName(sourceName: string): string {
  const lowered = sourceName.toLowerCase();
  if (lowered.includes("gmail") || lowered.includes("@")) return "Gmail";
  if (lowered.includes("podcast")) return "Podcast";
  if (lowered.includes("youtube")) return "YouTube";
  if (lowered.includes("market") || /^[A-Z0-9.=-]{1,12}$/.test(sourceName.trim())) return "Markets";
  if (lowered.includes("google_news") || lowered.includes("google news") || lowered.includes("google-news")) return "Google News";
  if (/[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/.test(sourceName)) return "Foreign Media";
  return "Web";
}

export function isActionableIssue(issue: ExplorationIssue): boolean {
  const source = issue.source_name.trim().toLowerCase();
  const reason = issue.reason.trim().toLowerCase();
  return source === "source audit" || source === "ai review" || reason.startsWith("audit could not complete");
}

export function formatDateTime(value: string | null | undefined): string {
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

export function releaseStamp(status: AdminStatus | null): string {
  const release = status?.system?.release;
  const timestamp = release?.timestamp ? formatDateTime(release.timestamp) : "";
  const revision = release?.revision ? release.revision : "";
  if (timestamp && revision) return `Release ${timestamp} · ${revision}`;
  if (timestamp) return `Release ${timestamp}`;
  if (revision) return `Release ${revision}`;
  return "Release unknown";
}

export function truncateSentence(value: string, maxLength: number): string {
  const cleaned = value.split(/\s+/).join(" ").trim();
  if (cleaned.length <= maxLength) return cleaned;
  return `${cleaned.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
}

export function strategyUpdateConfirmation(note: string | undefined, profile: TopicProfile): string {
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

export function dateValue(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = new Date(value).valueOf();
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function routeDraftFromStatus(status: AdminStatus | null): ModelRouteDraft {
  const routes = status?.model?.routing?.routes ?? {};
  const draft: ModelRouteDraft = {};
  Object.entries(routes).forEach(([agent, route]) => {
    draft[agent] = { model: route.model ?? "" };
  });
  return draft;
}

export function formatMetricMs(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  return `${Math.round(value)} ms`;
}

export function formatMetricNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  return `${Math.round(value)}`;
}

export function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  return `${Math.round(value * 100)}%`;
}

export type RefinementProgressState = {
  stage: string;
  detail: string;
  activity: string;
  percent: number;
  elapsedMs: number;
  alert: boolean;
};

export function refinementProgressState(progress: RefinementProgress, now: number): RefinementProgressState {
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

export function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}m ${seconds}s`;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
