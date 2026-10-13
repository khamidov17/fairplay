// Smoke tests for the teacher screens with a mocked API. They also pin the
// selectors the Playwright e2e test (e2e/e2e.py) relies on.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Exam, ExamInput, LiveRoom, Results } from "../api/types";

const api = vi.hoisted(() => ({
  listExams: vi.fn(),
  createExam: vi.fn(),
  updateExam: vi.fn(),
  getExam: vi.fn(),
  openExam: vi.fn(),
  closeExam: vi.fn(),
  live: vi.fn(),
  results: vi.fn(),
  extend: vi.fn(),
  reviewEvent: vi.fn(),
  release: vi.fn(),
  unrelease: vi.fn(),
  gradingSummary: vi.fn(),
  saveGrade: vi.fn(),
  clearGrade: vi.fn(),
  saveComment: vi.fn(),
}));
const me = vi.hoisted(() => vi.fn());

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return {
    ...orig,
    auth: { ...orig.auth, me },
    teacherApi: { ...orig.teacherApi, ...api },
    // slice 6: topic suggestions for the editor
    slice6Api: { ...orig.slice6Api, topics: vi.fn().mockResolvedValue([{ topic: "algebra", question_count: 2 }]) },
    // audit S-03: the editor needs a class
    classesApi: {
      ...orig.classesApi,
      list: vi.fn().mockResolvedValue([
        { id: 7, name: "Calc A", join_code: "K7QM-3XPA", member_count: 3, exam_count: 0, created_at: "2026-10-12T00:00:00Z" },
      ]),
    },
  };
});

import TeacherRoutes from "../pages/teacher/routes";
import { setToken } from "../api/client";

function exam(over: Partial<Exam> = {}): Exam {
  return {
    id: "e_abc",
    title: "E2E Calculus Quiz",
    duration_min: 20,
    status: "draft",
    start_code: null,
    questions: [],
    created_at: "2026-10-01T10:00:00Z",
    question_count: 3,
    total_points: 3,
    course: "MATH 201",
    room: "AB 1087",
    scheduled_at: null,
    shuffle_questions: false,
    grades_released: false,
    released_at: null,
    closed_at: null,
    pending_grading: 0,
    ...over,
  };
}

