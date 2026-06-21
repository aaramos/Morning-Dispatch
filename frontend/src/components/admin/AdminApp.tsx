import { useCallback, useEffect, useMemo, useState } from "react";
import type { ChangeEvent } from "react";
import { api } from "../../lib/api";
import { loadSessionValue } from "../../lib/drafts";
import { BrandMark } from "../BrandMark";
import { adminTabOptions, defaultBriefControls, defaultPipelineLimits, defaultSourceSelectionForControls, sourceOptions } from "../../lib/types";
import type { AdminStatus, AdminTab, BriefControlsDraft, BriefSettingsResponse, Digest, DigestLibraryItem, EditingDigestDraft, EditingRecencyDraft, Exploration, ExplorationIssue, ExplorationLibraryItem, GmailAllowlistResponse, LibraryResponse, ModelRouteDraft, PipelineLimitsDraft, SchedulePreset, SortMode, SourceStatusResponse, TopicProfileResponse } from "../../lib/types";
import {
  briefControlsFromProfile,
  briefPath,
  contentLimitsFromProfile,
  deliveryFailuresFromStatus,
  digestEmailEnabled,
  digestLibraryDate,
  digestLibraryName,
  digestRecipients,
  errorMessage,
  explorationLibraryDate,
  explorationLibraryName,
  formatDateTime,
  formatMetricMs,
  formatMetricNumber,
  formatRate,
  formatSourceSelection,
  buildAttentionIssues,
  hasActionableBuildIssues,
  lookbackHoursForBuild,
  openPath,
  parseEmailEntries,
  pipelineLimitsFromProfile,
  profileName,
  routeDraftFromStatus,
  sourceScopeFromLookbackHours,
  sourceSelectionFromRecord,
  topicRecencyLabel,
  uniqueCleanList,
  validateBriefControls,
} from "../../lib/appHelpers";
import { formatStage, isModelDegraded } from "../../lib/display";
import { BriefControlsPanel } from "../BriefControlsPanel";
import { DisclosureButton } from "../DisclosureButton";
import { LibraryBuildProgress } from "../LibraryBuildProgress";
import { PipelineLimitsPanel } from "../PipelineLimitsPanel";
import { QuickRecencyEditor } from "../QuickRecencyEditor";
import { ScheduledDeliveryAlert } from "../ScheduledDeliveryAlert";
import { SettingsErrorList } from "../SettingsErrorList";
import { SystemLimitsPanel } from "../SystemLimitsPanel";
import { DigestScheduleEditor } from "./DigestScheduleEditor";
import { GmailAllowlistGroup } from "./GmailAllowlistGroup";
import { LibrarySection } from "./LibrarySection";
import { ReportingTabContent } from "./ReportingTabContent";
import { SecretHealthPanel } from "./SecretHealthPanel";

