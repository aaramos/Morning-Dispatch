export function DisclosureButton(props: { expanded: boolean; label: string; onToggle: () => void }) {
  return (
    <button type="button" className="disclosure-button" onClick={props.onToggle} aria-expanded={props.expanded}>
      <span>{props.expanded ? "▾" : "▸"}</span>
      {props.label}
    </button>
  );
}
