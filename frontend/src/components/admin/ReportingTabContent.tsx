import { Fragment, useEffect, useMemo, useState } from "react";
import { api } from "../../lib/api";
import type { Exploration } from "../../lib/types";
import { errorMessage, formatDateTime } from "../../lib/appHelpers";
import { formatStage } from "../../lib/display";

export type CandidateReportStage = {
  discovery: string | null;
  screening: string | null;
  recency: string | null;
  fetch: string | null;
  audit: string | null;
  editorial: string | null;
  critic: string | null;
  inclusion: string | null;
};

export type CandidateReportItem = {
  id: string;
  title: string;
  url: string;
  source: string;
  stages: CandidateReportStage;
};

export function ReportingTabContent(props: {
  selectedRunId: string | null;
  onSelectRunId: (id: string | null) => void;
  explorations: Exploration[];
}) {
  const [report, setReport] = useState<CandidateReportItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedSources, setSelectedSources] = useState<string[]>([]);

  useEffect(() => {
    if (!props.selectedRunId) {
      setReport(null);
      return;
    }
    setLoading(true);
    setError(null);
    api<CandidateReportItem[]>(`/api/explore/explorations/${props.selectedRunId}/report`)
      .then((data) => {
        setReport(data);
      })
      .catch((err) => {
        setError(errorMessage(err, "Failed to load candidate report"));
      })
      .finally(() => {
        setLoading(false);
      });
  }, [props.selectedRunId]);

  useEffect(() => {
    setSelectedSources([]);
  }, [props.selectedRunId]);

  const completedExplorations = props.explorations.filter(
    (exp) => exp.status === "complete"
  );

  const uniqueSources = useMemo(() => {
    if (!report) return [];
    const sources = new Set<string>();
    report.forEach((item) => {
      if (item.source) {
        sources.add(item.source);
      }
    });
    return Array.from(sources).sort();
  }, [report]);

  const filteredReport = useMemo(() => {
    if (!report) return [];
    if (selectedSources.length === 0) return report;
    return report.filter((item) => selectedSources.includes(item.source));
  }, [report, selectedSources]);

  return (
    <section className="admin-panel">
      <style>{`
        .report-matrix-container {
          width: 100%;
          overflow-x: auto;
          margin-top: 18px;
          border: 1px solid #d8d7cf;
          border-radius: 8px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        .report-matrix-table {
          width: 100%;
          border-collapse: collapse;
          text-align: left;
          font-size: 0.9rem;
          min-width: 1200px;
        }
        .report-matrix-table th {
          background: #f0eee7;
          color: #4d4d49;
          font-weight: 850;
          text-transform: uppercase;
          letter-spacing: 0.05em;
          padding: 12px;
          font-size: 0.76rem;
          border-bottom: 2px solid #d8d7cf;
          border-right: 1px solid #d8d7cf;
          position: sticky;
          top: 0;
          z-index: 2;
        }
        .report-matrix-table th:last-child {
          border-right: 0;
        }
        .report-matrix-table td {
          padding: 10px 12px;
          border-bottom: 1px solid #e6e5df;
          border-right: 1px solid #e6e5df;
          vertical-align: top;
          line-height: 1.4;
        }
        .report-matrix-table td:last-child {
          border-right: 0;
        }
        .report-matrix-table tr:last-child td {
          border-bottom: 0;
        }
        .report-candidate-row {
          background: #fdfdfb;
        }
        .report-candidate-source {
          font-size: 0.72rem;
          font-weight: 850;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: #77756f;
          margin-bottom: 4px;
        }
        .report-candidate-title a {
          font-weight: 600;
          color: #171717;
          text-decoration: none;
        }
        .report-candidate-title a:hover {
          text-decoration: underline;
        }
        .report-cell-advanced {
          background: #f4faf6;
          color: #1b5e20;
          font-size: 0.8rem;
          font-weight: 550;
          text-align: center;
        }
        .report-cell-dropped {
          background: #fff6f2;
          color: #c0392b;
          font-size: 0.8rem;
          font-weight: 550;
        }
        .report-selector-row {
          display: flex;
          align-items: center;
          gap: 12px;
          margin-bottom: 18px;
          flex-wrap: wrap;
        }
        .report-selector-row label {
          font-weight: 600;
        }
        .report-selector-row select {
          padding: 6px 12px;
          border: 1px solid #d8d7cf;
          border-radius: 6px;
          background: #fff;
          font: inherit;
        }
        .report-filter-row {
          display: flex;
          align-items: center;
          gap: 12px;
          margin-bottom: 18px;
          flex-wrap: wrap;
        }
        .filter-label {
          font-weight: 600;
          color: #4d4d49;
          font-size: 0.9rem;
        }
        .filter-pills {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
        }
        .filter-pill {
          padding: 6px 12px;
          border: 1px solid #d8d7cf;
          border-radius: 20px;
          background: #fdfdfb;
          color: #55544f;
          font-size: 0.8rem;
          font-weight: 550;
          cursor: pointer;
          transition: all 0.2s ease;
          user-select: none;
        }
        .filter-pill:hover {
          background: #f0eee7;
          border-color: #c5c3b8;
          color: #171717;
        }
        .filter-pill.active {
          background: #171717;
          color: #ffffff;
          border-color: #171717;
        }
      `}</style>
      <div className="panel-title-row">
        <div>
          <p className="section-kicker">Reporting</p>
          <h1>Candidate Lifecycle Log</h1>
          <p className="muted">Track the fate of every item fetched or discovered during this run.</p>
        </div>
      </div>

      <div className="report-selector-row">
        <label htmlFor="report-run-select">Select Run:</label>
        <select
          id="report-run-select"
          value={props.selectedRunId || ""}
          onChange={(e) => props.onSelectRunId(e.target.value || null)}
        >
          <option value="">-- Choose an Exploration Run --</option>
          {completedExplorations.map((exp) => {
            const name = exp.progress?.brief?.title || `Run ${exp.exploration_id.slice(0, 8)}`;
            return (
              <option key={exp.exploration_id} value={exp.exploration_id}>
                {name} ({formatDateTime(exp.finished_at ?? exp.started_at)})
              </option>
            );
          })}
        </select>
      </div>

      {loading ? <p>Loading candidate reporting log...</p> : null}
      {error ? <p className="warning-text">{error}</p> : null}

      {!props.selectedRunId && !loading && !error ? (
        <p className="muted">Please select an exploration run from the dropdown above to view the candidate log.</p>
      ) : null}

      {props.selectedRunId && report && !loading && !error ? (
        report.length === 0 ? (
          <p className="muted">No candidates found for this exploration run.</p>
        ) : (
          <>
            {uniqueSources.length > 0 ? (
              <div className="report-filter-row">
                <span className="filter-label">Filter by Source:</span>
                <div className="filter-pills">
                  <button
                    className={`filter-pill ${selectedSources.length === 0 ? "active" : ""}`}
                    onClick={() => setSelectedSources([])}
                  >
                    All Sources
                  </button>
                  {uniqueSources.map((source) => {
                    const isActive = selectedSources.includes(source);
                    return (
                      <button
                        key={source}
                        className={`filter-pill ${isActive ? "active" : ""}`}
                        onClick={() => {
                          if (isActive) {
                            setSelectedSources(selectedSources.filter((s) => s !== source));
                          } else {
                            setSelectedSources([...selectedSources, source]);
                          }
                        }}
                      >
                        {formatStage(source)}
                      </button>
                    );
                  })}
                </div>
              </div>
            ) : null}

            {filteredReport.length === 0 ? (
              <p className="muted" style={{ marginTop: "18px" }}>No candidates match the selected source filter.</p>
            ) : (
              <div className="report-matrix-container">
                <table className="report-matrix-table">
                  <thead>
                    <tr>
                      <th style={{ width: "240px" }}>Candidate (Source & Title)</th>
                      <th>Discovery</th>
                      <th>Screening</th>
                      <th>Recency Filter</th>
                      <th>Fetch / Extract</th>
                      <th>Audit</th>
                      <th>Editorial</th>
                      <th>Critic</th>
                      <th>Inclusion</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredReport.map((item, index) => {
                      const stages = ["discovery", "screening", "recency", "fetch", "audit", "editorial", "critic", "inclusion"] as const;
                      let dropStage: string | null = null;
                      for (const s of stages) {
                        if (item.stages[s]) {
                          dropStage = s;
                          break;
                        }
                      }
                      const rowBg = index % 2 === 0 ? "#fdfdfb" : "#f6f5f0";

                      return (
                        <Fragment key={item.id}>
                          <tr className="report-candidate-row source-row" style={{ backgroundColor: rowBg }}>
                            <td style={{ borderBottom: "none", paddingBottom: "2px" }}>
                              <div className="report-candidate-source" style={{ fontWeight: 800, fontSize: "0.72rem", textTransform: "uppercase", color: "#77756f" }}>
                                {formatStage(item.source)}
                              </div>
                            </td>
                            {stages.map((stage) => {
                              const reason = item.stages[stage];
                              if (reason) {
                                return (
                                  <td key={stage} rowSpan={2} className="report-cell-dropped" style={{ verticalAlign: "middle" }}>
                                    {reason}
                                  </td>
                                );
                              }
                              
                              const stageIndex = stages.indexOf(stage);
                              const dropIndex = dropStage ? stages.indexOf(dropStage as typeof stages[number]) : -1;
                              
                              if (dropIndex !== -1 && stageIndex > dropIndex) {
                                return (
                                  <td key={stage} rowSpan={2} className="muted" style={{ fontSize: "0.8rem", textAlign: "center", verticalAlign: "middle" }}>
                                    —
                                  </td>
                                );
                              }

                              return (
                                <td key={stage} rowSpan={2} className="report-cell-advanced" style={{ verticalAlign: "middle" }}>
                                  ✓ Passed
                                </td>
                              );
                            })}
                          </tr>
                          <tr className="report-candidate-row title-row" style={{ backgroundColor: rowBg }}>
                            <td style={{ paddingTop: "2px", borderTop: "none" }}>
                              <div className="report-candidate-title">
                                {item.url ? (
                                  <a href={item.url} target="_blank" rel="noreferrer">
                                    {item.title || "Untitled Item"}
                                  </a>
                                ) : (
                                  <span>{item.title || "Untitled Item"}</span>
                                )}
                              </div>
                            </td>
                          </tr>
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )
      ) : null}
    </section>
  );
}
