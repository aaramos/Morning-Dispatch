import { foreignRegionGroups } from "../lib/types";

export function ForeignRegionPicker(props: {
  selected: string[];
  onChange: (regions: string[]) => void;
}) {
  const selected = new Set(props.selected);
  const toggle = (key: string) => {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    props.onChange(Array.from(next));
  };
  return (
    <div className="foreign-region-picker">
      <strong>Foreign regions</strong>
      <span className="foreign-region-hint">
        Selecting regions widens foreign-media coverage by 50%.
      </span>
      {foreignRegionGroups.map((group) => (
        <div key={group.continent} className="foreign-region-group">
          <span className="foreign-region-continent">{group.continent}</span>
          <div className="foreign-region-row">
            {group.regions.map((region) => {
              const enabled = selected.has(region.key);
              return (
                <button
                  key={region.key}
                  type="button"
                  className={enabled ? "active" : ""}
                  onClick={() => toggle(region.key)}
                >
                  {region.label}
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
