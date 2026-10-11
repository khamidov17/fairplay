// Types mirror CONTRACT.md exactly. Do not add fields that are not in the contract.

export type Role = "teacher" | "student";
export type QuestionType = "mcq" | "numeric" | "short" | "essay" | "external";
export type ExamStatus = "draft" | "live" | "closed";
export type AttemptStatus = "in_progress" | "submitted";
/** Slice 3: where students answer — in FairPlay or on another website (SEB-style). */
export type ExamMode = "questions" | "external";

/** Slice 3 exam fields (external-website exams). null/[] when mode is "questions". */
export interface ExternalExamFields {
  mode: ExamMode;
  external_url: string | null;
  allowed_domains: string[];
  finish_url_contains: string | null;
}

/** mcq -> option index, numeric -> number, short -> string. Missing key = not answered. */
export type AnswerValue = number | string;
export type AnswerMap = Record<string, AnswerValue>;

export interface LoginResponse {
  token: string;
  role: Role;
  name: string;
}

export interface Me {
  username: string;
  name: string;
  role: Role;
}

export interface Question {
  id: string;
  type: QuestionType;
  prompt: string;
  points: number;
  options: string[];
  correct_index: number | null;
  answer_number: number | null;
  tolerance: number | null;
  accepted_answers: string[];
  rubric: RubricItem[];
  min_words: number | null;
  max_words: number | null;
}

export interface RubricItem {
  label: string;
  points: number;
}

export type QuestionInput = Omit<Question, "id">;

export interface StudentQuestion {
  id: string;
  type: QuestionType;
  prompt: string;
  points: number;
  options: string[];
  rubric?: RubricItem[];
  min_words?: number | null;
  max_words?: number | null;
}

export interface Exam {
  id: string;
  title: string;
  duration_min: number;
  status: ExamStatus;
  start_code: string | null;
  questions: Question[];
  created_at: string;
  question_count: number;
  total_points: number;
  course: string;
  room: string;
  scheduled_at: string | null;
  shuffle_questions: boolean;
  grades_released: boolean;
  released_at: string | null;
  closed_at: string | null;
  pending_grading: number;
  // slice 3 (always sent by the server; optional so older fixtures still type-check)
  mode?: ExamMode;
  external_url?: string | null;
  allowed_domains?: string[];
  finish_url_contains?: string | null;
}

export interface ExamInput {
  title: string;
  duration_min: number;
  questions: QuestionInput[];
  course?: string;
  room?: string;
  scheduled_at?: string | null;
  shuffle_questions?: boolean;
  // slice 3: omitted mode = "questions". When "external", questions must be [].
  mode?: ExamMode;
  external_url?: string | null;
  allowed_domains?: string[];
  finish_url_contains?: string | null;
  /** slice 4: total points of an exam held on another website (default 100). */
  external_points?: number;
}

export interface LiveStudent {
  attempt_id: string;
  username: string;
  name: string;
  status: AttemptStatus;
  started_at: string;
  submitted_at: string | null;
  deadline_at: string;
  answered: number;
  question_count: number;
  current_question: number;
  locked: boolean;
  last_heartbeat_at: string | null;
  online: boolean;
  event_count: number;
  last_event: string | null;
  needs_look: boolean;
}

export interface LiveFlag {
  event_id: number;
  attempt_id: string;
  username: string;
  name: string;
  type: string;
  detail: string;
  server_time: string;
}

export interface LiveRoom {
  exam_id: string;
  status: ExamStatus;
  start_code: string | null;
  duration_min: number;
  students: LiveStudent[];
  flags: LiveFlag[];
  counts: { writing: number; submitted: number; offline: number; needs_look: number };
}

export type EventReview = "open" | "dismissed" | "noted";

export interface IntegrityEvent {
  type: string;
  detail: string;
  client_time: string | null;
}

export interface StoredIntegrityEvent extends IntegrityEvent {
  server_time: string;
  event_id: number;
  review: EventReview;
  note: string;
}

export interface ResultAnswer {
  question_id: string;
  given: AnswerValue | null;
  correct: boolean;
  points_awarded: number;
  max_points: number;
  manual: boolean;
  pending: boolean;
  comment: string;
  rubric_scores: number[] | null;
  /** Slice 6 D: scanned handwriting for paper exams. */
  image?: string | null;
}

