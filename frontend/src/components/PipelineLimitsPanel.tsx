import type { PipelineLimitsDraft } from "../lib/types";
import { defaultPipelineLimits, pipelineLimitFields } from "../lib/types";
import { clampNumber } from "../lib/appHelpers";
import { NumberStepper } from "./NumberStepper";

export function PipelineLimitsPanel(props: {
  limits: PipelineLimitsDraft;
  defaults?: PipelineLimitsDraft;
  onChange?: (limits: PipelineLimitsDraft) => void;
  showReset?: boolean;
}) {
  const defaults = props.defaults ?? defaultPipelineLimits;
  const editable = Boolean(props.onChange);
  const updateLimit = (key: keyof PipelineLimitsDraft, value: number, min: number, max: number) => {
    if (!props.onChange) return;
    props.onChange({ ...props.limits, [key]: clampNumber(value, min, max) });
  };
  return (
    <div className="pipeline-limits-panel">
      <div className="pipeline-limit-grid">
        {pipelineLimitFields.map((field) => (
          <article className={editable ? "pipeline-limit-card editable" : "pipeline-limit-card"} key={field.key}>
            {editable ? (
              <NumberStepper
                label={field.label}
                value={props.limits[field.key] ?? defaults[field.key]}
                min={field.min}
                max={field.max}
                onChange={(value) => updateLimit(field.key, value, field.min, field.max)}
              />
            ) : (
              <div>
                <span>{field.label}</span>
                <strong>{props.limits[field.key] ?? defaults[field.key]}</strong>
              </div>
            )}
            <p>{field.note}</p>
          </article>
        ))}
      </div>
      {editable && props.showReset !== false ? (
        <button type="button" className="ghost-action reset-limits-action" onClick={() => props.onChange?.(defaults)}>
          Reset to system limits
        </button>
      ) : null}
    </div>
  );
}
