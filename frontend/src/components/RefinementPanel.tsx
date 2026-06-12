import { useEffect, useRef } from "react";
import type { FormEvent } from "react";
import type { GmailCandidatePayload, QueryEditTarget } from "../lib/api";
import type { ConfirmationDraft, RefinementProgress, RefinementSession, SourceKey, SourceStatusResponse, TopicProfile } from "../lib/types";
import { sourceOptions } from "../lib/types";
import { formatElapsed, recencyText, refinementProgressState, sourceScopeFromLookbackHours, uniqueCleanList } from "../lib/appHelpers";
import { ChatMessageContent } from "./ChatMessageContent";
import { EditablePlanQuery } from "./EditablePlanQuery";
import { ForeignRegionPicker } from "./ForeignRegionPicker";
import { GmailApprovalCard } from "./GmailApprovalCard";
import { PodcastShowPicker } from "./PodcastShowPicker";
import { RecencyControl } from "./RecencyControl";
import { SourceChips } from "./SourceChips";

export function RefinementPanel(props: {
  flow: "idle" | "refining" | "confirm";
  session: RefinementSession | null;
  interest: string;
  profile: TopicProfile | null;
  draft: ConfirmationDraft;
  foreignRegions: string[];
  sourceSelection: Record<string, boolean>;
  answer: string;
  busy: boolean;
  streaming: boolean;
  streamingText: string;
  refinementProgress: RefinementProgress | null;
  progressNow: number;
  gmailCandidates: GmailCandidatePayload | null;
  queuedRefinementTurns: number;
  queuedBuildRequests: number;
  onAnswerChange: (value: string) => void;
  onSend: () => void;
  onGmailApprove: (approvedSenders: string[], instructions: string) => void;
  // Starting Flow props
  statement: string;
  onStatementChange: (value: string) => void;
  onDraftChange: (draft: ConfirmationDraft) => void;
  onForeignRegionsChange: (regions: string[]) => void;
  sourceStatus: SourceStatusResponse | null;
  sourceLocked: boolean;
  onSourceToggle: (source: SourceKey) => void;
  onSearchQueryEdit: (target: QueryEditTarget, nextValue: string | null) => void;
  onBuild: () => void;
  canSubmitInterest: boolean;
  onSubmitInterest: (event?: FormEvent) => void;
  onEnsurePodcastTopicId?: () => Promise<string | null>;
}) {
  const threadRef = useRef<HTMLDivElement | null>(null);
  const messages = props.session?.messages ?? [];
  const preview = props.session?.strategy_preview ?? null;
  const finalized = props.flow === "confirm" || props.session?.status === "finalized";
  const generalQueries = preview?.search_queries ?? props.profile?.search_queries ?? [];
  const selectedPreviewSources = (preview?.per_source ?? []).filter((source) => props.sourceSelection[source.key]);
  const marketSource = selectedPreviewSources.find((source) => source.key === "markets");
  const podcastSource = selectedPreviewSources.find((source) => source.key === "podcasts");
  const tickers = marketSource?.tickers ?? [];
  const podcastsSelected = Boolean(props.sourceSelection.podcasts);
  const podcastDirectQueries = podcastsSelected ? uniqueCleanList([
    ...(podcastSource?.direct_episode_queries ?? []),
    ...(props.profile?.direct_episode_queries ?? []),
  ]) : [];
  const podcastRelatedQueries = podcastsSelected ? uniqueCleanList([
    ...(podcastSource?.related_episode_queries ?? []),
    ...(props.profile?.related_episode_queries ?? []),
  ]) : [];
  const podcastPriorityTerms = podcastsSelected ? uniqueCleanList([
    ...(podcastSource?.priority_terms ?? []),
    ...(props.profile?.priority_terms ?? []),
  ]) : [];
  const podcastNegativeTerms = podcastsSelected ? uniqueCleanList([
    ...(podcastSource?.negative_constraints ?? []),
    ...(props.profile?.negative_constraints ?? []),
  ]) : [];
  const sourceQueries = selectedPreviewSources
    .filter((source) => source.key !== "markets")
    .flatMap((source) => source.queries.map((query, index) => ({
      source: source.source,
      sourceKey: source.key,
      query,
      index,
    })));
  const recencyLabel = recencyText(props.draft.recency_weighting, props.draft.lookback_hours);
  const scopeText = preview?.scope || props.profile?.scope || "";
  const mustHaveTerms = preview?.must_have_terms ?? props.profile?.must_have_terms ?? [];
  const mustHaveAliases = preview?.must_have_aliases ?? props.profile?.must_have_aliases ?? {};
  const foreignRegions = props.foreignRegions;
  const progressState = props.refinementProgress
    ? refinementProgressState(props.refinementProgress, props.progressNow)
    : null;
  const thinking = props.streaming || Boolean(progressState);
  const planSourceLabels: Partial<Record<SourceKey, string>> = {
    web_search: "Web search",
    foreign_media: "Foreign media",
    gmail: "Gmail newsletters",
    podcasts: "Podcasts",
    collections: "Your collections",
  };
  const strategySourcePills = sourceOptions.map((source) => ({
    key: source.key,
    label: planSourceLabels[source.key] ?? source.label,
    enabled: Boolean(props.sourceSelection[source.key]),
  }));
  const showStrategySources = props.flow !== "idle" || Boolean(preview);

  function updateDraftRecency(lookbackHours: number | null) {
    props.onDraftChange({
      ...props.draft,
      lookback_hours: lookbackHours,
      recency_weighting: sourceScopeFromLookbackHours(lookbackHours),
      sourceScopeTouched: true,
    });
  }

  useEffect(() => {
    const node = threadRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages.length, props.streamingText, props.streaming, props.gmailCandidates, progressState?.stage, progressState?.detail]);

  return (
    <section className="chat-redesign">
      <div className="chat-main">
        <div className="chat-head">
          <div>
            <p className="section-kicker">Reference librarian</p>
            <h2>{scopeText || props.interest || props.statement || "Shaping your brief"}</h2>
          </div>
          <span className={`status-pill ${finalized ? "good" : ""}`}>
            <span className={`live-dot ${thinking ? "live" : ""}`} />
            {finalized ? "Strategy confirmed" : thinking ? "Thinking through your plan" : "In progress"}
          </span>
        </div>
        <div className="chat-thread" ref={threadRef}>
          {props.flow === "idle" ? (
            <div className="chat-turn assistant">
              <div className="chat-avatar ai">M</div>
              <div className="chat-bubble2">
                Hello. What should this brief help you understand, decide, or monitor?
              </div>
            </div>
          ) : (
            <>
              {messages.map((message, index) => (
                <div className={`chat-turn ${message.role}`} key={`${message.role}-${index}`}>
                  <div className={`chat-avatar ${message.role === "user" ? "me" : "ai"}`}>
                    {message.role === "user" ? "You" : "M"}
                  </div>
                  <div className="chat-bubble2">
                    <ChatMessageContent content={message.content} />
                  </div>
                </div>
              ))}
              {props.streaming ? (
                <div className="chat-turn assistant">
                  <div className="chat-avatar ai">M</div>
                  <div className="chat-bubble2">
                    {props.streamingText ? (
                      <>
                        <ChatMessageContent content={props.streamingText} />
                        <span className="stream-caret" />
                      </>
                    ) : (
                      <span className="chat-model-waiting" role="status" aria-live="polite">
                        <span className="typing-dots" aria-hidden="true">
                          <span />
                          <span />
                          <span />
                        </span>
                        Waiting for the model…
                      </span>
                    )}
                  </div>
                </div>
              ) : null}
              {progressState ? (
                <div className="chat-turn assistant status-turn">
                  <div className="chat-avatar ai">M</div>
                  <div
                    className={`chat-refinement-status ${progressState.alert ? "alert" : ""}`}
                    role="status"
                    aria-live="polite"
                    aria-label={`${progressState.stage}. ${progressState.detail}`}
                  >
                    <span className="typing-dots small" aria-hidden="true">
                      <span />
                      <span />
                      <span />
                    </span>
                    <div>
                      <strong>{progressState.stage}</strong>
                      <small>{progressState.detail}</small>
                    </div>
                    <span className="chat-refinement-elapsed" aria-hidden="true">
                      {formatElapsed(progressState.elapsedMs)}
                    </span>
                  </div>
                </div>
              ) : null}
              {!props.session && !props.streaming ? (
                <div className="chat-turn assistant">
                  <div className="chat-avatar ai">M</div>
                  <div className="chat-bubble2">Tell me what you're curious about and I'll shape the brief with you.</div>
                </div>
              ) : null}
              {props.gmailCandidates ? (
                <div className="chat-turn assistant gmail-approval-turn">
                  <div className="chat-avatar ai">M</div>
                  <div className="chat-bubble2 gmail-chat-bubble">
                    <GmailApprovalCard
                      payload={props.gmailCandidates}
                      busy={props.busy}
                      onApprove={props.onGmailApprove}
                    />
                  </div>
                </div>
              ) : null}
              {finalized && props.sourceSelection.podcasts && props.onEnsurePodcastTopicId ? (
                <div className="chat-turn assistant podcast-show-turn">
                  <div className="chat-avatar ai">M</div>
                  <div className="chat-bubble2 podcast-show-chat-bubble">
                    <PodcastShowPicker ensureTopicId={props.onEnsurePodcastTopicId} />
                  </div>
                </div>
              ) : null}
              {props.queuedBuildRequests > 0 ? (
                <>
                  <div className="chat-turn user pending">
                    <div className="chat-avatar me">You</div>
                    <div className="chat-bubble2">Build brief requested.</div>
                  </div>
                  <div className="chat-turn assistant status-turn">
                    <div className="chat-avatar ai">M</div>
                    <div className="chat-refinement-status" role="status" aria-live="polite">
                      <span className="typing-dots small" aria-hidden="true">
                        <span />
                        <span />
                        <span />
                      </span>
                      <div>
                        <strong>Build queued</strong>
                        <small>
                          {props.queuedBuildRequests === 1
                            ? "I’ll start the brief as soon as the current turn finishes."
                            : `${props.queuedBuildRequests} build requests are waiting for the current turn to finish.`}
                        </small>
                      </div>
                    </div>
                  </div>
                </>
              ) : null}
            </>
          )}
        </div>
        <div className="chat-composer">
          {props.flow === "idle" ? (
            <form onSubmit={props.onSubmitInterest} style={{ display: "contents" }}>
              <div className="chat-field" style={{ padding: "12px", alignItems: "stretch" }}>
                <textarea
                  style={{ width: "100%", border: 0, resize: "none", font: "inherit", outline: "none", fontSize: "0.95rem", background: "transparent" }}
                  value={props.statement}
                  onChange={(event) => props.onStatementChange(event.target.value)}
                  placeholder="Describe what you're interested in?"
                  rows={4}
                  disabled={props.busy}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      props.onSubmitInterest?.();
                    }
                  }}
                />
              </div>
              <div className="chat-undercaption" style={{ marginTop: "12px", display: "flex", flexDirection: "column", alignItems: "stretch", gap: "10px" }}>
                <SourceChips
                  selection={props.sourceSelection}
                  status={props.sourceStatus}
                  locked={props.sourceLocked}
                  onToggle={props.onSourceToggle}
                />
                {props.sourceSelection.foreign_media ? (
                  <ForeignRegionPicker selected={foreignRegions} onChange={props.onForeignRegionsChange} />
                ) : null}
                <RecencyControl value={props.draft.lookback_hours} onChange={updateDraftRecency} compact />
                <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "4px" }}>
                  <button
                    className="primary-action"
                    type="submit"
                    disabled={!props.canSubmitInterest || props.busy}
                  >
                    Submit
                  </button>
                </div>
              </div>
            </form>
          ) : props.flow === "confirm" ? (
            <div>
              <div className="chat-field">
                <textarea
                  value={props.answer}
                  onChange={(event) => props.onAnswerChange(event.target.value)}
                  placeholder="Add anything else to adjust..."
                  rows={1}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      if (props.answer.trim()) props.onSend();
                    }
                  }}
                />
                <button
                  type="button"
                  className="chat-send"
                  onClick={props.onSend}
                  disabled={!props.answer.trim()}
                  aria-label="Send"
                >
                  →
                </button>
              </div>
              <div className="chat-undercaption" style={{ marginTop: "12px", display: "flex", flexDirection: "column", alignItems: "stretch", gap: "10px" }}>
                <SourceChips
                  selection={props.sourceSelection}
                  status={props.sourceStatus}
                  locked={props.sourceLocked}
                  onToggle={props.onSourceToggle}
                />
                {props.sourceSelection.foreign_media ? (
                  <ForeignRegionPicker selected={foreignRegions} onChange={props.onForeignRegionsChange} />
                ) : null}
                <div className="chat-build-row">
                  <span className="muted-hint">Or type further adjustments above</span>
                  <RecencyControl value={props.draft.lookback_hours} onChange={updateDraftRecency} compact />
                  <button
                    className="primary-action build-brief-action"
                    type="button"
                    onClick={props.onBuild}
                  >
                    Build brief
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div>
              <div className="chat-field">
                <textarea
                  value={props.answer}
                  onChange={(event) => props.onAnswerChange(event.target.value)}
                  placeholder={props.gmailCandidates ? "Reply with numbers, names, all, none, or click senders above…" : finalized ? "Add anything else…" : "Answer the next question or refine the strategy…"}
                  rows={1}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      if (props.answer.trim()) props.onSend();
                    }
                  }}
                />
                {props.queuedRefinementTurns > 0 ? (
                  <small className="muted-hint" style={{ whiteSpace: "nowrap" }}>
                    {props.queuedRefinementTurns} queued response{props.queuedRefinementTurns === 1 ? "" : "s"}
                  </small>
                ) : null}
                <button
                  type="button"
                  className="chat-send"
                  onClick={props.onSend}
                  disabled={!props.answer.trim()}
                  aria-label="Send"
                >
                  →
                </button>
              </div>
              <div className="chat-undercaption" style={{ marginTop: "12px", display: "flex", flexDirection: "column", alignItems: "stretch", gap: "10px" }}>
                <SourceChips
                  selection={props.sourceSelection}
                  status={props.sourceStatus}
                  locked={props.sourceLocked}
                  onToggle={props.onSourceToggle}
                />
                {props.sourceSelection.foreign_media ? (
                  <ForeignRegionPicker selected={foreignRegions} onChange={props.onForeignRegionsChange} />
                ) : null}
                <div className="chat-build-row">
                  <span className="muted-hint">
                    {props.streaming ? "" : "Enter to send · Shift+Enter for a new line"}
                  </span>
                  <span style={{ flex: 1 }} />
                  <RecencyControl value={props.draft.lookback_hours} onChange={updateDraftRecency} compact />
                  <button type="button" className="primary-action strategy-confirm-action" onClick={props.onBuild}>
                    Build brief
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
      <aside className="chat-plan">
        <div className="chat-plan-head">
          <p className="section-kicker">Search strategy</p>
          <h3>Building as we talk</h3>
          <p className="chat-plan-sub">Written by the AI, updates live with each reply.</p>
        </div>
        <div className="chat-plan-body">
          <div className="plan-group">
            <div className="plan-label">Scope</div>
            <div className={`plan-value ${scopeText ? "" : "empty"}`}>{scopeText || "Being shaped…"}</div>
          </div>
          {showStrategySources ? (
            <div className="plan-group">
              <div className="plan-label">Sources</div>
              <div className="plan-pillrow">
                {strategySourcePills.map((source) => (
                  <span className={`plan-pill ${source.enabled ? "on" : ""}`} key={source.key}>{source.label}</span>
                ))}
              </div>
            </div>
          ) : null}
          {recencyLabel ? (
            <div className="plan-group">
              <div className="plan-label">Recency</div>
              <div className="plan-value">{recencyLabel}</div>
            </div>
          ) : null}
          {tickers.length ? (
            <div className="plan-group">
              <div className="plan-label">Market tickers</div>
              <div className="plan-pillrow">
                {tickers.map((ticker) => (
                  <span className="plan-pill on" key={ticker}>{ticker}</span>
                ))}
              </div>
            </div>
          ) : null}
          {generalQueries.length ? (
            <div className="plan-group">
              <div className="plan-label">Live queries</div>
              <ul className="plan-qlist">
                {generalQueries.map((query, index) => (
                  <EditablePlanQuery
                    key={`gq-${index}`}
                    value={query}
                    label="General query"
                    onChange={(value) => props.onSearchQueryEdit({ kind: "general", index }, value)}
                    onDelete={() => props.onSearchQueryEdit({ kind: "general", index }, null)}
                  />
                ))}
              </ul>
            </div>
          ) : null}
          {sourceQueries.length ? (
            <div className="plan-group">
              <div className="plan-label">Source queries</div>
              <ul className="plan-qlist">
                {sourceQueries.map((item, index) => (
                  <EditablePlanQuery
                    key={`sq-${item.sourceKey}-${item.index}-${index}`}
                    value={item.query}
                    label={item.source}
                    sourceLabel={item.source}
                    onChange={(value) => props.onSearchQueryEdit({ kind: "source", sourceKey: item.sourceKey, index: item.index }, value)}
                    onDelete={() => props.onSearchQueryEdit({ kind: "source", sourceKey: item.sourceKey, index: item.index }, null)}
                  />
                ))}
              </ul>
            </div>
          ) : null}
          {podcastDirectQueries.length || podcastRelatedQueries.length || podcastPriorityTerms.length || podcastNegativeTerms.length ? (
            <div className="plan-group">
              <div className="plan-label">Podcast strategy</div>
              <div className="plan-value">
                {podcastDirectQueries.length ? `Direct: ${podcastDirectQueries.join(" · ")}` : null}
                {podcastRelatedQueries.length ? `${podcastDirectQueries.length ? " | " : ""}Related: ${podcastRelatedQueries.join(" · ")}` : null}
                {podcastPriorityTerms.length ? `${podcastDirectQueries.length || podcastRelatedQueries.length ? " | " : ""}Boost: ${podcastPriorityTerms.join(" · ")}` : null}
                {podcastNegativeTerms.length ? `${podcastDirectQueries.length || podcastRelatedQueries.length || podcastPriorityTerms.length ? " | " : ""}Avoid: ${podcastNegativeTerms.join(" · ")}` : null}
              </div>
              <div className="plan-ready-note">Approved shows contribute their latest eligible episode; these terms discover additional related podcast content.</div>
            </div>
          ) : null}
          {(preview?.exclusions?.length ?? 0) > 0 ? (
            <div className="plan-group">
              <div className="plan-label">Avoiding</div>
              <div className="plan-value">{preview!.exclusions.join(" · ")}</div>
            </div>
          ) : null}
          {mustHaveTerms.length ? (
            <div className="plan-group">
              <div className="plan-label">Must include</div>
              <div className="plan-value">
                {mustHaveTerms.map((term) => {
                  const aliases = mustHaveAliases[term.toLowerCase()] ?? [];
                  return aliases.length ? `${term} (${aliases.join(", ")})` : term;
                }).join(" · ")}
              </div>
            </div>
          ) : null}
          {finalized ? (
            <div className="plan-ready-note">Strategy is ready for the build step.</div>
          ) : null}
        </div>
      </aside>
    </section>
  );
}