export interface ResultAttempt {
  attempt_id: string;
  username: string;
  name: string;
  status: AttemptStatus;
  auto_submitted: boolean;
  /** null for external-website exams (graded on the other website). */
  score: number | null;
  total_points: number;
  /** null for external-website exams. */
  percent: number | null;
  submitted_at: string | null;
  receipt: string | null;
  answers: ResultAnswer[];
  events: StoredIntegrityEvent[];
  needs_grading: number;
  overall_comment: string;
  flag_count: number;
}

export interface ResultQuestion {
  question_id: string;
  prompt: string;
  type: QuestionType;
  percent_correct: number | null;
  points: number;
  avg_points: number | null;
}

export interface Results {
  exam_id: string;
  title: string;
  course: string;
  total_points: number;
  average_percent: number | null;
  median_percent: number | null;
  grades_released: boolean;
  released_at: string | null;
  pending_grading: number;
  submitted_count: number;
  attempt_count: number;
  flags_total: number;
  flags_open: number;
  score_buckets: { label: string; count: number }[];
  attempts: ResultAttempt[];
  questions: ResultQuestion[];
}

export interface Grade {
  points: number;
  comment: string;
  rubric_scores: number[] | null;
  graded_at: string;
}

export interface GradingQuestion {
  question_id: string;
  index: number;
  type: QuestionType;
  prompt: string;
  points: number;
  rubric: RubricItem[];
  to_grade: number;
  graded: number;
}

export interface GradingItem {
  attempt_id: string;
  anon_id: string;
  username: string;
  name: string;
  question_id: string;
  answer: AnswerValue | null;
  word_count: number | null;
  auto_points: number | null;
  grade: Grade | null;
  flag_count: number;
}

export interface GradingQueue {
  exam_id: string;
  title: string;
  course: string;
  status: ExamStatus;
  grades_released: boolean;
  questions: GradingQuestion[];
  items: GradingItem[];
}

export interface GradeInput {
  points: number;
  comment: string;
  rubric_scores: number[] | null;
}

export interface GradeResponse {
  attempt_id: string;
  question_id: string;
  grade: Grade | null;
  score: number;
  total_points: number;
  percent: number;
  needs_grading: number;
}

export interface GradingSummary {
  exam_id: string;
  title: string;
  course: string;
  status: ExamStatus;
  pending: number;
  submitted: number;
  grades_released: boolean;
}

export type GradebookState =
  | "graded"
  | "needs_grading"
  | "in_progress"
  | "missing"
  | "not_taken"
  /** slice 3: external-website exam handed in (no score here). */
  | "submitted";

export interface Gradebook {
  courses: string[];
  exams: { exam_id: string; title: string; course: string; total_points: number; status: ExamStatus; grades_released: boolean }[];
  students: {
    username: string;
    name: string;
    average_percent: number | null;
    cells: Record<string, { state: GradebookState; score: number | null; percent: number | null }>;
  }[];
}

export interface StudentExamSummary {
  id: string;
  title: string;
  duration_min: number;
  question_count: number;
  status: ExamStatus;
  course: string;
  room: string;
  scheduled_at: string | null;
  /** slice 3 (always sent by the server). */
  mode?: ExamMode;
  attempt: null | {
    attempt_id: string;
    status: AttemptStatus;
    submitted_at: string | null;
    receipt: string | null;
    released: boolean;
    percent: number | null;
  };
}

export interface StudentGradeSummary {
  exam_id: string;
  title: string;
  course: string;
  submitted_at: string | null;
  released: boolean;
  score: number | null;
  total_points: number;
  percent: number | null;
}

export interface FeedbackQuestion {
  question_id: string;
  index: number;
  type: QuestionType;
  prompt: string;
  options: string[];
  points: number;
  points_awarded: number;
  status: "correct" | "partial" | "wrong" | "unanswered";
  given: AnswerValue | null;
  correct_answer: AnswerValue | null;
  tolerance: number | null;
  comment: string;
  rubric: RubricItem[];
  rubric_scores: number[] | null;
}

