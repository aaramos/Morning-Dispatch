export function EditablePlanQuery(props: {
  value: string;
  label: string;
  sourceLabel?: string;
  onChange: (value: string) => void;
  onDelete: () => void;
}) {
  return (
    <li className="plan-query-row">
      {props.sourceLabel ? <span className="plan-qsource">{props.sourceLabel}</span> : null}
      <input
        className="plan-query-input"
        aria-label={`Edit ${props.label}`}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <button
        type="button"
        className="plan-query-delete"
        onClick={props.onDelete}
        aria-label={`Delete ${props.label}`}
        title="Delete query"
      >
        ×
      </button>
    </li>
  );
}
