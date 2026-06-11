import { useEffect, useRef, useState } from "react";
import type { PendingStrategyRefinement, StrategyPreview, TopicProfile } from "../lib/types";
import { strategyConversationTurns, strategyIntentSummary } from "../lib/appHelpers";
import { StrategyModalPlanPreview } from "./StrategyModalPlanPreview";

export function StrategyRefinementModal(props: {
  profile: TopicProfile | null;
  preview: StrategyPreview | null;
  pendingStrategy: PendingStrategyRefinement | null;
  strategyConfirmation: string;
  streaming: boolean;
  streamingText: string;
  preparingProposal: boolean;
  busy: boolean;
  error: string;
  onClose: () => void;
  onSubmit: (instruction: string) => void;
  onConfirm: (apply: boolean) => void;
}) {
  const [instruction, setInstruction] = useState("");
  const [pendingRequest, setPendingRequest] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const threadRef = useRef<HTMLDivElement | null>(null);
  const turns = strategyConversationTurns(props.pendingStrategy, props.strategyConfirmation);
  const proposalSummary = props.pendingStrategy?.assistant_response || props.strategyConfirmation;
  const findings = props.pendingStrategy?.findings ?? [];
  const intentSummary = strategyIntentSummary(props.profile, props.preview);
  const visibleProfile = props.pendingStrategy?.proposed_profile ?? props.profile;
  const visiblePreview = props.pendingStrategy?.strategy_preview ?? props.preview;

  useEffect(() => {
    const node = threadRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns.length, pendingRequest, props.streamingText, props.streaming]);

  useEffect(() => {
    if (!pendingRequest || props.streaming || props.preparingProposal) return;
    const normalizedPending = pendingRequest.trim().toLowerCase();
    const requestIsInThread = turns.some((turn) => (
      turn.role === "user" && turn.content.trim().toLowerCase() === normalizedPending
    ));
    if (requestIsInThread || props.pendingStrategy) {
      setPendingRequest(null);
    }
  }, [pendingRequest, props.pendingStrategy, props.preparingProposal, props.streaming, turns]);

  function submit() {
    const clean = instruction.trim();
    if (!clean) {
      inputRef.current?.focus();
      return;
    }
    setPendingRequest(clean);
    props.onSubmit(clean);
    setInstruction("");
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="strategy-modal" role="dialog" aria-modal="true" aria-labelledby="strategy-modal-title">
        <button type="button" className="modal-close" onClick={props.onClose} aria-label="Close refinement session">
          Close
        </button>
        <div className="strategy-modal-header">
          <p className="section-kicker">AI refinement session</p>
          <h2 id="strategy-modal-title">Refine search strategy</h2>
          <p>{intentSummary}</p>
        </div>

        <div className="strategy-modal-thread" ref={threadRef} aria-live="polite">
          {turns.length ? (
            turns.map((turn, index) => (
              <div className={`strategy-chat-turn ${turn.role === "user" ? "user" : "assistant"}`} key={`${turn.role}-${index}-${turn.content.slice(0, 24)}`}>
                <b>{turn.role === "user" ? "You" : "AI"}</b>
                <p>{turn.content}</p>
              </div>
            ))
          ) : (
            <div className="strategy-chat-turn assistant">
              <b>AI</b>
              <p>Tell me what is missing, too broad, too narrow, stale, or misweighted. I’ll translate your feedback into proposed search-strategy changes for review.</p>
            </div>
          )}
          {pendingRequest ? (
            <div className="strategy-chat-turn user pending">
              <b>You</b>
              <p>{pendingRequest}</p>
            </div>
          ) : null}
          {props.streaming ? (
            <div className="strategy-chat-turn assistant">
              <b>AI</b>
              {props.streamingText ? (
                <p>
                  {props.streamingText}
                  <span className="stream-caret" />
                </p>
              ) : (
                <span className="typing-dots">
                  <span /><span /><span />
                </span>
              )}
            </div>
          ) : null}
          {props.preparingProposal ? (
            <div className="strategy-chat-turn assistant strategy-preparing">
              <b>AI</b>
              <p className="muted">Preparing proposal<span className="ellipsis-dot">.</span><span className="ellipsis-dot">.</span><span className="ellipsis-dot">.</span></p>
            </div>
          ) : null}
        </div>

        {props.streaming || props.preparingProposal ? (
          <div className="strategy-modal-status" role="status">
            <span className="activity-pulse" />
            <div>
              <strong>{props.preparingProposal ? "Preparing proposed strategy" : "Waiting for AI"}</strong>
              <p>
                {props.streamingText
                  ? "The AI is responding in this conversation."
                  : "Your request was sent. If the model is unavailable, I’ll show the error here instead of silently doing nothing."}
              </p>
            </div>
          </div>
        ) : null}

        {props.error ? (
          <div className="strategy-modal-error" role="alert">
            <strong>AI request failed</strong>
            <p>{props.error}</p>
          </div>
        ) : null}

        {props.pendingStrategy && !props.streaming ? (
          <div className="strategy-modal-proposal">
            <strong>Proposed changes</strong>
            {proposalSummary ? <p>{proposalSummary}</p> : null}
            {findings.length ? (
              <ul>
                {findings.map((finding) => (
                  <li key={finding}>{finding}</li>
                ))}
              </ul>
            ) : null}
            <StrategyModalPlanPreview profile={visibleProfile} preview={visiblePreview} proposed />
            <div className="strategy-proposal-actions">
              <button type="button" className="primary-action" onClick={() => props.onConfirm(true)} disabled={props.busy}>
                Apply proposed strategy
              </button>
              <button type="button" className="secondary-action" onClick={() => inputRef.current?.focus()} disabled={props.busy}>
                Keep refining
              </button>
              <button type="button" className="secondary-action" onClick={() => props.onConfirm(false)} disabled={props.busy}>
                Discard
              </button>
            </div>
          </div>
        ) : null}
        {!props.pendingStrategy ? (
          <StrategyModalPlanPreview profile={visibleProfile} preview={visiblePreview} />
        ) : null}

        <div className="strategy-modal-composer">
          <label>
            Continue the conversation
            <span className="composer-hint">Your note will appear in this thread, then the AI will respond here with proposed strategy changes.</span>
            <textarea
              ref={inputRef}
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="Add a natural-language refinement, e.g. include frontier labs as demand signals but keep markets ticker-only..."
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault();
                  submit();
                }
              }}
            />
          </label>
          <div className="modal-actions">
            <button type="button" className="secondary-action" onClick={props.onClose} disabled={props.busy}>
              Done
            </button>
            <button type="button" className="primary-action" onClick={submit} disabled={!instruction.trim()}>
              Send to AI
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
