// Phase 2 — AI paper grading: answer key + rubric, step-level AI suggestions, review,
// export, phone capture, manual page mapping, class struggles. Own module so the shared
// api files stay untouched.
import { ApiError, NetworkError, getToken, request } from "./client";

const enc = encodeURIComponent;

// ---------------------------------------------------------------- answer key
export interface Criterion {
  id: string;
  label: string;
  points: number;
  description: string;
  /** error carried forward: award on a wrong earlier value when the method is right */
  ecf?: boolean;
}

/** The key as LaTeX for the math check (app.scan.mathverify). */
export interface KeyLatex {
  final_latex?: string;
  units?: string;
  solution_latex?: string[];
  sig_figs?: number | null;
  latex_source?: "teacher" | "ai" | null;
}

export interface CommonMistake {
  label: string;
  tag: string | null;
  deduction: number | null;
}

export interface QuestionKey {
  solution: string;
  criteria: Criterion[];
  partial_credit: string;
  common_mistakes: CommonMistake[];
  updated_at: string | null;
  updated_by: string | null;
  /** criteria used for grading: the key's, else the essay rubric (ids r1, r2…), else none */
  effective_criteria: Criterion[];
  criteria_source: "key" | "question" | "none";
  final_latex?: string;
  units?: string;
  solution_latex?: string[];
  sig_figs?: number | null;
  latex_source?: "teacher" | "ai" | null;
}

export interface KeySuggestion extends KeyLatex {
  solution: string;
  criteria: Criterion[];
  partial_credit: string;
  common_mistakes: CommonMistake[];
  provider: string;
  model: string;
  prompt_version: string;
  created_at: string;
}

export interface KeyFile {
  id: string;
  name: string;
  kind: "pdf" | "image" | "text";
  pages: string[];
  chars: number;
  text_excerpt?: string;
  uploaded_at: string;
}

export interface KeyQuestion {
  question_id: string;
  index: number;
  type: string;
  prompt: string;
  points: number;
  options: string[];
  correct_answer: string | null;
  key: QuestionKey;
  ai_suggestion: KeySuggestion | null;
}

export interface AnswerKey {
  exam_id: string;
  subject: string;
  subjects: string[];
  misconceptions: string[];
  text: string;
  files: KeyFile[];
  ai_enabled: boolean;
  updated_at: string | null;
  questions: KeyQuestion[];
  drafted?: number;
  failed?: number;
  converted?: number;
  latex_warnings?: Record<string, string[]>;
}

export interface QuestionKeyIn {
  solution: string;
  criteria: { id?: string; label: string; points: number; description: string; ecf?: boolean }[];
  partial_credit: string;
  common_mistakes: CommonMistake[];
  final_latex?: string;
  units?: string;
  solution_latex?: string[];
  sig_figs?: number | null;
}

// ---------------------------------------------------------------- AI suggestion (attempt.ai_drafts[qid])
export type StepStatus = "correct" | "error" | "missing";

export type VerifyStatus = "verified_ok" | "verified_error" | "unverifiable";

export interface SuggestionStep {
  n: number;
  text: string;
  status: StepStatus;
  note: string;
  teacher_flipped?: boolean;
  /** the student's line as LaTeX (prompt fp-step-grade-v2) */
  latex?: string;
  /** SymPy check of this step */
  verify?: VerifyStatus;
  verify_reason?: string;
  /** correct given an earlier error (error carried forward) */
  ecf?: boolean;
  /** the AI's mark when the math check overruled it */
  model_status?: StepStatus;
  verifier_disagrees?: string;
}

export interface VerifyCheck {
  status: VerifyStatus;
  reason: string;
  student?: string | null;
  key?: string | null;
  ecf?: boolean;
}

export interface Verification {
  engine: string;
  version: string;
  steps: { n: number; status: VerifyStatus; reason: string; ecf: boolean; latex: string | null }[];
  final: VerifyCheck | null;
  units: VerifyCheck | null;
  sig_figs: { required: number; given: number | number[] | null; ok: boolean | null } | null;
  summary?: Record<VerifyStatus, number>;
  disagreements: { n: number | string; model: unknown; verifier: unknown; reason: string; changes_points: boolean }[];
  agrees?: boolean;
  /** false when nothing in the answer could be checked (prose, chemistry...): the UI shows no math check */
  checked?: boolean;
  confidence_before?: number;
  points_before?: number;
  error?: string;
}

