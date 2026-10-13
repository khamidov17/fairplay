import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AttemptView, FeedbackQuestion, StudentFeedback, StudentQuestion } from "../api/types";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    auth: { login: vi.fn(), me: vi.fn().mockResolvedValue({ username: "alice", name: "Alice Chen", role: "student" }) },
    studentApi: {
      listExams: vi.fn(),
      start: vi.fn(),
      getAttempt: vi.fn(),
      saveAnswers: vi.fn(),
      heartbeat: vi.fn(),
      events: vi.fn(),
      submit: vi.fn(),
      grades: vi.fn(),
      feedback: vi.fn(),
    },
  };
});

import { ApiError, studentApi } from "../api/client";
import { EssayInput, wordAim } from "../components/student/AnswerInput";
import { FeedbackQuestionCard } from "../components/student/FeedbackQuestion";
import { ReviewPanel, reviewCounts } from "../components/student/ReviewPanel";
import { StartCodeBoxes, emptyCode } from "../components/student/StartCodeBoxes";
import { StartCodeForm } from "../components/student/StartCodeForm";
import { wordCount } from "../components/student/format";
import ExamTakingPage, { marksStorageKey } from "../pages/student/ExamTakingPage";
import FeedbackPage from "../pages/student/FeedbackPage";
import { groupByCourse } from "../pages/student/GradesPage";

const api = studentApi as unknown as Record<string, ReturnType<typeof vi.fn>>;

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------- start code

function Boxes({ onComplete }: { onComplete?: (c: string) => void }) {
  const [v, setV] = useState(emptyCode());
  return (
    <>
      <StartCodeBoxes idPrefix="t" value={v} onChange={setV} onComplete={onComplete} />
      <output data-testid="code">{v.join("")}</output>
    </>
  );
}

const box = (n: number) => screen.getByRole("textbox", { name: `Digit ${n} of 6` }) as HTMLInputElement;

describe("start code boxes", () => {
  it("typing a digit moves to the next box, and completes the code", async () => {
    const user = userEvent.setup();
    const done = vi.fn();
    render(<Boxes onComplete={done} />);
    await user.click(box(1));
    await user.keyboard("4");
    expect(box(1).value).toBe("4");
    expect(box(2)).toHaveFocus();
    await user.keyboard("72951");
    expect(screen.getByTestId("code").textContent).toBe("472951");
    expect(done).toHaveBeenCalledWith("472951");
  });

  it("ignores letters", async () => {
    const user = userEvent.setup();
    render(<Boxes />);
    await user.click(box(1));
    await user.keyboard("a");
    expect(box(1).value).toBe("");
    expect(box(1)).toHaveFocus();
  });

  it("backspace clears the box, then moves back and clears the previous one", async () => {
    const user = userEvent.setup();
    render(<Boxes />);
    await user.click(box(1));
    await user.keyboard("47");
    expect(box(3)).toHaveFocus();
    await user.keyboard("{Backspace}");
    // box 3 was empty: go back to box 2 and clear it
    expect(box(2)).toHaveFocus();
    expect(screen.getByTestId("code").textContent).toBe("4");
    await user.keyboard("{Backspace}");
    // box 2 is now empty -> moves to box 1 and clears it
    expect(box(1)).toHaveFocus();
    expect(screen.getByTestId("code").textContent).toBe("");
  });

  it("pasting a 6-digit code fills all boxes", async () => {
    const user = userEvent.setup();
    const done = vi.fn();
    render(<Boxes onComplete={done} />);
    await user.click(box(3));
    await user.paste("Code: 4 7 2 9 5 1");
    expect([1, 2, 3, 4, 5, 6].map((n) => box(n).value).join("")).toBe("472951");
    expect(done).toHaveBeenCalledWith("472951");
  });

  it("accepts a fill() style change event per box (Playwright e2e path)", () => {
    render(<Boxes />);
    "472951".split("").forEach((d, i) => fireEvent.change(box(i + 1), { target: { value: d } }));
    expect(screen.getByTestId("code").textContent).toBe("472951");
  });
});

