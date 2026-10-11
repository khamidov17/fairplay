// Countdown math. All times in ms. The server clock is the source of truth:
// offset = serverTime - clientTimeWhenReceived, so serverNow = Date.now() + offset.

export function parseTime(iso: string): number {
  return new Date(iso).getTime();
}

export function clockOffset(serverTimeIso: string, clientNow: number = Date.now()): number {
  const t = parseTime(serverTimeIso);
  return Number.isFinite(t) ? t - clientNow : 0;
}

export function remainingMs(deadlineIso: string, offset: number, clientNow: number = Date.now()): number {
  const deadline = parseTime(deadlineIso);
  if (!Number.isFinite(deadline)) return 0;
  return Math.max(0, deadline - (clientNow + offset));
}

/** 65_000 -> "1:05", 3_725_000 -> "1:02:05". Rounds up so "0:00" only shows at the end. */
export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${m}:${ss}`;
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

/** "12 s ago", "3 min ago" for heartbeat freshness. */
export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const t = parseTime(iso);
  if (!Number.isFinite(t)) return "never";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  return `${Math.round(m / 60)} h ago`;
}
