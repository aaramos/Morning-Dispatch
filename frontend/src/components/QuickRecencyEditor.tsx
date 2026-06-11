import type { EditingRecencyDraft } from "../lib/types";
import { RecencyControl } from "./RecencyControl";

export function QuickRecencyEditor(props: {
  draft: EditingRecencyDraft;
  busy: boolean;
  onDraftChange: (draft: EditingRecencyDraft) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="inline-recency-editor">
      <div className="schedule-editor-heading">
        <strong>Recency window</strong>
        <span>This saved window is used by rebuilds and scheduled digest runs.</span>
      </div>
      <RecencyControl
        value={props.draft.lookbackHours}
        onChange={(lookbackHours) => props.onDraftChange({ ...props.draft, lookbackHours })}
        compact
      />
      <button type="button" onClick={props.onSave} disabled={props.busy}>Save recency</button>
      <button type="button" className="ghost-action" onClick={props.onCancel} disabled={props.busy}>Cancel</button>
    </div>
  );
}
