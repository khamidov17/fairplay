import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AiDraft, QueueItem, ScanExamSummary, ScanQueue, ScanStatus, UnmatchedPage } from "../api/scan";
import type { Exam } from "../api/types";

const scan = vi.hoisted(() => ({
  exams: vi.fn(),
  roster: vi.fn(),
  saveRoster: vi.fn(),
  printInfo: vi.fn(),
  saveAnswerLines: vi.fn(),
  printPdf: vi.fn(),
  upload: vi.fn(),
  status: vi.fn(),
  unmatched: vi.fn(),
  assign: vi.fn(),
  ignore: vi.fn(),
  rerunAi: vi.fn(),
  queue: vi.fn(),
  approve: vi.fn(),
  regrade: vi.fn(),
  approveBulk: vi.fn(),
}));
const teacher = vi.hoisted(() => ({ getExam: vi.fn() }));

vi.mock("../api/scan", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/scan")>();
  return { ...orig, scanApi: scan };
});
vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return { ...orig, teacherApi: { ...orig.teacherApi, ...teacher } };
});

import { sectionFor } from "../components/teacher/TeacherLayout";
import { tabsFor } from "../pages/teacher/TeacherExamPage";
import {
  bubbleText,
  confidenceText,
  editError,
  firstOpenQuestion,
  groupsFor,
  itemsFor,
  keyAction,
  shownPoints,
  stepIndex,
  withDraft,
  withGrade,
} from "../pages/teacher/scan/queueLogic";
import { QueueStep } from "../pages/teacher/scan/QueueStep";
import ScanExamPage from "../pages/teacher/scan/ScanExamPage";
import ScanOverviewPage, { nextStepFor } from "../pages/teacher/scan/ScanOverviewPage";
import { UploadStep, batchLabel } from "../pages/teacher/scan/UploadStep";

const draft = (over: Partial<AiDraft> = {}): AiDraft => ({
  points: 3,
  comment: "Correct value.",
  confidence: 0.95,
  transcript: "3.14",
  mistake_tags: [],
  source: "key",
  low_confidence: false,
  created_at: "2026-10-11T10:00:00Z",
  ...over,
});

const item = (over: Partial<QueueItem> = {}): QueueItem => ({
  attempt_id: "a1",
  username: "alice",
  name: "Alice Chen",
  question_id: "q_num",
  image: "/api/uploads/abc",
  page: 1,
  mcq: null,
  marks: null,
  blank: false,
  draft: draft(),
  grade: null,
  status: "needs_review",
  auto_points: null,
  low_confidence: false,
  group: "t:3.14",
  ...over,
});

function makeQueue(): ScanQueue {
  return {
    exam_id: "e1",
    title: "Paper Midterm",
    course: "MATH 201",
    status: "closed",
    grades_released: false,
    ai_enabled: true,
    low_confidence_below: 0.7,
    questions: [
      {
        question_id: "q_mcq", index: 0, type: "mcq", prompt: "Which is 2+2?", points: 2, images: [],
        options: ["3", "4", "5", "6"], correct_answer: 1, total: 2, to_review: 0, approved: 0, auto: 2, low_confidence: 0,
      },
      {
        question_id: "q_num", index: 1, type: "numeric", prompt: "Write pi to two decimals.", points: 3, images: [],
        options: [], correct_answer: 3.14, total: 3, to_review: 3, approved: 0, auto: 0, low_confidence: 1,
      },
    ],
    items: [
      item({ question_id: "q_mcq", status: "auto", mcq: 1, marks: "one", draft: null, auto_points: 2, group: "none" }),
      item({ attempt_id: "a2", username: "bob", name: "Bob Karimov", question_id: "q_mcq", status: "auto", mcq: 0, marks: "one", draft: null, auto_points: 0, group: "none" }),
      item({}),
      item({ attempt_id: "a2", username: "bob", name: "Bob Karimov" }),
      item({
        attempt_id: "a3", username: "carol", name: "Carol Wang", low_confidence: true, group: "t:3,1",
        draft: draft({ transcript: "3,1", points: 1, confidence: 0.4, low_confidence: true, source: "ai", mistake_tags: ["rounding"] }),
      }),
    ],
    groups: [
      { question_id: "q_num", key: "t:3.14", label: "Wrote “3.14”", points: 3, count: 2,
        items: [{ attempt_id: "a1", question_id: "q_num" }, { attempt_id: "a2", question_id: "q_num" }] },
    ],
  };
}

