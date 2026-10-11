// Small formatting helpers for the student pages.

export function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return Number.isInteger(n) ? String(n) : String(parseFloat(n.toFixed(4)));
}

export function fmtPercent(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return "—";
  return `${Math.round(p)}%`;
}

function date(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "Oct 2" */
export function shortDate(iso: string | null | undefined): string {
  const d = date(iso);
  return d ? d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
}

/** "14:00" */
export function shortTime(iso: string | null | undefined): string {
  const d = date(iso);
  return d ? d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "";
}

/** "15:17:22" */
export function clockTime(iso: string | null | undefined): string {
  const d = date(iso);
  return d ? d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
}

export function monthDay(iso: string | null | undefined): { m: string; d: string } | null {
  const d = date(iso);
  return d ? { m: d.toLocaleDateString("en-US", { month: "short" }), d: String(d.getDate()) } : null;
}

export function isToday(iso: string | null | undefined): boolean {
  const d = date(iso);
  if (!d) return false;
  const n = new Date();
  return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
}

/** "MATH 201 · Midterm" (title alone when there is no course). */
export function examName(course: string | null | undefined, title: string): string {
  return course ? `${course} · ${title}` : title;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function wordCount(text: string): number {
  const t = text.trim();
  return t ? t.split(/\s+/).length : 0;
}

export const LETTERS = "ABCDEFGH";

export function firstName(name: string | null | undefined): string {
  return (name ?? "").trim().split(/\s+/)[0] ?? "";
}
