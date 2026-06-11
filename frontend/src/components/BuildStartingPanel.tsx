import { formatStage } from "../lib/display";

export function BuildStartingPanel() {
  return (
    <section className="progress-panel" role="status" aria-live="polite">
      <div className="progress-heading">
        <div>
          <p className="section-kicker">Starting build</p>
          <h2>Starting the newsletter build</h2>
        </div>
        <span className="status-pill good">Starting</span>
      </div>
      <p className="queue-note">Creating the build job. Progress will appear here as soon as the server accepts it.</p>
      <div className="pipeline-row">
        {["discovery", "fetch", "summarize", "audit", "rank", "review", "done"].map((stage) => (
          <span className="pipeline-pill pending" key={stage}>
            {formatStage(stage)}
          </span>
        ))}
      </div>
    </section>
  );
}