export interface SuggestionCriterion {
  id: string;
  label: string;
  points: number;
  max_points: number;
  met: boolean;
  note: string;
  steps?: number[];
  ecf?: boolean;
  model_points?: number;
  verifier_note?: string;
}

export interface Misconception {
  tag: string;
  text: string;
}

/** Fields phase 2 adds to the legacy AiDraft (all optional so old drafts still type-check). */
export interface SuggestionFields {
  max_points?: number;
  model_points?: number | null;
  criteria?: SuggestionCriterion[];
  steps?: SuggestionStep[];
  first_error_step?: number | null;
  explanation?: string;
  teacher_note?: string;
  misconception?: Misconception | null;
  legibility?: "clear" | "partly" | "illegible" | null;
  needs_review?: boolean;
  review_reasons?: string[];
  review_reason?: string;
  provider?: string;
  model?: string;
  prompt_version?: string;
  final_answer?: { latex: string; units: string; text: string } | null;
  verification?: Verification;
  /** graph / diagram questions: checklist results with marks on the picture */
  diagram?: import("./diagrams").DraftDiagram | null;
}

/** Phase 2 fields on attempt.grades[qid]. */
export interface GradeFields {
  source?: "ai_accepted" | "ai_edited" | "manual" | string;
  ai_suggested_points?: number | null;
  graded_by?: string | null;
  grading_seconds?: number | null;
  criteria?: { id: string; label: string; points: number; max_points: number }[] | null;
  steps?: SuggestionStep[] | null;
  misconception?: Misconception | null;
  mistake_tags?: string[];
  diagram?: import("./diagrams").GradeDiagram | null;
}

export interface ReviewSave {
  points?: number;
  comment?: string;
  criteria?: { id: string; points: number }[];
  steps?: { n: number; status: StepStatus }[];
  misconception?: Misconception | null;
  grading_seconds?: number;
}

export interface AiRun {
  exam_id: string;
  running: boolean;
  total: number;
  done: number;
  started_at: string | null;
  finished_at: string | null;
  ai_enabled: boolean;
}

// ---------------------------------------------------------------- struggles + export
export interface StruggleTag {
  tag: string;
  count: number;
  approved: number;
  students: { username: string; name: string }[];
}

export interface StruggleQuestion {
  question_id: string;
  index: number;
  prompt: string;
  points: number;
  graded: number;
  total: number;
  avg_points: number | null;
  avg_percent: number | null;
  misconceptions: StruggleTag[];
}

export interface Struggles {
  exam_id: string;
  questions: StruggleQuestion[];
  time: { graded: number; seconds: number; estimate_manual_seconds: number };
}

export interface ExportInfo {
  blob: Blob;
  filename: string;
  ungraded: number;
}

// ---------------------------------------------------------------- phone capture
export interface CaptureLink {
  token: string;
  path: string;
  url: string;
  expires_at: string;
  qr_svg: string;
}

export interface CaptureInfo {
  exam_title: string;
  course: string;
  expires_at: string;
  photos: number;
  uploads: number;
  unprinted: boolean;
}

// ---------------------------------------------------------------- manual mapping
export interface MapPage {
  page_id: number;
  batch_id: string;
  seq: number;
  status: string;
  preview: string | null;
  student_id: number | null;
  page_no: number | null;
}

export interface PageMap {
  exam_id: string;
  pages_per_student: number;
  question_pages: Record<string, number>;
  questions: { question_id: string; index: number; prompt: string; type: string }[];
  students: { id: number; username: string; name: string }[];
  pages: MapPage[];
}

// ---------------------------------------------------------------- helpers
function filenameOf(res: Response, fallback: string): string {
  const cd = res.headers.get("Content-Disposition") || "";
  const m = /filename="([^"]+)"/.exec(cd);
  return m ? m[1] : fallback;
}

