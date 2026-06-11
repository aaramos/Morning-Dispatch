import { formatSourceLabel, formatStage, isModelDegraded, modelDegradedMessage, progressDetail, progressHeadline } from "../lib/display";
import type { Exploration } from "../lib/types";

export function LibraryBuildProgress(props: { exploration: Exploration }) {
  const sources = Object.entries(props.exploration.progress.sources ?? {})
    .filter(([, data]) => data.status !== "disabled")
    .slice(0, 6);
  return (
    <div className="library-progress">
      <div className="library-progress-top">
        <strong>{progressHeadline(props.exploration)}</strong>
        <span>{isModelDegraded(props.exploration) ? "Needs attention" : formatStage(props.exploration.status)}</span>
      </div>
      <p>{progressDetail(props.exploration)}</p>
      <div className="pipeline-row compact">
        {["discovery", "fetch", "summarize", "audit", "rank", "review", "done"].map((stage) => (
          <span className={`pipeline-pill ${props.exploration.progress.pipeline?.[stage] ?? "pending"}`} key={stage}>
            {formatStage(stage)}
          </span>
        ))}
      </div>
      {sources.length ? (
        <div className="library-source-row">
          {sources.map(([source, data]) => (
            <span key={source}>{formatSourceLabel(source)}: {formatStage(data.status)}</span>
          ))}
        </div>
      ) : null}
      {isModelDegraded(props.exploration) ? (
        <p className="warning-text">{modelDegradedMessage(props.exploration)}</p>
      ) : null}
    </div>
  );
}
