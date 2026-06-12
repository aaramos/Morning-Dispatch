import type {
  ConfirmationDraft,
  FlowState,
  RefinementSession,
  SourceKey,
  TopicProfileResponse,
} from "./types";

const interestDraftCookieName = "morning_dispatch_interest_draft";
const interestDraftTtlSeconds = 60 * 60;
export const interestDraftTtlMs = interestDraftTtlSeconds * 1000;
const activeConversationStorageKey = "morning_dispatch_active_conversation";
export const activeConversationTtlMs = 15 * 60 * 1000;

export type ActiveConversationSnapshot = {
  flow: Extract<FlowState, "refining" | "confirm">;
  statement: string;
  submittedInterest: string;
  session: RefinementSession | null;
  answer: string;
  topicProfile: TopicProfileResponse | null;
  draft: ConfirmationDraft;
  sourceSelection: Record<SourceKey, boolean>;
  foreignRegionsDraft: string[];
  refinementTargetExplorationId: string | null;
};

type StoredActiveConversation = ActiveConversationSnapshot & {
  saved_at: number;
};

export function loadInterestDraft(): string {
  const rawCookie = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith(`${interestDraftCookieName}=`));
  if (!rawCookie) return "";
  try {
    const payload = JSON.parse(decodeURIComponent(rawCookie.split("=").slice(1).join("="))) as {
      statement?: string;
      expires_at?: number;
    };
    if (!payload.expires_at || payload.expires_at <= Date.now()) {
      clearInterestDraft();
      return "";
    }
    return typeof payload.statement === "string" ? payload.statement : "";
  } catch {
    clearInterestDraft();
    return "";
  }
}

export function saveInterestDraft(statement: string): void {
  const cleanStatement = statement.trim() ? statement : "";
  if (!cleanStatement) {
    clearInterestDraft();
    return;
  }
  const payload = encodeURIComponent(JSON.stringify({
    statement: cleanStatement,
    expires_at: Date.now() + interestDraftTtlMs,
  }));
  document.cookie = `${interestDraftCookieName}=${payload}; Max-Age=${interestDraftTtlSeconds}; Path=/; SameSite=Lax`;
}

export function clearInterestDraft(): void {
  document.cookie = `${interestDraftCookieName}=; Max-Age=0; Path=/; SameSite=Lax`;
}

function isActiveFlow(flow: unknown): flow is ActiveConversationSnapshot["flow"] {
  return flow === "refining" || flow === "confirm";
}

export function loadActiveConversationDraft(): ActiveConversationSnapshot | null {
  try {
    const raw = window.localStorage.getItem(activeConversationStorageKey);
    if (!raw) return null;
    const payload = JSON.parse(raw) as Partial<StoredActiveConversation>;
    if (!payload.saved_at || Date.now() - payload.saved_at > activeConversationTtlMs) {
      clearActiveConversationDraft();
      return null;
    }
    if (!isActiveFlow(payload.flow) || !payload.draft || !payload.sourceSelection) {
      clearActiveConversationDraft();
      return null;
    }
    return {
      flow: payload.flow,
      statement: typeof payload.statement === "string" ? payload.statement : "",
      submittedInterest: typeof payload.submittedInterest === "string" ? payload.submittedInterest : "",
      session: payload.session ?? null,
      answer: typeof payload.answer === "string" ? payload.answer : "",
      topicProfile: payload.topicProfile ?? null,
      draft: payload.draft,
      sourceSelection: payload.sourceSelection,
      foreignRegionsDraft: Array.isArray(payload.foreignRegionsDraft) ? payload.foreignRegionsDraft : [],
      refinementTargetExplorationId: typeof payload.refinementTargetExplorationId === "string"
        ? payload.refinementTargetExplorationId
        : null,
    };
  } catch {
    clearActiveConversationDraft();
    return null;
  }
}

export function saveActiveConversationDraft(snapshot: ActiveConversationSnapshot): void {
  try {
    window.localStorage.setItem(activeConversationStorageKey, JSON.stringify({
      ...snapshot,
      saved_at: Date.now(),
    } satisfies StoredActiveConversation));
  } catch {
    // Storage can fail in private or quota-limited contexts; the in-memory chat should keep working.
  }
}

export function clearActiveConversationDraft(): void {
  try {
    window.localStorage.removeItem(activeConversationStorageKey);
  } catch {
    // Best effort.
  }
}

export function loadSessionValue<T>(key: string, fallback: T): T {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}
