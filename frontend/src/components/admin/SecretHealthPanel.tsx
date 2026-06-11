import type { AdminStatus } from "../../lib/types";
import { DisclosureButton } from "../DisclosureButton";

export function SecretHealthPanel(props: {
  health: AdminStatus["secret_health"] | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  if (!props.health) return null;
  return (
    <section className="secret-health-panel">
      <div className="library-section-header">
        <div>
          <p className="section-kicker">
            {props.health.summary.configured_count} configured · {props.health.summary.warning_count} warning(s)
          </p>
          <h2>Secret health</h2>
        </div>
        <span className={props.health.summary.warning_count ? "status-pill" : "status-pill good"}>
          {props.health.summary.warning_count ? "Review" : "Owner-only"}
        </span>
        <DisclosureButton expanded={props.expanded} label={props.expanded ? "Hide" : "Show"} onToggle={props.onToggle} />
      </div>
      {props.expanded ? (
        <>
          <p className="muted">Secrets folder: {props.health.secrets_dir}</p>
          <div className="health-grid secret-health-grid">
            <article className={`health-card ${props.health.directory_permissions.status === "ok" ? "ok" : "warning"}`}>
              <strong>Folder permissions</strong>
              <p>
                {props.health.directory_permissions.status === "ok"
                  ? "Owner-only access."
                  : `Review folder mode ${props.health.directory_permissions.mode ?? "unknown"}.`}
              </p>
            </article>
            {props.health.items.map((item) => (
              <article className={`health-card ${item.status}`} key={item.id}>
                <strong>{item.label}</strong>
                <p>{item.configured ? item.storage : item.message}</p>
                {item.path ? <small>{item.path}</small> : null}
              </article>
            ))}
          </div>
          {props.health.external_plaintext.length ? (
            <div className="issue-note">
              <strong>Plaintext MCP config to review</strong>
              {props.health.external_plaintext.map((item) => (
                <p key={`${item.server}-${item.location}-${item.key}`}>
                  {item.server}: {item.location}.{item.key} in {item.path}
                </p>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