export interface StudentFeedback {
  exam_id: string;
  title: string;
  course: string;
  submitted_at: string | null;
  receipt: string | null;
  score: number;
  total_points: number;
  percent: number;
  class_average_percent: number | null;
  overall_comment: string;
  questions: FeedbackQuestion[];
}

export interface AttemptView {
  attempt_id: string;
  exam_id: string;
  title: string;
  course?: string;
  duration_min: number;
  started_at: string;
  deadline_at: string;
  server_time: string;
  questions: StudentQuestion[];
  answers: AnswerMap;
  status: AttemptStatus;
  // slice 3: external exams have questions [] and answers {}; allowed_domains always
  // includes the external_url host. null/[] for "questions" exams.
  mode?: ExamMode;
  external_url?: string | null;
  allowed_domains?: string[];
  finish_url_contains?: string | null;
  /** phase 1b: the stored client_seq (-1 = nothing saved yet); clients without a local copy continue above it */
  client_seq?: number;
}

export interface SaveAnswersResponse {
  saved: boolean;
  client_seq: number;
  server_time: string;
}

export interface HeartbeatResponse {
  ok: boolean;
  status: AttemptStatus;
  server_time: string;
  deadline_at: string;
}

export interface EventsResponse {
  ok: boolean;
  stored: number;
}

export interface SubmitResponse {
  status: "submitted";
  submitted_at: string;
  receipt: string;
  /** phase 1b: questions the server holds an answer for (what was really received) */
  answered?: number;
}

// ---------- slice 5: LMS sync ----------

export type LmsProviderId = "canvas" | "moodle" | "blackboard" | "brightspace";

/** Slice 5 exam fields (declaration merging keeps the Exam interface above untouched). */
export interface Exam {
  /** Linked LMS course (CourseLink id); when set only its students can start. */
  course_link_id?: number | null;
  /** LMS assignment created on the first grade push. */
  lms_assignment_id?: string | null;
}

export interface LmsProvider {
  id: LmsProviderId;
  name: string;
  auth: "token" | "oauth_token";
  status: "ready" | "beta";
  token_help: string;
  base_url_hint: string;
}

/** The token is never sent back by the server. */
export interface LmsConnection {
  id: number;
  provider: LmsProviderId;
  provider_name: string;
  status: "ready" | "beta";
  base_url: string;
  label: string;
  lms_user_name: string;
  created_at: string;
  last_sync_at: string | null;
  last_error: string | null;
  course_count: number;
}

export interface LmsConnectionInput {
  provider: LmsProviderId;
  base_url: string;
  token: string;
  label?: string;
}

/** A course the teacher teaches in the LMS; link_id set when already linked. */
export interface LmsCourse {
  lms_course_id: string;
  name: string;
  code: string;
  link_id: number | null;
}

export interface LmsCourseLink {
  id: number;
  connection_id: number;
  provider: LmsProviderId;
  provider_name: string;
  lms_course_id: string;
  name: string;
  code: string;
  member_count: number;
  exam_count: number;
  last_sync_at: string | null;
  last_error: string | null;
}

export interface LmsSyncResult {
  added: number;
  updated: number;
  removed: number;
  total: number;
}

export interface LmsLinkResponse extends LmsCourseLink {
  sync: LmsSyncResult;
}

export interface LmsPushResult {
  pushed: number;
  skipped: number;
  errors: { student: string; detail: string }[];
  lms_assignment_id?: string;
}

// ---------- slice 6: delivery, question images, topics, offline packages ----------

/** How an exam is delivered. "website" = mode "external"; "paper" is printed and scanned. */
export type Delivery = "computer" | "paper" | "website";

/** A picture shown above a question prompt. url = /api/uploads/<id> or https://… (or a data: URL inside an offline package). */
export interface QuestionImage {
  url: string;
  alt: string;
}

/** Slice 6 fields (declaration merging; optional so older fixtures still type-check). */
export interface Question {
  images?: QuestionImage[];
  /** 1–5 lower-case topic tags; required when saving. */
  topics?: string[];
}
export interface StudentQuestion {
  images?: QuestionImage[];
}
export interface FeedbackQuestion {
  images?: QuestionImage[];
}
export interface GradingQuestion {
  images?: QuestionImage[];
}
export interface Exam {
  delivery?: Delivery;
}
export interface ExamInput {
  delivery?: Delivery;
}
export interface StudentExamSummary {
  delivery?: Delivery;
}

