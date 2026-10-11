// Homework mode (paper assignments) + progress dashboard: types and API calls.
// Own module so the shared types.ts / client.ts stay untouched (CONTRACT.md "Homework mode and progress").
import { ApiError, NetworkError, getToken, request } from "./client";
import type { QuestionInput, SessionRole } from "./types";

const enc = encodeURIComponent;

// ---------------------------------------------------------------- teacher: homework
export type HomeworkStudentStatus = "not_submitted" | "submitted" | "ai_graded" | "reviewed" | "released";

export interface HomeworkStudentRow {
  id: number;
  username: string;
  name: string;
  status: HomeworkStudentStatus;
  late: boolean;
  submitted_at: string | null;
  /** student = uploaded from the student app, scan = the teacher scanned the class stack */
  source: "student" | "scan" | null;
  processing: "processing" | "grading" | "failed" | null;
  error: string | null;
  pages: string[];
  attempt_id: string | null;
  needs_grading: number;
  score: number | null;
  percent: number | null;
}

export type HomeworkCounts = Record<HomeworkStudentStatus, number> & { total: number; late: number; handed_in: number };

export interface HomeworkSummary {
  exam_id: string;
  title: string;
  course: string;
  due_at: string;
  instructions: string;
  allow_late: boolean;
  allow_resubmit: boolean;
  auto_release: boolean;
  auto_released_at: string | null;
  worksheet: { name: string; type: string } | null;
  status: "draft" | "live" | "closed";
  grades_released: boolean;
  released_at: string | null;
  question_count: number;
  total_points: number;
  created_at: string;
  past_due: boolean;
  classes: { id: number; name: string; section: string }[];
  counts: HomeworkCounts;
}

export interface HomeworkQuestion {
  question_id: string;
  index: number;
  type: string;
  prompt: string;
  points: number;
  options: string[];
  images: { url: string; alt: string }[];
  topics?: string[];
}

export interface HomeworkDetail extends HomeworkSummary {
  questions: HomeworkQuestion[];
  ai_enabled: boolean;
  pending: number;
  students: HomeworkStudentRow[];
}

export interface HomeworkInput {
  title: string;
  course?: string;
  due_at: string;
  class_ids: number[];
  instructions?: string;
  allow_late?: boolean;
  allow_resubmit?: boolean;
  auto_release?: boolean;
  questions: QuestionInput[];
}

export type HomeworkPatch = Partial<Omit<HomeworkInput, "course">>;

// ---------------------------------------------------------------- student
export type StudentHomeworkState = "not_submitted" | "processing" | "failed" | "handed_in" | "feedback";

export interface StudentHomework {
  exam_id: string;
  title: string;
  course: string;
  due_at: string;
  past_due: boolean;
  state: StudentHomeworkState;
  late: boolean;
  overdue: boolean;
  submitted_at: string | null;
  can_submit: boolean;
  block_reason: string | null;
  allow_late: boolean;
  allow_resubmit: boolean;
  released: boolean;
  feedback_new: boolean;
  score: number | null;
  total_points: number;
  percent: number | null;
  question_count: number;
  class_names: string[];
}

export interface StudentHomeworkDetail extends StudentHomework {
  instructions: string;
  worksheet: { name: string; type: string } | null;
  questions: HomeworkQuestion[];
  pages: string[];
  error: string | null;
  ai_enabled: boolean;
}

export interface StudentNotifications {
  feedback_ready: number;
  items: { exam_id: string; title: string; released_at: string | null }[];
}

export interface TopicProgress {
  topic: string;
  mastery: number | null;
  trend: number | null;
  trend_label: TrendLabel;
  n: number;
}

export interface MyProgress {
  mastery: number | null;
  level: MasteryLevel | null;
  trend_label: TrendLabel;
  improving: TopicProgress[];
  work_on: TopicProgress[];
  strong: TopicProgress[];
  topics: TopicProgress[];
  mistakes: { label: string; count: number; topics: string[] }[];
  recent: { exam_id: string; title: string; date: string; percent: number; homework: boolean }[];
}

// ---------------------------------------------------------------- teacher: progress
export type MasteryLevel = "strong" | "developing" | "struggling";
export type TrendLabel = "slipping" | "improving" | "steady" | "new";

export interface ProgressCell {
  mastery: number | null;
  n: number;
  level: MasteryLevel | null;
  trend: number | null;
}

export interface ProgressStudentRow {
  student_id: number;
  username: string;
  name: string;
  mastery: number | null;
  level: MasteryLevel | null;
  n: number;
  assignments: number;
  trend: number | null;
  trend_label: TrendLabel;
  cells: Record<string, ProgressCell>;
}

export interface ProgressTopic {
  topic: string;
  mastery: number | null;
  level: MasteryLevel | null;
  students: number;
  struggling: { username: string; name: string; mastery: number | null }[];
}

export interface ProgressMisconception {
  label: string;
  this_week: number;
  last_week: number;
  change: number;
  approved: number;
  students: { username: string; name: string }[];
  topics: string[];
}

export interface ProgressAction {
  topic: string;
  kind: "reteach" | "practice";
  mastery: number;
  text: string;
  students: string[];
  misconception: string | null;
}

export interface TrendStudent {
  username: string;
  name: string;
  trend: number | null;
  mastery: number | null;
}