describe("start code form", () => {
  function renderForm() {
    return render(
      <MemoryRouter initialEntries={["/student"]}>
        <Routes>
          <Route path="/student" element={<StartCodeForm examId="e_1" />} />
          <Route path="/student/exam/:aid" element={<p>exam page</p>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("shows a wrong-code message on 403 and clears the boxes", async () => {
    const user = userEvent.setup();
    api.start.mockRejectedValueOnce(new ApiError(403, "Wrong start code"));
    renderForm();
    await user.click(box(1));
    await user.keyboard("111111");
    await user.click(screen.getByRole("button", { name: "Start exam" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Wrong start code/);
    expect(box(1).value).toBe("");
    expect(api.start).toHaveBeenCalledWith("e_1", "111111");
  });

  it("shows the app-only message instead of 'wrong code'", async () => {
    const user = userEvent.setup();
    api.start.mockRejectedValueOnce(
      new ApiError(403, "Open this exam in the FairPlay app. It cannot be taken in a web browser."),
    );
    renderForm();
    await user.click(box(1));
    await user.keyboard("111111");
    await user.click(screen.getByRole("button", { name: "Start exam" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/FairPlay app/);
  });

  it("shows the server detail on 429 lockout", async () => {
    const user = userEvent.setup();
    api.start.mockRejectedValueOnce(new ApiError(429, "Too many wrong codes, wait 60 seconds"));
    renderForm();
    await user.click(box(1));
    await user.keyboard("123456");
    await user.click(screen.getByRole("button", { name: "Start exam" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many wrong codes, wait 60 seconds");
  });

  it("navigates to the exam on success", async () => {
    const user = userEvent.setup();
    api.start.mockResolvedValueOnce({ attempt_id: "a_1" });
    renderForm();
    await user.click(box(1));
    await user.keyboard("472951");
    await user.click(screen.getByRole("button", { name: "Start exam" }));
    expect(await screen.findByText("exam page")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- essay

const essayQ: StudentQuestion = {
  id: "q_e",
  type: "essay",
  prompt: "Explain why.",
  points: 10,
  options: [],
  rubric: [{ label: "Two clear reasons", points: 10 }],
  min_words: 5,
  max_words: 8,
};

describe("essay input", () => {
  it("counts words live and shows the aim", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<EssayInput question={essayQ} value={undefined} onChange={onChange} labelledBy="p" />);
    expect(screen.getByTestId("word-count")).toHaveTextContent("0 words");
    expect(screen.getByText(/aim for 5–8 words/)).toBeInTheDocument();
    const ta = screen.getByRole("textbox", { name: "Your answer" });
    expect(ta).toHaveAttribute("spellcheck", "false");
    expect(ta).toHaveAttribute("autocorrect", "off");
    expect(ta).toHaveAttribute("autocapitalize", "off");
    await user.type(ta, "Silk  road\ntrade declined");
    expect(screen.getByTestId("word-count")).toHaveTextContent("4 words");
    expect(onChange).toHaveBeenLastCalledWith("Silk  road\ntrade declined");
    await user.type(ta, " because of new sea routes");
    expect(screen.getByTestId("word-count")).toHaveTextContent("9 words");
    expect(screen.getByText(/over the limit/)).toBeInTheDocument();
  });

  it("word helpers", () => {
    expect(wordCount("")).toBe(0);
    expect(wordCount("  one  ")).toBe(1);
    expect(wordCount("a b\tc\nd")).toBe(4);
    expect(wordAim(600, 900)).toBe("aim for 600–900 words");
    expect(wordAim(600, null)).toBe("aim for at least 600 words");
    expect(wordAim(null, 900)).toBe("at most 900 words");
    expect(wordAim(null, null)).toBe("");
  });
});

// ---------------------------------------------------------------- review & submit

const qs: StudentQuestion[] = [
  { id: "q1", type: "mcq", prompt: "Pick one", points: 1, options: ["3", "6"] },
  { id: "q2", type: "numeric", prompt: "What is 4.5 × 2?", points: 1, options: [] },
  { id: "q3", type: "short", prompt: "Capital of France?", points: 1, options: [] },
  { id: "q4", type: "essay", prompt: "Explain.", points: 5, options: [], rubric: [], min_words: null, max_words: null },
];

describe("review and submit", () => {
  it("counts answered / marked / not answered", () => {
    expect(reviewCounts(qs, { q1: 1, q2: 9 }, new Set(["q2", "q3"]))).toEqual({ answered: 2, marked: 2, empty: 2 });
  });

  it("lists marked and empty questions, Go to, and confirms before submitting", async () => {
    const user = userEvent.setup();
    const onGoTo = vi.fn();
    const onSubmit = vi.fn();
    render(
      <ReviewPanel
        questions={qs}
        answers={{ q1: 1, q2: 9 }}
        marked={new Set(["q2"])}
        onGoTo={onGoTo}
        onKeepWorking={vi.fn()}
        onSubmit={onSubmit}
        submitting={false}
        error={null}
      />,
    );
    expect(screen.getByTestId("count-answered")).toHaveTextContent("2");
    expect(screen.getByTestId("count-marked")).toHaveTextContent("1");
    expect(screen.getByTestId("count-empty")).toHaveTextContent("2");
    const list = screen.getByRole("list", { name: "Questions to check" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      expect.stringMatching(/^Q2.*Marked/),
      expect.stringMatching(/^Q3.*Empty/),
      expect.stringMatching(/^Q4.*Empty/),
    ]);
    await user.click(screen.getByRole("button", { name: "Go to question 3" }));
    expect(onGoTo).toHaveBeenCalledWith(2);

    await user.click(screen.getByRole("button", { name: "Submit exam" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("2 questions are not answered");
    expect(onSubmit).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Submit exam" }));
    await user.click(screen.getByRole("button", { name: "Yes, submit" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------- exam page (integration, mocked API)

function attempt(): AttemptView {
  const now = Date.now();
  return {
    attempt_id: "a_1",
    exam_id: "e_1",
    title: "E2E Calculus Quiz",
    course: "MATH 201",
    duration_min: 30,
    started_at: new Date(now).toISOString(),
    deadline_at: new Date(now + 30 * 60_000).toISOString(),
    server_time: new Date(now).toISOString(),
    questions: qs.slice(0, 3),
    answers: {},
    status: "in_progress",
  };
}

describe("exam taking page", () => {
  beforeEach(() => {
    api.getAttempt.mockResolvedValue(attempt());
    api.heartbeat.mockResolvedValue({ ok: true, status: "in_progress", server_time: new Date().toISOString(), deadline_at: attempt().deadline_at });
    api.saveAnswers.mockImplementation(async (_a: string, _ans: unknown, seq: number) => ({ saved: true, client_seq: seq, server_time: new Date().toISOString() }));
    api.submit.mockResolvedValue({ status: "submitted", submitted_at: new Date().toISOString(), receipt: "K7Q2-M9XD" });
    api.listExams.mockResolvedValue([]);
  });

  function renderExam() {
    return render(
      <MemoryRouter initialEntries={["/student/exam/a_1"]}>
        <Routes>
          <Route path="/student/exam/:attemptId" element={<ExamTakingPage />} />
          <Route path="/student/done/:attemptId" element={<p>done page</p>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("answers, marks for review (persisted), reviews and submits", async () => {
    const user = userEvent.setup();
    renderExam();
    expect((await screen.findAllByText(/Question 1 of 3/))[0]).toBeInTheDocument();
    expect(screen.getByText("All answers saved")).toBeInTheDocument();

    // MCQ: radio semantics, clickable by option text
    await user.click(screen.getByText("6"));
    expect(screen.getByRole("radio", { name: /6/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("Saved on this computer")).toBeInTheDocument();

    // Mark for review is a checkbox (not a button) and is stored per attempt
    const mark = screen.getByRole("checkbox", { name: /Mark for review/ });
    await user.click(mark);
    expect(screen.getByRole("checkbox", { name: /Marked for review/ })).toHaveAttribute("aria-checked", "true");
    expect(JSON.parse(localStorage.getItem(marksStorageKey("a_1")) ?? "[]")).toEqual(["q1"]);

    await user.click(screen.getByRole("button", { name: /^Next/ }));
    expect(screen.getAllByText(/Question 2 of 3/)[0]).toBeInTheDocument();
    await user.type(document.querySelector("input")!, "9.005");
    await user.click(screen.getByRole("button", { name: /^Next/ }));
    await user.type(document.querySelector("input")!, "paris");

    // the first button matching /Review/ must be "Review and submit" (e2e relies on it)
    await user.click(screen.getAllByRole("button", { name: /Review/i })[0]);
    expect(screen.getByRole("heading", { name: "Ready to submit?" })).toBeInTheDocument();
    expect(screen.getByTestId("count-answered")).toHaveTextContent("3");
    expect(screen.getByTestId("count-marked")).toHaveTextContent("1");
    expect(screen.getByTestId("count-empty")).toHaveTextContent("0");

    const submits = screen.getAllByRole("button", { name: /^Submit/i });
    await user.click(submits[submits.length - 1]);
    const confirm = screen.getAllByRole("button", { name: /^(Submit|Yes)/i });
    await user.click(confirm[confirm.length - 1]);

    expect(await screen.findByText("done page")).toBeInTheDocument();
    expect(api.submit).toHaveBeenCalledWith("a_1", { q1: 1, q2: 9.005, q3: "paris" }, expect.any(Number));
    expect(localStorage.getItem(marksStorageKey("a_1"))).toBeNull();
  });

  it("restores marks from localStorage", async () => {
    localStorage.setItem(marksStorageKey("a_1"), JSON.stringify(["q1"]));
    renderExam();
    expect(await screen.findByRole("checkbox", { name: /Marked for review/ })).toHaveAttribute("aria-checked", "true");
    // the navigator opens from the bottom-bar "Question n of N" button
    const toggle = screen.getByRole("button", { name: "Question 1 of 3" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.setup().click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Question 1, not answered, marked for review, current/ })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- feedback

function fq(over: Partial<FeedbackQuestion>): FeedbackQuestion {
  return {
    question_id: "q",
    index: 0,
    type: "mcq",
    prompt: "Prompt",
    options: [],
    points: 2,
    points_awarded: 0,
    status: "wrong",
    given: null,
    correct_answer: null,
    tolerance: null,
    comment: "",
    rubric: [],
    rubric_scores: null,
    ...over,
  };
}

describe("feedback", () => {
  it("renders correct / partial / wrong / unanswered statuses and answers", () => {
    render(
      <>
        <FeedbackQuestionCard q={fq({ question_id: "a", index: 0, options: ["3", "6"], given: 1, correct_answer: 1, status: "correct", points_awarded: 2 })} />
        <FeedbackQuestionCard q={fq({ question_id: "b", index: 1, type: "numeric", given: 8, correct_answer: 9, tolerance: 0.01, status: "wrong", comment: "Check units." })} />
        <FeedbackQuestionCard q={fq({ question_id: "c", index: 2, type: "short", given: null, correct_answer: "paris", status: "unanswered" })} />
        <FeedbackQuestionCard
          q={fq({
            question_id: "d",
            index: 3,
            type: "essay",
            points: 10,
            points_awarded: 7,
            status: "partial",
            given: "My essay text",
            rubric: [
              { label: "Two clear reasons", points: 6 },
              { label: "Clear writing", points: 4 },
            ],
            rubric_scores: [5, 2],
          })}
        />
      </>,
    );
    const cards = screen.getAllByRole("article");
    expect(cards).toHaveLength(4);

    const [mcq, num, short, essay] = cards;
    expect(within(mcq).getByRole("heading")).toHaveTextContent("Question 1 · Multiple choice");
    expect(mcq.querySelector("[data-status]")).toHaveTextContent("2 / 2 ✓");
    expect(within(mcq).getByText("B · 6")).toBeInTheDocument();
    expect(within(mcq).queryByText("CORRECT ANSWER")).not.toBeInTheDocument();

    expect(num.querySelector("[data-status]")).toHaveTextContent("0 / 2 · wrong");
    expect(within(num).getByText("8")).toBeInTheDocument();
    expect(within(num).getByText("9 ± 0.01")).toBeInTheDocument();
    expect(within(num).getByText(/Check units\./)).toBeInTheDocument();

    expect(short.querySelector("[data-status]")).toHaveTextContent("0 / 2 · not answered");
    expect(within(short).getByText("No answer")).toBeInTheDocument();
    expect(within(short).getByText("paris")).toBeInTheDocument();

    expect(essay.querySelector("[data-status]")).toHaveTextContent("7 / 10 · partial");
    expect(within(essay).getByText("My essay text")).toBeInTheDocument();
    expect(within(essay).getByText("5 / 6")).toBeInTheDocument();
    expect(within(essay).getByText("2 / 4")).toBeInTheDocument();
    expect(within(essay).queryByText("CORRECT ANSWER")).not.toBeInTheDocument();
  });

  function renderFeedback() {
    return render(
      <MemoryRouter initialEntries={["/student/grades/e_1"]}>
        <Routes>
          <Route path="/student/grades/:examId" element={<FeedbackPage />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("shows score, class average and teacher comment", async () => {
    const data: StudentFeedback = {
      exam_id: "e_1",
      title: "Quiz 4",
      course: "MATH 201",
      submitted_at: "2026-09-25T10:00:00Z",
      receipt: "K7Q2-M9XD",
      score: 9,
      total_points: 10,
      percent: 90,
      class_average_percent: 74,
      overall_comment: "Strong work.",
      questions: [fq({ question_id: "a", options: ["x", "y"], given: 0, correct_answer: 0, status: "correct", points_awarded: 2 })],
    };
    api.feedback.mockResolvedValueOnce(data);
    renderFeedback();
    expect(await screen.findByRole("heading", { name: "Quiz 4" })).toBeInTheDocument();
    expect(screen.getByText(/Strong work\./)).toBeInTheDocument();
    expect(screen.getByText(/Class average 74%/)).toBeInTheDocument();
    expect(api.feedback).toHaveBeenCalledWith("e_1");
  });

  it("shows a friendly message when grades are not released (403)", async () => {
    api.feedback.mockRejectedValueOnce(new ApiError(403, "Grades not released yet"));
    renderFeedback();
    expect(await screen.findByRole("heading", { name: "Not released yet" })).toBeInTheDocument();
  });
});

describe("grades grouping", () => {
  it("averages released exams per course", () => {
    const g = groupByCourse([
      { exam_id: "1", title: "A", course: "MATH 201", submitted_at: null, released: true, score: 9, total_points: 10, percent: 90 },
      { exam_id: "2", title: "B", course: "MATH 201", submitted_at: null, released: true, score: 7, total_points: 10, percent: 70 },
      { exam_id: "3", title: "C", course: "MATH 201", submitted_at: null, released: false, score: null, total_points: 10, percent: null },
      { exam_id: "4", title: "D", course: "", submitted_at: null, released: false, score: null, total_points: 5, percent: null },
    ]);
    expect(g.map((x) => [x.course, x.average, x.graded, x.waiting])).toEqual([
      ["MATH 201", 80, 2, 1],
      ["Other exams", null, 0, 1],
    ]);
  });
});
