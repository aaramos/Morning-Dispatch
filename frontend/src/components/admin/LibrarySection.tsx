import type { ReactNode } from "react";
import type { SortMode } from "../../lib/types";
import { DisclosureButton } from "../DisclosureButton";

export function LibrarySection(props: {
  title: string;
  sort: SortMode;
  onSort: (sort: SortMode) => void;
  count: number;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className="library-section">
      <div className="library-section-header">
        <div>
          <p className="section-kicker">{props.count} total</p>
          <h2>{props.title}</h2>
        </div>
        <div className="segmented-control">
          <button type="button" className={props.sort === "recent" ? "active" : ""} onClick={() => props.onSort("recent")}>Recent</button>
          <button type="button" className={props.sort === "name" ? "active" : ""} onClick={() => props.onSort("name")}>Name</button>
        </div>
        <DisclosureButton expanded={props.expanded} label={props.expanded ? "Hide" : "Show"} onToggle={props.onToggle} />
      </div>
      {props.expanded ? <div className="library-list">{props.children}</div> : null}
    </section>
  );
}