export function AdminApp() {
  const requestedTab = new URLSearchParams(window.location.search).get("tab") ?? "status";
  const initialTab = adminTabOptions.includes(requestedTab as AdminTab) ? (requestedTab as AdminTab) : "status";
  const [selectedRunId, setSelectedRunId] = useState<string | null>(() => {
    return new URLSearchParams(window.location.search).get("issue_run");
  });
  const issueRun = selectedRunId;
  const [tab, setTab] = useState(initialTab);

  function handleSelectRunId(id: string | null) {
    setSelectedRunId(id);
    const url = new URL(window.location.href);
    if (id) {
      url.searchParams.set("issue_run", id);
    } else {
      url.searchParams.delete("issue_run");
    }
    window.history.replaceState(null, "", url.toString());
  }
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [sources, setSources] = useState<SourceStatusResponse | null>(null);
  const [library, setLibrary] = useState<LibraryResponse>({ explorations: [], deleted_explorations: [], topics: [], digests: [], legacy_digests: [] });
  const [message, setMessage] = useState("Loading Admin...");
  const [busy, setBusy] = useState(false);
  const [explorationSort, setExplorationSort] = useState<SortMode>(() => loadSessionValue("admin.explorationSort", "recent"));
  const [digestSort, setDigestSort] = useState<SortMode>(() => loadSessionValue("admin.digestSort", "recent"));
  const [editingDigest, setEditingDigest] = useState<EditingDigestDraft | null>(null);
  const [editingRecency, setEditingRecency] = useState<EditingRecencyDraft | null>(null);
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
  const [webProvider, setWebProvider] = useState<"tavily" | "brave" | "serpapi" | "serper">("serper");
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
  const [jobModel, setJobModel] = useState("");
  const [jobLimit, setJobLimit] = useState(100);
  const [modelApiKey, setModelApiKey] = useState("");
  const [modelRoutes, setModelRoutes] = useState<ModelRouteDraft>({});
  const [secretsExpanded, setSecretsExpanded] = useState(() => loadSessionValue("admin.secretsExpanded", false));
  const [sourceConfigExpanded, setSourceConfigExpanded] = useState(() => loadSessionValue("admin.sourceConfigExpanded", false));
  const [explorationsExpanded, setExplorationsExpanded] = useState(() => loadSessionValue("admin.explorationsExpanded", false));
  const [deletedExpanded, setDeletedExpanded] = useState(() => loadSessionValue("admin.deletedExpanded", false));
  const [digestsExpanded, setDigestsExpanded] = useState(() => loadSessionValue("admin.digestsExpanded", false));
  const [expandedIssueRows, setExpandedIssueRows] = useState<Set<string>>(() => new Set());
  const toggleIssueRow = (explorationId: string) =>
    setExpandedIssueRows((current) => {
      const next = new Set(current);
      if (next.has(explorationId)) next.delete(explorationId);
      else next.add(explorationId);
      return next;
    });

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
  const scheduledDeliveryFailures = useMemo(
    () => deliveryFailuresFromStatus(status, library.digests),
    [library.digests, status],
  );
  const modelOptions = status?.model?.catalog.models ?? [];
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
    setSelectedLocalModel(preferredLocalModel);
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
    const currentTab = new URLSearchParams(window.location.search).get("tab") || "status";
    if (currentTab === "reporting") {
      void api<{ built_with_issues: boolean; issues: ExplorationIssue[] }>(`/api/admin/explorations/${issueRun}/issues`)
        .then(setIssueDetails)
        .catch(() => setIssueDetails(null));
      return;
    }
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

  async function saveModel() {
    const modelName = selectedLocalModel.trim();
    if (!modelName) return;
    setBusy(true);
    try {
      await api("/api/admin/model/selection", {
        method: "POST",
        body: JSON.stringify({ provider: "local", model_name: modelName }),
      });
      await loadAdmin();
      setMessage("Default model saved");
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
      setMessage("Model default restored");
    } catch (error) {
      setMessage(errorMessage(error, "Could not restore model defaults"));
    } finally {
      setBusy(false);
    }
  }

  async function saveModelApiKey() {
    if (!modelApiKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/model/api-key", {
        method: "POST",
        body: JSON.stringify({ api_key: modelApiKey.trim() }),
      });
      setModelApiKey("");
      await loadAdmin();
      setMessage("Model API key saved");
    } catch (error) {
      setMessage(errorMessage(error, "Could not save model API key"));
    } finally {
      setBusy(false);
    }
  }

  async function clearModelApiKey() {
    setBusy(true);
    try {
      await api("/api/admin/model/api-key", { method: "DELETE" });
      setModelApiKey("");
      await loadAdmin();
      setMessage("Model API key removed");
    } catch (error) {
      setMessage(errorMessage(error, "Could not remove model API key"));
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
        model: current[agent]?.model ?? "",
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

  function refineTopicFromAdmin(topic: TopicProfileResponse) {
    window.location.href = `/?refine_topic=${encodeURIComponent(topic.topic_id)}`;
  }

  async function cloneAndRefineFromAdmin(exploration: Exploration) {
    setBusy(true);
    try {
      const result = await api<{ topic_profile: TopicProfileResponse }>(
        `/api/explore/explorations/${exploration.exploration_id}/clone-topic-profile`,
        { method: "POST" },
      );
      window.location.href = `/?refine_topic=${encodeURIComponent(result.topic_profile.topic_id)}`;
    } catch (error) {
      setMessage(errorMessage(error, "Could not clone search strategy"));
      setBusy(false);
    }
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

  async function deleteTopicFromAdmin(topic: TopicProfileResponse) {
    if (!window.confirm(`Delete incomplete brief plan "${profileName(topic)}" from the library?`)) return;
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${topic.topic_id}`, { method: "DELETE" });
      await loadAdmin();
      setMessage("Incomplete brief plan deleted");
    } catch (error) {
      setMessage(errorMessage(error, "Could not delete brief plan"));
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
    const recipients = digestRecipients(topic, status?.delivery?.email.recipient_email ?? "");
    setEditingRecency(null);
    setEditingDigest({
      topicId: topic.topic_id,
      preset: ((topic.schedule ?? "daily") as SchedulePreset),
      time: typeof config.time_of_day === "string" ? config.time_of_day : "08:00",
      emailEnabled: digestEmailEnabled(topic),
      recipients,
      newRecipient: "",
    });
    setMessage("Editing schedule");
  }

  function startEditingRecency(topic: TopicProfileResponse) {
    setEditingDigest(null);
    setEditingRecency({
      topicId: topic.topic_id,
      lookbackHours: lookbackHoursForBuild(topic.profile, undefined, defaultControlsDraft.lookback_hours),
    });
    setMessage("Editing recency");
  }

  async function saveTopicRecency(topic: TopicProfileResponse) {
    if (!editingRecency || editingRecency.topicId !== topic.topic_id) return;
    setBusy(true);
    try {
      const saved = await api<TopicProfileResponse>(`/api/explore/topic-profiles/${topic.topic_id}/recency`, {
        method: "POST",
        body: JSON.stringify({
          lookback_hours: editingRecency.lookbackHours,
          recency_weighting: sourceScopeFromLookbackHours(editingRecency.lookbackHours),
        }),
      });
      setEditingRecency(null);
      await loadAdmin();
      setMessage(`Recency updated to ${topicRecencyLabel(saved, defaultControlsDraft.lookback_hours)}`);
    } catch (error) {
      setMessage(errorMessage(error, "Could not update recency"));
    } finally {
      setBusy(false);
    }
  }

  async function saveDigestSchedule(topic: TopicProfileResponse) {
    if (!editingDigest || editingDigest.topicId !== topic.topic_id) return;
    const recipients = uniqueCleanList(editingDigest.recipients);
    if (editingDigest.emailEnabled && recipients.length === 0) {
      setMessage("Add at least one email address or turn off email delivery.");
      return;
    }
    setBusy(true);
    try {
      await api(`/api/explore/topic-profiles/${topic.topic_id}/schedule`, {
        method: "POST",
        body: JSON.stringify({
          schedule: editingDigest.preset,
          time_of_day: editingDigest.time || "08:00",
          timezone: "America/Los_Angeles",
          email_enabled: editingDigest.emailEnabled,
          recipient_emails: recipients,
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

  function addDigestRecipient() {
    if (!editingDigest) return;
    const additions = parseEmailEntries(editingDigest.newRecipient);
    if (!additions.length) return;
    const invalid = additions.find((email) => !email.includes("@"));
    if (invalid) {
      setMessage(`Enter a valid email address: ${invalid}`);
      return;
    }
    setEditingDigest({
      ...editingDigest,
      recipients: uniqueCleanList([...editingDigest.recipients, ...additions]),
      newRecipient: "",
      emailEnabled: true,
    });
  }

  function removeDigestRecipient(email: string) {
    if (!editingDigest) return;
    const key = email.toLowerCase();
    const recipients = editingDigest.recipients.filter((item) => item.toLowerCase() !== key);
    setEditingDigest({
      ...editingDigest,
      recipients,
      emailEnabled: recipients.length > 0 ? editingDigest.emailEnabled : false,
    });
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

  async function deleteLegacyDigest(digest: Digest) {
    if (!window.confirm(`Delete "${digest.name || "Digest"}" from the library?`)) return;
    setBusy(true);
    try {
      await api(`/api/digests/${digest.id}`, { method: "DELETE" });
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
          <BrandMark />
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
                    <select value={webProvider} onChange={(event) => setWebProvider(event.target.value as "tavily" | "brave" | "serpapi" | "serper")}>
                      <option value="serper">Serper</option>
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
          {scheduledDeliveryFailures.length ? (
            <ScheduledDeliveryAlert failures={scheduledDeliveryFailures} onChanged={loadAdmin} />
          ) : null}
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
                      <small>
                        Ready to build · {formatDateTime(item.topic.updated_at ?? item.topic.created_at)} · {formatSourceSelection(item.topic.profile.source_selection)}
                        {" · "}{topicRecencyLabel(item.topic, defaultControlsDraft.lookback_hours)}
                      </small>
                    </div>
                    <div className="button-row">
                      <button type="button" className="secondary-action" onClick={() => openAdvancedSettings(item.topic)} disabled={busy}>Advanced Settings</button>
                      <button type="button" className="secondary-action" onClick={() => startEditingRecency(item.topic)} disabled={busy}>Recency</button>
                      <button type="button" className="secondary-action" onClick={() => refineTopicFromAdmin(item.topic)} disabled={busy}>Refine</button>
                      <button type="button" className="secondary-action" onClick={() => void buildTopicFromAdmin(item.topic)} disabled={busy}>Build brief</button>
                      <button type="button" className="secondary-action destructive" onClick={() => void deleteTopicFromAdmin(item.topic)} disabled={busy}>Delete</button>
                    </div>
                    {editingRecency?.topicId === item.topic.topic_id ? (
                      <QuickRecencyEditor
                        draft={editingRecency}
                        busy={busy}
                        onDraftChange={setEditingRecency}
                        onSave={() => void saveTopicRecency(item.topic)}
                        onCancel={() => setEditingRecency(null)}
                      />
                    ) : null}
                  </article>
                );
              }
              const isScheduledDigest = Boolean(item.topic?.schedule);
              return (
                <article className="library-row" key={item.exploration.exploration_id}>
                  <div>
                    <strong>{explorationLibraryName(item)}</strong>
                    <small>
                      {formatDateTime(item.exploration.finished_at ?? item.exploration.started_at)} · {formatSourceSelection(item.exploration.source_selection)}
                      {item.topic ? ` · ${topicRecencyLabel(item.topic, defaultControlsDraft.lookback_hours)}` : ""}
                    </small>
                    {isModelDegraded(item.exploration) ? (
                      <p className="warning-text">Built with AI issues.</p>
                    ) : hasActionableBuildIssues(item.exploration) ? (
                      <div className="warning-text source-issues-disclosure">
                        <DisclosureButton
                          expanded={expandedIssueRows.has(item.exploration.exploration_id)}
                          label={item.exploration.status === "complete" ? "Built with source issues." : "Source issues detected so far."}
                          onToggle={() => toggleIssueRow(item.exploration.exploration_id)}
                        />
                        {expandedIssueRows.has(item.exploration.exploration_id) ? (
                          <ul className="source-issue-list">
                            {buildAttentionIssues(item.exploration).map((issue) => (
                              <li key={`${issue.source_name}-${issue.reason}`}>
                                <strong>{issue.source_name}:</strong> {issue.reason}
                              </li>
                            ))}
                          </ul>
                        ) : null}
                      </div>
                    ) : null}
                    {isScheduledDigest ? (
                      <p className="muted">Scheduled digest · {formatStage(item.topic?.schedule ?? "daily")}</p>
                    ) : null}
                  </div>
                  <div className="button-row">
                    <button type="button" className="secondary-action" onClick={() => openPath(briefPath(item.exploration))} disabled={!briefPath(item.exploration)}>Open</button>
                    {item.exploration.status === "complete" ? (
                      <button
                        type="button"
                        className="secondary-action"
                        onClick={() => {
                          handleSelectRunId(item.exploration.exploration_id);
                          changeTab("reporting");
                        }}
                      >
                        Report
                      </button>
                    ) : null}
                    <button type="button" className="secondary-action" onClick={() => item.topic && openAdvancedSettings(item.topic)} disabled={busy || !item.topic}>Advanced Settings</button>
                    <button type="button" className="secondary-action" onClick={() => item.topic && startEditingRecency(item.topic)} disabled={busy || !item.topic}>Recency</button>
                    <button type="button" className="secondary-action" onClick={() => refineFromAdmin(item.exploration)} disabled={busy || item.exploration.status === "queued" || item.exploration.status === "running"}>Refine</button>
                    <button type="button" className="secondary-action" onClick={() => void cloneAndRefineFromAdmin(item.exploration)} disabled={busy || item.exploration.status === "queued" || item.exploration.status === "running"}>Clone and refine</button>
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
                    <DigestScheduleEditor
                      draft={editingDigest}
                      busy={busy}
                      onDraftChange={setEditingDigest}
                      onAddRecipient={addDigestRecipient}
                      onRemoveRecipient={removeDigestRecipient}
                      onSave={() => void saveDigestSchedule(item.topic!)}
                      onCancel={() => setEditingDigest(null)}
                    />
                  ) : null}
                  {item.topic && editingRecency?.topicId === item.topic.topic_id ? (
                    <QuickRecencyEditor
                      draft={editingRecency}
                      busy={busy}
                      onDraftChange={setEditingRecency}
                      onSave={() => void saveTopicRecency(item.topic!)}
                      onCancel={() => setEditingRecency(null)}
                    />
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
	                      <button type="button" className="secondary-action destructive" onClick={() => void deleteLegacyDigest(item.digest)} disabled={busy}>Delete</button>
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
                      {" · "}{topicRecencyLabel(topic, defaultControlsDraft.lookback_hours)}
                    </small>
                  </div>
                  <div className="button-row">
                    <button type="button" className="secondary-action" onClick={() => topic.latest_exploration && openPath(briefPath(topic.latest_exploration))} disabled={!topic.latest_exploration}>Open latest</button>
                    {topic.latest_exploration && topic.latest_exploration.status === "complete" ? (
                      <button
                        type="button"
                        className="secondary-action"
                        onClick={() => {
                          handleSelectRunId(topic.latest_exploration!.exploration_id);
                          changeTab("reporting");
                        }}
                      >
                        Report
                      </button>
                    ) : null}
                    <button type="button" className="secondary-action" onClick={() => openAdvancedSettings(topic)} disabled={busy}>Advanced Settings</button>
                    <button type="button" className="secondary-action" onClick={() => topic.latest_exploration && refineFromAdmin(topic.latest_exploration)} disabled={busy || !topic.latest_exploration || topic.latest_exploration.status === "queued" || topic.latest_exploration.status === "running"}>Refine</button>
                    <button type="button" className="secondary-action" onClick={() => void rebuildDigest(topic)} disabled={busy}>Rebuild</button>
                    <button type="button" className="secondary-action" onClick={() => startEditingDigest(topic)} disabled={busy}>Edit schedule</button>
                    <button type="button" className="secondary-action" onClick={() => startEditingRecency(topic)} disabled={busy}>Recency</button>
                    <button type="button" className="secondary-action" onClick={() => void pauseDigest(topic)} disabled={busy || topic.profile.status === "paused"}>Pause</button>
                    <button type="button" className="secondary-action" onClick={() => void archiveDigest(topic)} disabled={busy}>Archive</button>
                    <button type="button" className="secondary-action destructive" onClick={() => void deleteDigest(topic)} disabled={busy}>Delete</button>
                  </div>
                  {topic.latest_exploration?.status === "queued" || topic.latest_exploration?.status === "running" ? (
                    <LibraryBuildProgress exploration={topic.latest_exploration} />
                  ) : null}
                  {editingDigest?.topicId === topic.topic_id ? (
                    <DigestScheduleEditor
                      draft={editingDigest}
                      busy={busy}
                      onDraftChange={setEditingDigest}
                      onAddRecipient={addDigestRecipient}
                      onRemoveRecipient={removeDigestRecipient}
                      onSave={() => void saveDigestSchedule(topic)}
                      onCancel={() => setEditingDigest(null)}
                    />
                  ) : null}
                  {editingRecency?.topicId === topic.topic_id ? (
                    <QuickRecencyEditor
                      draft={editingRecency}
                      busy={busy}
                      onDraftChange={setEditingRecency}
                      onSave={() => void saveTopicRecency(topic)}
                      onCancel={() => setEditingRecency(null)}
                    />
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
              Default model
              <select value={selectedLocalModel} onChange={(event) => setSelectedLocalModel(event.target.value)} disabled={!modelOptions.length}>
                {modelOptions.length ? modelOptions.map((model) => <option key={model.id} value={model.id}>{model.id}</option>) : <option value="">No models reported</option>}
              </select>
            </label>
            <button type="button" onClick={() => void saveModel()} disabled={busy || !selectedLocalModel}>Save default model</button>
            <div className="admin-form-note">
              <strong>Current default</strong>
              <span>{status?.model?.local_model ?? "Not set"}</span>
              <span>Source: {status?.model?.selection_sources?.local ?? "environment"}</span>
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
              <h2>Local model server</h2>
              <p>
                {status?.model?.routing?.local.configured
                  ? "API key configured."
                  : "This server requires an API key. Add it below so the model can run."}
              </p>
              <p className="muted">{status?.model?.routing?.local.base_url ?? "http://127.0.0.1:1234/v1"}</p>
              <label>
                Model API key
                <input
                  type="password"
                  value={modelApiKey}
                  placeholder={status?.model?.api_key_configured ? "•••••••• (configured)" : "Paste the server API key"}
                  onChange={(event) => setModelApiKey(event.target.value)}
                />
              </label>
              <div className="model-key-actions">
                <button type="button" onClick={() => void saveModelApiKey()} disabled={busy || !modelApiKey.trim()}>Save API key</button>
                {status?.model?.api_key_configured ? (
                  <button type="button" className="secondary-action" onClick={() => void clearModelApiKey()} disabled={busy}>Remove</button>
                ) : null}
              </div>
            </section>
            <section className="source-setup-card model-routing-card">
              <div className="library-section-header">
                <div>
                  <p className="section-kicker">Per-agent models</p>
                  <h2>Model routing</h2>
                </div>
                <button type="button" onClick={() => void saveModelRoutes()} disabled={busy}>Save routes</button>
              </div>
              <p className="muted">Every agent runs on the local model server. Pick a specific model per agent, or leave it on the default.</p>
              <div className="model-route-list">
                {(status?.model?.routing?.agents ?? []).map((agent) => {
                  const route = modelRoutes[agent.id] ?? { model: "" };
                  return (
                    <article className="model-route-row" key={agent.id}>
                      <div>
                        <strong>{agent.label}</strong>
                        <p>{agent.description}</p>
                      </div>
                      <label>
                        Model
                        <select
                          value={route.model}
                          onChange={(event) => updateModelRoute(agent.id, { model: event.target.value })}
                        >
                          <option value="">Default</option>
                          {modelOptions.map((model) => <option key={`${agent.id}-${model.id}`} value={model.id}>{model.id}</option>)}
                        </select>
                      </label>
                      <span className="status-pill good">Default: {status?.model?.routing?.defaults?.local ?? "Local default"}</span>
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
                    <strong>
                      {route.route_name}
                      {route.backend ? <em>{route.backend}</em> : null}
                    </strong>
                    <span>{route.model}</span>
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
                  <h2>Model summary</h2>
                </div>
              </div>
              <div className="metrics-table">
                <div className="metrics-table-header">
                  <span>Model</span>
                  <span>Provider</span>
                  <span>Calls</span>
                  <span>Avg time</span>
                  <span>P95</span>
                  <span>Avg prompt tokens</span>
                  <span>Avg completion tokens</span>
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

      {tab === "reporting" ? (
        <ReportingTabContent
          selectedRunId={selectedRunId}
          onSelectRunId={handleSelectRunId}
          explorations={library.explorations}
        />
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
