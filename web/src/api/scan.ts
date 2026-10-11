// Slice 6 D — Scan & Grade (paper exams): types + API calls. Kept in its own module so
// the shared types.ts / client.ts stay untouched by this feature.
import { ApiError, NetworkError, getToken, request } from "./client";
import type { Criterion, GradeFields, ReviewSave, SuggestionFields } from "./scanAi";

const enc = encodeURIComponent;

export interface ScanStudent {
  id: number;
  username: string;
  name: string;
}

export interface Roster {
  exam_id: string;
  /** picked = chosen by the teacher; classes = union of the exam's classes; course = linked LMS course; all = every student */
  source: "picked" | "classes" | "course" | "all";
  students: ScanStudent[];
  all_students: ScanStudent[];
}

export interface PrintQuestion {
  question_id: string;
  index: number;
  type: string;
  prompt: string;
  answer_lines: number | null;
  default_lines: number | null;
}

export interface PrintInfo {
  exam_id: string;
  delivery: string;
  printed_at: string | null;
  printed_pages: number | null;
  pages_per_student: number;
  layout_changed: boolean;
  roster_source: Roster["source"];
  student_count: number;
  questions: PrintQuestion[];
}

export type BatchStatus = "queued" | "reading" | "grading" | "done" | "failed" | "stalled";

export interface ScanBatch {
  batch_id: string;
  status: BatchStatus;
  filename: string;
  page_count: number;
  processed: number;
  matched: number;
  unmatched: number;
  ignored: number;
  ai_total: number;
  ai_done: number;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  /** phase 2: "printed" (QR booklets) | "unprinted" (own sheets, mapped by hand) */
  mode?: "printed" | "unprinted";
  origin?: "upload" | "phone";
}

export interface ScanStatus {
  exam_id: string;
  ai_enabled: boolean;
  pages_per_student: number | null;
  students_expected: number;
  students_scanned: number;
  unmatched: number;
  batches: ScanBatch[];
}

export interface UnmatchedPage {
  page_id: number;
  batch_id: string;
  seq: number;
  status: string;
  reason: string;
  preview: string | null;
  name_crop: string | null;
  qr_found: number;
  page_no: number | null;
  student_id: number | null;
  suggested_name: string | null;
  suggested_student_id: number | null;
  can_crop: boolean;
}

export interface AiDraft extends SuggestionFields {
  points: number;
  comment: string;
  confidence: number;
  transcript: string;
  mistake_tags: string[];
  source: "ai" | "fallback" | "blank" | "bubbles" | "key";
  low_confidence: boolean;
  created_at: string;
}

export interface PaperGrade extends GradeFields {
  points: number;
  comment: string;
  rubric_scores: number[] | null;
  graded_at: string;
  transcript?: string;
}

export type QueueItemStatus = "needs_review" | "approved" | "auto";

export interface QueueItem {
  attempt_id: string;
  username: string;
  name: string;
  question_id: string;
  image: string | null;
  page: number | null;
  mcq: number | null;
  marks: "one" | "none" | "multiple" | "unclear" | null;
  blank: boolean;
  draft: AiDraft | null;
  grade: PaperGrade | null;
  status: QueueItemStatus;
  auto_points: number | null;
  low_confidence: boolean;
  group: string;
  /** phase 2: preview of the whole scanned page this answer is on */
  page_image?: string | null;
  /** any paper (no QR): where the answer was found; null for QR booklets / hand mapping */
  found?: {
    by: "ai" | "teacher";
    confidence: number | null;
    not_found: boolean;
    regions: { page: number; page_id: number; box: [number, number, number, number] }[];
  } | null;
  /** homework: every picture of this answer (the first one is `image`) */
  images?: string[] | null;
}

export interface QueueQuestion {
  question_id: string;
  index: number;
  type: string;
  prompt: string;
  points: number;
  images: { url: string; alt?: string }[];
  options: string[];
  correct_answer: number | string | null;
  total: number;
  to_review: number;
  approved: number;
  auto: number;
  low_confidence: number;
  /** phase 2: rubric criteria used for grading (answer key, else essay rubric) */
  criteria?: Criterion[];
  solution?: string;
  /** graph / diagram question: its checklist (the criteria are its features), else null */
  diagram?: import("./diagrams").DiagramSpec | null;
}

export interface QueueGroup {
  question_id: string;
  key: string;
  label: string;
  points: number;
  count: number;
  items: { attempt_id: string; question_id: string }[];
}

