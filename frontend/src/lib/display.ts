import type { Exploration } from "./types";

export function formatSourceLabel(source: string): string {
  if (source === "web_search") return "Web";
  if (source === "foreign_media") return "Foreign Media";
  if (source === "gmail") return "Gmail";
  if (source === "podcasts") return "Podcast";
  if (source === "youtube") return "YouTube";
  if (source === "collections") return "Collections";
  if (source === "markets") return "Markets";
  if (source === "google_news") return "Google News";
  return source.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function isModelDegraded(exploration: Exploration): boolean {
  if (exploration.progress.model_health?.status === "degraded") return true;
  const stats = exploration.progress.brief?.stats;
  const modelCalls = Number(stats?.model_call_count ?? 0);
  const modelSuccesses = Number(stats?.model_success_count ?? 0);
  const modelFailures = Number(stats?.model_failure_count ?? 0);
  const includedArticles = Number(stats?.included_article_count ?? 0);
  return modelCalls > 0 && (modelSuccesses === 0 || (modelFailures > 0 && includedArticles === 0));
}

export function modelDegradedMessage(exploration: Exploration): string {
  if (exploration.progress.model_health?.message) return exploration.progress.model_health.message;
  const stats = exploration.progress.brief?.stats;
  const modelCalls = Number(stats?.model_call_count ?? 0);
  const modelSuccesses = Number(stats?.model_success_count ?? 0);
  if (modelCalls > 0 && modelSuccesses === 0) {
    return "AI review did not complete; the brief was built with fallback checks.";
  }
  return "The brief finished, but AI review had failures. Rebuild after the model service is healthy.";
}

export function formatStage(value: string): string {
  return value.split("_").filter(Boolean).map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`).join(" ");
}

export function progressHeadline(exploration: Exploration): string {
  if (exploration.status === "queued") return "Waiting its turn";
  if (exploration.status === "failed") return "Build failed";
  if (isModelDegraded(exploration)) return "Brief built with AI issues";
  const running = Object.entries(exploration.progress.pipeline ?? {}).find(([, status]) => status === "running");
  if (!running) return exploration.status === "complete" ? "Brief ready" : "Preparing build";
  const labels: Record<string, string> = {
    discovery: "Discovering sources",
    fetch: "Fetching source content",
    summarize: "Enriching and translating items",
    audit: "Auditing source fit",
    rank: "Ranking the complete set",
    review: "Reviewing the brief",
    done: "Rendering the brief",
  };
  return labels[running[0]] ?? `${formatStage(running[0])} running`;
}

export function progressDetail(exploration: Exploration): string {
  if (exploration.status === "queued") {
    return exploration.progress.queue?.message ?? "Queued behind another brief. It will start automatically.";
  }
  if (exploration.status === "failed") return exploration.progress.error ?? "The build stopped before the brief was ready.";
  if (isModelDegraded(exploration)) return modelDegradedMessage(exploration);
  if (exploration.progress.source_audit?.message) return exploration.progress.source_audit.message;
  if (exploration.progress.source_audit?.summary) return exploration.progress.source_audit.summary;
  const candidateCount = exploration.progress.candidate_count ?? 0;
  const running = Object.entries(exploration.progress.pipeline ?? {}).find(([, status]) => status === "running")?.[0];
  if (running === "discovery") return "Searching every selected source from scratch.";
  if (running === "fetch") return `Fetching full content for ${candidateCount || "the discovered"} candidate items.`;
  if (running === "summarize") return "Cleaning, summarizing, and translating usable source material.";
  if (running === "audit") return "Checking whether the retrieved sources match the requested strategy.";
  if (running === "rank") return "Comparing all candidates together before choosing the lead stories.";
  if (running === "review") return "The critic is checking quality, cuts, and adherence before publish.";
  if (running === "done") return "Writing the finished brief HTML.";
  return "Preparing the full rebuild pipeline.";
}
