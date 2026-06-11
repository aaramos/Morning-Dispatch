import type { EditingDigestDraft, SchedulePreset } from "../../lib/types";
import { schedulePresets } from "../../lib/types";

export function DigestScheduleEditor(props: {
  draft: EditingDigestDraft;
  busy: boolean;
  onDraftChange: (draft: EditingDigestDraft) => void;
  onAddRecipient: () => void;
  onRemoveRecipient: (email: string) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="inline-schedule-editor">
      <div className="schedule-editor-heading">
        <strong>Schedule delivery</strong>
        <span>Add or remove email recipients for this digest, then save.</span>
      </div>
      <select
        value={props.draft.preset}
        onChange={(event) => props.onDraftChange({ ...props.draft, preset: event.target.value as SchedulePreset })}
      >
        {schedulePresets.map((preset) => (
          <option value={preset.value} key={preset.value}>{preset.label}</option>
        ))}
      </select>
      <input
        type="time"
        value={props.draft.time}
        onChange={(event) => props.onDraftChange({ ...props.draft, time: event.target.value })}
      />
      <label className="inline-check">
        <input
          type="checkbox"
          checked={props.draft.emailEnabled}
          onChange={(event) => props.onDraftChange({ ...props.draft, emailEnabled: event.target.checked })}
        />
        Email this digest
      </label>
      <div className="digest-recipient-editor">
        <span className="digest-recipient-label">Email recipients</span>
        <div className="digest-recipient-list">
          {props.draft.recipients.length ? props.draft.recipients.map((email) => (
            <span className="digest-recipient-chip" key={email}>
              {email}
              <button type="button" onClick={() => props.onRemoveRecipient(email)} aria-label={`Remove ${email}`} disabled={props.busy}>x</button>
            </span>
          )) : <em>No email recipients</em>}
        </div>
        <div className="digest-recipient-add">
          <input
            type="email"
            value={props.draft.newRecipient}
            onChange={(event) => props.onDraftChange({ ...props.draft, newRecipient: event.target.value })}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                props.onAddRecipient();
              }
            }}
            placeholder="name@example.com"
          />
          <button type="button" className="secondary-action" onClick={props.onAddRecipient} disabled={props.busy || !props.draft.newRecipient.trim()}>Add email</button>
        </div>
      </div>
      <button type="button" onClick={props.onSave} disabled={props.busy}>Save</button>
      <button type="button" className="ghost-action" onClick={props.onCancel} disabled={props.busy}>Cancel</button>
    </div>
  );
}