async function download(path: string, fallback: string): Promise<ExportInfo> {
  const token = getToken("teacher");
  let res: Response;
  try {
    res = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  } catch {
    throw new NetworkError();
  }
  if (!res.ok) {
    let detail = `Error ${res.status}`;
    try {
      const b = await res.json();
      if (b && typeof b.detail === "string") detail = b.detail;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  return {
    blob: await res.blob(),
    filename: filenameOf(res, fallback),
    ungraded: Number(res.headers.get("X-FairPlay-Ungraded") || "0"),
  };
}

/** Multipart POST (one field name, many files) with optional progress. */
export function postFiles<T>(
  path: string,
  field: string,
  files: File[],
  opts: { token?: string | null; onProgress?: (p: number) => void } = {},
): Promise<T> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach((f) => form.append(field, f, f.name));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    const token = opts.token === undefined ? getToken("teacher") : opts.token;
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable && opts.onProgress) opts.onProgress(ev.loaded / ev.total);
    };
    xhr.onerror = () => reject(new NetworkError());
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* empty */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T);
      else {
        const d = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
        reject(new ApiError(xhr.status, typeof d === "string" ? d : `Upload failed (${xhr.status})`));
      }
    };
    xhr.send(form);
  });
}

export function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const scanAiApi = {
  // answer key
  key: (id: string) => request<AnswerKey>("teacher", "GET", `/api/exams/${enc(id)}/answer-key`),
  saveKey: (id: string, body: { subject?: string; text?: string }) =>
    request<AnswerKey>("teacher", "PUT", `/api/exams/${enc(id)}/answer-key`, body),
  saveQuestionKey: (id: string, qid: string, body: QuestionKeyIn) =>
    request<{ question_id: string; key: QuestionKey; ai_suggestion: KeySuggestion | null; latex_warnings?: string[] }>(
      "teacher", "PUT", `/api/exams/${enc(id)}/answer-key/questions/${enc(qid)}`, body),
  uploadKeyFile: (id: string, file: File) => postFiles<AnswerKey>(`/api/exams/${enc(id)}/answer-key/files`, "file", [file]),
  deleteKeyFile: (id: string, fileId: string) =>
    request<AnswerKey>("teacher", "DELETE", `/api/exams/${enc(id)}/answer-key/files/${enc(fileId)}`),
  keyLatex: (id: string, body: { question_ids?: string[]; overwrite?: boolean } = {}) =>
    request<AnswerKey>("teacher", "POST", `/api/exams/${enc(id)}/answer-key/latex`, body),
  draftKey: (id: string, questionIds: string[] = []) =>
    request<AnswerKey>("teacher", "POST", `/api/exams/${enc(id)}/answer-key/ai-draft`, { question_ids: questionIds }),
  // AI grading run
  runAi: (id: string, body: { question_ids?: string[]; redo?: boolean } = {}) =>
    request<AiRun & { queued: number }>("teacher", "POST", `/api/exams/${enc(id)}/scan/ai-grade`, body),
  aiRun: (id: string) => request<AiRun>("teacher", "GET", `/api/exams/${enc(id)}/scan/ai-grade`),
  // struggles + export
  struggles: (id: string) => request<Struggles>("teacher", "GET", `/api/exams/${enc(id)}/scan/struggles`),
  exportGrades: (id: string, format: "csv" | "xlsx") =>
    download(`/api/exams/${enc(id)}/grades/export.${format}`, `grades.${format}`),
  // phone capture
  newCapture: (id: string, unprinted = false) =>
    request<CaptureLink>("teacher", "POST", `/api/exams/${enc(id)}/capture`, { unprinted }),
  captureInfo: (token: string) => request<CaptureInfo>(null, "GET", `/api/capture/${enc(token)}`),
  captureUpload: (token: string, files: File[], onProgress?: (p: number) => void) =>
    postFiles<{ batch_id: string; photos: number }>(`/api/capture/${enc(token)}/photos`, "files", files, {
      token: null,
      onProgress,
    }),
  // unprinted sheets
  uploadUnprinted: (id: string, files: File[], onProgress?: (p: number) => void) =>
    postFiles<{ batch_id: string }>(`/api/exams/${enc(id)}/scans?unprinted=1`, "files", files, { onProgress }),
  pageMap: (id: string) => request<PageMap>("teacher", "GET", `/api/exams/${enc(id)}/scans/map`),
  saveMapSettings: (id: string, body: { pages_per_student: number; question_pages: Record<string, number> }) =>
    request<PageMap>("teacher", "PUT", `/api/exams/${enc(id)}/scans/map-settings`, body),
  applyMap: (id: string, assignments: { page_id: number; student_id: number | null; page_no: number }[]) =>
    request<{ assigned: number; students: number; queued: number }>(
      "teacher", "POST", `/api/exams/${enc(id)}/scans/map`, { assignments }),
};
