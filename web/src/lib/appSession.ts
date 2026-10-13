// Audit 2026-10-12 (#3): every attempt is bound to one FairPlay app session. The server
// returns it from POST start; it goes along as X-FairPlay-Session on every request for
// that attempt. Kept in localStorage so a reload (or an offline restart) keeps working.

const key = (aid: string) => `fairplay:session:${aid}`;

export function getAttemptSession(aid: string): string | null {
  try {
    return localStorage.getItem(key(aid));
  } catch {
    return null;
  }
}

export function setAttemptSession(aid: string, session: string): void {
  try {
    localStorage.setItem(key(aid), session);
  } catch {
    /* storage unavailable */
  }
}

export function clearAttemptSession(aid: string): void {
  try {
    localStorage.removeItem(key(aid));
  } catch {
    /* storage unavailable */
  }
}

/** Attempt id of a student API path (/api/student/attempts/<aid>/...), or null. */
export function attemptIdOfPath(path: string): string | null {
  const m = /^\/api\/student\/attempts\/([^/?#]+)/.exec(path);
  return m?.[1] ? decodeURIComponent(m[1]) : null;
}