export interface ScanQueue {
  exam_id: string;
  title: string;
  course: string;
  status: string;
  grades_released: boolean;
  ai_enabled: boolean;
  low_confidence_below: number;
  questions: QueueQuestion[];
  items: QueueItem[];
  groups: QueueGroup[];
}

export interface ApproveResponse {
  attempt_id: string;
  question_id: string;
  grade: PaperGrade;
  score: number;
  total_points: number;
  percent: number;
  needs_grading: number;
}

export interface ScanExamSummary {
  exam_id: string;
  title: string;
  course: string;
  status: string;
  delivery: string;
  question_count: number;
  total_points: number;
  printed: boolean;
  batches: number;
  unmatched: number;
  paper_attempts: number;
  to_review: number;
  grades_released: boolean;
  created_at: string;
}

async function downloadBlob(path: string): Promise<Blob> {
  const token = getToken("teacher");
  let res: Response;
  try {
    res = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  } catch {
    throw new NetworkError();
  }
  if (!res.ok) {
    let detail = res.statusText || `Error ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.blob();
}

/** Multipart upload with progress (0..1). XHR because fetch has no upload progress. */
function uploadFiles(path: string, files: File[], onProgress?: (p: number) => void): Promise<{ batch_id: string }> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f, f.name));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    const token = getToken("teacher");
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable && onProgress) onProgress(ev.loaded / ev.total);
    };
    xhr.onerror = () => reject(new NetworkError());
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* empty */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as { batch_id: string });
      else {
        const d = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
        reject(new ApiError(xhr.status, typeof d === "string" ? d : `Upload failed (${xhr.status})`));
      }
    };
    xhr.send(form);
  });
}

export const scanApi = {
  exams: () => request<ScanExamSummary[]>("teacher", "GET", "/api/scan/exams"),
  roster: (id: string) => request<Roster>("teacher", "GET", `/api/exams/${enc(id)}/roster`),
  saveRoster: (id: string, usernames: string[]) =>
    request<Roster>("teacher", "PUT", `/api/exams/${enc(id)}/roster`, { usernames }),
  printInfo: (id: string) => request<PrintInfo>("teacher", "GET", `/api/exams/${enc(id)}/print`),
  saveAnswerLines: (id: string, answerLines: Record<string, number | null>) =>
    request<PrintInfo>("teacher", "PUT", `/api/exams/${enc(id)}/print-settings`, { answer_lines: answerLines }),
  printPdf: (id: string, blank = false) => downloadBlob(`/api/exams/${enc(id)}/print.pdf${blank ? "?blank=1" : ""}`),
  upload: (id: string, files: File[], onProgress?: (p: number) => void) =>
    uploadFiles(`/api/exams/${enc(id)}/scans`, files, onProgress),
  status: (id: string) => request<ScanStatus>("teacher", "GET", `/api/exams/${enc(id)}/scans`),
  unmatched: (id: string) => request<UnmatchedPage[]>("teacher", "GET", `/api/exams/${enc(id)}/scans/unmatched`),
  assign: (id: string, pageId: number, studentId: number, pageNo: number) =>
    request<{ assigned: { page_id: number; page_no: number; student_id: number }[] }>(
      "teacher", "POST", `/api/exams/${enc(id)}/scans/pages/${pageId}/assign`,
      { student_id: studentId, page_no: pageNo }),
  ignore: (id: string, pageId: number) =>
    request<UnmatchedPage>("teacher", "POST", `/api/exams/${enc(id)}/scans/pages/${pageId}/ignore`),
  rerunAi: (id: string, batchId: string) =>
    request<{ batch_id: string; queued: number }>("teacher", "POST", `/api/exams/${enc(id)}/scans/${enc(batchId)}/rerun-ai`),
  queue: (id: string) => request<ScanQueue>("teacher", "GET", `/api/exams/${enc(id)}/scan/queue`),
  approve: (id: string, aid: string, qid: string, edit?: ReviewSave) =>
    request<ApproveResponse>("teacher", "POST", `/api/exams/${enc(id)}/scan/answers/${enc(aid)}/${enc(qid)}/approve`, edit ?? {}),
  regrade: (id: string, aid: string, qid: string, hint = "") =>
    request<{ attempt_id: string; question_id: string; draft: AiDraft }>(
      "teacher", "POST", `/api/exams/${enc(id)}/scan/answers/${enc(aid)}/${enc(qid)}/regrade`, { hint }),
  approveBulk: (id: string, items: { attempt_id: string; question_id: string }[]) =>
    request<{ approved: number; skipped: number }>("teacher", "POST", `/api/exams/${enc(id)}/scan/approve-bulk`, { items }),
};
