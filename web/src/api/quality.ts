// Phase 3 — grading quality: types + API calls (kept in its own module so the shared
// types.ts / client.ts stay untouched). Rates are fractions 0..1; points are numbers.
import { ApiError, NetworkError, getToken, request } from "./client";

const enc = encodeURIComponent;

export interface Agreement {
  n: number;
  exact: number | null;
  within_1: number | null;
  within_10pct: number | null;
  mae: number | null;
  mae_pct: number | null;
  bias: number | null;
  kappa: number | null;
  severe_rate: number | null;
  severe: number;
  mean_ai: number | null;
  mean_human: number | null;
}

export interface AgreementGroup extends Agreement {
  key: string;
}

export interface QuestionAgreement extends Agreement {
  exam_id: string;
  question_id: string;
  question_index: number;
  prompt: string;
  type: string;
}

export interface Correction {
  ai_suggested: number;
  accepted: number;
  edited: number;
  correction_rate: number | null;
  points_changed: number;
  mean_edit: number | null;
  mean_edit_pct: number | null;
}

export interface TimeSummary {
  n: number;
  mean: number | null;
  median: number | null;
  total: number;
}

export interface Timing {
  ai_assisted: TimeSummary;
  manual: TimeSummary;
  time_saved_fraction: number | null;
  batches: { batch: string; items: number; total_seconds: number; mean_seconds: number; ai_assisted: number }[];
}

export interface Disagreement {
  exam_id: string;
  exam_title: string;
  attempt_id: string;
  anon_id: string;
  question_id: string;
  question_index: number;
  prompt: string;
  type: string;
  source: string;
  graded_by: string | number | null;
  confidence: number | null;
  ai_points: number;
  final_points: number;
  max_points: number;
  diff: number;
  severe: boolean;
}

export interface QualityBlock {
  agreement: Agreement;
  by_type: AgreementGroup[];
  by_question: QuestionAgreement[];
  correction: Correction;
  timing: Timing;
  worst: Disagreement[];
  graded: number;
  severe_fraction: number;
}

export interface WeakAreas {
  types: Record<string, number>;
  subjects: Record<string, number>;
}

export interface QualityExamRow {
  exam_id: string;
  title: string;
  course: string;
  graded: number;
  n: number;
  exact: number | null;
  within_1: number | null;
  mae: number | null;
  kappa: number | null;
  severe_rate: number | null;
  bias: number | null;
  correction_rate: number | null;
  mean_seconds_ai: number | null;
  mean_seconds_manual: number | null;
}

export interface QualityOverview extends QualityBlock {
  exams: QualityExamRow[];
  courses: string[];
  all_exams: { exam_id: string; title: string; course: string }[];
  weak_areas: WeakAreas;
  benchmarks: { set_id: string; name: string; run_id: string; finished_at: string | null; overall: Agreement | null }[];
}

export interface RoutingReason {
  code: string;
  message: string;
}

export interface RoutingSummary {
  total: number;
  routed: number;
  routed_ungraded: number;
  by_reason: Record<string, number>;
}

export interface ExamQuestionInfo {
  question_id: string;
  index: number;
  type: string;
  prompt: string;
  points: number;
  rubric: { label: string; points: number }[];
  options: string[];
  images: { url: string; alt: string }[];
}

export interface ExamQuality extends QualityBlock {
  exam_id: string;
  title: string;
  course: string;
  status: string;
  grades_released: boolean;
  questions: ExamQuestionInfo[];
  routing: RoutingSummary;
  moderation: { total: number; open: number; by_status: Record<string, number> };
}

export interface RoutingPolicy {
  confidence_threshold: number;
  review_illegible: boolean;
  review_ai_flagged: boolean;
  boundary_margin: number;
  grade_boundaries: number[];
  check_criteria: boolean;
  weak_kappa: number;
  weak_min_items: number;
  review_without_ai: boolean;
}

export interface PolicyResponse {
  policy: RoutingPolicy;
  teacher_settings: Partial<RoutingPolicy>;
  defaults: RoutingPolicy;
  exam_settings?: Partial<RoutingPolicy> | null;
  inherits?: boolean;
}

