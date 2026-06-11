import type { StrategyPreview, TopicProfile } from "../lib/types";
import { formatSourceLabel } from "../lib/display";
import { gmailLookbackLabel, sourceSearchPlanGroups } from "../lib/appHelpers";

export function StrategyModalPlanPreview(props: {
  profile: TopicProfile | null;
  preview: StrategyPreview | null;
  proposed?: boolean;
}) {
  const groups = sourceSearchPlanGroups(props.profile).filter((group) => group.queries.length).slice(0, 5);
  const queryCount = groups.reduce((total, group) => total + group.queries.filter((query) => query.trim()).length, 0);
  const lookback = props.preview?.lookback_hours ?? props.profile?.lookback_hours ?? null;
  const sourceLabels = Object.entries(props.profile?.source_selection ?? {})
    .filter(([, enabled]) => enabled)
    .map(([source]) => formatSourceLabel(source))
    .filter((source) => source !== "Collections")
    .slice(0, 6);
  if (!props.profile && !props.preview) return null;
  return (
    <div className="strategy-modal-plan">
      <div className="strategy-modal-plan-head">
        <strong>{props.proposed ? "Updated search strategy" : "Current search strategy"}</strong>
        <div className="strategy-modal-preview">
          <span>{lookback ? gmailLookbackLabel(lookback) : "Open-ended recency"}</span>
          <span>{sourceLabels.length ? sourceLabels.join(", ") : "Selected sources"}</span>
          <span>{queryCount} planned query(s)</span>
        </div>
      </div>
      {groups.length ? (
        <div className="strategy-modal-plan-groups">
          {groups.map((group) => (
            <div className="strategy-modal-plan-group" key={group.key}>
              <b>{group.label}</b>
              <ul>
                {group.queries.slice(0, 3).map((query) => (
                  <li key={query}>{query}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
