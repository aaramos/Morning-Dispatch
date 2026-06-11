import type { RecencyUnit } from "../lib/types";

function clampRecencyAmount(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min;
  return Math.max(min, Math.min(max, Math.round(value)));
}

function recencyControlValue(lookbackHours: number | null): { unlimited: boolean; amount: number; unit: RecencyUnit } {
  if (lookbackHours === null) return { unlimited: true, amount: 7, unit: "days" };
  const hours = Math.max(0, Number(lookbackHours) || 168);
  const days = Math.max(1, Math.round(hours / 24));
  if (days > 365 || (days >= 30 && days % 30 === 0)) {
    return { unlimited: false, amount: Math.min(365, Math.round(days / 30)), unit: "months" };
  }
  return { unlimited: false, amount: Math.min(365, days), unit: "days" };
}

function lookbackHoursFromRecencyControl(amount: number, unit: RecencyUnit, unlimited: boolean): number | null {
  if (unlimited) return null;
  const cleanAmount = clampRecencyAmount(amount, 0, 365);
  if (unit === "months") return Math.min(262800, cleanAmount * 30 * 24);
  if (cleanAmount === 0) return 24;
  return Math.min(262800, cleanAmount * 24);
}

export function RecencyControl(props: {
  label?: string;
  value: number | null;
  onChange: (lookbackHours: number | null) => void;
  compact?: boolean;
}) {
  const current = recencyControlValue(props.value);
  const amountMax = 365;

  function update(next: Partial<typeof current>) {
    const merged = { ...current, ...next };
    props.onChange(lookbackHoursFromRecencyControl(merged.amount, merged.unit, merged.unlimited));
  }

  return (
    <div className={`recency-control ${props.compact ? "compact" : ""}`}>
      <strong>{props.label ?? "Recency"}</strong>
      <label className="recency-unlimited-toggle">
        <input
          type="checkbox"
          checked={current.unlimited}
          onChange={(event) => update({ unlimited: event.target.checked })}
        />
        Unlimited
      </label>
      <select
        className="recency-amount-select"
        value={current.amount}
        disabled={current.unlimited}
        onChange={(event) => update({ amount: Number(event.target.value) })}
      >
        {Array.from({ length: amountMax }, (_, index) => index + 1).map((amount) => (
          <option value={amount} key={amount}>{amount}</option>
        ))}
      </select>
      <select
        value={current.unit}
        disabled={current.unlimited}
        onChange={(event) => update({ unit: event.target.value as RecencyUnit })}
      >
        <option value="days">Days</option>
        <option value="months">Months</option>
      </select>
    </div>
  );
}
