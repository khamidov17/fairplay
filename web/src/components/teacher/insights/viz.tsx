// Slice 6 F/G: small visual pieces for Insights and Student profiles.
// Heatmap colors are ONE sequential hue (the primary blue), light = low score,
// dark = high score — never red/green. Every colored cell also prints its number.
import "../../../styles/insights.css";

/** Lower bound (percent) of each color step. */
export const HEAT_BOUNDS = [0, 40, 60, 75, 90] as const;
export const HEAT_LABELS = ["Below 40%", "40–59%", "60–74%", "75–89%", "90% and up"] as const;

/** Color step 0..4 for a percent (null = no data). */
export function heatStep(percent: number | null | undefined): number | null {
  if (percent == null || Number.isNaN(percent)) return null;
  let step = 0;
  HEAT_BOUNDS.forEach((b, i) => {
    if (percent >= b) step = i;
  });
  return step;
}

export function HeatLegend() {
  return (
    <div className="ins-legend" aria-label="Color scale: average score">
      <span className="ins-legend-title">Average score</span>
      {HEAT_LABELS.map((label, i) => (
        <span key={label} className="ins-legend-item">
          <i className={`ins-swatch ins-heat-${i}`} aria-hidden="true" />
          {label}
        </span>
      ))}
      <span className="ins-legend-item">
        <i className="ins-swatch ins-heat-none" aria-hidden="true" />
        Not asked
      </span>
    </div>
  );
}

/** "+12.5 pts" / "−4 pts" / "—" with an arrow, never color alone. */
export function TrendText({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="t-muted">—</span>;
  const r = Math.round(value * 10) / 10;
  if (r === 0) return <span className="ins-trend flat">→ no change</span>;
  const up = r > 0;
  return (
    <span className={`ins-trend ${up ? "up" : "down"}`}>
      {up ? "↑" : "↓"} {up ? "+" : "−"}
      {Math.abs(r)} pts
    </span>
  );
}

export interface SparkPoint {
  label: string;
  percent: number;
}

/** Inline SVG sparkline on a fixed 0–100 % scale. Each point has a hover title. */
export function Sparkline({ points, width = 120, height = 32, label }: {
  points: SparkPoint[];
  width?: number;
  height?: number;
  label: string;
}) {
  const pad = 5;
  if (points.length === 0) return <span className="t-muted">—</span>;
  const x = (i: number) => (points.length === 1 ? width / 2 : pad + (i * (width - 2 * pad)) / (points.length - 1));
  const y = (p: number) => pad + ((100 - Math.max(0, Math.min(100, p))) * (height - 2 * pad)) / 100;
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.percent).toFixed(1)}`).join(" ");
  const summary = points.map((p) => `${p.label}: ${Math.round(p.percent)}%`).join(", ");
  return (
    <svg
      className="ins-spark"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${label}: ${summary}`}
    >
      <line x1={pad} x2={width - pad} y1={y(60)} y2={y(60)} className="ins-spark-ref" />
      {points.length > 1 && <path d={d} className="ins-spark-line" />}
      {points.map((p, i) => (
        <circle key={i} cx={x(i)} cy={y(p.percent)} r={i === points.length - 1 ? 3.5 : 2.5} className="ins-spark-dot">
          <title>{`${p.label}: ${Math.round(p.percent * 10) / 10}%`}</title>
        </circle>
      ))}
    </svg>
  );
}

export const practiceForTopic = (topic: string) => `/teacher/practice?topic=${encodeURIComponent(topic)}`;
export const practiceForStudent = (username: string) => `/teacher/practice?student=${encodeURIComponent(username)}`;
export const profilePath = (username: string) => `/teacher/students/${encodeURIComponent(username)}`;

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}
