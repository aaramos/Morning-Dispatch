import type { StrategyPreview } from "../lib/types";

export function StrategyReviewCard(props: { preview: StrategyPreview }) {
  const { preview } = props;
  const looksAt = preview.looks_at.filter((label) => label.toLowerCase() !== "reddit");
  const ignores = preview.ignores.filter((label) => label.toLowerCase() !== "reddit");
  return (
    <div className="strategy-review-card">
      {preview.reasoning_summary ? (
        <p className="strategy-review-summary">{preview.reasoning_summary}</p>
      ) : null}
      <div className="strategy-review-row">
        {looksAt.length ? (
          <div className="strategy-review-block">
            <strong>Looks at</strong>
            <span>{looksAt.join(", ")}</span>
          </div>
        ) : null}
        {ignores.length ? (
          <div className="strategy-review-block">
            <strong>Ignores</strong>
            <span>{ignores.join(", ")}</span>
          </div>
        ) : null}
        {preview.exclusions.length ? (
          <div className="strategy-review-block">
            <strong>Avoids</strong>
            <span>{preview.exclusions.join(", ")}</span>
          </div>
        ) : null}
        {preview.must_have_terms?.length ? (
          <div className="strategy-review-block">
            <strong>Must include</strong>
            <span>
              {preview.must_have_terms.map((term) => {
                const aliases = preview.must_have_aliases?.[term.toLowerCase()] ?? [];
                return aliases.length ? `${term} (${aliases.join(", ")})` : term;
              }).join(", ")}
            </span>
          </div>
        ) : null}
      </div>
      {preview.search_queries.length ? (
        <div className="strategy-review-block">
          <strong>Searches it will run</strong>
          <ul className="strategy-review-queries">
            {preview.search_queries.map((query) => (
              <li key={query}>{query}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {preview.per_source.some((entry) => entry.approved_senders?.length) ? (
        <div className="strategy-review-block">
          <strong>Approved Gmail newsletters</strong>
          {preview.per_source
            .filter((entry) => entry.approved_senders?.length)
            .map((entry) => (
              <span key={entry.key}>{entry.approved_senders!.join(", ")}</span>
            ))}
        </div>
      ) : null}
      {preview.per_source.some((entry) => entry.tickers?.length) ? (
        <div className="strategy-review-block">
          <strong>Market tickers</strong>
          <div className="strategy-review-tickers">
            {preview.per_source
              .filter((entry) => entry.tickers?.length)
              .flatMap((entry) => entry.tickers!)
              .map((ticker) => (
                <span key={ticker} className="strategy-ticker-chip">{ticker}</span>
              ))}
          </div>
          {preview.per_source
            .filter((entry) => entry.tickers?.length && entry.note)
            .map((entry) => (
              <span key={entry.key} className="strategy-review-note">{entry.note}</span>
            ))}
        </div>
      ) : null}
    </div>
  );
}