export interface ClassProgress {
  class: { id: number; name: string; section: string };
  generated_at: string;
  assignments: number;
  topics: ProgressTopic[];
  students: ProgressStudentRow[];
  misconceptions: ProgressMisconception[];
  actions: ProgressAction[];
  slipping: TrendStudent[];
  improving: TrendStudent[];
}

export interface SeriesPoint {
  exam_id: string;
  title: string;
  date: string;
  percent: number;
}

export interface StudentProgress {
  student: { id: number; username: string; name: string };
  classes: { id: number; name: string }[];
  mastery: number | null;
  level: MasteryLevel | null;
  n: number;
  trend: number | null;
  trend_label: TrendLabel;
  assignments: SeriesPoint[];
  topics: (TopicProgress & { level: MasteryLevel | null; series: SeriesPoint[] })[];
  mistakes: {
    label: string;
    count: number;
    recurring: boolean;
    topics: string[];
    last_seen: string;
    examples: {
      exam_id: string;
      title: string;
      question_id: string;
      prompt: string;
      image: string | null;
      date: string;
      comment: string;
      approved: boolean;
    }[];
  }[];
  notes: { id: number; text: string; created_at: string }[];
}

// ---------------------------------------------------------------- multipart + downloads
/** Multipart upload with progress (0..1). XHR because fetch has no upload progress. */
export function uploadForm<T>(
  role: SessionRole,
  method: string,
  path: string,
  form: FormData,
  onProgress?: (p: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, path);
    const token = getToken(role);
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
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T);
      else {
        const d = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
        reject(new ApiError(xhr.status, typeof d === "string" ? d : `Upload failed (${xhr.status})`));
      }
    };
    xhr.send(form);
  });
}

/** Download a file that needs the bearer token (worksheet). */
export async function downloadWithAuth(role: SessionRole, path: string, filename: string): Promise<void> {
  const token = getToken(role);
  let res: Response;
  try {
    res = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  } catch {
    throw new NetworkError();
  }
  if (!res.ok) throw new ApiError(res.status, res.status === 404 ? "The worksheet is not available" : `Error ${res.status}`);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// ---------------------------------------------------------------- calls
export const homeworkApi = {
  list: (classId?: number) =>
    request<HomeworkSummary[]>("teacher", "GET", `/api/homework${classId ? `?class_id=${classId}` : ""}`),
  create: (input: HomeworkInput) => request<HomeworkDetail>("teacher", "POST", "/api/homework", input),
  get: (id: string) => request<HomeworkDetail>("teacher", "GET", `/api/homework/${enc(id)}`),
  update: (id: string, patch: HomeworkPatch) =>
    request<HomeworkDetail>("teacher", "PATCH", `/api/homework/${enc(id)}`, patch),
  remove: (id: string) => request<void>("teacher", "DELETE", `/api/homework/${enc(id)}`),
  release: (id: string, force = false) =>
    request<HomeworkDetail>("teacher", "POST", `/api/homework/${enc(id)}/release`, { force }),
  unrelease: (id: string) => request<HomeworkDetail>("teacher", "POST", `/api/homework/${enc(id)}/unrelease`),
  uploadWorksheet: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return uploadForm<HomeworkSummary>("teacher", "PUT", `/api/homework/${enc(id)}/worksheet`, form);
  },
  removeWorksheet: (id: string) => request<HomeworkSummary>("teacher", "DELETE", `/api/homework/${enc(id)}/worksheet`),
  downloadWorksheet: (id: string, name: string) => downloadWithAuth("teacher", `/api/homework/${enc(id)}/worksheet`, name),
};

export const studentHomeworkApi = {
  list: () => request<StudentHomework[]>("student", "GET", "/api/student/homework"),
  get: (id: string) => request<StudentHomeworkDetail>("student", "GET", `/api/student/homework/${enc(id)}`),
  submit: (id: string, files: File[], onProgress?: (p: number) => void) => {
    const form = new FormData();
    files.forEach((f, i) => form.append("files", f, f.name || `page-${i + 1}.jpg`));
    return uploadForm<{ submission_id: string; pages: number; late: boolean; state: string }>(
      "student",
      "POST",
      `/api/student/homework/${enc(id)}/submit`,
      form,
      onProgress,
    );
  },
  downloadWorksheet: (id: string, name: string) =>
    downloadWithAuth("student", `/api/student/homework/${enc(id)}/worksheet`, name),
  notifications: () => request<StudentNotifications>("student", "GET", "/api/student/notifications"),
  progress: () => request<MyProgress>("student", "GET", "/api/student/progress"),
};

export const progressApi = {
  classProgress: (classId: number) => request<ClassProgress>("teacher", "GET", `/api/progress?class_id=${classId}`),
  student: (username: string) => request<StudentProgress>("teacher", "GET", `/api/progress/students/${enc(username)}`),
  addNote: (username: string, text: string) =>
    request<{ id: number; text: string; created_at: string }>(
      "teacher",
      "POST",
      `/api/progress/students/${enc(username)}/notes`,
      { text },
    ),
  deleteNote: (id: number) => request<void>("teacher", "DELETE", `/api/progress/notes/${id}`),
  /** One click: draft targeted practice for the struggling group (teacher still approves + sends). */
  practiceDrafts: (usernames: string[], topics: string[], count = 5) =>
    request<{ sets: { id: string }[]; skipped: { username: string; reason: string }[] }>(
      "teacher",
      "POST",
      "/api/practice/generate",
      { usernames, topics, count },
    ),
};
