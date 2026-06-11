import { foreignRegionOptions } from "../lib/types";

export function ForeignRegionPicker(props: {
  selected: string[];
  onChange: (regions: string[]) => void;
}) {
  const selected = new Set(props.selected);
  return (
    <div className="foreign-region-picker">
      <strong>Foreign regions</strong>
      <div className="foreign-region-row">
        {foreignRegionOptions.map((region) => {
          const enabled = selected.has(region.key);
          return (
            <button
              key={region.key}
              type="button"
              className={enabled ? "active" : ""}
              onClick={() => {
                const next = new Set(selected);
                if (enabled) next.delete(region.key);
                else next.add(region.key);
                props.onChange(Array.from(next));
              }}
            >
              {region.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
