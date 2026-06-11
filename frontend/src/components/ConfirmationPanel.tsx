import { useEffect, useState } from "react";
import type { ConfirmationDraft, ContentLimitsDraft, PendingStrategyRefinement, SourceKey, SourceScope, SourceStatusResponse, StrategyPreview, TopicProfile } from "../lib/types";
import { formatSourceLabel } from "../lib/display";
import { gmailLookbackLabel, lookbackHoursFromSourceScope, sourceReadinessItems, sourceScopeConfirmation, sourceScopeFromLookbackHours, sourceSearchPlanGroups, strategyConversationTurns, validateContentLimits } from "../lib/appHelpers";
import { DisclosureButton } from "./DisclosureButton";
import { RecencyControl } from "./RecencyControl";
import { SettingsErrorList } from "./SettingsErrorList";
import { SourceChips } from "./SourceChips";
import { StrategyReviewCard } from "./StrategyReviewCard";
import { ContentLimitsPanel } from "./ContentLimitsPanel";
import { StrategyRefinementModal } from "./StrategyRefinementModal";

export function ConfirmationPanel(props: {
  draft: ConfirmationDraft;
  profile: TopicProfile | null;
  strategyPreview: StrategyPreview | null;
  pendingStrategy: PendingStrategyRefinement | null;
  strategyConfirmation: string;
  strategyStreaming: boolean;
  strategyStreamingText: string;
  strategyPreparingProposal: boolean;
  strategyError: string;
  sources: Record<SourceKey, boolean>;
  sourceStatus: SourceStatusResponse | null;
  defaultContentLimits: ContentLimitsDraft;
  canBuild: boolean;
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
  podcastPresets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
  gmailPresets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [strategyModalOpen, setStrategyModalOpen] = useState(false);
  const [inlineStrategyInstruction, setInlineStrategyInstruction] = useState("");
  const [pendingInlineStrategyRequest, setPendingInlineStrategyRequest] = useState("");
  const reviewProfile = props.pendingStrategy?.proposed_profile ?? props.profile;
  const reviewPreview = props.pendingStrategy?.strategy_preview ?? props.strategyPreview;
  const contentLimitErrors = validateContentLimits(props.draft.content_limits, props.sources);
  const searchPlanGroups = sourceSearchPlanGroups(reviewProfile);
  const readinessItems = sourceReadinessItems(props.sources, props.sourceStatus, reviewProfile);
  const strategyTurns = strategyConversationTurns(props.pendingStrategy, props.strategyConfirmation);
  const strategyFindings = props.pendingStrategy?.findings ?? [];
  const proposalSummary = props.pendingStrategy?.assistant_response || props.strategyConfirmation;

  function updateContentLimits(next: ContentLimitsDraft) {
    props.onDraftChange({ ...props.draft, content_limits: next });
  }

  function submitInlineStrategyRefinement() {
    const clean = inlineStrategyInstruction.trim();
    if (!clean) return;
    setPendingInlineStrategyRequest(clean);
    props.onStrategyRefine(clean);
    setInlineStrategyInstruction("");
  }

  useEffect(() => {
    if (!props.strategyStreaming && !props.strategyPreparingProposal) {
      setPendingInlineStrategyRequest("");
    }
  }, [props.strategyPreparingProposal, props.strategyStreaming]);

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
        <label>
          Must include
          <input
            value={props.draft.must_have}
            onChange={(event) => props.onDraftChange({ ...props.draft, must_have: event.target.value })}
            placeholder="Term every item must mention"
          />
          <small>
            Every item must mention ALL of these. Add one entry per concept; synonyms and translations are matched automatically.
          </small>
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
        <div className="strategy-inline-session">
          <div className="strategy-inline-head">
            <div>
              <strong>Refine search strategy</strong>
              <p>Tell the AI what to adjust. I’ll keep the request, response, and proposed changes here before you build.</p>
            </div>
            <button
              type="button"
              className="ghost-link"
              onClick={() => setStrategyModalOpen(true)}
              disabled={props.busy}
            >
              Open larger chat
            </button>
          </div>
          {strategyTurns.length ? (
            <div className="strategy-inline-thread" aria-live="polite">
              {strategyTurns.slice(-4).map((turn, index) => (
                <div className={`strategy-turn ${turn.role === "user" ? "user" : "assistant"}`} key={`${turn.role}-${index}-${turn.content.slice(0, 24)}`}>
                  <b>{turn.role === "user" ? "You asked" : "AI replied"}</b>
                  <span>{turn.content}</span>
                </div>
              ))}
            </div>
          ) : null}
          {pendingInlineStrategyRequest ? (
            <div className="strategy-inline-thread pending-request" aria-live="polite">
              <div className="strategy-turn user">
                <b>You asked</b>
                <span>{pendingInlineStrategyRequest}</span>
              </div>
            </div>
          ) : null}
          {props.strategyStreaming || props.strategyPreparingProposal ? (
            <div className="strategy-turn assistant strategy-preparing" aria-live="polite">
              <b>AI is working</b>
              <span>
                {props.strategyStreamingText || (
                  <>Reviewing your request and preparing proposed changes<span className="ellipsis-dot">.</span><span className="ellipsis-dot">.</span><span className="ellipsis-dot">.</span></>
                )}
              </span>
            </div>
          ) : null}
          {props.strategyError ? (
            <div className="strategy-turn assistant error" role="alert">
              <b>AI request failed</b>
              <span>{props.strategyError}</span>
            </div>
          ) : null}
          <div className="strategy-inline-composer">
            <textarea
              value={inlineStrategyInstruction}
              onChange={(event) => setInlineStrategyInstruction(event.target.value)}
              placeholder="Example: include frontier labs as demand signals, keep markets ticker-only, and remove stale year-specific queries..."
              rows={3}
              disabled={false}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault();
                  submitInlineStrategyRefinement();
                }
              }}
            />
            <button
              type="button"
              className="secondary-action"
              onClick={submitInlineStrategyRefinement}
              disabled={!inlineStrategyInstruction.trim()}
            >
              Send to AI
            </button>
          </div>
        </div>
        {proposalSummary ? (
          <div className={`strategy-confirmation ${props.pendingStrategy ? "pending" : ""}`}>
            <strong>{props.pendingStrategy ? "AI proposed update" : "AI update"}</strong>
            <p>{proposalSummary}</p>
            {strategyFindings.length ? (
              <ul className="strategy-findings">
                {strategyFindings.map((finding) => (
                  <li key={finding}>{finding}</li>
                ))}
              </ul>
            ) : null}
            {props.pendingStrategy ? (
              <div className="strategy-proposal-actions">
                <button type="button" className="primary-action" onClick={() => props.onStrategyConfirm(true)} disabled={props.busy}>
                  Apply proposed strategy
                </button>
                <button type="button" className="secondary-action" onClick={() => setInlineStrategyInstruction("")} disabled={props.busy}>
                  Keep refining
                </button>
                <button type="button" className="secondary-action" onClick={() => props.onStrategyConfirm(false)} disabled={props.busy}>
                  Discard
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
      {strategyModalOpen ? (
        <StrategyRefinementModal
          profile={reviewProfile}
          preview={reviewPreview}
          pendingStrategy={props.pendingStrategy}
          strategyConfirmation={props.strategyConfirmation}
          streaming={props.strategyStreaming}
          streamingText={props.strategyStreamingText}
          preparingProposal={props.strategyPreparingProposal}
          busy={props.busy}
          onClose={() => setStrategyModalOpen(false)}
          onSubmit={props.onStrategyRefine}
          onConfirm={(apply) => {
            props.onStrategyConfirm(apply);
            if (!apply) setStrategyModalOpen(false);
            if (apply) setStrategyModalOpen(false);
          }}
          error={props.strategyError}
        />
      ) : null}
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
              podcastPresets={props.podcastPresets}
              gmailPresets={props.gmailPresets}
            />
            <SettingsErrorList errors={contentLimitErrors} />
          </>
        ) : null}
      </div>
      <div className="confirmation-actions">
        <RecencyControl
          value={props.draft.lookback_hours}
          onChange={(lookbackHours) => props.onDraftChange({
            ...props.draft,
            lookback_hours: lookbackHours,
            recency_weighting: sourceScopeFromLookbackHours(lookbackHours),
            sourceScopeTouched: true,
          })}
          compact
        />
        <button
          type="button"
          className="primary-action build-brief-action"
          onClick={props.onBuild}
          disabled={!props.canBuild || contentLimitErrors.length > 0}
        >
          {props.busy ? "Working..." : props.pendingStrategy ? "Build with proposed strategy" : "Build brief"}
        </button>
      </div>
    </section>
  );
}
