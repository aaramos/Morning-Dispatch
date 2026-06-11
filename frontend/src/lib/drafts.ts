const interestDraftCookieName = "morning_dispatch_interest_draft";
const interestDraftTtlSeconds = 60 * 60;
export const interestDraftTtlMs = interestDraftTtlSeconds * 1000;

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

export function loadSessionValue<T>(key: string, fallback: T): T {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}
