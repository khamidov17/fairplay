// Pure helpers of the progress dashboard (heatmap colours, sorting, sparklines, trends).
import type { MasteryLevel, ProgressStudentRow, SeriesPoint, TrendLabel } from "../../../api/homework";

/** Same thresholds as the server (app.homework.progress): >= 80 strong, >= 60 developing. */
export function levelOf(mastery: number | null | undefined): MasteryLevel | null {
  if (mastery == null) return null;
  return mastery >= 80 ? "strong" : mastery >= 60 ? "developing" : "struggling";
}

export const LEVEL_LABEL: Record<MasteryLevel, string> = {
  strong: "Strong (80 %+)",
  developing: "Developing (60–79 %)",
  struggling: "Struggling (under 60 %)",
};

/** CSS class of a heatmap cell. */
export function cellClass(mastery: number | null | undefined): string {
  const l = levelOf(mastery);
  return l ? `hm-cell hm-${l}` : "hm-cell hm-none";
}

export type StudentSort = "name" | "mastery" | "trend";

export function sortStudents(rows: ProgressStudentRow[], by: StudentSort): ProgressStudentRow[] {
  const out = [...rows];
  const name = (a: ProgressStudentRow, b: ProgressStudentRow) =>
    a.name.localeCompare(b.name) || a.username.localeCompare(b.username);
  if (by === "name") return out.sort(name);
  if (by === "mastery")
    // weakest first; students without graded work last
    return out.sort((a, b) => (a.mastery ?? 1e9) - (b.mastery ?? 1e9) || name(a, b));
  return out.sort((a, b) => (a.trend ?? 1e9) - (b.trend ?? 1e9) || name(a, b));
}

export function trendText(trend: number | null | undefined, label?: TrendLabel): string {
  if (trend == null || label === "new") return "new";
  const r = Math.round(trend);
  if (r === 0) return "±0";
  return `${r > 0 ? "▲" : "▼"} ${Math.abs(r)}`;
}

/** "+3", "−2", "0" for week-over-week counts. */
export function changeText(n: number): string {
  if (n > 0) return `+${n}`;
  if (n < 0) return `−${Math.abs(n)}`;
  return "0";
}

/** SVG polyline points for a small line (0..100 % on the y axis). */
export function sparkPoints(series: Pick<SeriesPoint, "percent">[], width = 120, height = 32, pad = 3): string {
  if (series.length === 0) return "";
  const n = series.length;
  const x = (i: number) => (n === 1 ? width / 2 : pad + (i * (width - 2 * pad)) / (n - 1));
  const y = (p: number) => pad + ((100 - Math.max(0, Math.min(100, p))) * (height - 2 * pad)) / 100;
  return series.map((s, i) => `${round(x(i))},${round(y(s.percent))}`).join(" ");
}

function round(v: number): number {
  return Math.round(v * 10) / 10;
}

/** Practice page prefill link (?student=a,b&topic=x) for the struggling group. */
export function practiceLink(usernames: string[], topic: string): string {
  const p = new URLSearchParams();
  if (usernames.length) p.set("student", usernames.join(","));
  p.set("topic", topic);
  return `/teacher/practice?${p.toString()}`;
}
