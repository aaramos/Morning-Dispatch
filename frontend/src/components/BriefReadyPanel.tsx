import type { Exploration, ExplorationIssue } from "../lib/types";

export function BriefReadyPanel(props: {
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
