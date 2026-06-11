import type { SchedulePreset } from "../lib/types";
import { schedulePresets } from "../lib/types";

export function SchedulePanel(props: {
  preset: SchedulePreset;
  time: string;
  emailEnabled: boolean;
  deliveryConfigured: boolean;
  busy: boolean;
  onPresetChange: (preset: SchedulePreset) => void;
  onTimeChange: (time: string) => void;
  onEmailChange: (enabled: boolean) => void;
  onCancel: () => void;
  onSchedule: () => void;
}) {
  return (
    <section className="schedule-panel">
      <div className="panel-title-row">
        <div>
          <p className="section-kicker">Schedule</p>
          <h2>Make this a digest</h2>
        </div>
      </div>
      <div className="schedule-controls">
        <div className="segmented-control">
          {schedulePresets.map((option) => (
            <button
              key={option.value}
              type="button"
              className={props.preset === option.value ? "active" : ""}
              onClick={() => props.onPresetChange(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <label>
          Time
          <input type="time" value={props.time} onChange={(event) => props.onTimeChange(event.target.value)} />
        </label>
        {props.deliveryConfigured ? (
          <label className="checkbox-row">
            <input type="checkbox" checked={props.emailEnabled} onChange={(event) => props.onEmailChange(event.target.checked)} />
            Send by email
          </label>
        ) : (
          <p className="muted">Email can be enabled later in Admin.</p>
        )}
      </div>
      <div className="button-row">
        <button type="button" className="secondary-action" onClick={props.onCancel}>Cancel</button>
        <button type="button" className="primary-action" onClick={props.onSchedule} disabled={props.busy}>Schedule</button>
      </div>
    </section>
  );
}
