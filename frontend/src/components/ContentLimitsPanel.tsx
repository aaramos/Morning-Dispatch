import type { ContentLimitsDraft, SourceKey } from "../lib/types";
import { briefControlBounds, defaultContentLimits, scaleContentLimits, sourceOptions } from "../lib/types";
import { NumberStepper } from "./NumberStepper";

export function ContentLimitsPanel(props: {
  limits: ContentLimitsDraft;
  sourceSelection: Record<SourceKey, boolean>;
  defaults?: ContentLimitsDraft;
  resetLabel?: string;
  showReset?: boolean;
  onChange: (limits: ContentLimitsDraft) => void;
  youtubePresets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
  podcastPresets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
  gmailPresets?: {
    max: number;
    large: number;
    medium: number;
    focused: number;
  };
}) {
  const selectedSources = sourceOptions.filter((source) => props.sourceSelection[source.key]);
  const defaults = props.defaults ?? defaultContentLimits;

  function updateNumber(key: "total_items" | "target_items" | "lead_items", value: number) {
    props.onChange({ ...props.limits, [key]: value });
  }

  function updateSourceLimit(source: SourceKey, value: number) {
    props.onChange({
      ...props.limits,
      per_source: {
        ...props.limits.per_source,
        [source]: value,
      },
    });
  }

  function applyPreset(scale: number) {
    props.onChange(scaleContentLimits(defaultContentLimits, scale));
  }

  return (
    <div className="content-limits-panel">
      <div className="preset-control-row">
        <strong>Load preset</strong>
        <button type="button" onClick={() => applyPreset(1)}>Max</button>
        <button type="button" onClick={() => applyPreset(0.8)}>Large</button>
        <button type="button" onClick={() => applyPreset(0.6)}>Medium</button>
        <button type="button" onClick={() => applyPreset(0.4)}>Focused</button>
      </div>
      <div className="content-limit-grid">
        <NumberStepper
          label="Candidate budget"
          value={props.limits.total_items}
          min={briefControlBounds.total_items.min}
          max={briefControlBounds.total_items.max}
          onChange={(value) => updateNumber("total_items", value)}
        />
        <NumberStepper
          label="Target visible stories"
          value={props.limits.target_items}
          min={briefControlBounds.target_items.min}
          max={briefControlBounds.target_items.max}
          onChange={(value) => updateNumber("target_items", value)}
        />
        <NumberStepper
          label="Lead stories"
          value={props.limits.lead_items}
          min={briefControlBounds.lead_items.min}
          max={briefControlBounds.lead_items.max}
          onChange={(value) => updateNumber("lead_items", value)}
        />
        <label>
          Quality floor
          <select
            value={props.limits.quality_floor}
            onChange={(event) => props.onChange({ ...props.limits, quality_floor: event.target.value as ContentLimitsDraft["quality_floor"] })}
          >
            <option value="standard">Standard signal</option>
            <option value="strong">Strong signal only</option>
          </select>
        </label>
      </div>
      {selectedSources.length ? (
        <div className="source-limit-list">
          <strong>Per-source maximums</strong>
          {selectedSources.map((source) => (
            <NumberStepper
              key={source.key}
              label={source.label}
              value={props.limits.per_source[source.key] ?? defaults.per_source[source.key] ?? 3}
              min={briefControlBounds.per_source.min}
              max={defaultContentLimits.per_source[source.key] ?? briefControlBounds.per_source.max}
              compact
              onChange={(value) => updateSourceLimit(source.key, value)}
            />
          ))}
        </div>
      ) : null}
      {props.showReset !== false ? (
        <button type="button" className="ghost-action reset-limits-action" onClick={() => props.onChange(defaultContentLimits)}>
          {props.resetLabel ?? "Reset to defaults"}
        </button>
      ) : null}
    </div>
  );
}
