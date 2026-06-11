import type { Exploration } from "../lib/types";
import { formatSourceLabel, formatStage, isModelDegraded, modelDegradedMessage, progressDetail, progressHeadline } from "../lib/display";
import { actionableIssues, filterDecisionNotes, formatPipeline, sourceFromIssueName, sourcePlan } from "../lib/appHelpers";

export function ProgressPanel(props: {
  exploration: Exploration;
  sourceSelection: Record<string, boolean>;
  onStop?: () => void;
  stopping?: boolean;
}) {
  const pipeline = Object.entries(props.exploration.progress.pipeline ?? {});
  const sources = Object.entries(props.exploration.progress.sources ?? {});
  const filterNotes = filterDecisionNotes(props.exploration);
  const auditIssues = actionableIssues(props.exploration.progress.source_audit_issues);
  const queuedMessage = props.exploration.status === "queued"
    ? props.exploration.progress.queue?.message ?? "Waiting for the current brief build to finish."
    : null;
  return (
    <section className="progress-panel">
      <div className="progress-heading">
        <div>
          <p className="section-kicker">{props.exploration.status === "queued" ? "Queued" : "Full pipeline running"}</p>
          <h2>{progressHeadline(props.exploration)}</h2>
        </div>
        <div className="progress-heading-actions">
          {props.exploration.status === "queued" || props.exploration.status === "running" ? (
            <button
              type="button"
              className="secondary-action destructive compact-action"
              onClick={props.onStop}
              disabled={!props.onStop}
            >
              Stop
            </button>
          ) : null}
          <span className={`status-pill ${props.exploration.status === "running" ? "good" : ""} ${isModelDegraded(props.exploration) ? "warning" : ""}`}>
            {isModelDegraded(props.exploration) ? "Needs attention" : formatStage(props.exploration.status)}
          </span>
        </div>
      </div>
      <p className="queue-note">{progressDetail(props.exploration)}</p>
      <p className="section-kicker">{sourcePlan(props.sourceSelection)}</p>
      {queuedMessage ? <p className="queue-note">{queuedMessage}</p> : null}
      <div className="pipeline-row">
        {["discovery", "fetch", "summarize", "audit", "rank", "review", "done"].map((stage) => (
          <span className={`pipeline-pill ${props.exploration.progress.pipeline?.[stage] ?? "pending"}`} key={stage}>
            {formatStage(stage)}
          </span>
        ))}
      </div>
      {props.exploration.progress.source_audit?.message ? (
        <p className="queue-note">{props.exploration.progress.source_audit.message}</p>
      ) : props.exploration.progress.source_audit?.summary ? (
        <p className="queue-note">{props.exploration.progress.source_audit.summary}</p>
      ) : null}
      {isModelDegraded(props.exploration) ? (
        <div className="issue-note strong">
          <p>{modelDegradedMessage(props.exploration)}</p>
        </div>
      ) : null}
      <div className="source-progress-grid">
        {sources.map(([source, data]) => (
          <article className={`source-progress ${data.status}`} key={source}>
            <strong>{formatSourceLabel(source)}</strong>
            <span>{formatStage(data.status)}</span>
            <small>{data.candidate_count ? `${data.candidate_count} item(s)` : data.message ?? "Waiting"}</small>
          </article>
        ))}
      </div>
      {props.exploration.progress.requested_source_issues?.length ? (
        <div className="issue-note">
          {props.exploration.progress.requested_source_issues.map((issue) => (
            <p key={`${issue.source_name}-${issue.reason}`}>
              {issue.source_name}: {issue.reason}
            </p>
          ))}
        </div>
      ) : null}
      {auditIssues.length ? (
        <div className="issue-note">
          {auditIssues.map((issue) => (
            <p key={`${issue.source_name}-${issue.reason}`}>
              {issue.source_name}: {issue.reason}
            </p>
          ))}
        </div>
      ) : null}
      {filterNotes.length ? (
        <details className="filter-note">
          <summary>{filterNotes.length} item(s) filtered out</summary>
          <div className="filter-matrix" role="table" aria-label="Filtered source items">
            <div className="filter-matrix-row header" role="row">
              <strong>Source</strong>
              <strong>Item</strong>
              <strong>Reject reason</strong>
            </div>
            {filterNotes.slice(0, 40).map((issue) => (
              <div className="filter-matrix-row" role="row" key={`${issue.source_name}-${issue.item ?? ""}-${issue.reason}`}>
                <span>{issue.source || sourceFromIssueName(issue.source_name)}</span>
                <span>
                  {issue.item_url ? (
                    <a href={issue.item_url} target="_blank" rel="noreferrer">{issue.item || issue.source_name}</a>
                  ) : (
                    issue.item || issue.source_name
                  )}
                </span>
                <span>{issue.reason}</span>
              </div>
            ))}
          </div>
        </details>
      ) : null}
      {pipeline.length ? <p className="muted">{formatPipeline(pipeline)}</p> : null}
    </section>
  );
}
