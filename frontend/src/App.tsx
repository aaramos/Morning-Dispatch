import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";

import {
  api,
  requestStrategyRefinement,
  streamRefinement,
  streamStrategyRefinement,
  streamStrategyReview,
} from "./lib/api";
import type { GmailCandidatePayload, QueryEditTarget, RefinementStreamBody } from "./lib/api";
import {
  defaultBriefControls,
  defaultContentLimits,
  defaultSourceSelection,
} from "./lib/types";
import type {
  AdminStatus,
  BriefSettingsResponse,
  ConfirmationDraft,
  ConfirmedProfilePayload,
  Exploration,
  FlowState,
  RefinementProgress,
  RefinementProgressPhase,
  RefinementSession,
  SchedulePreset,
  SourceKey,
  SourceStatusResponse,
  StrategyPreview,
  TopicProfile,
  TopicProfileResponse,
} from "./lib/types";
import type { ActiveConversationSnapshot } from "./lib/drafts";
import {
  activeConversationTtlMs,
  clearActiveConversationDraft,
  clearInterestDraft,
  interestDraftTtlMs,
  loadActiveConversationDraft,
  loadInterestDraft,
  saveActiveConversationDraft,
  saveInterestDraft,
} from "./lib/drafts";
import { BrandMark } from "./components/BrandMark";
import { BuildStartingPanel } from "./components/BuildStartingPanel";
import { ScheduledDeliveryAlert } from "./components/ScheduledDeliveryAlert";
import { AdminApp } from "./components/admin/AdminApp";
import { BriefReadyPanel } from "./components/BriefReadyPanel";
import { ConfirmationPanel } from "./components/ConfirmationPanel";
import { EnableSourceModal } from "./components/EnableSourceModal";
import { ProgressPanel } from "./components/ProgressPanel";
import { RefinementPanel } from "./components/RefinementPanel";
import { SchedulePanel } from "./components/SchedulePanel";
import {
  briefPath,
  buildAttentionIssues,
  cleanSourceQueryRecord,
  deliveryFailuresFromStatus,
  draftFromProfile,
  emptyDraft,
  enabledSourceSelection,
  errorMessage,
  firstBlockedSelectedSource,
  hasEnabledSource,
  lookbackHoursForBuild,
  lookbackHoursForConfirmedDraft,
  mergeSourceSelections,
  openPath,
  releaseStamp,
  sleep,
  sourceSelectionFromRecord,
  splitList,
  strategyUpdateConfirmation,
  uniqueCleanList,
} from "./lib/appHelpers";


export default function App() {
  if (window.location.pathname === "/admin") {
    return <AdminApp />;
  }
  return <DispatchApp />;
}

