import type { SystemLimitGroup } from "../lib/types";

export function SystemLimitsPanel(props: { groups: SystemLimitGroup[] }) {
  return (
    <div className="system-limits-panel">
      {props.groups.map((group) => (
        <section className="system-limit-group" key={group.group}>
          <h3>{group.group}</h3>
          <div className="system-limit-grid">
            {group.items.map((item) => (
              <article className="system-limit-card" key={`${group.group}-${item.label}`}>
                <span>{item.label}</span>
                <strong>{item.value}</strong>
                {item.note ? <p>{item.note}</p> : null}
              </article>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