export interface UploadResult {
  id: string;
  url: string;
  width: number;
  height: number;
}

export interface TopicCount {
  topic: string;
  question_count: number;
}

/** GET /api/student/exams/{id}/package — AES-GCM, key = PBKDF2-SHA256(start_code + exam_id, salt, 100 000). base64 fields. */
export interface OfflinePackage {
  exam_id: string;
  version: number;
  alg: "AES-GCM";
  salt: string;
  iv: string;
  ciphertext: string;
}

/** Decrypted package plaintext: the student exam view (no answer keys). */
export interface OfflineExam {
  exam_id: string;
  title: string;
  course: string;
  duration_min: number;
  mode: ExamMode;
  shuffle_questions: boolean;
  questions: StudentQuestion[];
  generated_at: string;
}

// ---------- slice 6 F/G: class insights + student profiles ----------

export interface InsightsExam {
  exam_id: string;
  title: string;
  course: string;
  date: string | null;
  delivery?: string;
}

export interface TopicExamCell {
  exam_id: string;
  avg_percent: number;
  n_answers: number;
}

export interface TopicInsight {
  topic: string;
  avg_percent: number;
  n_answers: number;
  n_students: number;
  /** Last exam's average minus the average of earlier exams (null with one exam). */
  trend: number | null;
  /** Answers to questions without topics (old exams). */
  untagged?: boolean;
  by_exam: TopicExamCell[];
}

export interface HardestQuestion {
  exam_id: string;
  exam_title: string;
  course: string;
  question_id: string;
  question_index: number;
  type: string;
  prompt: string;
  topics: string[];
  avg_percent: number;
  lost_points: number;
  n_answers: number;
}

export interface MistakeExample {
  exam_id: string;
  exam_title: string;
  attempt_id: string;
  question_id: string;
  question_index: number;
  username: string;
  name: string;
  text: string;
  source: "tag" | "teacher" | "ai";
}

export interface MistakeCluster {
  label: string;
  count: number;
  examples: MistakeExample[];
  topics: string[];
  sources?: { tag: number; teacher: number; ai: number };
}

/** GET /api/insights?course=&exam_ids= */
export interface Insights {
  exams: InsightsExam[];
  all_exams: InsightsExam[];
  courses: string[];
  topics: TopicInsight[];
  hardest: HardestQuestion[];
  mistakes: MistakeCluster[];
  attempt_count: number;
}

/** GET /api/students */
export interface StudentSummary {
  username: string;
  name: string;
  exam_count: number;
  average_percent: number | null;
  weakest_topic: { topic: string; avg_percent: number } | null;
  last_exam_at: string | null;
}

export interface ProfileTopic {
  topic: string;
  avg_percent: number;
  n: number;
  untagged?: boolean;
  trend: { date: string | null; percent: number; exam_id: string; title: string }[];
}

export interface ProfileExam {
  exam_id: string;
  attempt_id: string;
  title: string;
  course: string;
  delivery: string;
  date: string | null;
  score: number | null;
  total_points: number;
  percent: number | null;
  state: "graded" | "needs_grading";
  /** Minutes from start to submit (computer exams only). Context only — never a cheating signal. */
  time_on_task_min: number | null;
}

/** Practice results, when the practice feature provides them (shape owned by that feature). */
export interface ProfilePractice {
  id?: string;
  title?: string;
  status?: string;
  topics?: string[];
  percent?: number | null;
  correct?: number;
  total?: number;
  date?: string | null;
  [key: string]: unknown;
}

/** GET /api/students/{username}/profile */
export interface StudentProfile {
  student: { username: string; name: string };
  topics: ProfileTopic[];
  strengths: string[];
  weak: string[];
  exams: ProfileExam[];
  average_percent: number | null;
  thresholds?: { strength_min: number; weak_below: number; min_answers: number };
  practice?: ProfilePractice[];
  courses?: string[];
}

// ---------- slice 6 H: practice assignments (formative; never part of grades) ----------