function renderAt(path: string) {
  setToken("teacher", "tok");
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/teacher/*" element={<TeacherRoutes />} />
      </Routes>
    </MemoryRouter>,
  );
}

const liveRoom: LiveRoom = {
  exam_id: "e_abc",
  status: "live",
  start_code: "4729",
  duration_min: 20,
  students: [
    {
      attempt_id: "a1",
      username: "alice",
      name: "Alice Chen",
      status: "in_progress",
      started_at: new Date(Date.now() - 60_000).toISOString(),
      submitted_at: null,
      deadline_at: new Date(Date.now() + 19 * 60_000).toISOString(),
      answered: 1,
      question_count: 3,
      current_question: 1,
      locked: true,
      last_heartbeat_at: new Date().toISOString(),
      online: true,
      event_count: 1,
      last_event: "blocked_shortcut Cmd-Tab",
      needs_look: true,
    },
    {
      attempt_id: "a2",
      username: "bob",
      name: "Bob Karimov",
      status: "submitted",
      started_at: new Date(Date.now() - 120_000).toISOString(),
      submitted_at: new Date().toISOString(),
      deadline_at: new Date(Date.now() + 18 * 60_000).toISOString(),
      answered: 3,
      question_count: 3,
      current_question: 2,
      locked: false,
      last_heartbeat_at: new Date().toISOString(),
      online: true,
      event_count: 0,
      last_event: null,
      needs_look: false,
    },
  ],
  flags: [
    {
      event_id: 7,
      attempt_id: "a1",
      username: "alice",
      name: "Alice Chen",
      type: "blocked_shortcut",
      detail: "Cmd-Tab",
      server_time: new Date().toISOString(),
    },
  ],
  counts: { writing: 1, submitted: 1, offline: 0, needs_look: 1 },
};

const results: Results = {
  exam_id: "e_abc",
  title: "E2E Calculus Quiz",
  course: "MATH 201",
  total_points: 3,
  average_percent: 50,
  median_percent: 50,
  grades_released: false,
  released_at: null,
  pending_grading: 2,
  submitted_count: 1,
  attempt_count: 2,
  flags_total: 1,
  flags_open: 1,
  score_buckets: Array.from({ length: 10 }, (_, i) => ({ label: i === 9 ? "90–100" : `${i * 10}–${i * 10 + 9}`, count: i === 5 ? 1 : 0 })),
  attempts: [
    {
      attempt_id: "a2",
      username: "bob",
      name: "Bob Karimov",
      status: "submitted",
      auto_submitted: false,
      score: 1.5,
      total_points: 3,
      percent: 50,
      submitted_at: new Date().toISOString(),
      receipt: "K7Q2-M9XD",
      answers: [
        {
          question_id: "q1",
          given: 0,
          correct: false,
          points_awarded: 0,
          max_points: 1,
          manual: false,
          pending: false,
          comment: "",
          rubric_scores: null,
        },
      ],
      events: [],
      needs_grading: 0,
      overall_comment: "",
      flag_count: 0,
    },
  ],
  questions: [{ question_id: "q1", prompt: "Derivative?", type: "mcq", percent_correct: 0, points: 1, avg_points: 0 }],
};

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset();
  me.mockReset().mockResolvedValue({ username: "teacher", name: "Demo Teacher", role: "teacher" });
  api.gradingSummary.mockResolvedValue([]);
  api.live.mockResolvedValue(liveRoom);
  api.results.mockResolvedValue(results);
});

describe("teacher home", () => {
  it("greets the teacher, shows the live banner, exams table and grading card", async () => {
    api.listExams.mockResolvedValue([
      exam({ id: "e1", title: "Live quiz", status: "live", start_code: "4729" }),
      exam({ id: "e2", title: "Old quiz", status: "closed", pending_grading: 3 }),
      exam({ id: "e3", title: "Released quiz", status: "closed", grades_released: true }),
    ]);
    api.gradingSummary.mockResolvedValue([
      { exam_id: "e2", title: "Old quiz", course: "MATH 201", status: "closed", pending: 3, submitted: 5, grades_released: false },
    ]);
    renderAt("/teacher");
    expect(await screen.findByRole("heading", { level: 1, name: /Demo Teacher/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open live room" })).toHaveAttribute("href", "/teacher/exams/e1");
    const table = await screen.findByRole("table", { name: "Your exams" });
    expect(within(table).getByText("3 to grade")).toBeInTheDocument();
    expect(within(table).getByText("Grades released")).toBeInTheDocument();
    expect(within(table).getByText("Live")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("aria-current", "page");
    // 2026-10-13: simple menu; optional parts hidden unless the server switches them on
    for (const name of ["Exams", "Classes", "Grades", "Students"]) expect(screen.getByRole("link", { name })).toBeInTheDocument();
    for (const name of ["Scan & Grade", "LMS sync", "Practice", "Insights"])
      expect(screen.queryByRole("link", { name })).not.toBeInTheDocument();
  });
});

describe("e2e path: create, save, open, live room, results", () => {
  it("keeps the selectors e2e.py uses", { timeout: 20000 }, async () => {
    const user = userEvent.setup();
    let saved: ExamInput | null = null;
    api.createExam.mockImplementation(async (input: ExamInput) => {
      saved = input;
      return exam();
    });
    api.getExam.mockResolvedValue(exam());
    api.openExam.mockResolvedValue(exam({ status: "live", start_code: "4729" }));

    renderAt("/teacher/exams/new");
    await user.type(document.querySelector<HTMLInputElement>("#exam-title")!, "E2E Calculus Quiz");
    const dur = document.querySelector<HTMLInputElement>("#exam-duration")!;
    await user.clear(dur);
    await user.type(dur, "20");
    await user.click(screen.getByRole("button", { name: "+ Multiple choice" }));
    await user.click(screen.getByRole("button", { name: /\+ Numeric/ }));
    await user.click(screen.getByRole("button", { name: "+ Short answer" }));
    const prompts = document.querySelectorAll<HTMLTextAreaElement>("[id$='-prompt']");
    expect(prompts).toHaveLength(3);
    await user.type(prompts[0], "Derivative of x^2 at x = 3?");
    await user.type(document.querySelector<HTMLInputElement>("[id$='-opt-0']")!, "3");
    await user.type(document.querySelector<HTMLInputElement>("[id$='-opt-1']")!, "6");
    await user.click(screen.getByLabelText("Option B is correct"));
    await user.type(prompts[1], "Integral of 2x from 0 to 3?");
    await user.type(document.querySelector<HTMLInputElement>("[id$='-answer']")!, "9");
    const tol = document.querySelector<HTMLInputElement>("[id$='-tolerance']")!;
    await user.clear(tol);
    await user.type(tol, "0.01");
    await user.type(prompts[2], "Capital of France?");
    await user.type(document.querySelector<HTMLInputElement>("[id$='-accepted']")!, "Paris");
    // topics are optional (input id ends with -topics; Enter adds it)
    const topicInputs = document.querySelectorAll<HTMLInputElement>("[id$='-topics']");
    expect(topicInputs).toHaveLength(3);
    for (const t of topicInputs) await user.type(t, "algebra{Enter}");
    // 2026-10-13: the class is optional while writing; e2e.py picks one before saving
    await waitFor(() => expect(document.querySelector<HTMLSelectElement>("#exam-class")!.disabled).toBe(false));
    await user.selectOptions(document.querySelector<HTMLSelectElement>("#exam-class")!, "7");
    await user.click(screen.getByRole("button", { name: "Save exam" }));

    await waitFor(() => expect(api.createExam).toHaveBeenCalled());
    expect(saved!).toMatchObject({ title: "E2E Calculus Quiz", duration_min: 20 });
    expect(saved!.questions.map((q) => q.type)).toEqual(["mcq", "numeric", "short"]);
    expect(saved!.questions[0].correct_index).toBe(1);
    expect(saved!.questions.map((q) => q.topics)).toEqual([["algebra"], ["algebra"], ["algebra"]]);
    expect(saved!.delivery).toBe("computer");
    expect(saved!.class_id).toBe(7);

    // Now on /teacher/exams/e_abc: exactly one "Open exam" button.
    const openBtns = await screen.findAllByRole("button", { name: /Open exam/i });
    expect(openBtns).toHaveLength(1);
    await user.click(openBtns[0]);
    expect(await screen.findByText("4729")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Live room" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findAllByText("Alice Chen")).not.toHaveLength(0);

    await user.click(screen.getByRole("tab", { name: "Results" }));
    expect(await screen.findByRole("button", { name: "Show answers" })).toBeInTheDocument();
  });
});

describe("2026-10-13 simpler exam editor", () => {
  async function writeOneQuestion(user: ReturnType<typeof userEvent.setup>) {
    await user.type(document.querySelector<HTMLInputElement>("#exam-title")!, "Quiz");
    await user.click(screen.getByRole("button", { name: "+ Numeric answer" }));
    await user.type(document.querySelector<HTMLTextAreaElement>("[id$='-prompt']")!, "2+2?");
    await user.type(document.querySelector<HTMLInputElement>("[id$='-answer']")!, "4");
  }

  it("saves without a class and asks for one only when opening", { timeout: 20000 }, async () => {
    const user = userEvent.setup();
    const inputs: ExamInput[] = [];
    api.createExam.mockImplementation(async (input: ExamInput) => {
      inputs.push(input);
      return exam();
    });
    api.updateExam.mockImplementation(async (_id: string, input: ExamInput) => {
      inputs.push(input);
      return exam({ class_id: input.class_id ?? null });
    });
    api.openExam.mockResolvedValue(exam({ status: "live", start_code: "4729", class_id: 7 }));
    api.getExam.mockResolvedValue(exam({ status: "live", start_code: "4729", class_id: 7 }));
    renderAt("/teacher/exams/new");
    await writeOneQuestion(user);
    // no topic, no class: open asks which class
    await user.click(screen.getByRole("button", { name: "Open exam" }));
    const ask = await screen.findByRole("dialog", { name: "Which class takes this exam?" });
    expect(api.createExam).not.toHaveBeenCalled();
    await waitFor(() => expect(within(ask).getByLabelText("Class")).not.toBeDisabled());
    await user.selectOptions(within(ask).getByLabelText("Class"), "7");
    await user.click(within(ask).getByRole("button", { name: "Open exam" }));
    await waitFor(() => expect(api.openExam).toHaveBeenCalled());
    expect(inputs[0]!.class_id).toBe(7);
    expect(inputs[0]!.questions[0]!.topics).toEqual([]);
  });

  it("switches a question's type in its card and keeps the text", async () => {
    const user = userEvent.setup();
    renderAt("/teacher/exams/new");
    await writeOneQuestion(user);
    const kind = document.querySelector<HTMLSelectElement>("[id$='-kind']")!;
    await user.selectOptions(kind, "short");
    expect(document.querySelector<HTMLTextAreaElement>("[id$='-prompt']")!.value).toBe("2+2?");
    expect(document.querySelector("[id$='-accepted']")).not.toBeNull();
    expect(document.querySelector("[id$='-answer']")).toBeNull();
  });

  it("copies questions from another exam", async () => {
    const user = userEvent.setup();
    const q = { id: "q1", type: "numeric" as const, prompt: "Old question", points: 2, answer_number: 4, tolerance: 0, topics: ["x"] };
    api.listExams.mockResolvedValue([exam({ id: "e_old", title: "Old quiz", question_count: 1 })]);
    api.getExam.mockResolvedValue(exam({ id: "e_old", title: "Old quiz", questions: [q] as Exam["questions"] }));
    renderAt("/teacher/exams/new");
    await user.click(screen.getByRole("button", { name: /Copy questions from another exam/ }));
    await user.click(await screen.findByRole("button", { name: "Copy its questions" }));
    expect(await screen.findByText("Old question")).toBeInTheDocument();
  });

  it("a new exam from a class page has that class picked", async () => {
    renderAt("/teacher/exams/new?class=7");
    await waitFor(() => expect(document.querySelector<HTMLSelectElement>("#exam-class")!.value).toBe("7"));
  });
});

describe("live room", () => {
  it("shows counts, student tiles, flags and reviews a flag", async () => {
    const user = userEvent.setup();
    api.getExam.mockResolvedValue(exam({ status: "live", start_code: "4729" }));
    api.reviewEvent.mockResolvedValue({ event_id: 7, review: "dismissed", note: "" });
    renderAt("/teacher/exams/e_abc");
    expect(await screen.findByText("Need your look")).toBeInTheDocument();
    const tiles = await screen.findByRole("list", { name: "Students in the room" });
    expect(within(tiles).getByText("Alice Chen").closest("li")).toHaveClass("look");
    expect(within(tiles).getByText("Bob Karimov").closest("li")).toHaveClass("done");
    expect(screen.getByText("Time left")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Looks fine" }));
    await waitFor(() => expect(api.reviewEvent).toHaveBeenCalledWith("e_abc", 7, "dismissed", ""));
  });

  it("adds 10 minutes and closes with a confirm", async () => {
    const user = userEvent.setup();
    api.getExam.mockResolvedValue(exam({ status: "live", start_code: "4729" }));
    api.extend.mockResolvedValue(exam({ status: "live", start_code: "4729", duration_min: 30 }));
    api.closeExam.mockResolvedValue(exam({ status: "closed" }));
    renderAt("/teacher/exams/e_abc");
    await user.click(await screen.findByRole("button", { name: "Add 10 min" }));
    await waitFor(() => expect(api.extend).toHaveBeenCalledWith("e_abc", 10));
    await user.click(screen.getByRole("button", { name: "Close exam" }));
    expect(api.closeExam).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Yes, close exam" }));
    await waitFor(() => expect(api.closeExam).toHaveBeenCalledWith("e_abc"));
    expect(await screen.findByRole("tab", { name: "Results" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("results", () => {
  it("offers Release anyway when answers still need grading, then overrides a score", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("../api/client");
    api.getExam.mockResolvedValue(
      exam({
        status: "closed",
        pending_grading: 2,
        questions: [
          {
            id: "q1",
            type: "mcq",
            prompt: "Derivative?",
            points: 1,
            options: ["3", "6"],
            correct_index: 1,
            answer_number: null,
            tolerance: null,
            accepted_answers: [],
            rubric: [],
            min_words: null,
            max_words: null,
          },
        ],
      }),
    );
    api.release
      .mockRejectedValueOnce(new ApiError(409, "2 answers still need grading"))
      .mockResolvedValueOnce(exam({ status: "closed", grades_released: true }));
    api.saveGrade.mockResolvedValue({});
    renderAt("/teacher/exams/e_abc");

    expect(await screen.findByText("Score spread")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Grade 2 answers" })[0]).toHaveAttribute("href", "/teacher/exams/e_abc/grading");
    await user.click(screen.getByRole("button", { name: "Release grades" }));
    await user.click(screen.getByRole("button", { name: "Yes, release" }));
    await user.click(await screen.findByRole("button", { name: "Release anyway" }));
    await waitFor(() => expect(api.release).toHaveBeenLastCalledWith("e_abc", true));

    await user.click(screen.getByRole("button", { name: "Show answers" }));
    const box = screen.getByLabelText("Points for question 1");
    await user.clear(box);
    await user.type(box, "1");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.saveGrade).toHaveBeenCalledWith("e_abc", "a2", "q1", { points: 1, comment: "", rubric_scores: null }),
    );
  });
});
