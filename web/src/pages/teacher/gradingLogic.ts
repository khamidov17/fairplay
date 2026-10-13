// Pure helpers for the grading screen (tested in test/teacherGrading.test.tsx).

import type { AnswerValue, GradeInput, GradingItem, GradingQuestion } from "../../api/types";

export function isBlankAnswer(answer: AnswerValue | null | undefined): boolean {
  return answer == null || (typeof answer === "string" && answer.trim() === "");
}

/** An item still needs the teacher: no grade yet, and not a blank essay (blank essays count as graded, 0). */
export function isPending(item: GradingItem, q: Pick<GradingQuestion, "type"> | undefined): boolean {
  if (item.grade) return false;
  if (q?.type === "essay" && isBlankAnswer(item.answer)) return false;
  return true;
}

export function countWords(text: string): number {
  const t = text.trim();
  return t ? t.split(/\s+/).length : 0;
}

/** Sum of chosen rubric levels; unchosen (null) count as 0. */
export function rubricTotal(scores: (number | null)[]): number {
  return scores.reduce<number>((s, v) => s + (v ?? 0), 0);
}

export interface GradeForm {
  scores: (number | null)[];
  points: string;
  comment: string;
}

export function initialForm(item: GradingItem, q: GradingQuestion): GradeForm {
  const rubric = q.rubric ?? [];
  const g = item.grade;
  if (g) {
    const scores =
      g.rubric_scores && g.rubric_scores.length === rubric.length ? [...g.rubric_scores] : rubric.map(() => null);
    return { scores, points: String(g.points), comment: g.comment ?? "" };
  }
  let points = "";
  if (q.type !== "essay") points = String(item.auto_points ?? 0);
  else if (isBlankAnswer(item.answer)) points = "0";
  return {
    scores: rubric.map(() => (q.type === "essay" && isBlankAnswer(item.answer) ? 0 : null)),
    points,
    comment: "",
  };
}

export type BuildResult = { ok: true; input: GradeInput; total: number } | { ok: false; error: string };

/** Validate the form and produce the API body. With a rubric, points = sum of levels. */
export function buildGrade(form: GradeForm, q: GradingQuestion): BuildResult {
  const rubric = q.rubric ?? [];
  const comment = form.comment.trim();
  if (rubric.length > 0) {
    if (form.scores.length !== rubric.length || form.scores.some((s) => s == null))
      return { ok: false, error: "Pick a level for every criterion." };
    const scores = form.scores as number[];
    for (let i = 0; i < rubric.length; i++)
      if ((scores[i] ?? -1) < 0 || (scores[i] ?? 0) > (rubric[i]?.points ?? 0)) return { ok: false, error: `Criterion ${i + 1} is out of range.` };
    const total = rubricTotal(scores);
    return { ok: true, total, input: { points: total, comment, rubric_scores: scores } };
  }
  const raw = form.points.trim().replace(",", ".");
  const n = Number(raw);
  if (raw === "" || !Number.isFinite(n)) return { ok: false, error: `Enter points from 0 to ${q.points}.` };
  if (n < 0 || n > q.points) return { ok: false, error: `Points must be between 0 and ${q.points}.` };
  return { ok: true, total: n, input: { points: n, comment, rubric_scores: null } };
}

/**
 * After saving item `current` (an index into `list`), the next pending item in the
 * list, searching forward and wrapping around. null when nothing is pending.
 */
export function nextPendingIndex(
  list: GradingItem[],
  current: number,
  q: Pick<GradingQuestion, "type"> | undefined,
): number | null {
  const n = list.length;
  for (let step = 1; step <= n; step++) {
    const i = (current + step) % n;
    const item = list[i];
    if (item && isPending(item, q)) return i;
  }
  return null;
}

/** Live counts per question computed from the items (stays right after local saves). */
export function questionCounts(
  questions: GradingQuestion[],
  items: GradingItem[],
): Record<string, { total: number; pending: number; graded: number }> {
  const out: Record<string, { total: number; pending: number; graded: number }> = {};
  const byId = new Map(questions.map((q) => [q.question_id, q] as const));
  for (const q of questions) out[q.question_id] = { total: 0, pending: 0, graded: 0 };
  for (const it of items) {
    const c = out[it.question_id] ?? (out[it.question_id] = { total: 0, pending: 0, graded: 0 });
    c.total += 1;
    if (isPending(it, byId.get(it.question_id))) c.pending += 1;
    else c.graded += 1;
  }
  return out;
}
