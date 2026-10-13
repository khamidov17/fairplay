// Editor state for an exam, plus validation and conversion to the API body.

import type { Delivery, Exam, ExamInput, QuestionImage, QuestionInput, QuestionType } from "../../api/types";
import {
  EMPTY_EXTERNAL_SETTINGS,
  externalExamInput,
  externalSettingsFrom,
  validateExternalSettings,
  type ExternalSettingsErrors,
  type ExternalSettingsValue,
} from "../../components/teacher/externalValidation";

/** UI-level kinds. "tf" is stored as an mcq with options ["True", "False"]. */
export type DraftKind = QuestionType | "tf";

export interface DraftRubricRow {
  key: string;
  label: string;
  points: string;
}

export interface DraftQuestion {
  key: string;
  type: QuestionType;
  /** true when this mcq is a True / false question (fixed options). */
  tf: boolean;
  prompt: string;
  points: string;
  options: string[];
  correct_index: number | null;
  answer_number: string;
  tolerance: string;
  accepted: string; // comma separated
  rubric: DraftRubricRow[];
  min_words: string;
  max_words: string;
  /** slice 6: pictures above the prompt, and topic tags (≥ 1 required) */
  images: QuestionImage[];
  topics: string[];
}

export interface DraftExam {
  title: string;
  duration: string;
  course: string;
  room: string;
  /** yyyy-mm-dd (local), "" = not scheduled */
  date: string;
  /** HH:MM (local) */
  time: string;
  shuffle: boolean;
  /** Audit S-03: id of the class that takes the exam ("" = none chosen yet). */
  classId: string;
  questions: DraftQuestion[];
  /** Slice 3: where students answer (FairPlay questions or another website). */
  external: ExternalSettingsValue;
  /** Slice 4: points for a website exam; the teacher enters each student's score. */
  externalPoints: string;
  /** Slice 6: on computers / on paper / on another website (website <=> external.mode). */
  delivery: Delivery;
}

export type QuestionErrors = Partial<
  Record<
    "prompt" | "points" | "options" | "correct" | "answer" | "tolerance" | "accepted" | "rubric" | "words" | "topics",
    string
  >
>;

export interface DraftErrors {
  title?: string;
  duration?: string;
  questions?: string;
  course?: string;
  room?: string;
  schedule?: string;
  external?: ExternalSettingsErrors;
  externalPoints?: string;
  byKey: Record<string, QuestionErrors>;
}

let keyCounter = 0;
const newKey = () => `k${Date.now().toString(36)}_${++keyCounter}`;

export const TYPE_LABEL: Record<QuestionType, string> = {
  mcq: "Multiple choice",
  numeric: "Number",
  short: "Short answer",
  essay: "Essay",
  external: "Exam website",
};

export const KIND_LABEL: Record<DraftKind, string> = {
  ...TYPE_LABEL,
  tf: "True / false",
};

export const TF_OPTIONS = ["True", "False"];

export function isTrueFalse(options: string[]): boolean {
  return options.length === 2 && options[0] === "True" && options[1] === "False";
}

export function kindOf(q: { type: QuestionType; options: string[] }): DraftKind {
  return q.type === "mcq" && isTrueFalse(q.options) ? "tf" : q.type;
}

export function newRubricRow(label = "", points = ""): DraftRubricRow {
  return { key: newKey(), label, points };
}

/** 2026-10-13: switch a question's type in place; text, points, images and topics stay. */
export function changeKind(q: DraftQuestion, kind: DraftKind): DraftQuestion {
  const fresh = newQuestion(kind);
  return { ...fresh, key: q.key, prompt: q.prompt, points: q.points || fresh.points, images: q.images, topics: q.topics };
}

/** 2026-10-13: questions copied from another exam (new keys, so they are separate copies). */
export function copyQuestions(exam: Exam): DraftQuestion[] {
  return draftFromExam(exam).questions.map((q) => ({ ...q, key: newKey(), rubric: q.rubric.map((r) => ({ ...r, key: newKey() })) }));
}