export interface RoutingItem {
  attempt_id: string;
  anon_id: string;
  question_id: string;
  review: boolean;
  reasons: RoutingReason[];
  graded: boolean;
  ai_points: number | null;
  confidence: number | null;
}

export interface ExamRouting {
  exam_id: string;
  policy: RoutingPolicy;
  weak_areas: WeakAreas;
  summary: RoutingSummary;
  items: RoutingItem[];
}

export interface GradedAnswer {
  attempt_id: string;
  anon_id: string;
  question_id: string;
  question_index: number;
  prompt: string;
  type: string;
  max_points: number;
  suggested: number | null;
  final: number;
  source: string;
}

// ---------- benchmark ----------

export interface BenchmarkRun {
  id: string;
  set_id: string;
  grader: string;
  provider: string;
  model: string;
  prompt_version: string;
  status: "running" | "done" | "failed";
  severe_fraction: number;
  item_count: number;
  done_count: number;
  metrics: {
    overall?: Agreement;
    by_type?: AgreementGroup[];
    by_subject?: AgreementGroup[];
    failed?: number;
    human_vs_human?: Agreement | null;
  };
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface BenchmarkItem {
  id: string;
  set_id: string;
  question_prompt: string;
  question_type: string;
  subject: string;
  max_points: number;
  rubric: { label: string; points: number }[];
  answer_key: string;
  answer_image: string | null;
  answer_text: string;
  human_marks: number[];
  human_points: number;
  human_comment: string;
  source: { exam_id: string; attempt_id: string; question_id: string } | null;
  consent: boolean;
  consent_by: number | null;
  consent_at: string | null;
  created_at: string;
}

export interface BenchmarkSet {
  id: string;
  name: string;
  subject: string;
  consent_note: string;
  consent_basis: string;
  created_at: string;
  item_count: number;
  last_run: BenchmarkRun | null;
}

export interface BenchmarkSetDetail extends BenchmarkSet {
  items: BenchmarkItem[];
  runs: BenchmarkRun[];
}

export interface BenchmarkSetInput {
  name: string;
  subject?: string;
  consent_note?: string;
  consent_basis?: string;
}

export interface BenchmarkItemInput {
  question_prompt: string;
  question_type: string;
  subject?: string;
  max_points: number;
  rubric?: { label: string; points: number }[];
  answer_key?: string;
  answer_text?: string;
  answer_image?: string | null;
  human_marks: number[];
  human_comment?: string;
  consent: boolean;
}

export interface RunItem {
  item_id: string;
  question_type: string;
  subject: string;
  prompt: string;
  max_points: number;
  human_points: number;
  ai_points: number | null;
  diff: number | null;
  severe: boolean;
  confidence: number | null;
  needs_review: boolean;
  explanation: string;
  error: string | null;
}

export interface BenchmarkRunDetail extends BenchmarkRun {
  set: { id: string; name: string; subject: string } | null;
  items: RunItem[];
}

export interface ImportResult {
  imported: number;
  errors: { row: number; error: string }[];
  ignored_columns: string[];
  dropped_personal_columns: string[];
}

// ---------- second marking ----------

export interface AnswerView {
  text: string | null;
  image: string | null;
}

export type SecondMarkStatus = "awaiting_second" | "awaiting_first" | "agreed" | "needs_moderation" | "moderated";

export interface SecondMarkRow {
  id: string;
  attempt_id: string;
  anon_id: string;
  username: string | null;
  name: string | null;
  question_id: string;
  question_index: number;
  prompt: string;
  type: string;
  max_points: number;
  answer: AnswerView;
  marker: { username: string; name: string } | null;
  tolerance: number;
  reason: string;
  status: SecondMarkStatus;
  first: { points: number; comment: string; graded_by: string | number | null; graded_at: string | null } | null;
  second: { points: number; comment: string; marked_at: string | null } | null;
  ai: { points: number; confidence: number | null; comment: string } | null;
  moderated: { points: number; comment: string; by: number | null; at: string | null } | null;
  created_at: string;
}

export interface SecondMarkingList {
  exam_id: string;
  title: string;
  counts: Partial<Record<SecondMarkStatus, number>>;
  open: number;
  items: SecondMarkRow[];
}

export interface SecondMarkingAssign {
  marker_username: string;
  question_ids?: string[] | null;
  sample_percent?: number | null;
  routed_only?: boolean;
  tolerance?: number | null;
}

export interface MarkerItem {
  id: string;
  exam_id: string;
  exam_title: string;
  course: string;
  anon_id: string;
  question: ExamQuestionInfo | null;
  answer: AnswerView;
  status: "to_mark" | "marked" | "closed";
  my_mark: { points: number; comment: string; marked_at: string | null } | null;
}

function query(params: Record<string, string | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${enc(v as string)}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export const qualityApi = {
  overview: (course = "", examIds: string[] = []) =>
    request<QualityOverview>("teacher", "GET", `/api/quality/overview${query({ course, exam_ids: examIds.join(",") })}`),
  exam: (examId: string) => request<ExamQuality>("teacher", "GET", `/api/exams/${enc(examId)}/quality`),
  graded: (examId: string) => request<GradedAnswer[]>("teacher", "GET", `/api/exams/${enc(examId)}/quality/graded`),
  routing: (examId: string, reviewOnly = false) =>
    request<ExamRouting>("teacher", "GET", `/api/exams/${enc(examId)}/quality/routing${reviewOnly ? "?review_only=true" : ""}`),
  examPolicy: (examId: string) =>
    request<PolicyResponse>("teacher", "GET", `/api/exams/${enc(examId)}/quality/routing-policy`),
  saveExamPolicy: (examId: string, p: Partial<RoutingPolicy>) =>
    request<PolicyResponse>("teacher", "PUT", `/api/exams/${enc(examId)}/quality/routing-policy`, p),
  resetExamPolicy: (examId: string) =>
    request<PolicyResponse>("teacher", "DELETE", `/api/exams/${enc(examId)}/quality/routing-policy`),
  defaultPolicy: () => request<PolicyResponse>("teacher", "GET", "/api/quality/routing-policy"),
  saveDefaultPolicy: (p: Partial<RoutingPolicy>) =>
    request<PolicyResponse>("teacher", "PUT", "/api/quality/routing-policy", p),
  // benchmark
  sets: () => request<BenchmarkSet[]>("teacher", "GET", "/api/quality/benchmarks"),
  createSet: (body: BenchmarkSetInput) => request<BenchmarkSet>("teacher", "POST", "/api/quality/benchmarks", body),
  set: (id: string) => request<BenchmarkSetDetail>("teacher", "GET", `/api/quality/benchmarks/${enc(id)}`),
  deleteSet: (id: string) => request<void>("teacher", "DELETE", `/api/quality/benchmarks/${enc(id)}`),
  addItem: (id: string, body: BenchmarkItemInput) =>
    request<BenchmarkItem>("teacher", "POST", `/api/quality/benchmarks/${enc(id)}/items`, body),
  addFromGrade: (id: string, body: { exam_id: string; attempt_id: string; question_id: string; consent: boolean }) =>
    request<BenchmarkItem>("teacher", "POST", `/api/quality/benchmarks/${enc(id)}/items/from-grade`, body),
  deleteItem: (id: string, itemId: string) =>
    request<void>("teacher", "DELETE", `/api/quality/benchmarks/${enc(id)}/items/${enc(itemId)}`),
  startRun: (id: string, grader?: string) =>
    request<BenchmarkRun>("teacher", "POST", `/api/quality/benchmarks/${enc(id)}/runs`, grader ? { grader } : {}),
  run: (runId: string) => request<BenchmarkRunDetail>("teacher", "GET", `/api/quality/runs/${enc(runId)}`),
  graders: () => request<{ graders: string[]; default: string; ai_enabled: boolean }>("teacher", "GET", "/api/quality/graders"),
  /** Multipart CSV (+ optional images zip) import. */
  async importCsv(id: string, csv: Blob, images: Blob | null, consent: boolean): Promise<ImportResult> {
    const token = getToken("teacher");
    const form = new FormData();
    form.append("file", csv, "benchmark.csv");
    if (images) form.append("images", images, "images.zip");
    form.append("consent", consent ? "true" : "false");
    let res: Response;
    try {
      res = await fetch(`/api/quality/benchmarks/${enc(id)}/import`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}`, Accept: "application/json" } : { Accept: "application/json" },
        body: form,
      });
    } catch {
      throw new NetworkError();
    }
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!res.ok) {
      const d = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : null;
      throw new ApiError(res.status, typeof d === "string" ? d : res.statusText || `Error ${res.status}`);
    }
    return data as ImportResult;
  },
  // second marking
  teachers: () => request<{ username: string; name: string }[]>("teacher", "GET", "/api/quality/second-marking/teachers"),
  secondMarking: (examId: string) =>
    request<SecondMarkingList>("teacher", "GET", `/api/exams/${enc(examId)}/quality/second-marking`),
  assign: (examId: string, body: SecondMarkingAssign) =>
    request<{ created: number; skipped: number; ids: string[] }>(
      "teacher", "POST", `/api/exams/${enc(examId)}/quality/second-marking`, body),
  unassign: (examId: string, smId: string) =>
    request<void>("teacher", "DELETE", `/api/exams/${enc(examId)}/quality/second-marking/${enc(smId)}`),
  moderate: (examId: string, smId: string, points: number, comment: string) =>
    request<{ id: string; status: string }>(
      "teacher", "POST", `/api/exams/${enc(examId)}/quality/second-marking/${enc(smId)}/moderate`, { points, comment }),
  queue: () => request<{ items: MarkerItem[]; to_mark: number }>("teacher", "GET", "/api/quality/second-marking/queue"),
  mark: (smId: string, points: number, comment: string) =>
    request<MarkerItem>("teacher", "PUT", `/api/quality/second-marking/${enc(smId)}/mark`, { points, comment }),
};

// ---------- formatting ----------

/** 0.734 -> "73%"; null -> "—". */
export const rate = (x: number | null | undefined): string =>
  x == null || Number.isNaN(x) ? "—" : `${Math.round(x * 100)}%`;

export const fixed = (x: number | null | undefined, digits = 2): string =>
  x == null || Number.isNaN(x) ? "—" : (Math.round(x * 10 ** digits) / 10 ** digits).toString();

/** "+0.4 pts" / "−1.2 pts" / "0 pts". */
export function signedPts(x: number | null | undefined): string {
  if (x == null || Number.isNaN(x)) return "—";
  const r = Math.round(x * 100) / 100;
  if (r === 0) return "0 pts";
  return `${r > 0 ? "+" : "−"}${Math.abs(r)} pts`;
}

/** 95 -> "1 min 35 s", 40 -> "40 s". */
export function seconds(x: number | null | undefined): string {
  if (x == null || Number.isNaN(x)) return "—";
  const s = Math.round(x);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
}

/** Kappa in words (Landis & Koch bands). */
export function kappaWord(k: number | null | undefined): string {
  if (k == null) return "not enough data";
  if (k >= 0.8) return "very strong";
  if (k >= 0.6) return "good";
  if (k >= 0.4) return "moderate";
  if (k >= 0.2) return "weak";
  return "poor";
}

export const REASON_LABEL: Record<string, string> = {
  no_ai_suggestion: "No AI suggestion",
  confidence_unknown: "Confidence unknown",
  low_confidence: "Low confidence",
  illegible_handwriting: "Hard-to-read handwriting",
  ai_flagged: "AI asked for review",
  near_boundary: "Near a grade boundary",
  criteria_inconsistent: "Criteria don't add up",
  points_out_of_range: "Points out of range",
  weak_question_type: "Weak question type",
  weak_subject: "Weak subject",
};

export const STATUS_LABEL: Record<SecondMarkStatus, string> = {
  awaiting_second: "Waiting for second mark",
  awaiting_first: "Waiting for first mark",
  agreed: "Marks agree",
  needs_moderation: "Needs moderation",
  moderated: "Moderated",
};
