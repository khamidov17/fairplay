import type {
  AnswerMap,
  AttemptView,
  EventsResponse,
  Exam,
  ExamInput,
  EventReview,
  GradeInput,
  GradeResponse,
  Gradebook,
  GradingQueue,
  GradingSummary,
  StudentFeedback,
  StudentGradeSummary,
  HeartbeatResponse,
  IntegrityEvent,
  LiveRoom,
  LoginResponse,
  Me,
  Results,
  Role,
  SaveAnswersResponse,
  StudentExamSummary,
  SubmitResponse,
} from "./types";
import type {
  LmsConnection,
  LmsConnectionInput,
  LmsCourse,
  LmsCourseLink,
  LmsLinkResponse,
  LmsProvider,
  LmsPushResult,
} from "./types"; // slice 5

import * as bridge from "../lib/bridge";
import { attemptIdOfPath, getAttemptSession, setAttemptSession } from "../lib/appSession";

// ---------- token storage (one token per role) ----------

export const tokenKey = (role: Role) => `proctora:token:${role}`;

export function getToken(role: Role): string | null {
  try {
    return localStorage.getItem(tokenKey(role));
  } catch {
    return null;
  }
}

export function setToken(role: Role, token: string): void {
  try {
    localStorage.setItem(tokenKey(role), token);
  } catch {
    /* storage unavailable */
  }
}

export function clearToken(role: Role): void {
  try {
    localStorage.removeItem(tokenKey(role));
  } catch {
    /* storage unavailable */
  }
}

// ---------- errors ----------

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Network failure (offline, server down). Callers treat it as "try again later". */
export class NetworkError extends Error {
  constructor(message = "Cannot reach the server") {
    super(message);
    this.name = "NetworkError";
  }
}

// ---------- 401 handling ----------

type UnauthorizedHandler = (role: Role) => void;

let onUnauthorized: UnauthorizedHandler = (role) => {
  window.location.assign(`/${role}/login`);
};

/** Override the 401 behaviour (the app uses router navigation; tests use a spy). */
export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  onUnauthorized = handler;
}

// ---------- core request ----------

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d.length > 0) {
      // FastAPI 422 validation list
      return d
        .map((e) => (e && typeof e === "object" && "msg" in e ? String((e as { msg: unknown }).msg) : String(e)))
        .join("; ");
    }
  }
  return fallback;
}

export async function request<T>(
  role: Role | null,
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = role ? getToken(role) : null;
  if (token) headers.Authorization = `Bearer ${token}`;
  if (role === "student") {
    // Audit #3: the attempt's app session goes along with every request for it.
    const aid = attemptIdOfPath(path);
    const sess = aid ? getAttemptSession(aid) : null;
    if (sess) headers["X-FairPlay-Session"] = sess;
  }

  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new NetworkError();
  }

  let data: unknown = null;
  if (res.status !== 204) {
    const text = await res.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
  }

  if (!res.ok) {
    if (res.status === 401 && role) {
      clearToken(role);
      onUnauthorized(role);
    }
    throw new ApiError(res.status, detailOf(data, res.statusText || `Error ${res.status}`));
  }
  return data as T;
}

const enc = encodeURIComponent;

// ---------- auth ----------

export const auth = {
  /** Does not send a token; a 401 here means wrong credentials, never a redirect. */
  login(username: string, password: string): Promise<LoginResponse> {
    return request<LoginResponse>(null, "POST", "/api/auth/login", { username, password });
  },
  me(role: Role): Promise<Me> {
    return request<Me>(role, "GET", "/api/me");
  },
};

// ---------- teacher ----------