const EXAM = {
  id: "e1", title: "Paper Midterm", duration_min: 50, status: "closed", start_code: null, questions: [],
  created_at: "2026-10-01T00:00:00Z", question_count: 2, total_points: 5, course: "MATH 201", room: "",
  scheduled_at: null, shuffle_questions: false, grades_released: false, released_at: null, closed_at: null,
  pending_grading: 3, mode: "questions", external_url: null, allowed_domains: [], finish_url_contains: null,
  course_link_id: null, lms_assignment_id: null, delivery: "paper",
} as unknown as Exam;

beforeEach(() => {
  for (const f of [...Object.values(scan), ...Object.values(teacher)]) f.mockReset();
});

describe("queue logic", () => {
  it("filters, orders low confidence first and groups similar drafts", () => {
    const q = makeQueue();
    const review = itemsFor(q, "q_num", "review");
    expect(review.map((i) => i.username)).toEqual(["carol", "alice", "bob"]);
    expect(itemsFor(q, "q_mcq", "review")).toEqual([]);
    expect(itemsFor(q, "q_mcq", "all")).toHaveLength(2);
    const groups = groupsFor(q, "q_num");
    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(2);
    // once one of them is approved the group disappears (only 1 left)
    const after = withGrade(q, "a1", "q_num", { points: 3, comment: "", rubric_scores: null, graded_at: "x" });
    expect(groupsFor(after, "q_num")).toEqual([]);
    expect(after.questions[1].to_review).toBe(2);
    expect(after.questions[1].approved).toBe(1);
    expect(itemsFor(after, "q_num", "approved").map((i) => i.username)).toEqual(["alice"]);
    expect(firstOpenQuestion(q)).toBe("q_num");
  });

  it("maps keys, ignores typing, and clamps navigation", () => {
    expect(keyAction({ key: "a" })).toBe("approve");
    expect(keyAction({ key: "E" })).toBe("edit");
    expect(keyAction({ key: "j" })).toBe("next");
    expect(keyAction({ key: "k" })).toBe("prev");
    expect(keyAction({ key: "r" })).toBe("regrade");
    expect(keyAction({ key: "a", metaKey: true })).toBeNull();
    const input = document.createElement("input");
    expect(keyAction({ key: "a", target: input })).toBeNull();
    expect(keyAction({ key: "x" })).toBeNull();
    expect(stepIndex(3, 2, 1)).toBe(2);
    expect(stepIndex(3, 0, -1)).toBe(0);
    expect(stepIndex(0, 0, 1)).toBe(0);
  });

  it("describes drafts, bubbles and points", () => {
    expect(confidenceText(draft({ confidence: 0.42 }))).toBe("42% sure");
    expect(confidenceText(draft({ source: "fallback" }))).toBe("AI off — grade by hand");
    expect(confidenceText(null)).toBe("No draft yet");
    expect(bubbleText({ marks: "one", mcq: 1 })).toBe("Filled: B");
    expect(bubbleText({ marks: "multiple", mcq: null })).toBe("More than one bubble filled");
    expect(bubbleText({ marks: null, mcq: null })).toBeNull();
    expect(shownPoints(item({ status: "auto", auto_points: 2 }))).toEqual({ value: 2, kind: "auto" });
    expect(shownPoints(item({}))).toEqual({ value: 3, kind: "draft" });
    expect(editError("", 3)).toBe("Enter the points.");
    expect(editError("4", 3)).toBe("Points must be between 0 and 3.");
    expect(editError("2.5", 3)).toBeNull();
    const q = withDraft(makeQueue(), "a3", "q_num", draft({ low_confidence: false }));
    expect(q.questions[1].low_confidence).toBe(0);
  });

  it("labels batches and next steps; nav and tabs know paper exams", () => {
    const base = { batch_id: "b", filename: "x.pdf", page_count: 10, processed: 3, matched: 0, unmatched: 0, ignored: 0,
      ai_total: 4, ai_done: 1, error: null, created_at: "", finished_at: null };
    expect(batchLabel({ ...base, status: "reading" }).text).toBe("Reading page 4 of 10");
    expect(batchLabel({ ...base, status: "grading" }).text).toBe("AI drafting 1 of 4");
    expect(batchLabel({ ...base, status: "done" }).tone).toBe("green");
    const s: ScanExamSummary = { exam_id: "e", title: "t", course: "", status: "draft", delivery: "paper", question_count: 1,
      total_points: 1, printed: false, batches: 0, unmatched: 0, paper_attempts: 0, to_review: 0, grades_released: false, created_at: "" };
    expect(nextStepFor(s).step).toBe("print");
    expect(nextStepFor({ ...s, printed: true }).step).toBe("upload");
    expect(nextStepFor({ ...s, printed: true, batches: 1, to_review: 4 }).label).toBe("Review 4 answers");
    expect(sectionFor("/teacher/scan")).toBe("scan");
    expect(sectionFor("/teacher/exams/e1/scan")).toBe("scan");
    expect(tabsFor(EXAM).map((t) => t.id)).toEqual(["scan", "results", "questions"]);
    expect(tabsFor({ ...EXAM, delivery: "computer", status: "live" } as Exam)[0].id).toBe("live");
  });
});

