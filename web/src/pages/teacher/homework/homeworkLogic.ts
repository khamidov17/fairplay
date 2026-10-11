// Pure helpers of the teacher homework pages.
import type { HomeworkStudentRow, HomeworkStudentStatus } from "../../../api/homework";
import { draftFromExam, hasErrors, toQuestionInput, validateDraft, type DraftQuestion } from "../examDraft";

export const STATUS_LABEL: Record<HomeworkStudentStatus, string> = {
  not_submitted: "Not submitted",
  submitted: "Submitted",
  ai_graded: "AI graded",
  reviewed: "Reviewed",
  released: "Released",
};

export const STATUS_TONE: Record<HomeworkStudentStatus, string> = {
  not_submitted: "grey",
  submitted: "orange",
  ai_graded: "blue",
  reviewed: "green",
  released: "green",
};

/** Status text for one row, with "reading pages" / "failed" detail for uploads in flight. */
export function rowStatusText(r: Pick<HomeworkStudentRow, "status" | "processing">): string {
  if (r.processing === "processing") return "Reading pages…";
  if (r.processing === "grading") return "AI grading…";
  if (r.processing === "failed" && r.status === "not_submitted") return "Upload failed";
  return STATUS_LABEL[r.status];
}

/** Local date + time inputs -> ISO (UTC). */
export function dueIso(date: string, time: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const t = /^\d{2}:\d{2}$/.test(time) ? time : "23:59";
  const d = new Date(`${date}T${t}:00`);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Default due date: in 7 days at 23:59 (local). */
export function defaultDue(now: Date = new Date()): { date: string; time: string } {
  const d = new Date(now.getTime() + 7 * 86_400_000);
  return { date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`, time: "23:59" };
}

/** Validate questions with the exam editor's rules; returns per-question errors. */
export function validateQuestions(questions: DraftQuestion[]) {
  const errors = validateDraft({ ...draftFromExam(), title: "homework", duration: "60", questions });
  return { errors, ok: !hasErrors(errors), inputs: questions.map(toQuestionInput) };
}

/** Next action for the teacher, shown above the student table. */
export function nextStep(c: { handed_in: number; submitted: number; ai_graded: number; reviewed: number; released: number },
  released: boolean): string {
  if (released) return "Feedback is released. Students see their marks, step feedback and explanations.";
  if (c.handed_in === 0) return "No work yet. Students upload photos from the student app, or scan the class stack.";
  if (c.submitted > 0) return `${c.submitted} hand-in${c.submitted === 1 ? " is" : "s are"} being read and graded by the AI.`;
  if (c.ai_graded > 0) return `${c.ai_graded} student${c.ai_graded === 1 ? "" : "s"} ready for your review.`;
  return "Everything is reviewed — release the feedback.";
}