export function newQuestion(kind: DraftKind): DraftQuestion {
  const type: QuestionType = kind === "tf" ? "mcq" : kind;
  return {
    key: newKey(),
    type,
    tf: kind === "tf",
    prompt: "",
    points: kind === "essay" ? "10" : "1",
    options: kind === "tf" ? [...TF_OPTIONS] : type === "mcq" ? ["", ""] : [],
    correct_index: null,
    answer_number: "",
    tolerance: type === "numeric" ? "0" : "",
    accepted: "",
    rubric: [],
    min_words: "",
    max_words: "",
    images: [],
    topics: [],
  };
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** ISO -> local date / time strings for the inputs. */
export function splitSchedule(iso: string | null | undefined): { date: string; time: string } {
  if (!iso) return { date: "", time: "" };
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return { date: "", time: "" };
  return {
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
  };
}

/** Local date + time -> ISO8601Z, or null if no date. Time defaults to 09:00. */
export function joinSchedule(date: string, time: string): string | null {
  if (!date.trim()) return null;
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = (time.trim() || "09:00").split(":").map(Number);
  const dt = new Date(y ?? NaN, (m || 1) - 1, d || 1, hh || 0, mm || 0, 0, 0);
  if (Number.isNaN(dt.getTime())) return null;
  return dt.toISOString();
}

export function draftFromExam(exam?: Exam): DraftExam {
  if (!exam)
    return {
      title: "", duration: "30", course: "", room: "", date: "", time: "", shuffle: false, classId: "", questions: [],
      external: { ...EMPTY_EXTERNAL_SETTINGS },
      externalPoints: "100",
      delivery: "computer",
    };
  const sched = splitSchedule(exam.scheduled_at);
  return {
    title: exam.title,
    duration: String(exam.duration_min),
    course: exam.course ?? "",
    room: exam.room ?? "",
    date: sched.date,
    time: sched.time,
    shuffle: !!exam.shuffle_questions,
    classId: exam.class_id == null ? "" : String(exam.class_id),
    external: externalSettingsFrom(exam),
    externalPoints: exam.mode === "external" && exam.total_points ? String(exam.total_points) : "100",
    delivery: exam.delivery ?? (exam.mode === "external" ? "website" : "computer"),
    questions: exam.questions.map((q) => ({
      key: newKey(),
      type: q.type,
      tf: q.type === "mcq" && isTrueFalse(q.options),
      prompt: q.prompt,
      points: String(q.points),
      options: q.type === "mcq" ? [...q.options] : [],
      correct_index: q.correct_index,
      answer_number: q.answer_number == null ? "" : String(q.answer_number),
      tolerance: q.tolerance == null ? "0" : String(q.tolerance),
      accepted: (q.accepted_answers ?? []).join(", "),
      rubric: (q.rubric ?? []).map((r) => newRubricRow(r.label, String(r.points))),
      min_words: q.min_words == null ? "" : String(q.min_words),
      max_words: q.max_words == null ? "" : String(q.max_words),
      images: (q.images ?? []).map((im) => ({ url: im.url, alt: im.alt ?? "" })),
      topics: [...(q.topics ?? [])],
    })),
  };
}

export function splitAccepted(s: string): string[] {
  return s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

export function parseNum(s: string): number | null {
  const t = s.trim().replace(",", ".");
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

function isPositiveInt(s: string, max: number): boolean {
  return /^\d+$/.test(s.trim()) && Number(s) >= 1 && Number(s) <= max;
}

function isNonNegInt(s: string): boolean {
  return /^\d+$/.test(s.trim());
}

/** Sum of the rubric rows' points (invalid rows count as 0). */
export function rubricSum(rows: DraftRubricRow[]): number {
  return rows.reduce((s, r) => s + (parseNum(r.points) ?? 0), 0);
}

export function validateDraft(d: DraftExam): DraftErrors {
  const errors: DraftErrors = { byKey: {} };
  if (!d.title.trim()) errors.title = "Give the exam a title.";
  if (!isPositiveInt(d.duration, 1440)) errors.duration = "Enter whole minutes between 1 and 1440.";
  if (d.course.length > 64) errors.course = "Keep the course under 64 characters.";
  if (d.room.length > 64) errors.room = "Keep the room under 64 characters.";
  if (d.time.trim() && !d.date.trim()) errors.schedule = "Pick a date for the start time.";
  if (d.external.mode === "external") {
    const ext = validateExternalSettings(d.external);
    if (Object.keys(ext).length) errors.external = ext;
    const pts = Number(d.externalPoints);
    if (!(pts > 0 && pts <= 10000)) errors.externalPoints = "Enter the exam's total points (1 to 10000).";
    return errors; // questions are not used for exams held on another website
  }
  if (d.questions.length === 0) errors.questions = "Add at least one question.";
  for (const q of d.questions) {
    const e: QuestionErrors = {};
    if (!q.prompt.trim()) e.prompt = "Write the question.";
    if (!isPositiveInt(q.points, 1000)) e.points = "Points must be a whole number from 1 to 1000.";
    // 2026-10-13: topics are optional (the server files a question under the course).
    if (q.type === "mcq") {
      if (q.options.length < 2 || q.options.length > 8) e.options = "Use between 2 and 8 options.";
      else if (q.options.some((o) => !o.trim())) e.options = "Fill in every option or remove empty ones.";
      if (q.correct_index == null || q.correct_index < 0 || q.correct_index >= q.options.length)
        e.correct = q.tf ? "Pick True or False." : "Pick the correct option.";
    } else if (q.type === "numeric") {
      if (parseNum(q.answer_number) == null) e.answer = "Enter the correct number.";
      const tol = q.tolerance.trim() === "" ? 0 : parseNum(q.tolerance);
      if (tol == null || tol < 0) e.tolerance = "Tolerance must be 0 or more.";
    } else if (q.type === "short") {
      if (splitAccepted(q.accepted).length === 0) e.accepted = "Enter at least one accepted answer.";
    } else if (q.type === "essay") {
      if (q.rubric.length > 0) {
        if (q.rubric.some((r) => !r.label.trim())) e.rubric = "Describe every rubric row or remove empty ones.";
        else if (q.rubric.some((r) => !isNonNegInt(r.points))) e.rubric = "Rubric points must be whole numbers.";
        else if (!e.points && rubricSum(q.rubric) !== Number(q.points))
          e.rubric = `Rubric points add up to ${rubricSum(q.rubric)}, but the question is worth ${q.points}.`;
      }
      const min = q.min_words.trim();
      const max = q.max_words.trim();
      if ((min && !isNonNegInt(min)) || (max && !isNonNegInt(max))) e.words = "Word limits must be whole numbers.";
      else if (min && max && Number(min) > Number(max)) e.words = "Minimum words cannot be more than maximum.";
    }
    if (Object.keys(e).length) errors.byKey[q.key] = e;
  }
  return errors;
}

export function hasErrors(e: DraftErrors): boolean {
  return !!(
    e.title || e.duration || e.questions || e.course || e.room || e.schedule ||
    (e.external && Object.keys(e.external).length) || e.externalPoints || Object.keys(e.byKey).length
  );
}

const NON_ESSAY = { rubric: [], min_words: null, max_words: null };

export function toQuestionInput(q: DraftQuestion): QuestionInput {
  const base = {
    type: q.type,
    prompt: q.prompt.trim(),
    points: Number(q.points),
    images: (q.images ?? []).map((im) => ({ url: im.url, alt: im.alt.trim() })),
    topics: [...(q.topics ?? [])],
  };
  if (q.type === "mcq") {
    return {
      ...base,
      ...NON_ESSAY,
      options: q.tf ? [...TF_OPTIONS] : q.options.map((o) => o.trim()),
      correct_index: q.correct_index,
      answer_number: null,
      tolerance: null,
      accepted_answers: [],
    };
  }
  if (q.type === "numeric") {
    return {
      ...base,
      ...NON_ESSAY,
      options: [],
      correct_index: null,
      answer_number: parseNum(q.answer_number),
      tolerance: q.tolerance.trim() === "" ? 0 : parseNum(q.tolerance),
      accepted_answers: [],
    };
  }
  if (q.type === "short") {
    return {
      ...base,
      ...NON_ESSAY,
      options: [],
      correct_index: null,
      answer_number: null,
      tolerance: null,
      accepted_answers: splitAccepted(q.accepted),
    };
  }
  return {
    ...base,
    options: [],
    correct_index: null,
    answer_number: null,
    tolerance: null,
    accepted_answers: [],
    rubric: q.rubric.map((r) => ({ label: r.label.trim(), points: Number(r.points) })),
    min_words: q.min_words.trim() ? Number(q.min_words) : null,
    max_words: q.max_words.trim() ? Number(q.max_words) : null,
  };
}

export function toExamInput(d: DraftExam): ExamInput {
  return {
    title: d.title.trim(),
    duration_min: Number(d.duration),
    course: d.course.trim(),
    room: d.room.trim(),
    scheduled_at: joinSchedule(d.date, d.time),
    shuffle_questions: d.shuffle,
    class_id: d.classId ? Number(d.classId) : null,
    questions: d.external.mode === "external" ? [] : d.questions.map(toQuestionInput),
    ...externalExamInput(d.external),
    ...(d.external.mode === "external" ? { external_points: Number(d.externalPoints) } : {}),
    delivery: d.external.mode === "external" ? "website" : d.delivery === "paper" ? "paper" : "computer",
  };
}

export function moveItem<T>(list: T[], index: number, delta: number): T[] {
  const to = index + delta;
  if (to < 0 || to >= list.length) return list;
  const next = [...list];
  const [item] = next.splice(index, 1);
  if (item === undefined) return list;
  next.splice(to, 0, item);
  return next;
}

/** One-line summary like the design: "Multiple choice · 4 options · 4 pts". */
export function questionMeta(q: DraftQuestion): string {
  const parts: string[] = [KIND_LABEL[q.tf ? "tf" : q.type]];
  if (q.type === "mcq" && !q.tf) parts.push(`${q.options.length} options`);
  if (q.type === "essay" && q.rubric.length) parts.push(`${q.rubric.length}-row rubric`);
  parts.push(`${q.points || 0} ${q.points === "1" ? "pt" : "pts"}`);
  return parts.join(" · ");
}
