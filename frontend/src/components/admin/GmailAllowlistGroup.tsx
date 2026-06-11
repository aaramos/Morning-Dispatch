import { useState } from "react";
import type { GmailAllowlistAction, GmailSenderRecord } from "../../lib/types";

export function GmailAllowlistGroup(props: {
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
