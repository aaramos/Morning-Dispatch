import { sourceOptions } from "../lib/types";
import type { SourceKey, SourceStatusResponse } from "../lib/types";

export function SourceChips(props: {
  selection: Record<SourceKey, boolean>;
  status: SourceStatusResponse | null;
  locked: boolean;
  onToggle: (source: SourceKey) => void;
}) {
  return (
    <div className="source-chips">
      {sourceOptions.map((source) => {
        const status = props.status?.sources[source.key];
        const enabled = status?.enabled ?? false;
        const selected = Boolean(props.selection[source.key] && enabled);
        return (
          <button
            type="button"
            key={source.key}
            className={`source-chip ${selected ? "selected" : ""} ${enabled ? "" : "disabled"}`}
            onClick={() => props.onToggle(source.key)}
            disabled={props.locked}
            aria-pressed={selected}
            data-source-state={selected ? "selected" : enabled ? "available" : "disabled"}
            title={enabled ? source.label : status?.reason ?? "Setup required"}
          >
            <span>{source.icon}</span>
            {source.label}
          </button>
        );
      })}
    </div>
  );
}
