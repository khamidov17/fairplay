// Pure logic for graph / diagram questions (unit-tested in src/test/diagrams.test.tsx).
import type {
  Box,
  DiagramFeature,
  DiagramKindInfo,
  DiagramSpec,
  DraftFeature,
  FeatureStatus,
  Point,
} from "../../api/diagrams";

/** Share `total` points by weight in multiples of `step` (largest remainder); sums exactly (same as the server). */
export function splitPoints(total: number, weights: number[], step = 0.5): number[] {
  if (weights.length === 0) return [];
  const wsum = weights.reduce((s, w) => s + w, 0) || 1;
  let units = Math.round(total / step);
  if (Math.abs(units * step - total) > 1e-9) units = Math.floor(total / step);
  const raw = weights.map((w) => (units * w) / wsum);
  const base = raw.map((r) => Math.floor(r));
  let left = units - base.reduce((s, b) => s + b, 0);
  const order = raw
    .map((_, i) => i)
    .sort((a, b) => raw[b] - base[b] - (raw[a] - base[a]) || weights[b] - weights[a] || a - b);
  for (const i of order) {
    if (left <= 0) break;
    base[i] += 1;
    left -= 1;
  }
  const out = base.map((b) => b * step);
  out[out.length - 1] = round2(out[out.length - 1] + (total - out.reduce((s, v) => s + v, 0)));
  return out;
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/** Rescale a checklist to new question points, keeping the relative weights. */
export function rescaleFeatures(features: DiagramFeature[], total: number): DiagramFeature[] {
  if (features.length === 0) return [];
  let weights = features.map((f) => Math.max(0, Number(f.points) || 0));
  if (weights.every((w) => w === 0)) weights = features.map(() => 1);
  const pts = splitPoints(total, weights);
  return features.map((f, i) => ({ ...f, points: pts[i] }));
}

/** A fresh spec for `kind` with its default checklist sized to `total` points. */
export function defaultSpec(kind: DiagramKindInfo, total: number): DiagramSpec {
  return {
    kind: kind.kind,
    features: rescaleFeatures(
      kind.features.map((f) => ({ ...f, points_xy: f.points_xy ? [...f.points_xy] : undefined })),
      total,
    ),
    reference_image: null,
    notes: "",
  };
}

export function checklistTotal(features: { points: number }[]): number {
  return round2(features.reduce((s, f) => s + (Number.isFinite(Number(f.points)) ? Number(f.points) : 0), 0));
}

/** Error text for a checklist, or null when it can be saved. */
export function checklistError(spec: DiagramSpec, max: number): string | null {
  if (spec.features.length === 0) return "Add at least one checklist feature.";
  for (const f of spec.features) {
    if (!f.label.trim()) return "Each checklist feature needs a label.";
    if (!Number.isFinite(Number(f.points)) || Number(f.points) < 0) return `“${f.label}” needs points (0 or more).`;
  }
  const t = checklistTotal(spec.features);
  if (Math.abs(t - max) > 1e-6) return `Checklist points must add up to ${max} (now ${t}).`;
  return null;
}

/** "1, 2; 2, 4" -> [[1, 2], [2, 4]]; null when it cannot be read. */
export function parsePointList(text: string): [number, number][] | null {
  const t = text.trim();
  if (!t) return [];
  const out: [number, number][] = [];
  for (const part of t.split(/[;\n]+/)) {
    const s = part.replace(/[()[\]]/g, "").trim();
    if (!s) continue;
    const nums = s.split(/[,\s]+/).filter(Boolean).map(Number);
    if (nums.length !== 2 || nums.some((n) => !Number.isFinite(n))) return null;
    out.push([nums[0], nums[1]]);
  }
  return out;
}

export function formatPointList(xy: [number, number][] | undefined): string {
  return (xy ?? []).map(([x, y]) => `${x}, ${y}`).join("; ");
}

/** Status of a feature from the points the teacher (or AI) gives now. */
export function statusFor(points: number, max: number, aiStatus?: FeatureStatus): FeatureStatus {
  if (max > 0 && Math.abs(points - max) < 1e-9) return "present";
  if (points <= 1e-9) {
    if (aiStatus === "missing" || aiStatus === "wrong" || aiStatus === "not_assessed") return aiStatus;
    return "wrong";
  }
  return "partial";
}

export const STATUS_WORD: Record<FeatureStatus, string> = {
  present: "Correct",
  wrong: "Wrong",
  missing: "Missing",
  partial: "Partly right",
  not_assessed: "Not checked",
};

export const STATUS_ICON: Record<FeatureStatus, string> = {
  present: "✓",
  wrong: "✗",
  missing: "✗",
  partial: "◐",
  not_assessed: "?",
};

export interface OverlayMark {
  id: string;
  n: number;
  label: string;
  status: FeatureStatus;
  ok: boolean;
  kind: "box" | "point";
  /** CSS percentages, relative to the image box */
  style: { left: string; top: string; width?: string; height?: string };
  title: string;
}

function pct(v: number): string {
  return `${Math.round(Math.min(1, Math.max(0, v)) * 10000) / 100}%`;
}

/** CSS placement for a normalised box or point over the picture. */
export function markStyle(where: { box?: Box | null; point?: Point | null }): OverlayMark["style"] | null {
  if (where.box) {
    const [x0, y0, x1, y1] = where.box;
    return { left: pct(x0), top: pct(y0), width: pct(x1 - x0), height: pct(y1 - y0) };
  }
  if (where.point) return { left: pct(where.point[0]), top: pct(where.point[1]) };
  return null;
}

/**
 * Overlay marks for the features that have a place on the picture. `points` (feature id -> points
 * given now) recomputes the ✓/✗ when the teacher toggles a feature; without it the stored status is used.
 */
export function overlayMarks(features: DraftFeature[], points?: Record<string, number>): OverlayMark[] {
  const out: OverlayMark[] = [];
  features.forEach((f, i) => {
    const style = markStyle(f);
    if (!style) return;
    const pts = points && f.id in points ? points[f.id] : f.points;
    const status = points ? statusFor(pts, f.max_points, f.status) : f.status;
    out.push({
      id: f.id,
      n: i + 1,
      label: f.label,
      status,
      ok: status === "present",
      kind: f.box ? "box" : "point",
      style,
      title: `${i + 1}. ${f.label}: ${STATUS_WORD[status]}${f.note ? ` — ${f.note}` : ""}`,
    });
  });
  return out;
}