/** ?course=…&class_id=… for the gradebook (2026-10-13: by class). */
function gbQuery(course: string, classId: number | null): string {
  const q = new URLSearchParams();
  if (course) q.set("course", course);
  if (classId != null) q.set("class_id", String(classId));
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const teacherApi = {
  listExams: () => request<Exam[]>("teacher", "GET", "/api/exams"),
  createExam: (input: ExamInput) => request<Exam>("teacher", "POST", "/api/exams", input),
  getExam: (id: string) => request<Exam>("teacher", "GET", `/api/exams/${enc(id)}`),
  updateExam: (id: string, input: ExamInput) => request<Exam>("teacher", "PUT", `/api/exams/${enc(id)}`, input),
  deleteExam: (id: string) => request<void>("teacher", "DELETE", `/api/exams/${enc(id)}`),
  openExam: (id: string) => request<Exam>("teacher", "POST", `/api/exams/${enc(id)}/open`),
  closeExam: (id: string) => request<Exam>("teacher", "POST", `/api/exams/${enc(id)}/close`),
  /** Audit #13: new start code for a live exam. */
  rotateCode: (id: string) => request<Exam>("teacher", "POST", `/api/exams/${enc(id)}/rotate-code`),
  /** Let a student continue on another computer (the old app session stops working). */
  releaseDevice: (examId: string, aid: string) =>
    request<{ ok: boolean }>("teacher", "POST", `/api/exams/${enc(examId)}/attempts/${enc(aid)}/release-device`),
  live: (id: string) => request<LiveRoom>("teacher", "GET", `/api/exams/${enc(id)}/live`),
  /** 8-digit exit code for one student's locked Mac that cannot reach the server
   *  (the Mac shows a Mac code). Only works on that Mac's current session; logged. */
  offlineExit: (id: string, aid: string, macCode: string) =>
    request<{ code: string }>("teacher", "POST", `/api/exams/${enc(id)}/attempts/${enc(aid)}/offline-exit`, {
      mac_code: macCode,
    }),
  results: (id: string) => request<Results>("teacher", "GET", `/api/exams/${enc(id)}/results`),
  extend: (id: string, minutes: number) =>
    request<Exam>("teacher", "POST", `/api/exams/${enc(id)}/extend`, { minutes }),
  reviewEvent: (id: string, eventId: number, status: Exclude<EventReview, "open">, note = "") =>
    request<{ event_id: number; review: EventReview; note: string }>(
      "teacher", "POST", `/api/exams/${enc(id)}/events/${eventId}/review`, { status, note }),
  grading: (id: string) => request<GradingQueue>("teacher", "GET", `/api/exams/${enc(id)}/grading`),
  saveGrade: (id: string, aid: string, qid: string, grade: GradeInput) =>
    request<GradeResponse>("teacher", "PUT", `/api/exams/${enc(id)}/attempts/${enc(aid)}/grades/${enc(qid)}`, grade),
  clearGrade: (id: string, aid: string, qid: string) =>
    request<GradeResponse>("teacher", "DELETE", `/api/exams/${enc(id)}/attempts/${enc(aid)}/grades/${enc(qid)}`),
  saveComment: (id: string, aid: string, comment: string) =>
    request<{ attempt_id: string; overall_comment: string }>(
      "teacher", "PUT", `/api/exams/${enc(id)}/attempts/${enc(aid)}/comment`, { comment }),
  release: (id: string, force = false) =>
    request<Exam>("teacher", "POST", `/api/exams/${enc(id)}/release`, { force }),
  unrelease: (id: string) => request<Exam>("teacher", "POST", `/api/exams/${enc(id)}/unrelease`),
  gradingSummary: () => request<GradingSummary[]>("teacher", "GET", "/api/grading"),
  gradebook: (course = "", classId: number | null = null) =>
    request<Gradebook>("teacher", "GET", `/api/gradebook${gbQuery(course, classId)}`),
  /** Downloads the CSV with the auth header and returns it as a Blob. */
  async gradebookCsv(course = "", classId: number | null = null): Promise<Blob> {
    const token = getToken("teacher");
    let res: Response;
    try {
      res = await fetch(`/api/gradebook.csv${gbQuery(course, classId)}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
    } catch {
      throw new NetworkError();
    }
    if (!res.ok) throw new ApiError(res.status, res.statusText || `Error ${res.status}`);
    return res.blob();
  },
};

// ---------- student ----------

/**
 * POST start (audit #1): inside the FairPlay app it first gets a single-use challenge and
 * has the app sign it; in a browser it sends no proof (the server refuses unless
 * FAIRPLAY_REQUIRE_APP=0). Stores the returned app session for the attempt.
 */
async function startAttempt(examId: string, startCode: string, offlineStartedAt?: string): Promise<AttemptView> {
  const body: Record<string, unknown> = { start_code: startCode };
  if (offlineStartedAt) body.offline_started_at = offlineStartedAt;
  if (bridge.isNative()) {
    const ch = await request<{ nonce: string }>("student", "GET", `/api/student/exams/${enc(examId)}/challenge`);
    const proof = await bridge.attest(ch.nonce);
    if (proof) body.client = proof;
  }
  const view = await request<AttemptView>("student", "POST", `/api/student/exams/${enc(examId)}/start`, body);
  if (view.session) {
    setAttemptSession(view.attempt_id, view.session);
    const token = getToken("student");
    if (token) bridge.setExamSession({ attempt_id: view.attempt_id, token, session: view.session });
  }
  return view;
}

export const studentApi = {
  listExams: () => request<StudentExamSummary[]>("student", "GET", "/api/student/exams"),
  start: (examId: string, startCode: string) => startAttempt(examId, startCode),
  getAttempt: (aid: string) => request<AttemptView>("student", "GET", `/api/student/attempts/${enc(aid)}`),
  saveAnswers: (aid: string, answers: AnswerMap, clientSeq: number) =>
    request<SaveAnswersResponse>("student", "PUT", `/api/student/attempts/${enc(aid)}/answers`, {
      answers,
      client_seq: clientSeq,
    }),
  /** `locked` is informational only: the server takes lock state from the app's signed beats. */
  heartbeat: (aid: string, currentQuestion: number, locked: boolean) =>
    request<HeartbeatResponse>("student", "POST", `/api/student/attempts/${enc(aid)}/heartbeat`, {
      current_question: currentQuestion,
      locked,
    }),
  events: (aid: string, events: IntegrityEvent[]) =>
    request<EventsResponse>("student", "POST", `/api/student/attempts/${enc(aid)}/events`, { events }),
  submit: (aid: string, answers: AnswerMap, clientSeq: number) =>
    request<SubmitResponse>("student", "POST", `/api/student/attempts/${enc(aid)}/submit`, {
      answers,
      client_seq: clientSeq,
    }),
  grades: () => request<StudentGradeSummary[]>("student", "GET", "/api/student/grades"),
  feedback: (examId: string) =>
    request<StudentFeedback>("student", "GET", `/api/student/grades/${enc(examId)}`),
};

// ---------- teacher: LMS sync (slice 5) ----------

export const lmsApi = {
  providers: () => request<LmsProvider[]>("teacher", "GET", "/api/lms/providers"),
  connections: () => request<LmsConnection[]>("teacher", "GET", "/api/lms/connections"),
  connect: (input: LmsConnectionInput) =>
    request<LmsConnection>("teacher", "POST", "/api/lms/connections", input),
  disconnect: (id: number) => request<void>("teacher", "DELETE", `/api/lms/connections/${id}`),
  courses: (connectionId: number) =>
    request<LmsCourse[]>("teacher", "GET", `/api/lms/connections/${connectionId}/courses`),
  linkCourse: (connectionId: number, lmsCourseId: string) =>
    request<LmsLinkResponse>(
      "teacher", "POST", `/api/lms/connections/${connectionId}/courses/${enc(lmsCourseId)}/link`),
  resync: (linkId: number) => request<LmsLinkResponse>("teacher", "POST", `/api/lms/courses/${linkId}/sync`),
  linkedCourses: () => request<LmsCourseLink[]>("teacher", "GET", "/api/lms/courses"),
  linkExam: (examId: string, courseLinkId: number | null) =>
    request<Exam>("teacher", "PUT", `/api/exams/${enc(examId)}/lms`, { course_link_id: courseLinkId }),
  pushGrades: (examId: string) =>
    request<LmsPushResult>("teacher", "POST", `/api/exams/${enc(examId)}/lms/push`),
};

// ---------- slice 6: uploads, topics, offline packages ----------

import type { OfflinePackage, TopicCount, UploadResult } from "./types";

export const slice6Api = {
  /** POST /api/uploads (multipart). PNG/JPEG/GIF/WebP up to 5 MB. */
  async uploadImage(file: Blob, filename = "image"): Promise<UploadResult> {
    const token = getToken("teacher");
    const form = new FormData();
    form.append("file", file, filename);
    let res: Response;
    try {
      res = await fetch("/api/uploads", {
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
      if (res.status === 401) {
        clearToken("teacher");
        onUnauthorized("teacher");
      }
      throw new ApiError(res.status, detailOf(data, res.statusText || `Error ${res.status}`));
    }
    return data as UploadResult;
  },
  topics: () => request<TopicCount[]>("teacher", "GET", "/api/topics"),
  examPackage: (examId: string) =>
    request<OfflinePackage>("student", "GET", `/api/student/exams/${enc(examId)}/package`),
  /** Audit S-03: second half of the package key, only while the exam is live (class members only). */
  packageKey: (examId: string) =>
    request<{ exam_id: string; key: string }>("student", "GET", `/api/student/exams/${enc(examId)}/package-key`),
  /** Start (or resume) after working offline: the server sets the deadline from offline_started_at. */
  startAfterOffline: (examId: string, startCode: string, offlineStartedAt: string) =>
    startAttempt(examId, startCode, offlineStartedAt),
};

// ---------- slice 6 F/G: class insights + student profiles ----------

import type { Insights, StudentProfile, StudentSummary } from "./types";

function query(params: Record<string, string | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${enc(v as string)}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export const insightsApi = {
  insights: (course = "", examIds: string[] = []) =>
    request<Insights>("teacher", "GET", `/api/insights${query({ course, exam_ids: examIds.join(",") })}`),
  students: (course = "") => request<StudentSummary[]>("teacher", "GET", `/api/students${query({ course })}`),
  profile: (username: string, course = "") =>
    request<StudentProfile>("teacher", "GET", `/api/students/${enc(username)}/profile${query({ course })}`),
};

// ---------- slice 6 H: practice assignments ----------

import type {
  PracticeAnswerResponse,
  PracticeBankQuestion,
  PracticeGenerateInput,
  PracticeGenerateResult,
  PracticeItemEdit,
  PracticeSet,
  PracticeSetSummary,
  PracticeStudents,
  StudentPracticeSet,
  StudentPracticeSummary,
} from "./types";

export const practiceApi = {
  students: () => request<PracticeStudents>("teacher", "GET", "/api/practice/students"),
  bank: (topic = "") => request<PracticeBankQuestion[]>("teacher", "GET", `/api/practice/bank${topic ? `?topic=${enc(topic)}` : ""}`),
  generate: (body: PracticeGenerateInput) => request<PracticeGenerateResult>("teacher", "POST", "/api/practice/generate", body),
  sets: (status = "") => request<PracticeSetSummary[]>("teacher", "GET", `/api/practice/sets${status ? `?status=${enc(status)}` : ""}`),
  set: (id: string) => request<PracticeSet>("teacher", "GET", `/api/practice/sets/${enc(id)}`),
  deleteSet: (id: string) => request<void>("teacher", "DELETE", `/api/practice/sets/${enc(id)}`),
  editItem: (id: string, itemId: string, body: PracticeItemEdit) =>
    request<PracticeSet>("teacher", "PUT", `/api/practice/sets/${enc(id)}/items/${enc(itemId)}`, body),
  removeItem: (id: string, itemId: string) =>
    request<PracticeSet>("teacher", "DELETE", `/api/practice/sets/${enc(id)}/items/${enc(itemId)}`),
  addItem: (id: string, examId: string, questionId: string) =>
    request<PracticeSet>("teacher", "POST", `/api/practice/sets/${enc(id)}/items`, { exam_id: examId, question_id: questionId }),
  approve: (id: string) => request<PracticeSet>("teacher", "POST", `/api/practice/sets/${enc(id)}/approve`),
  send: (id: string) => request<PracticeSet>("teacher", "POST", `/api/practice/sets/${enc(id)}/send`),
  // student
  mySets: () => request<StudentPracticeSummary[]>("student", "GET", "/api/student/practice"),
  mySet: (id: string) => request<StudentPracticeSet>("student", "GET", `/api/student/practice/${enc(id)}`),
  answer: (id: string, itemId: string, answer: unknown) =>
    request<PracticeAnswerResponse>("student", "POST", `/api/student/practice/${enc(id)}/items/${enc(itemId)}/answer`, { answer }),
};

// ---------- audit 2026-10-12 S-03: classes ----------

import type { AddMembersResult, ClassDetail, SchoolClass, StudentClass } from "./types";

export const classesApi = {
  list: () => request<SchoolClass[]>("teacher", "GET", "/api/classes"),
  create: (name: string, term = "") => request<SchoolClass>("teacher", "POST", "/api/classes", { name, term }),
  get: (id: number) => request<ClassDetail>("teacher", "GET", `/api/classes/${id}`),
  rename: (id: number, name: string) => request<SchoolClass>("teacher", "PUT", `/api/classes/${id}`, { name }),
  update: (id: number, name: string, term: string) =>
    request<SchoolClass>("teacher", "PUT", `/api/classes/${id}`, { name, term }),
  newJoinCode: (id: number) => request<SchoolClass>("teacher", "POST", `/api/classes/${id}/join-code`),
  addMembers: (id: number, usernames: string[]) =>
    request<AddMembersResult>("teacher", "POST", `/api/classes/${id}/members`, { usernames }),
  removeMember: (id: number, username: string) =>
    request<void>("teacher", "DELETE", `/api/classes/${id}/members/${enc(username)}`),
  /** Audit S-15: sign the student out on every device. */
  signOutMember: (id: number, username: string) =>
    request<{ ok: boolean }>("teacher", "POST", `/api/classes/${id}/members/${enc(username)}/sign-out`),
  remove: (id: number) => request<void>("teacher", "DELETE", `/api/classes/${id}`),
};

export const studentClassesApi = {
  list: () => request<StudentClass[]>("student", "GET", "/api/student/classes"),
  join: (code: string) => request<StudentClass>("student", "POST", "/api/student/classes/join", { code }),
  leave: (id: number) => request<void>("student", "DELETE", `/api/student/classes/${id}`),
};
