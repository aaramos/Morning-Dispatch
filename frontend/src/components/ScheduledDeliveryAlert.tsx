import { useState } from "react";
import type { ScheduledDeliveryFailure } from "../lib/types";
import { api } from "../lib/api";
import { errorMessage } from "../lib/appHelpers";

type ActionKind = "retry" | "reconnect" | "clear";

function needsGmailReconnect(error: string): boolean {
  return /token|revok|reconnect|expired|permission|auth|credential|unauthor/i.test(error);
}

export function ScheduledDeliveryAlert(props: {
  failures: ScheduledDeliveryFailure[];
  onChanged?: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState<{ id: string; action: ActionKind } | null>(null);
  const [note, setNote] = useState<string | null>(null);

  if (!props.failures.length) return null;

  async function run(failure: ScheduledDeliveryFailure, action: ActionKind) {
    setBusy({ id: failure.topic_id, action });
    setNote(null);
    try {
      if (action === "reconnect") {
        const result = await api<{ authorization_url: string }>("/api/admin/gmail/oauth/start", { method: "POST" });
        window.location.href = result.authorization_url;
        return;
      }
      const result = await api<{ status?: string; error?: string }>(
        `/api/admin/delivery/failures/${encodeURIComponent(failure.topic_id)}/${action}`,
        { method: "POST" },
      );
      if (action === "retry") {
        setNote(
          result.status === "sent"
            ? `Sent "${failure.name}".`
            : `Retry failed for "${failure.name}": ${result.error || "Email delivery failed."}`,
        );
      } else {
        setNote(`Cleared "${failure.name}".`);
      }
      await props.onChanged?.();
    } catch (error) {
      setNote(errorMessage(error, action === "retry" ? "Could not retry delivery" : "Could not clear failure"));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="delivery-alert" role="alert">
      <div>
        <p className="section-kicker">Email delivery paused</p>
        <strong>Scheduled brief email failed.</strong>
        <p>
          I will keep building scheduled briefs, but I will not keep trying to send email for the failed schedule
          until you reconnect Gmail or save the schedule again.
        </p>
      </div>
      <ul>
        {props.failures.slice(0, 4).map((failure) => {
          const isBusy = busy?.id === failure.topic_id;
          const showReconnect = needsGmailReconnect(failure.error);
          return (
            <li key={failure.topic_id}>
              <span>{failure.name}</span>
              <em>{failure.error}</em>
              <div className="delivery-alert-actions">
                <button
                  type="button"
                  className="delivery-alert-btn"
                  disabled={isBusy}
                  onClick={() => run(failure, "retry")}
                >
                  {isBusy && busy?.action === "retry" ? "Retrying…" : "Retry"}
                </button>
                {showReconnect && (
                  <button
                    type="button"
                    className="delivery-alert-btn"
                    disabled={isBusy}
                    onClick={() => run(failure, "reconnect")}
                  >
                    {isBusy && busy?.action === "reconnect" ? "Opening…" : "Reconnect Gmail"}
                  </button>
                )}
                <button
                  type="button"
                  className="delivery-alert-btn delivery-alert-btn--ghost"
                  disabled={isBusy}
                  onClick={() => run(failure, "clear")}
                >
                  {isBusy && busy?.action === "clear" ? "Clearing…" : "Dismiss"}
                </button>
              </div>
            </li>
          );
        })}
      </ul>
      {note && <p className="delivery-alert-note">{note}</p>}
    </div>
  );
}
