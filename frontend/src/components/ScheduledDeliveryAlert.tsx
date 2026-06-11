import type { ScheduledDeliveryFailure } from "../lib/types";

export function ScheduledDeliveryAlert(props: { failures: ScheduledDeliveryFailure[] }) {
  if (!props.failures.length) return null;
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
        {props.failures.slice(0, 4).map((failure) => (
          <li key={failure.topic_id}>
            <span>{failure.name}</span>
            <em>{failure.error}</em>
          </li>
        ))}
      </ul>
    </div>
  );
}