const pane = () => document.querySelector(".rv-pane") as HTMLElement;

describe("QueueStep (side-by-side review)", () => {
  it("shows the suggestion next to the scan, accepts with Enter, moves with arrows and bulk-approves", async () => {
    const q = makeQueue();
    scan.queue.mockResolvedValue(q);
    scan.approve.mockResolvedValue({
      attempt_id: "a3", question_id: "q_num", grade: { points: 1, comment: "", rubric_scores: null, graded_at: "x", grading_seconds: 3 },
      score: 3, total_points: 5, percent: 60, needs_grading: 0,
    });
    render(<QueueStep examId="e1" />);
    expect(await screen.findByText("Write pi to two decimals.")).toBeInTheDocument();
    expect(screen.getByText(/Answer key:/).parentElement).toHaveTextContent("3.14");
    // low confidence first, flagged, with its picture
    await screen.findAllByText("Carol Wang");
    expect(within(pane()).getByText("Carol Wang")).toBeInTheDocument();
    expect(within(pane()).getByText("Needs review")).toBeInTheDocument();
    expect(within(pane()).getByText("40% sure")).toBeInTheDocument();
    expect(within(pane()).getByRole("img", { name: "Answer of Carol Wang" })).toHaveAttribute("src", "/api/uploads/abc");
    expect(within(pane()).getByLabelText("Points")).toHaveValue("1");
    expect(screen.getByRole("button", { name: "Not graded (3)" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Low confidence (1)" })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Question 2 graded" })).toHaveAttribute("aria-valuenow", "0");

    fireEvent.keyDown(document, { key: "Enter" });
    await waitFor(() => expect(scan.approve).toHaveBeenCalledTimes(1));
    const [eid, aid, qid, body] = scan.approve.mock.calls[0];
    expect([eid, aid, qid]).toEqual(["e1", "a3", "q_num"]);
    expect(Object.keys(body)).toEqual(["grading_seconds"]); // accepted as is
    expect(typeof body.grading_seconds).toBe("number");
    // carol left the "not graded" list: alice is next, the arrow moves to bob
    await waitFor(() => expect(within(pane()).getByText("Alice Chen")).toBeInTheDocument());
    expect(screen.getByRole("progressbar", { name: "Question 2 graded" })).toHaveAttribute("aria-valuenow", "1");
    fireEvent.keyDown(document, { key: "ArrowRight" });
    await waitFor(() => expect(within(pane()).getByText("Bob Karimov")).toBeInTheDocument());
    fireEvent.keyDown(document, { key: "ArrowLeft" });
    await waitFor(() => expect(within(pane()).getByText("Alice Chen")).toBeInTheDocument());

    // group bar
    scan.approveBulk.mockResolvedValue({ approved: 2, skipped: 0 });
    const done = makeQueue();
    done.items = done.items.map((i) => (i.question_id === "q_num" ? { ...i, status: "approved", grade: { points: 3, comment: "", rubric_scores: null, graded_at: "x" } } : i));
    done.questions[1] = { ...done.questions[1], to_review: 0, approved: 3, low_confidence: 0 };
    scan.queue.mockResolvedValue(done);
    await userEvent.click(screen.getByRole("button", { name: "Approve all 2" }));
    await waitFor(() =>
      expect(scan.approveBulk).toHaveBeenCalledWith("e1", [
        { attempt_id: "a1", question_id: "q_num" },
        { attempt_id: "a2", question_id: "q_num" },
      ]),
    );
    expect(await screen.findByText("Approved 2 answers.")).toBeInTheDocument();
    expect(screen.getByText(/All 5 answers are graded/)).toBeInTheDocument();
  });

  it("number keys set the points, E edits feedback, R asks the AI again", async () => {
    scan.queue.mockResolvedValue(makeQueue());
    scan.regrade.mockResolvedValue({ attempt_id: "a3", question_id: "q_num", draft: draft({ transcript: "3.1", points: 2, confidence: 0.8, source: "ai" }) });
    scan.approve.mockResolvedValue({
      attempt_id: "a3", question_id: "q_num", grade: { points: 2.5, comment: "Close.", rubric_scores: null, graded_at: "x" },
      score: 2.5, total_points: 5, percent: 50, needs_grading: 0,
    });
    render(<QueueStep examId="e1" />);
    await screen.findAllByText("Carol Wang");
    fireEvent.keyDown(document, { key: "r" });
    await waitFor(() => expect(scan.regrade).toHaveBeenCalledWith("e1", "a3", "q_num"));
    expect(await screen.findByText("80% sure")).toBeInTheDocument();
    expect(within(pane()).getByLabelText("Points")).toHaveValue("2");

    fireEvent.keyDown(document, { key: "7" }); // above the maximum (3): ignored
    expect(within(pane()).getByLabelText("Points")).toHaveValue("2");
    fireEvent.keyDown(document, { key: "1" });
    expect(within(pane()).getByLabelText("Points")).toHaveValue("1");
    fireEvent.keyDown(document, { key: "e" });
    const fb = within(pane()).getByLabelText("Feedback for the student");
    expect(fb).toHaveFocus();
    // typing in the feedback never triggers shortcuts
    await userEvent.clear(fb);
    await userEvent.type(fb, "Close, two places.");
    expect(scan.approve).not.toHaveBeenCalled();
    const pts = within(pane()).getByLabelText("Points");
    await userEvent.clear(pts);
    await userEvent.type(pts, "7");
    await userEvent.click(within(pane()).getByRole("button", { name: "Accept and next" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Points must be between 0 and 3.");
    expect(scan.approve).not.toHaveBeenCalled();
    await userEvent.clear(pts);
    await userEvent.type(pts, "2.5{Enter}");
    await waitFor(() => expect(scan.approve).toHaveBeenCalled());
    expect(scan.approve.mock.calls[0][3]).toMatchObject({ points: 2.5, comment: "Close, two places." });
  });

  it("criteria recompute the points and steps can be flipped", async () => {
    const q = makeQueue();
    q.questions[1] = {
      ...q.questions[1], type: "essay", points: 4,
      criteria: [
        { id: "c1", label: "Setup", points: 1, description: "" },
        { id: "c2", label: "Integration", points: 2, description: "" },
        { id: "c3", label: "Constant", points: 1, description: "" },
      ],
    };
    q.groups = [];
    q.items = [
      item({
        attempt_id: "a1", low_confidence: false, group: "none",
        draft: draft({
          source: "ai", points: 3, confidence: 0.9, explanation: "Remember +C.", needs_review: false,
          criteria: [
            { id: "c1", label: "Setup", points: 1, max_points: 1, met: true, note: "" },
            { id: "c2", label: "Integration", points: 2, max_points: 2, met: true, note: "" },
            { id: "c3", label: "Constant", points: 0, max_points: 1, met: false, note: "no +C" },
          ],
          steps: [
            { n: 1, text: "u = x", status: "correct", note: "" },
            { n: 2, text: "x e^x - e^x", status: "correct", note: "" },
            { n: 3, text: "", status: "missing", note: "+C" },
          ],
          first_error_step: 3,
          misconception: { tag: "missing step", text: "" },
        }),
      }),
    ];
    scan.queue.mockResolvedValue(q);
    scan.approve.mockResolvedValue({
      attempt_id: "a1", question_id: "q_num", grade: { points: 4, comment: "Remember +C.", rubric_scores: null, graded_at: "x" },
      score: 4, total_points: 6, percent: 66.7, needs_grading: 0,
    });
    render(<QueueStep examId="e1" />);
    await screen.findAllByText("Alice Chen");
    expect(within(pane()).getByLabelText("Points")).toHaveValue("3");
    expect(within(pane()).getByLabelText("Constant")).not.toBeChecked();
    expect(within(pane()).getByText("no +C")).toBeInTheDocument();
    expect(within(pane()).getByText(/first problem at step 3/)).toBeInTheDocument();
    await userEvent.click(within(pane()).getByLabelText("Constant"));
    expect(within(pane()).getByLabelText("Points")).toHaveValue("4");
    await userEvent.click(within(pane()).getByLabelText("Setup"));
    expect(within(pane()).getByLabelText("Points")).toHaveValue("3");
    const setup = within(pane()).getByLabelText("Points for Setup (out of 1)");
    await userEvent.clear(setup);
    await userEvent.type(setup, "0.5");
    expect(within(pane()).getByLabelText("Points")).toHaveValue("3.5");
    await userEvent.click(within(pane()).getByRole("button", { name: "Step 3: missing. Change" }));
    expect(within(pane()).getByRole("button", { name: "Step 3: correct. Change" })).toBeInTheDocument();
    expect(within(pane()).queryByText(/first problem at step/)).not.toBeInTheDocument();
    await userEvent.click(within(pane()).getByRole("button", { name: "Remove tag" }));
    fireEvent.keyDown(document, { key: "Enter" });
    await waitFor(() => expect(scan.approve).toHaveBeenCalled());
    const body = scan.approve.mock.calls[0][3];
    expect(body.points).toBe(3.5);
    expect(body.criteria).toEqual([{ id: "c1", points: 0.5 }, { id: "c2", points: 2 }, { id: "c3", points: 1 }]);
    expect(body.steps).toEqual([{ n: 1, status: "correct" }, { n: 2, status: "correct" }, { n: 3, status: "correct" }]);
    expect(body.misconception).toEqual({ tag: "", text: "" });
    expect(body.comment).toBe("Remember +C.");
  });
});

describe("QueueStep with AI off", () => {
  it("never approves the 'grade by hand' placeholder", async () => {
    const q = makeQueue();
    q.ai_enabled = false;
    q.groups = [];
    q.items = q.items.map((i) =>
      i.question_id === "q_num"
        ? { ...i, low_confidence: true, draft: draft({ source: "fallback", points: 0, confidence: 0, low_confidence: true, transcript: "", comment: "AI is off — grade by hand" }) }
        : i,
    );
    scan.queue.mockResolvedValue(q);
    render(<QueueStep examId="e1" />);
    await screen.findByText(/AI is off: type the points/);
    expect(within(pane()).getByText("Grade by hand")).toBeInTheDocument();
    expect(screen.queryByText("Needs review")).not.toBeInTheDocument();
    expect(within(pane()).getByLabelText("Points")).toHaveValue("");
    expect(within(pane()).getByLabelText("Feedback for the student")).toHaveValue("");
    fireEvent.keyDown(document, { key: "Enter" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the points.");
    expect(scan.approve).not.toHaveBeenCalled();
  });
});

describe("UploadStep", () => {
  it("lists batches and assigns an unmatched page", async () => {
    const status: ScanStatus = {
      exam_id: "e1", ai_enabled: false, pages_per_student: 2, students_expected: 2, students_scanned: 1, unmatched: 1,
      batches: [{ batch_id: "b1", status: "done", filename: "class.pdf", page_count: 5, processed: 5, matched: 4, unmatched: 1,
        ignored: 0, ai_total: 0, ai_done: 0, error: null, created_at: "2026-10-11T10:00:00Z", finished_at: null }],
    };
    const page: UnmatchedPage = {
      page_id: 7, batch_id: "b1", seq: 4, status: "unmatched", reason: "Blank copy — choose the student", preview: "/api/uploads/p",
      name_crop: "/api/uploads/n", qr_found: 2, page_no: 1, student_id: null, suggested_name: "Carol Wang",
      suggested_student_id: 4, can_crop: true,
    };
    scan.status.mockResolvedValue(status);
    scan.unmatched.mockResolvedValueOnce([page]).mockResolvedValue([]);
    scan.roster.mockResolvedValue({ exam_id: "e1", source: "all", students: [], all_students: [
      { id: 2, username: "alice", name: "Alice Chen" }, { id: 4, username: "carol", name: "Carol Wang" }] });
    scan.assign.mockResolvedValue({ assigned: [{ page_id: 7, page_no: 1, student_id: 4 }] });
    render(<UploadStep examId="e1" />);
    expect(await screen.findByText("class.pdf")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByText(/AI is off on this server/)).toBeInTheDocument();
    expect(screen.getByText(/AI read “Carol Wang”/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Student")).toHaveValue("4"));
    await userEvent.click(screen.getByRole("button", { name: "Assign" }));
    await waitFor(() => expect(scan.assign).toHaveBeenCalledWith("e1", 7, 4, 1));
    await waitFor(() => expect(screen.queryByText("Pages to fix (1)")).not.toBeInTheDocument());
  });
});

describe("pages", () => {
  it("overview lists paper exams with the next step", async () => {
    scan.exams.mockResolvedValue([
      { exam_id: "e1", title: "Paper Midterm", course: "MATH 201", status: "closed", delivery: "paper", question_count: 2,
        total_points: 5, printed: true, batches: 1, unmatched: 0, paper_attempts: 2, to_review: 3, grades_released: false,
        created_at: "2026-10-01T00:00:00Z" },
    ]);
    render(
      <MemoryRouter>
        <ScanOverviewPage />
      </MemoryRouter>,
    );
    const link = await screen.findByRole("link", { name: "Review 3 answers" });
    expect(link).toHaveAttribute("href", "/teacher/exams/e1/scan?step=grade");
    expect(screen.getByRole("link", { name: "Paper Midterm" })).toHaveAttribute("href", "/teacher/exams/e1/scan");
  });

  it("exam scan page shows the four steps and the print step", async () => {
    teacher.getExam.mockResolvedValue(EXAM);
    scan.roster.mockResolvedValue({ exam_id: "e1", source: "picked", students: [{ id: 2, username: "alice", name: "Alice Chen" }],
      all_students: [{ id: 2, username: "alice", name: "Alice Chen" }, { id: 3, username: "bob", name: "Bob Karimov" }] });
    scan.printInfo.mockResolvedValue({ exam_id: "e1", delivery: "paper", printed_at: null, printed_pages: null, pages_per_student: 2,
      layout_changed: false, roster_source: "picked", student_count: 1,
      questions: [{ question_id: "q1", index: 0, type: "essay", prompt: "Explain.", answer_lines: null, default_lines: 12 }] });
    scan.saveRoster.mockResolvedValue({ exam_id: "e1", source: "picked", students: [
      { id: 2, username: "alice", name: "Alice Chen" }, { id: 3, username: "bob", name: "Bob Karimov" }], all_students: [] });
    render(
      <MemoryRouter initialEntries={["/teacher/exams/e1/scan"]}>
        <Routes>
          <Route path="/teacher/exams/:id/scan" element={<ScanExamPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Paper Midterm" })).toBeInTheDocument();
    for (const label of ["Print", "Upload scans", "AI grading", "Release"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeInTheDocument();
    }
    expect(await screen.findByRole("button", { name: "Download booklets (1 student)" })).toBeInTheDocument();
    expect(screen.getByLabelText("Alice Chen", { exact: false })).toBeChecked();
    await userEvent.click(screen.getByLabelText("Bob Karimov", { exact: false }));
    await userEvent.click(screen.getByRole("button", { name: "Save list (2)" }));
    await waitFor(() => expect(scan.saveRoster).toHaveBeenCalledWith("e1", ["alice", "bob"]));
    // release step links to Results
    await userEvent.click(screen.getByRole("button", { name: /Release/ }));
    expect(screen.getByRole("link", { name: "Open Results & Release" })).toHaveAttribute("href", "/teacher/exams/e1?tab=results");
  });
});
