import { ApiError } from "../../api/client";
import type { Exam } from "../../api/types";

/** Human message for any thrown error (ApiError detail, else a connection message). */
export function errorText(e: unknown, fallback = "Cannot reach the server. Check your connection and try again."): string {
  if (e instanceof ApiError) return e.detail || fallback;
  return fallback;
}

export const pct = (n: number | null | undefined): string =>
  n == null || Number.isNaN(n) ? "—" : `${Math.round(n * 10) / 10}%`;

export const num = (n: number | null | undefined): string =>
  n == null || Number.isNaN(n) ? "—" : String(Math.round(n * 100) / 100);

export const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export type StatusTone = "grey" | "green" | "orange" | "blue";

/** Status label for an exam row: Draft / Live / Closed / "N to grade" / "Grades released". */
export function examStatus(exam: Pick<Exam, "status" | "grades_released" | "pending_grading">): {
  label: string;
  tone: StatusTone;
  live?: boolean;
} {
  if (exam.status === "draft") return { label: "Draft", tone: "grey" };
  if (exam.status === "live") return { label: "Live", tone: "green", live: true };
  if (exam.grades_released) return { label: "Grades released", tone: "blue" };
  const pending = exam.pending_grading ?? 0;
  if (pending > 0) return { label: `${pending} to grade`, tone: "orange" };
  return { label: "Closed", tone: "grey" };
}

/** "Today, 14:00" / "Thu, 10:00" / "Dec 18, 09:00" like the design table. */
export function formatWhen(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, now)) return `Today, ${time}`;
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (sameDay(d, tomorrow)) return `Tomorrow, ${time}`;
  const diffDays = (d.getTime() - now.getTime()) / 86_400_000;
  if (diffDays > 0 && diffDays < 6) return `${d.toLocaleDateString([], { weekday: "short" })}, ${time}`;
  const opts: Intl.DateTimeFormatOptions =
    d.getFullYear() === now.getFullYear() ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", year: "numeric" };
  return `${d.toLocaleDateString([], opts)}, ${time}`;
}

/** "01:12:40" style duration. */
export function hms(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(h)}:${p(m)}:${p(s)}`;
}

/** Readable integrity event type: "blocked_shortcut" -> "Blocked shortcut". */
export function eventLabel(type: string): string {
  const known: Record<string, string> = {
    blocked_shortcut: "Blocked shortcut",
    app_switch: "Tried to switch apps",
    focus_lost: "Left the exam window",
    window_blur: "Left the exam window",
    paste: "Tried to paste",
    copy: "Tried to copy",
    lock_failed: "Lockdown failed",
    offline: "Went offline",
    fullscreen_exit: "Left full screen",
  };
  if (known[type]) return known[type];
  const s = type.replace(/[_-]+/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : "Event";
}