function DispatchApp() {
  const [restoredConversation] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("refine_exploration") || params.has("refine_topic")) return null;
    return loadActiveConversationDraft();
  });
  const [sourceStatus, setSourceStatus] = useState<SourceStatusResponse | null>(null);
  const [sourceSelection, setSourceSelection] = useState<Record<SourceKey, boolean>>(
    () => restoredConversation?.sourceSelection ?? defaultSourceSelection,
  );
  const [statement, setStatement] = useState(() => restoredConversation?.statement ?? loadInterestDraft());
  const [submittedInterest, setSubmittedInterest] = useState(() => restoredConversation?.submittedInterest ?? "");
  const [session, setSession] = useState<RefinementSession | null>(() => restoredConversation?.session ?? null);
  const [answer, setAnswer] = useState(() => restoredConversation?.answer ?? "");
  const [topicProfile, setTopicProfile] = useState<TopicProfileResponse | null>(
    () => restoredConversation?.topicProfile ?? null,
  );
  const [draft, setDraft] = useState<ConfirmationDraft>(() => restoredConversation?.draft ?? emptyDraft());
  const [flow, setFlow] = useState<FlowState>(() => restoredConversation?.flow ?? "idle");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(() => (
    restoredConversation
      ? restoredConversation.flow === "confirm" ? "Conversation restored; confirm the brief setup" : "Conversation restored"
      : "Ready"
  ));
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
  const [deliveryConfigured, setDeliveryConfigured] = useState(false);
  const [emailSendReady, setEmailSendReady] = useState(false);
  const [briefEmailRecipient, setBriefEmailRecipient] = useState("");
  const [schedulePreset, setSchedulePreset] = useState<SchedulePreset>("daily");
  const [scheduleTime, setScheduleTime] = useState("08:00");
  const [emailOnSchedule, setEmailOnSchedule] = useState(false);
  const [refinementProgress, setRefinementProgress] = useState<RefinementProgress | null>(null);
  const [refinementFallbackStartedAt, setRefinementFallbackStartedAt] = useState(0);
  const [refinementTargetExplorationId, setRefinementTargetExplorationId] = useState<string | null>(
    () => restoredConversation?.refinementTargetExplorationId ?? null,
  );
  const [streamingText, setStreamingText] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [strategyStreamingText, setStrategyStreamingText] = useState("");
  const [strategyStreaming, setStrategyStreaming] = useState(false);
  const [strategyPreparingProposal, setStrategyPreparingProposal] = useState(false);
  const [strategyError, setStrategyError] = useState("");
  const [gmailCandidates, setGmailCandidates] = useState<GmailCandidatePayload | null>(null);
  const [briefSettings, setBriefSettings] = useState<BriefSettingsResponse | null>(null);
  const [adminStatus, setAdminStatus] = useState<AdminStatus | null>(null);
  const [strategyConfirmation, setStrategyConfirmation] = useState("");
  const [foreignRegionsDraft, setForeignRegionsDraft] = useState<string[]>(
    () => restoredConversation?.foreignRegionsDraft ?? [],
  );
  const [conversationActivityAt, setConversationActivityAt] = useState(() => Date.now());
  const [queuedStrategyRefinementTurns, setQueuedStrategyRefinementTurns] = useState(0);
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
  const [initialRefineTopicId] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const refineTopicId = params.get("refine_topic");
    if (refineTopicId) {
      params.delete("refine_topic");
      const nextUrl = `${window.location.pathname}${params.toString() ? `?${params}` : ""}`;
      window.history.replaceState(null, "", nextUrl);
    }
    return refineTopicId;
  });
  const [progressNow, setProgressNow] = useState(0);
  const activeRefinementStreams = useRef(0);
  const activeRefinementTurns = useRef(0);
  const refinementAnswerQueue = useRef<string[]>([]);
  const strategyRefinementQueue = useRef<string[]>([]);
  const buildRequestQueue = useRef(0);
  const [queuedRefinementTurns, setQueuedRefinementTurns] = useState(0);
  const [queuedBuildRequests, setQueuedBuildRequests] = useState(0);
  const buildBriefRef = useRef<() => void>(() => undefined);
  const backgroundBuildRef = useRef<{ id: string; status: Exploration["status"] } | null>(null);
  const [autoBuildRequest, setAutoBuildRequest] = useState(0);
  const recencyOverrideRef = useRef<Pick<ConfirmationDraft, "recency_weighting" | "lookback_hours"> | null>(
    restoredConversation?.draft.sourceScopeTouched
      ? {
        recency_weighting: restoredConversation.draft.recency_weighting,
        lookback_hours: restoredConversation.draft.lookback_hours,
      }
      : null,
  );

  const scheduledDeliveryFailures = useMemo(
    () => deliveryFailuresFromStatus(adminStatus, scheduledTopics),
    [adminStatus, scheduledTopics],
  );
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
  const canBuild = buildInterest.length > 0;
  const updateDraft = useCallback((nextDraft: ConfirmationDraft) => {
    setDraft((current) => {
      const recencyChanged = (
        nextDraft.sourceScopeTouched
        || nextDraft.recency_weighting !== current.recency_weighting
        || nextDraft.lookback_hours !== current.lookback_hours
      );
      if (recencyChanged) {
        recencyOverrideRef.current = {
          recency_weighting: nextDraft.recency_weighting,
          lookback_hours: nextDraft.lookback_hours,
        };
      }
      return nextDraft;
    });
  }, []);
  const draftWithStickyRecency = useCallback((
    profile: TopicProfile,
    defaults = defaultContentLimits,
    current?: ConfirmationDraft,
  ) => {
    const currentOverride = current?.sourceScopeTouched
      ? {
        recency_weighting: current.recency_weighting,
        lookback_hours: current.lookback_hours,
      }
      : null;
    const override = currentOverride ?? recencyOverrideRef.current;
    if (!override) return draftFromProfile(profile, defaults, current);
    return draftFromProfile(
      profile,
      defaults,
      {
        ...(current ?? emptyDraft(defaults)),
        recency_weighting: override.recency_weighting,
        lookback_hours: override.lookback_hours,
        sourceScopeTouched: true,
      },
    );
  }, []);
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
  const activeConversationSnapshot = useMemo<ActiveConversationSnapshot | null>(() => {
    if (flow === "building" || flow === "ready" || flow === "schedule") return null;
    const hasConversation = Boolean(
      flow === "refining"
      || flow === "confirm"
      || session
      || topicProfile
      || submittedInterest.trim()
      || answer.trim(),
    );
    if (!hasConversation) return null;
    return {
      flow: flow === "confirm" ? "confirm" : "refining",
      statement,
      submittedInterest,
      session,
      answer,
      topicProfile,
      draft,
      sourceSelection,
      foreignRegionsDraft,
      refinementTargetExplorationId,
    };
  }, [
    answer,
    draft,
    flow,
    foreignRegionsDraft,
    refinementTargetExplorationId,
    session,
    sourceSelection,
    statement,
    submittedInterest,
    topicProfile,
  ]);

  const loadStatics = useCallback(async () => {
    const [sources, , admin, settings] = await Promise.all([
      api<SourceStatusResponse>("/api/explore/source-status").catch(() => null),
      api<TopicProfileResponse[]>("/api/explore/topic-profiles").catch(() => []),
      api<AdminStatus>("/api/admin/status").catch(() => null),
      api<BriefSettingsResponse>("/api/admin/brief-settings").catch(() => null),
    ]);
    if (sources) setSourceStatus(sources);
    if (admin) setAdminStatus(admin);
    if (settings) setBriefSettings(settings);
    const email = admin?.delivery?.email;
    const configured = Boolean(email?.enabled && email.recipient_email && email.gmail_send_ready !== false);
    const sendReady = Boolean(email?.gmail_send_ready);
    setDeliveryConfigured(configured);
    setEmailSendReady(sendReady);
    if (email?.recipient_email) setBriefEmailRecipient((current) => current || String(email.recipient_email));
    setEmailOnSchedule(configured);
  }, []);

  const loadBuildState = useCallback(async (refreshStaticsOnCompletion = false) => {
    const [explorations, scheduled] = await Promise.all([
      api<Exploration[]>("/api/explore/explorations?limit=25").catch(() => []),
      api<TopicProfileResponse[]>("/api/explore/scheduled-topic-profiles").catch(() => []),
    ]);
    setRecentExplorations(explorations);
    setScheduledTopics(scheduled);

    const previous = backgroundBuildRef.current;
    const active = explorations.find((item) => item.status === "queued" || item.status === "running") ?? null;
    const previousNow = previous
      ? explorations.find((item) => item.exploration_id === previous.id) ?? null
      : null;
    const previousFinished = Boolean(
      previous
      && (!previousNow || previousNow.status === "complete" || previousNow.status === "failed"),
    );
    backgroundBuildRef.current = active ? { id: active.exploration_id, status: active.status } : null;
    if (refreshStaticsOnCompletion && previousFinished) {
      await loadStatics();
    }
  }, [loadStatics]);

  const loadHome = useCallback(async () => {
    await Promise.all([loadStatics(), loadBuildState()]);
  }, [loadBuildState, loadStatics]);

  useEffect(() => {
    void loadHome();
  }, [loadHome]);

  useEffect(() => {
    if (!backgroundBuild) return;
    const timer = window.setInterval(() => {
      void loadBuildState(true);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [backgroundBuild, loadBuildState]);

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
    if (!activeConversationSnapshot) return;
    saveActiveConversationDraft(activeConversationSnapshot);
  }, [activeConversationSnapshot, conversationActivityAt]);

  useEffect(() => {
    if (!activeConversationSnapshot) return;
    const persistConversation = () => saveActiveConversationDraft(activeConversationSnapshot);
    const persistHiddenConversation = () => {
      if (document.visibilityState === "hidden") persistConversation();
    };
    window.addEventListener("pagehide", persistConversation);
    document.addEventListener("visibilitychange", persistHiddenConversation);
    return () => {
      window.removeEventListener("pagehide", persistConversation);
      document.removeEventListener("visibilitychange", persistHiddenConversation);
    };
  }, [activeConversationSnapshot]);

  useEffect(() => {
    if (!activeConversationSnapshot) return;
    const markConversationActive = () => setConversationActivityAt(Date.now());
    window.addEventListener("focus", markConversationActive);
    window.addEventListener("pointerdown", markConversationActive);
    window.addEventListener("keydown", markConversationActive);
    return () => {
      window.removeEventListener("focus", markConversationActive);
      window.removeEventListener("pointerdown", markConversationActive);
      window.removeEventListener("keydown", markConversationActive);
    };
  }, [activeConversationSnapshot]);

  useEffect(() => {
    if (!activeConversationSnapshot) return;
    if (busy || streaming || activeRefinementProgress || strategyStreaming || strategyPreparingProposal) {
      setConversationActivityAt(Date.now());
    }
  }, [
    activeConversationSnapshot,
    activeRefinementProgress,
    busy,
    strategyPreparingProposal,
    strategyStreaming,
    streaming,
  ]);

  useEffect(() => {
    if (!activeConversationSnapshot) return;
    if (busy || streaming || activeRefinementProgress || strategyStreaming || strategyPreparingProposal) return;
    const timeRemaining = Math.max(0, activeConversationTtlMs - (Date.now() - conversationActivityAt));
    const timer = window.setTimeout(() => {
      clearActiveConversationDraft();
      clearInterestDraft();
      recencyOverrideRef.current = null;
      setStrategyConfirmation("");
      setRefinementTargetExplorationId(null);
      setStatement("");
      setSubmittedInterest("");
      setSession(null);
      setTopicProfile(null);
      setDraft(emptyDraft());
      setAnswer("");
      setFlow("idle");
      setMessage("Conversation cleared after 15 minutes of inactivity");
    }, timeRemaining);
    return () => window.clearTimeout(timer);
  }, [
    activeConversationSnapshot,
    activeRefinementProgress,
    busy,
    conversationActivityAt,
    strategyPreparingProposal,
    strategyStreaming,
    streaming,
  ]);

  useEffect(() => {
    if (!session) return;
    setDraft((current) => draftWithStickyRecency(session.profile, defaultControls.content_limits, current));
    setForeignRegionsDraft(session.profile.foreign_regions ?? []);
  }, [defaultControls.content_limits, draftWithStickyRecency, session]);

  useEffect(() => {
    if (session || !topicProfile) return;
    setForeignRegionsDraft(topicProfile.profile.foreign_regions ?? []);
  }, [session, topicProfile]);

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

  function beginRefinementTurn() {
    activeRefinementTurns.current += 1;
    setBusy(true);
  }

  function endRefinementTurn() {
    activeRefinementTurns.current = Math.max(0, activeRefinementTurns.current - 1);
    if (activeRefinementTurns.current === 0) {
      setBusy(false);
    }
  }

  const beginLiveRefinementStream = useCallback(() => {
    activeRefinementStreams.current += 1;
    if (activeRefinementStreams.current === 1) {
      setStreamingText("");
    }
    setStreaming(true);
  }, []);

  const endLiveRefinementStream = useCallback(() => {
    activeRefinementStreams.current = Math.max(0, activeRefinementStreams.current - 1);
    if (activeRefinementStreams.current === 0) {
      setStreaming(false);
      setStreamingText("");
    }
  }, []);

  function queueRefinementMessage(message: string) {
    const clean = message.trim();
    if (!clean) return;
    refinementAnswerQueue.current.push(clean);
    setQueuedRefinementTurns(refinementAnswerQueue.current.length);
  }

  function queueStrategyRefinementMessage(message: string) {
    const clean = message.trim();
    if (!clean) return;
    strategyRefinementQueue.current.push(clean);
    setQueuedStrategyRefinementTurns(strategyRefinementQueue.current.length);
  }

  function shiftQueuedRefinementMessage(): string | null {
    const next = refinementAnswerQueue.current.shift() ?? null;
    if (!next) return null;
    setQueuedRefinementTurns(refinementAnswerQueue.current.length);
    return next;
  }

  function shiftQueuedStrategyRefinementMessage(): string | null {
    const next = strategyRefinementQueue.current.shift() ?? null;
    if (!next) return null;
    setQueuedStrategyRefinementTurns(strategyRefinementQueue.current.length);
    return next;
  }

  function queueBuildRequest() {
    buildRequestQueue.current += 1;
    setQueuedBuildRequests(buildRequestQueue.current);
    setMessage("Build brief queued. It will start when the current AI turn finishes.");
  }

  function shiftQueuedBuildRequest(): boolean {
    if (buildRequestQueue.current <= 0) return false;
    buildRequestQueue.current = Math.max(0, buildRequestQueue.current - 1);
    setQueuedBuildRequests(buildRequestQueue.current);
    return true;
  }

  function updateSearchQuery(target: QueryEditTarget, nextValue: string | null) {
    const applyEdit = (profile: TopicProfile): TopicProfile => {
      const nextProfile: TopicProfile = {
        ...profile,
        search_queries: [...(profile.search_queries ?? [])],
        source_queries: { ...(profile.source_queries ?? {}) },
      };
      if (target.kind === "general") {
        const queries = [...(nextProfile.search_queries ?? [])];
        if (nextValue === null) {
          queries.splice(target.index, 1);
        } else {
          queries[target.index] = nextValue;
        }
        nextProfile.search_queries = queries;
      } else {
        const sourceQueries = [...(nextProfile.source_queries?.[target.sourceKey] ?? [])];
        if (nextValue === null) {
          sourceQueries.splice(target.index, 1);
        } else {
          sourceQueries[target.index] = nextValue;
        }
        nextProfile.source_queries = {
          ...(nextProfile.source_queries ?? {}),
          [target.sourceKey]: sourceQueries,
        };
      }
      return nextProfile;
    };

    const applyPreviewEdit = (preview: StrategyPreview | undefined): StrategyPreview | undefined => {
      if (!preview) return preview;
      if (target.kind === "general") {
        const queries = [...preview.search_queries];
        if (nextValue === null) {
          queries.splice(target.index, 1);
        } else {
          queries[target.index] = nextValue;
        }
        return { ...preview, search_queries: queries };
      }
      return {
        ...preview,
        per_source: preview.per_source.map((source) => {
          if (source.key !== target.sourceKey) return source;
          const queries = [...source.queries];
          if (nextValue === null) {
            queries.splice(target.index, 1);
          } else {
            queries[target.index] = nextValue;
          }
          return { ...source, queries };
        }),
      };
    };

    setSession((current) => {
      if (!current) return current;
      const nextProfile = applyEdit(current.profile);
      const pending = current.pending_strategy_refinement
        ? {
          ...current.pending_strategy_refinement,
          proposed_profile: applyEdit(current.pending_strategy_refinement.proposed_profile),
          strategy_preview: applyPreviewEdit(current.pending_strategy_refinement.strategy_preview),
        }
        : current.pending_strategy_refinement;
      return {
        ...current,
        profile: nextProfile,
        pending_strategy_refinement: pending,
        strategy_preview: applyPreviewEdit(current.strategy_preview),
      };
    });
    setTopicProfile((current) => (
      current ? { ...current, profile: applyEdit(current.profile) } : current
    ));
  }

  function updateForeignRegions(nextRegions: string[]) {
    const cleanRegions = uniqueCleanList(nextRegions);
    setForeignRegionsDraft(cleanRegions);
    const applyRegions = (profile: TopicProfile): TopicProfile => ({
      ...profile,
      foreign_regions: cleanRegions,
    });
    const applyPreview = (preview: StrategyPreview | undefined): StrategyPreview | undefined => (
      preview
        ? {
          ...preview,
          reasoning_summary: preview.reasoning_summary,
        }
        : preview
    );
    setSession((current) => {
      if (!current) return current;
      const pending = current.pending_strategy_refinement
        ? {
          ...current.pending_strategy_refinement,
          proposed_profile: applyRegions(current.pending_strategy_refinement.proposed_profile),
          strategy_preview: applyPreview(current.pending_strategy_refinement.strategy_preview),
        }
        : current.pending_strategy_refinement;
      return {
        ...current,
        profile: applyRegions(current.profile),
        pending_strategy_refinement: pending,
        strategy_preview: applyPreview(current.strategy_preview),
      };
    });
    setTopicProfile((current) => (
      current ? { ...current, profile: applyRegions(current.profile) } : current
    ));
  }

  // Drives one AI-led streaming turn: streams prose into the live bubble, applies the
  // plan snapshot, and advances the flow. Returns the final session (or null on error).
  const runRefinementStream = useCallback(async function runRefinementStream(
    body: RefinementStreamBody,
    optimisticUser?: string,
  ): Promise<RefinementSession | null> {
    beginLiveRefinementStream();
    if (optimisticUser) {
      setSession((prev) =>
        prev ? { ...prev, messages: [...prev.messages, { role: "user", content: optimisticUser }] } : prev,
      );
    }
    let live = "";
    let finalSession: RefinementSession | null = null;
    let liveSessionId = body.session_id ?? null;
    let ready = false;
    let triggerBuild = false;
    let streamError = "";
    try {
      await streamRefinement(body, (event) => {
        if (event.type === "session") {
          liveSessionId = event.session_id;
        } else if (event.type === "token") {
          live += event.text;
          setStreamingText(live);
        } else if (event.type === "strategy") {
          // Reflect strategy edits in the side panel mid-stream, before the turn (and
          // its slower finalize-time model calls) completes. On the very first turn no
          // session exists yet, so seed a minimal one that carries just the preview.
          const preview = event.strategy_preview;
          setSession((prev) =>
            prev
              ? { ...prev, strategy_preview: preview }
              : {
                  session_id: liveSessionId ?? "",
                  statement: body.statement ?? body.answer ?? "",
                  status: "active",
                  turn_count: 0,
                  messages: [],
                  profile: {} as TopicProfile,
                  topic_id: null,
                  strategy_preview: preview,
                },
          );
        } else if (event.type === "plan") {
          finalSession = event.session;
        } else if (event.type === "done") {
          finalSession = event.session;
          ready = event.ready;
          triggerBuild = event.trigger_build === true;
        } else if (event.type === "gmail_candidates") {
          // Pause the stream; render the approval UI — next user turn is the approval reply.
          setGmailCandidates({
            candidates: event.candidates,
            intro: event.intro,
            criteria: event.criteria,
            search_phrase: event.search_phrase,
            lookback_hours: event.lookback_hours,
          });
        } else if (event.type === "gmail_approved") {
          setGmailCandidates(null);
        } else if (event.type === "error") {
          streamError = event.message;
        }
      });
    } catch (error) {
      endLiveRefinementStream();
      throw error;
    }
    endLiveRefinementStream();
    if (!finalSession) {
      throw new Error(streamError || "Refinement stream ended unexpectedly");
    }
    const resolved: RefinementSession = finalSession;
    // The backend is the single source of truth for the ordered chat transcript. The
    // optimistic user bubble we showed during streaming is discarded here in favor of
    // the server's persisted messages, which already include this turn's user + AI
    // messages in order. Merging the two produced out-of-order / duplicated turns
    // (notably for "Build brief", where the optimistic and persisted text differ).
    setSession(resolved);
    setDraft((current) => draftWithStickyRecency(resolved.profile, defaultControls.content_limits, current));
    setSourceSelection((current) => mergeSourceSelections(sourceSelectionFromRecord(resolved.profile.source_selection), current));
    if (resolved.topic_profile) setTopicProfile(resolved.topic_profile);
    const finalized = resolved.status === "finalized" || ready;
    setFlow(finalized ? "confirm" : "refining");
    setMessage(triggerBuild ? "Building the brief..." : finalized ? "Confirm the brief setup" : "Your turn");
    if (triggerBuild) setAutoBuildRequest((value) => value + 1);
    return resolved;
  }, [beginLiveRefinementStream, defaultControls.content_limits, draftWithStickyRecency, endLiveRefinementStream]);

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
    setSession(null);
    setFlow("refining");
    setBusy(true);
    setMessage("Starting the conversation...");
    setBriefHtml("");
    setExploration(null);
    try {
      await runRefinementStream({
        statement: interest,
        source_selection: selectedEnabledSources,
        foreign_regions: foreignRegionsDraft,
        recency_weighting: draft.recency_weighting,
        lookback_hours: draft.lookback_hours,
        source_scope_touched: draft.sourceScopeTouched === true,
        models: {},
      });
    } catch (error) {
      setStatement(interest);
      saveInterestDraft(interest);
      setSubmittedInterest("");
      setFlow("idle");
      setMessage(errorMessage(error, "Could not start refinement"));
    } finally {
      setBusy(false);
    }
  }

  const answerRefinement = useCallback(async (justGoNow = false, answerOverride?: string) => {
    if (!activeInterest) return;
    const effectiveAnswer = (answerOverride ?? answer).trim();
    if (!session && !justGoNow && !effectiveAnswer) return;
    if (!justGoNow && !effectiveAnswer) return;
    if (busy || streaming || activeRefinementTurns.current > 0) {
      if (!justGoNow && effectiveAnswer) {
        queueRefinementMessage(effectiveAnswer);
        setMessage("Reply queued while model is working.");
        setAnswer("");
      }
      return;
    }
    const pendingAnswer = effectiveAnswer;
    setAnswer("");
    beginRefinementTurn();
    setMessage(justGoNow ? "Confirming search strategy..." : "Thinking...");
    try {
      await runRefinementStream(
        {
          session_id: session?.session_id ?? null,
          statement: activeInterest,
          source_selection: selectedEnabledSources,
          foreign_regions: foreignRegionsDraft,
          recency_weighting: draft.recency_weighting,
          lookback_hours: draft.lookback_hours,
          source_scope_touched: draft.sourceScopeTouched === true,
          answer: justGoNow ? "" : pendingAnswer,
          just_go_now: justGoNow,
          models: {},
        },
        justGoNow ? "Build brief requested." : pendingAnswer,
      );
    } catch (error) {
      if (!justGoNow && pendingAnswer) setAnswer(pendingAnswer);
      setMessage(errorMessage(error, "Could not update refinement"));
    } finally {
      endRefinementTurn();
    }
  }, [
    activeInterest,
    answer,
    busy,
    draft.lookback_hours,
    draft.recency_weighting,
    draft.sourceScopeTouched,
    foreignRegionsDraft,
    runRefinementStream,
    selectedEnabledSources,
    session,
    streaming,
  ]);

  useEffect(() => {
    const canProcessRefinementQueue = !busy && !streaming && activeRefinementTurns.current === 0;
    if (!canProcessRefinementQueue || queuedRefinementTurns === 0) return;
    const timer = window.setTimeout(() => {
      const queuedMessage = shiftQueuedRefinementMessage();
      if (!queuedMessage) return;
      void answerRefinement(false, queuedMessage);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [queuedRefinementTurns, streaming, busy, answerRefinement]);

  const refineSearchStrategy = useCallback(async (instruction: string, fromQueue = false) => {
    const cleanInstruction = instruction.trim();
    if (!cleanInstruction) return;
    if (!fromQueue && (busy || strategyStreaming || strategyPreparingProposal)) {
      queueStrategyRefinementMessage(cleanInstruction);
      setMessage("Strategy update queued while model is working.");
      return;
    }
    if (fromQueue && (busy || strategyStreaming || strategyPreparingProposal)) {
      queueStrategyRefinementMessage(cleanInstruction);
      return;
    }
    const baseStatement = activeInterest || topicProfile?.statement || session?.statement || "";
    if (!baseStatement) {
      const message = "I do not have an active brief plan to refine. Close this and start from the brief interest again.";
      setStrategyError(message);
      setMessage(message);
      return;
    }
    setBusy(true);
    setStrategyStreaming(true);
    setStrategyStreamingText("");
    setStrategyError("");
    setStrategyPreparingProposal(false);
    setMessage("Asking AI to review your strategy...");
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

      let liveText = "";
      let finalSession: RefinementSession | null = null;
      let hasProposal = false;

      try {
        await streamStrategyRefinement(currentSession.session_id, cleanInstruction, (event) => {
          if (event.type === "token") {
            liveText += event.text;
            setStrategyStreamingText(liveText);
          } else if (event.type === "proposal") {
            // Prose streamed; now running critique pass — show shimmer.
            setStrategyStreamingText("");
            setStrategyPreparingProposal(true);
            finalSession = event.session;
          } else if (event.type === "done") {
            finalSession = event.session;
            hasProposal = event.has_proposal;
            setStrategyPreparingProposal(false);
          } else if (event.type === "error") {
            throw new Error(event.message || "AI strategy refinement failed before returning a proposal.");
          }
        });
      } catch (streamError) {
        setStrategyStreaming(false);
        setStrategyStreamingText("");
        setStrategyPreparingProposal(true);
        setMessage("Streaming failed; retrying the AI request directly...");
        try {
          finalSession = await requestStrategyRefinement(currentSession.session_id, cleanInstruction);
          hasProposal = Boolean(finalSession.pending_strategy_refinement);
        } catch (fallbackError) {
          throw new Error(
            errorMessage(fallbackError, errorMessage(streamError, "AI strategy refinement failed")),
            { cause: fallbackError },
          );
        } finally {
          setStrategyPreparingProposal(false);
        }
      }

      if (finalSession) {
        const resolved: RefinementSession = finalSession;
        setSession(resolved);
        const proposal = resolved.pending_strategy_refinement?.proposed_profile;
        if (proposal) setDraft((current) => draftWithStickyRecency(proposal, defaultControls.content_limits, current));
        if (resolved.topic_profile) setTopicProfile(resolved.topic_profile);
        setFlow("confirm");
        if (hasProposal) {
          setStrategyConfirmation(resolved.pending_strategy_refinement?.assistant_response || "AI prepared a proposed strategy update for review.");
          setMessage("Review the proposed strategy update");
        } else {
          setStrategyConfirmation(resolved.strategy_review?.assistant_response || "Strategy looks good — no changes needed.");
          setMessage("Strategy review complete");
        }
      } else {
        throw new Error("AI strategy refinement finished without returning a proposal.");
      }
    } catch (error) {
      const message = errorMessage(error, "Could not update search strategy");
      setStrategyError(message);
      setMessage(message);
    } finally {
      setBusy(false);
      setStrategyStreaming(false);
      setStrategyStreamingText("");
      setStrategyPreparingProposal(false);
    }
  }, [activeInterest, busy, defaultControls.content_limits, draftWithStickyRecency, selectedEnabledSources, session, strategyPreparingProposal, strategyStreaming, topicProfile?.statement, topicProfile?.topic_id]);

  useEffect(() => {
    const canProcessStrategyQueue = !busy && !strategyStreaming && !strategyPreparingProposal;
    if (!canProcessStrategyQueue || queuedStrategyRefinementTurns === 0) return;
    const timer = window.setTimeout(() => {
      const queuedMessage = shiftQueuedStrategyRefinementMessage();
      if (!queuedMessage) return;
      void refineSearchStrategy(queuedMessage, true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [busy, queuedStrategyRefinementTurns, strategyPreparingProposal, strategyStreaming, refineSearchStrategy]);

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
      setDraft((current) => draftWithStickyRecency(updated.profile, defaultControls.content_limits, current));
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
      lookback_hours: lookbackHours,
      exclusions: splitList(draftOverride.exclusions),
      must_have_terms: splitList(draftOverride.must_have),
      must_have_aliases: baseProfile?.must_have_aliases ?? {},
      source_selection: selectedEnabledSources,
      requested_sources: baseProfile?.requested_sources ?? [],
      subtopics: baseProfile?.subtopics ?? [],
      keywords: baseProfile?.keywords ?? [],
      foreign_regions: baseProfile?.foreign_regions ?? foreignRegionsDraft,
      search_queries: uniqueCleanList(baseProfile?.search_queries ?? []),
      source_queries: cleanSourceQueryRecord(baseProfile?.source_queries),
      direct_episode_queries: uniqueCleanList(baseProfile?.direct_episode_queries ?? []),
      related_episode_queries: uniqueCleanList(baseProfile?.related_episode_queries ?? []),
      negative_constraints: uniqueCleanList(baseProfile?.negative_constraints ?? []),
      priority_terms: uniqueCleanList(baseProfile?.priority_terms ?? []),
      gmail_rules: baseProfile?.gmail_rules ?? {},
      models: {},
      schedule: baseProfile?.schedule ?? null,
      schedule_config: baseProfile?.schedule_config ?? {},
      delivery_config: baseProfile?.delivery_config ?? {},
      candidate_limit: draftOverride.content_limits.total_items,
      content_limits: draftOverride.content_limits,
    };
  }

  // Persist the confirmed profile so podcast show-discovery can run against the
  // latest queries + selection, returning a topic_id for the show picker.
  async function ensurePodcastTopicId(): Promise<string | null> {
    const existing = topicProfile?.topic_id ?? session?.topic_id ?? null;
    try {
      const payload = { ...confirmedProfilePayload(draft), source_selection: selectedEnabledSources };
      const saved = await api<TopicProfileResponse>("/api/explore/topic-profiles", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (saved?.topic_id) {
        setTopicProfile(saved);
        return saved.topic_id;
      }
    } catch (error) {
      setMessage(errorMessage(error, "Could not save the topic for show discovery"));
    }
    return existing;
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

    profilePayload.refinement_session_id = currentSession.session_id;
    let finalSession: RefinementSession | null = null;
    let hasProposal = false;

    setStrategyStreaming(true);
    setStrategyStreamingText("");

    try {
      await streamStrategyReview(
        currentSession.session_id,
        profilePayload as Record<string, unknown>,
        (event) => {
          if (event.type === "token") {
            setStrategyStreamingText((prev) => prev + event.text);
          } else if (event.type === "proposal") {
            finalSession = event.session;
          } else if (event.type === "done") {
            finalSession = event.session;
            hasProposal = event.has_proposal;
          } else if (event.type === "error") {
            throw new Error(event.message || "AI strategy review failed before returning a proposal.");
          }
        },
      );
    } finally {
      setStrategyStreaming(false);
      setStrategyStreamingText("");
    }

    if (!finalSession) return true;
    const resolved: RefinementSession = finalSession;
    setSession(resolved);

    const proposal = resolved.pending_strategy_refinement?.proposed_profile;
    if (proposal && hasProposal) {
      setDraft((current) => draftWithStickyRecency(proposal, defaultControls.content_limits, current));
      if (resolved.topic_profile) setTopicProfile(resolved.topic_profile);
      setFlow("confirm");
      setStrategyConfirmation(
        resolved.pending_strategy_refinement?.assistant_response
          || "AI found strategy changes to review before building."
      );
      setMessage("Review the AI strategy proposal before building");
      return false;
    }
    if (resolved.strategy_review?.assistant_response) {
      setStrategyConfirmation(resolved.strategy_review.assistant_response);
    }
    if (resolved.topic_profile) setTopicProfile(resolved.topic_profile);
    return true;
  }

  async function buildBrief() {
    if (!canBuild) return;
    const buildHasPendingStrategy = Boolean(session?.pending_strategy_refinement);
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
      let strategyReady = true;
      if (!buildHasPendingStrategy) {
        try {
          strategyReady = await reviewSearchStrategyBeforeBuild(profilePayload);
        } catch (error) {
          setStrategyConfirmation(`${errorMessage(error, "AI strategy review was unavailable")}. Continuing with the current confirmed strategy.`);
          setMessage("Strategy review unavailable; building with current strategy");
        }
      } else {
        setStrategyConfirmation("Building with the proposed strategy update.");
        setMessage("Building with proposed strategy");
      }
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

  useEffect(() => {
    buildBriefRef.current = () => {
      void buildBrief();
    };
  });

  function requestBuildBrief() {
    if (!canBuild) return;
    if (busy || streaming || activeRefinementTurns.current > 0 || strategyStreaming || strategyPreparingProposal) {
      queueBuildRequest();
      return;
    }
    void buildBrief();
  }

  useEffect(() => {
    const canProcessBuildQueue = (
      queuedBuildRequests > 0
      && canBuild
      && !busy
      && !streaming
      && activeRefinementTurns.current === 0
      && !strategyStreaming
      && !strategyPreparingProposal
    );
    if (!canProcessBuildQueue) return;
    const timer = window.setTimeout(() => {
      if (!shiftQueuedBuildRequest()) return;
      buildBriefRef.current();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [busy, canBuild, queuedBuildRequests, strategyPreparingProposal, strategyStreaming, streaming]);

  useEffect(() => {
    if (!autoBuildRequest) return;
    if (flow !== "confirm" || busy || streaming) return;
    setAutoBuildRequest(0);
    buildBriefRef.current();
  }, [autoBuildRequest, busy, flow, streaming]);

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
      recencyOverrideRef.current = null;
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

  async function stopExploration(record = exploration) {
    if (!record || (record.status !== "queued" && record.status !== "running")) return;
    setBusy(true);
    setMessage("Stopping the build...");
    try {
      const result = await api<{ status: string; exploration: Exploration }>(
        `/api/explore/explorations/${record.exploration_id}/cancel`,
        { method: "POST" },
      );
      setExploration(result.exploration);
      await loadHome();
      setFlow("building");
      setMessage("Build stopped.");
    } catch (error) {
      setMessage(errorMessage(error, "Could not stop the build"));
    } finally {
      setBusy(false);
    }
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


  function resetForNewBrief() {
    clearActiveConversationDraft();
    clearInterestDraft();
    recencyOverrideRef.current = null;
    setConversationActivityAt(Date.now());
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
    setSourceSelection((current) => {
      const nextSelection = { ...current, [key]: !current[key] };
      const nextSourceSelection = enabledSourceSelection(nextSelection, sourceStatus);
      setSession((existing) => existing ? {
        ...existing,
        profile: { ...existing.profile, source_selection: nextSourceSelection },
      } : existing);
      setTopicProfile((existing) => existing ? {
        ...existing,
        profile: { ...existing.profile, source_selection: nextSourceSelection },
      } : existing);
      return nextSelection;
    });
  }

  async function refreshSourcesAndSelect(key: SourceKey) {
    const status = await api<SourceStatusResponse>("/api/explore/source-status");
    setSourceStatus(status);
    if (status.sources[key]?.enabled) {
      setSourceSelection((current) => ({ ...current, [key]: true }));
      setEnableSource(null);
    }
    await loadStatics();
  }

  async function saveWebKey() {
    if (!webKey.trim()) return;
    setBusy(true);
    try {
      await api("/api/admin/web-search/credentials", {
        method: "POST",
        body: JSON.stringify({ provider: "serper", api_key: webKey.trim() }),
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
        recencyOverrideRef.current = null;
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

  useEffect(() => {
    if (!initialRefineTopicId) return;
    const topicId = initialRefineTopicId;
    let cancelled = false;
    setBusy(true);
    setFlow("refining");
    setMessage("Loading cloned strategy to refine...");
    const now = Date.now();
    setRefinementFallbackStartedAt(now);
    setProgressNow(now);
    setRefinementProgress({ phase: "starting", label: "Opening cloned strategy", startedAt: now });
    void (async () => {
      try {
        const topic = await api<TopicProfileResponse>(`/api/explore/topic-profiles/${topicId}`);
        const nextSourceSelection = sourceSelectionFromRecord(topic.profile.source_selection);
        const nextSession = await api<RefinementSession>("/api/explore/refinement-sessions", {
          method: "POST",
          body: JSON.stringify({
            statement: topic.statement,
            topic_id: topic.topic_id,
            revisit: true,
            source_selection: nextSourceSelection,
            models: {},
          }),
        });
        if (cancelled) return;
        clearInterestDraft();
        setExploration(null);
        setRefinementTargetExplorationId(null);
        setTopicProfile(topic);
        setSubmittedInterest(topic.statement);
        setStatement("");
        setSourceSelection(nextSourceSelection);
        setSession(nextSession);
        recencyOverrideRef.current = null;
        setDraft(draftFromProfile(nextSession.profile));
        setAnswer("");
        setBriefHtml("");
        setFlow(nextSession.status === "finalized" ? "confirm" : "refining");
        setMessage(nextSession.status === "finalized" ? "Confirm the cloned strategy" : "Refine the cloned strategy");
      } catch (error) {
        if (!cancelled) setMessage(errorMessage(error, "Could not load cloned strategy"));
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
  }, [initialRefineTopicId]);

  // Retained for the legacy confirmation panel while the unified refinement UI rolls out.
  void strategyStreamingText;
  void strategyStreaming;
  void strategyPreparingProposal;
  void strategyError;
  void strategyConfirmation;
  void refineSearchStrategy;
  void confirmStrategyRefinement;

  return (
    <main className="dispatch-page">
      <section className="dispatch-frame">
        <header className="dispatch-header">
          <a className="brand-lockup" href="/" aria-label="Dispatch home">
            <BrandMark />
            <span>Dispatch</span>
          </a>
          <span className="release-stamp">{releaseStamp(adminStatus)}</span>
          <a className="icon-menu" href="/admin" aria-label="Open Admin">•••</a>
        </header>

        <section className="dispatch-body">
          {scheduledDeliveryFailures.length ? (
            <ScheduledDeliveryAlert failures={scheduledDeliveryFailures} />
          ) : null}

          {flow === "idle" || flow === "refining" || flow === "confirm" || streaming ? (
            <RefinementPanel
              flow={flow === "idle" || flow === "confirm" ? flow : "refining"}
              session={session}
              interest={submittedInterest || statement}
              profile={session?.profile ?? topicProfile?.profile ?? null}
              draft={draft}
              foreignRegions={session?.profile.foreign_regions ?? topicProfile?.profile.foreign_regions ?? foreignRegionsDraft}
              sourceSelection={sourceSelection}
              answer={answer}
              busy={busy}
              streaming={streaming}
              streamingText={streamingText}
              refinementProgress={activeRefinementProgress}
              progressNow={progressNow}
              gmailCandidates={gmailCandidates}
              queuedRefinementTurns={queuedRefinementTurns}
              queuedBuildRequests={queuedBuildRequests}
              onAnswerChange={setAnswer}
              onSend={() => void answerRefinement(false)}
              onGmailApprove={(approvedSenders, instructions) => {
                let reply = "none";
                if (!approvedSenders.length) {
                  setSourceSelection((current) => ({ ...current, gmail: false }));
                }
                if (approvedSenders.length) {
                  reply = `Approved: ${approvedSenders.join(", ")}`;
                  if (instructions.trim()) {
                    reply += `\nInstructions: ${instructions.trim()}`;
                  }
                }
                void answerRefinement(false, reply);
              }}
              statement={statement}
              onStatementChange={setStatement}
              onDraftChange={updateDraft}
              onForeignRegionsChange={updateForeignRegions}
              sourceStatus={sourceStatus}
              sourceLocked={sourceLocked}
              onSourceToggle={updateSource}
              onSearchQueryEdit={updateSearchQuery}
              onBuild={requestBuildBrief}
              onEnsurePodcastTopicId={ensurePodcastTopicId}
              canSubmitInterest={canSubmitInterest}
              onSubmitInterest={(event) => {
                if (event) event.preventDefault();
                void startFlow();
              }}
            />
          ) : null}

          {flow === "building" && !exploration ? (
            <BuildStartingPanel />
          ) : null}

          {visibleBuild ? (
            <ProgressPanel
              exploration={visibleBuild}
              sourceSelection={visibleBuild.source_selection ?? selectedEnabledSources}
              onStop={() => void stopExploration(visibleBuild)}
              stopping={busy}
            />
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
    </main>
  );
}





void ConfirmationPanel;














































































