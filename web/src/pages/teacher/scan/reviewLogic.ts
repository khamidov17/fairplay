// Pure logic of the side-by-side review screen (unit-tested in src/test/scanReview.test.tsx).
import type { QueueItem } from "../../../api/scan";
import type { Criterion, Misconception, ReviewSave, StepStatus, SuggestionStep } from "../../../api/scanAi";

export interface ReviewForm {
  points: string;
  /** criterion id -> points typed/checked */
  crit: Record<string, number>;
  steps: SuggestionStep[];
  comment: string;
  misconception: Misconception | null;
  /** what the teacher touched (decides between "accept as is" and an edit) */
  touched: { points: boolean; crit: boolean; steps: boolean; comment: boolean; misconception: boolean };
}

const NONE = { points: false, crit: false, steps: false, comment: false, misconception: false };

/** Has an AI (or rule) suggestion the teacher can accept as is. */
export function hasSuggestion(it: QueueItem): boolean {
  return !!it.draft && it.draft.source !== "fallback";
}

/** Initial form for an item: the teacher's grade, else the AI suggestion, else empty. */
export function initialForm(it: QueueItem, criteria: Criterion[]): ReviewForm {
  const g = it.grade;
  const d = hasSuggestion(it) ? it.draft : null;
  const crit: Record<string, number> = {};
  const source = g?.criteria ?? d?.criteria ?? null;
  for (const c of criteria) {
    const hit = source?.find((x) => x.id === c.id);
    crit[c.id] = hit ? hit.points : 0;
  }
  let points = "";
  if (g) points = String(g.points);
  else if (it.status === "auto" && it.auto_points !== null) points = String(it.auto_points);
  else if (d) points = String(d.points);
  return {
    points,
    crit,
    steps: (g?.steps ?? d?.steps ?? []).map((s) => ({ ...s })),
    comment: g ? g.comment : d ? (d.explanation ?? d.comment ?? "") : "",
    misconception: (g ? g.misconception : d?.misconception) ?? null,
    touched: { ...NONE },
  };
}

export function critTotal(crit: Record<string, number>): number {
  return Math.round(Object.values(crit).reduce((s, v) => s + v, 0) * 100) / 100;
}

/** Tick/untick a criterion (full points / 0); the total is recomputed. */
export function toggleCriterion(form: ReviewForm, c: Criterion, on: boolean): ReviewForm {
  return setCriterion(form, c, on ? c.points : 0);
}

export function setCriterion(form: ReviewForm, c: Criterion, value: number): ReviewForm {
  const v = Math.max(0, Math.min(c.points, Number.isFinite(value) ? value : 0));
  const crit = { ...form.crit, [c.id]: v };
  return { ...form, crit, points: String(critTotal(crit)), touched: { ...form.touched, crit: true, points: false } };
}

/** Number key / typed total: overrides the criteria breakdown. */
export function setPoints(form: ReviewForm, value: string): ReviewForm {
  return { ...form, points: value, touched: { ...form.touched, points: true, crit: false } };
}

const CYCLE: Record<StepStatus, StepStatus> = { correct: "error", error: "missing", missing: "correct" };

/** Flip a step's mark: correct -> error -> missing -> correct. */
export function flipStep(form: ReviewForm, n: number): ReviewForm {
  return {
    ...form,
    steps: form.steps.map((s) => (s.n === n ? { ...s, status: CYCLE[s.status], teacher_flipped: true } : s)),
    touched: { ...form.touched, steps: true },
  };
}

export function firstErrorStep(steps: SuggestionStep[]): number | null {
  return steps.find((s) => s.status !== "correct")?.n ?? null;
}

export function isDirty(form: ReviewForm): boolean {
  return Object.values(form.touched).some(Boolean);
}

/** What to send when the teacher accepts. Returns an error string when it cannot be saved. */
export function buildSave(
  it: QueueItem,
  form: ReviewForm,
  criteria: Criterion[],
  max: number,
  seconds: number,
): { body: ReviewSave } | { error: string } | { skip: true } {
  const grading_seconds = Math.max(0, Math.round(seconds * 10) / 10);
  if (!isDirty(form)) {
    if (it.status === "approved") return { skip: true }; // keep the teacher's grade as it is
    if (it.status === "auto" || hasSuggestion(it)) return { body: { grading_seconds } };
  }
  if (form.points.trim() === "") return { error: "Enter the points." };
  const pts = Number(form.points);
  if (!Number.isFinite(pts)) return { error: "Points must be a number." };
  if (pts < 0 || pts > max) return { error: `Points must be between 0 and ${max}.` };
  const body: ReviewSave = { points: pts, comment: form.comment, grading_seconds };
  if (form.touched.crit && criteria.length > 0) {
    body.criteria = criteria.map((c) => ({ id: c.id, points: form.crit[c.id] ?? 0 }));
    body.points = critTotal(form.crit);
  }
  if (form.touched.steps) body.steps = form.steps.map((s) => ({ n: s.n, status: s.status }));
  if (form.touched.misconception) body.misconception = form.misconception ?? { tag: "", text: "" };
  return { body };
}

/** Rough time a teacher needs per answer without AI (seconds), used for "time saved". */
export function manualEstimateSeconds(type: string): number {
  if (type === "essay") return 120;
  if (type === "mcq") return 10;
  return 30;
}

export interface TimeSummary {
  graded: number;
  seconds: number;
  estimate: number;
  saved: number;
}

/** Sum of grading_seconds of answers graded in this screen vs. a by-hand estimate. */
export function timeSummary(items: QueueItem[], typeOf: (qid: string) => string): TimeSummary {
  let graded = 0;
  let seconds = 0;
  let estimate = 0;
  for (const it of items) {
    const s = it.grade?.grading_seconds;
    if (it.status !== "approved" || s === null || s === undefined) continue;
    graded += 1;
    seconds += s;
    estimate += manualEstimateSeconds(typeOf(it.question_id));
  }
  return { graded, seconds, estimate, saved: Math.max(0, estimate - seconds) };
}

export function minutes(sec: number): string {
  if (sec < 90) return `${Math.round(sec)} s`;
  return `${Math.round(sec / 60)} min`;
}
