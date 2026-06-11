export function NumberStepper(props: {
  label: string;
  value: number;
  min: number;
  max: number;
  compact?: boolean;
  onChange: (value: number) => void;
}) {
  const changeValue = (rawValue: string) => {
    const digits = rawValue.replace(/\D/g, "");
    props.onChange(digits ? Number(digits) : 0);
  };
  return (
    <label className={props.compact ? "number-stepper compact" : "number-stepper"}>
      {props.label}
      <span>
        <input
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          aria-invalid={props.value < props.min || props.value > props.max}
          value={props.value}
          onChange={(event) => changeValue(event.target.value)}
          onBlur={() => {
            if (!Number.isFinite(props.value)) props.onChange(0);
          }}
        />
      </span>
    </label>
  );
}