export interface PracticeTopicScore {
  topic: string;
  avg_percent: number;
  n: number;
}

export interface PracticeStudent {
  username: string;
  name: string;
  topics: PracticeTopicScore[];
  weak: PracticeTopicScore[];
  /** true when no topic is < 60 % with ≥ 2 answers and the lowest topics are used instead */
  weak_is_fallback: boolean;
}

export interface PracticeStudents {
  students: PracticeStudent[];
  topics: { topic: string; question_count: number }[];
  ai_enabled: boolean;
}

export type PracticeItemStatus = "verified" | "ready" | "needs_answer";
export type PracticeItemKind = "perturbed" | "ai_rewrite" | "shuffled" | "original" | "bank" | "teacher_edit";

export interface PracticeItem {
  id: string;
  type: "mcq" | "numeric" | "short" | "essay";
  prompt: string;
  options: string[];
  correct_index: number | null;
  answer_number: number | null;
  tolerance: number | null;
  accepted_answers: string[];
  math: boolean;
  rubric: { label: string; points: number }[];
  explanation: string;
  topics: string[];
  images: QuestionImage[];
  status: PracticeItemStatus;
  kind: PracticeItemKind;
  verification: { method: string; how?: string; formula?: string; [k: string]: unknown } | null;
  note: string;
  suggested_answer?: string;
  source: { exam_id: string | null; exam_title: string; question_id: string | null };
  student_result?: { answer: unknown; correct: boolean | null; tries: number; ever_correct: boolean } | null;
}

export interface PracticeProgress {
  answered: number;
  correct: number;
  total: number;
}

export interface PracticeSetSummary {
  id: string;
  username: string | null;
  name: string | null;
  status: "draft" | "approved" | "sent";
  title: string;
  topics: string[];
  formative: true;
  item_count: number;
  needs_answer: number;
  verified: number;
  created_at: string | null;
  approved_at: string | null;
  sent_at: string | null;
  progress: PracticeProgress;
}

export interface PracticeSet extends PracticeSetSummary {
  items: PracticeItem[];
  dropped?: { question_id: string | null; reason: string }[];
}

export interface PracticeGenerateInput {
  username?: string;
  usernames?: string[];
  topics?: string[];
  count: number;
}

export interface PracticeGenerateResult {
  sets: PracticeSet[];
  skipped: { username: string; reason: string }[];
}

export interface PracticeItemEdit {
  prompt?: string;
  options?: string[];
  correct_index?: number;
  answer_number?: number;
  tolerance?: number;
  accepted_answers?: string[];
  explanation?: string;
}

export interface PracticeBankQuestion {
  exam_id: string;
  exam_title: string;
  question_id: string;
  type: string;
  prompt: string;
  topics: string[];
}

export interface StudentPracticeSummary {
  id: string;
  title: string;
  topics: string[];
  formative: true;
  item_count: number;
  answered: number;
  correct: number;
  progress: PracticeProgress;
  sent_at: string | null;
  teacher_name: string;
}

export interface StudentPracticeResult {
  given: unknown;
  correct: boolean | null;
  tries: number;
  ever_correct: boolean;
  explanation: string;
  /** mcq: {index, text}; numeric/short: string; essay: null. Shown after a right answer or 2 tries. */
  correct_answer: { index: number | null; text: string | null } | string | null;
  answered_at: string | null;
}

export interface StudentPracticeItem {
  id: string;
  type: "mcq" | "numeric" | "short" | "essay";
  prompt: string;
  options: string[];
  images: QuestionImage[];
  topics: string[];
  math: boolean;
  rubric?: { label: string; points: number }[];
  result: StudentPracticeResult | null;
}

export interface StudentPracticeSet {
  id: string;
  title: string;
  topics: string[];
  formative: true;
  sent_at: string | null;
  items: StudentPracticeItem[];
  progress: PracticeProgress;
}

export interface PracticeAnswerResponse {
  item_id: string;
  correct: boolean | null;
  readable: boolean;
  feedback: string;
  explanation?: string;
  correct_answer?: StudentPracticeResult["correct_answer"];
  tries?: number;
  result: StudentPracticeResult | null;
  progress: PracticeProgress;
}
