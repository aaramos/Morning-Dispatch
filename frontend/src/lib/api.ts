import type { RefinementSession, SourceScope } from "./types";

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

export type PodcastShowCandidate = {
  feed_url: string;
  title: string;
  description?: string;
  author?: string | null;
  site_url?: string | null;
  latest_episode_title?: string | null;
  latest_published_at?: string | null;
  stale?: boolean | null;
  subscribed?: boolean;
};

export type PodcastShowsResponse = {
  topic_id: string;
  staleness_days: number;
  queries?: string[];
  candidates: PodcastShowCandidate[];
};

export async function fetchPodcastShows(topicId: string): Promise<PodcastShowsResponse> {
  return api<PodcastShowsResponse>(`/api/explore/topic-profiles/${topicId}/podcast-shows`);
}

export async function savePodcastShows(
  topicId: string,
  shows: Array<{ feed_url: string; title: string }>,
): Promise<unknown> {
  return api(`/api/explore/topic-profiles/${topicId}/podcast-shows`, {
    method: "POST",
    body: JSON.stringify({ shows }),
  });
}

export type GmailCandidatePayload = {
  candidates: Array<{
    sender: string;
    sender_name?: string | null;
    message_count?: number;
    subject?: string | null;
    ai_rationale?: string | null;
  }>;
  intro: string;
  criteria: string;
  search_phrase: string;
  lookback_hours: number;
};

export type RefinementStreamEvent =
  | { type: "session"; session_id: string }
  | { type: "token"; text: string }
  | { type: "plan"; session: RefinementSession }
  | { type: "done"; session: RefinementSession; ready: boolean; trigger_build?: boolean }
  | { type: "gmail_candidates" } & GmailCandidatePayload
  | { type: "gmail_approved"; senders: string[] }
  | { type: "error"; message: string };

export type StrategyStreamEvent =
  | { type: "token"; text: string }
  | { type: "proposal"; session: RefinementSession }
  | { type: "done"; session: RefinementSession; has_proposal: boolean }
  | { type: "error"; message: string };

export type QueryEditTarget =
  | { kind: "general"; index: number }
  | { kind: "source"; sourceKey: string; index: number };

export type RefinementStreamBody = {
  session_id?: string | null;
  statement?: string;
  source_selection?: Record<string, boolean>;
  foreign_regions?: string[];
  recency_weighting?: SourceScope;
  lookback_hours?: number | null;
  source_scope_touched?: boolean;
  answer?: string;
  models?: Record<string, unknown>;
  just_go_now?: boolean;
};

// Generic SSE reader — POST body, yield parsed JSON events.
export async function readSSE<T>(url: string, body: unknown, onEvent: (event: T) => void): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    throw new Error(response.ok ? "Streaming is unavailable" : await response.text());
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const dataLine = rawEvent.split("\n").find((line) => line.startsWith("data:"));
      if (dataLine) {
        const payload = dataLine.slice(5).trim();
        if (payload) {
          let parsed: T | null = null;
          try {
            parsed = JSON.parse(payload) as T;
          } catch {
            // Ignore malformed frames; the stream continues.
          }
          if (parsed) onEvent(parsed);
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}

export async function streamRefinement(
  body: RefinementStreamBody,
  onEvent: (event: RefinementStreamEvent) => void,
): Promise<void> {
  return readSSE("/api/explore/refinement-sessions/stream", body, onEvent);
}

export async function streamStrategyRefinement(
  sessionId: string,
  instruction: string,
  onEvent: (event: StrategyStreamEvent) => void,
): Promise<void> {
  return readSSE(
    `/api/explore/refinement-sessions/${sessionId}/strategy/stream`,
    { instruction, models: {} },
    onEvent,
  );
}

export async function requestStrategyRefinement(sessionId: string, instruction: string): Promise<RefinementSession> {
  return api<RefinementSession>(`/api/explore/refinement-sessions/${sessionId}/strategy`, {
    method: "POST",
    body: JSON.stringify({ instruction, models: {} }),
  });
}

export async function streamStrategyReview(
  sessionId: string,
  profilePayload: Record<string, unknown>,
  onEvent: (event: StrategyStreamEvent) => void,
): Promise<void> {
  return readSSE(
    `/api/explore/refinement-sessions/${sessionId}/strategy/review/stream`,
    { profile: profilePayload, models: {} },
    onEvent,
  );
}
