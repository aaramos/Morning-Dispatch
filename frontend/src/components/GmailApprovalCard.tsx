import { useState } from "react";
import type { GmailCandidatePayload } from "../lib/api";

export function GmailApprovalCard(props: {
  payload: GmailCandidatePayload;
  busy: boolean;
  onApprove: (senders: string[], instructions: string) => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(props.payload.candidates.map((c) => c.sender)),
  );
  const [extractionRules, setExtractionRules] = useState("");

  function toggle(sender: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(sender)) next.delete(sender);
      else next.add(sender);
      return next;
    });
  }

  const isSubmitDisabled = false;

  return (
    <div className="gmail-approval-card">
      <div className="gmail-approval-intro">
        <p>
          {props.payload.intro} I need your approval before I read from any inbox sender.
          Select senders here, or reply below in plain English.
        </p>
      </div>
      {props.payload.candidates.length > 0 ? (
        <ul className="gmail-sender-list">
          {props.payload.candidates.map((candidate) => {
            const isSelected = selected.has(candidate.sender);
            return (
              <li
                key={candidate.sender}
                className={`gmail-sender-row ${isSelected ? "selected" : ""}`}
                onClick={() => toggle(candidate.sender)}
                role="checkbox"
                aria-checked={isSelected}
                tabIndex={0}
                onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(candidate.sender); } }}
              >
                <span className={`gmail-sender-check ${isSelected ? "on" : ""}`}>
                  {isSelected ? "✓" : ""}
                </span>
                <div className="gmail-sender-details">
                  <strong>{candidate.sender_name || candidate.sender}</strong>
                  <span className="gmail-sender-email">{candidate.sender_name ? candidate.sender : null}</span>
                  {candidate.ai_rationale ? (
                    <span className="gmail-sender-rationale">{candidate.ai_rationale}</span>
                  ) : null}
                  <span className="gmail-sender-meta">
                    {candidate.message_count != null ? `${candidate.message_count} found` : null}
                    {candidate.subject ? ` · Latest: ${candidate.subject}` : null}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="gmail-no-candidates">
          No newsletter senders matched that search. Name specific senders below, or confirm with none selected to skip Gmail.
        </p>
      )}

      {selected.size > 0 ? (
        <div style={{ marginTop: "14px", marginBottom: "14px" }} className="gmail-instructions-block">
          <label style={{ display: "block", marginBottom: "6px", fontSize: "0.88rem", fontWeight: 700, color: "#1d1d1b" }} htmlFor="gmail-rules-textarea">
            Add extraction instructions for these newsletters (optional):
          </label>
          <textarea
            id="gmail-rules-textarea"
            style={{ width: "100%", padding: "10px", border: "1px solid #c8c7bf", borderRadius: "8px", fontFamily: "inherit", fontSize: "0.9rem", boxSizing: "border-box" }}
            value={extractionRules}
            onChange={(e) => setExtractionRules(e.target.value)}
            placeholder="e.g. Extract dev tools and ignore sponsorships"
            rows={3}
            disabled={false}
          />
        </div>
      ) : null}

      <div className="gmail-approval-actions">
        <button
          type="button"
          className="primary-action"
          onClick={() => props.onApprove([...selected], extractionRules.trim())}
          disabled={isSubmitDisabled}
        >
          {selected.size > 0
            ? `Approve ${selected.size} sender${selected.size === 1 ? "" : "s"}`
            : "Continue without Gmail"}
        </button>
        {selected.size > 0 && props.payload.candidates.length > 0 ? (
          <button
            type="button"
            className="secondary-action"
            onClick={() => props.onApprove([], "")}
            disabled={false}
          >
            Skip Gmail
          </button>
        ) : null}
      </div>
      <p className="gmail-approval-hint">
        You can also type things like "approve 2, 3, and 5", "only Tech Brew", "all", or "none" in the same chat box.
      </p>
    </div>
  );
}
