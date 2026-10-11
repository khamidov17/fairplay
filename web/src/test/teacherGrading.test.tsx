import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { GradeInput, GradingItem, GradingQueue, GradingQuestion } from "../api/types";
import {
  buildGrade,
  initialForm,
  isPending,
  nextPendingIndex,
  questionCounts,
  rubricTotal,
} from "../pages/teacher/gradingLogic";

const api = vi.hoisted(() => ({
  grading: vi.fn(),
  getExam: vi.fn(),
  saveGrade: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return { ...orig, teacherApi: { ...orig.teacherApi, ...api } };
});

import GradingPage from "../pages/teacher/GradingPage";

const essayQ: GradingQuestion = {
  question_id: "q_essay",
  index: 1,
  type: "essay",
  prompt: "Explain the integral test.",
  points: 5,
  rubric: [
    { label: "Reasons", points: 3 },
    { label: "Clarity", points: 2 },
  ],
  to_grade: 2,
  graded: 0,
};

const shortQ: GradingQuestion = {
  question_id: "q_short",
  index: 2,
  type: "short",
  prompt: "Capital of France?",
  points: 2,
  rubric: [],
  to_grade: 1,
  graded: 0,
};

function item(over: Partial<GradingItem>): GradingItem {
  return {
    attempt_id: "a1",
    anon_id: "#A7F2",
    username: "alice",
    name: "Alice Chen",
    question_id: "q_essay",
    answer: "Because the terms are positive and decreasing.",
    word_count: 7,
    auto_points: null,
    grade: null,
    flag_count: 0,
    ...over,
  };
}

function queue(): GradingQueue {
  return {
    exam_id: "e1",
    title: "Midterm",
    course: "MATH 201",
    status: "closed",
    grades_released: false,
    questions: [essayQ, shortQ],
    items: [
      item({}),
      item({ attempt_id: "a2", anon_id: "#B3C4", username: "bob", name: "Bob Karimov", answer: "It converges." }),
      item({
        attempt_id: "a1",
        question_id: "q_short",
        answer: "Paris, France",
        word_count: null,
        auto_points: 0,
      }),
    ],
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/teacher/exams/e1/grading"]}>
      <Routes>
        <Route path="/teacher/exams/:id/grading" element={<GradingPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function okResponse(aid: string, qid: string, g: GradeInput) {
  return Promise.resolve({
    attempt_id: aid,
    question_id: qid,
    grade: { ...g, graded_at: "2026-10-11T10:00:00Z" },
    score: g.points,
    total_points: 7,
    percent: 50,
    needs_grading: 0,
  });
}

describe("grading logic", () => {
  it("sums rubric levels, treating unpicked as 0", () => {
    expect(rubricTotal([2, 1])).toBe(3);
    expect(rubricTotal([2, null])).toBe(2);
    expect(rubricTotal([])).toBe(0);
  });

  it("builds a rubric grade whose points equal the sum", () => {
    const r = buildGrade({ scores: [3, 1], points: "", comment: " nice " }, essayQ);
    expect(r).toEqual({ ok: true, total: 4, input: { points: 4, comment: "nice", rubric_scores: [3, 1] } });
  });

  it("refuses a rubric grade with an unpicked criterion or out-of-range level", () => {
    expect(buildGrade({ scores: [3, null], points: "", comment: "" }, essayQ)).toEqual({
      ok: false,
      error: "Pick a level for every criterion.",
    });
    expect(buildGrade({ scores: [4, 0], points: "", comment: "" }, essayQ).ok).toBe(false);
  });

  it("validates plain points against the question maximum", () => {
    expect(buildGrade({ scores: [], points: "1.5", comment: "" }, shortQ)).toEqual({
      ok: true,
      total: 1.5,
      input: { points: 1.5, comment: "", rubric_scores: null },
    });
    expect(buildGrade({ scores: [], points: "3", comment: "" }, shortQ).ok).toBe(false);
    expect(buildGrade({ scores: [], points: "", comment: "" }, shortQ).ok).toBe(false);
  });

  it("treats blank essays as graded and finds the next pending item with wrap-around", () => {
    const blank = item({ attempt_id: "a3", answer: "   " });
    expect(isPending(blank, essayQ)).toBe(false);
    const graded = item({ attempt_id: "a4", grade: { points: 1, comment: "", rubric_scores: [1, 0], graded_at: "x" } });
    const list = [item({}), graded, blank, item({ attempt_id: "a5" })];
    expect(nextPendingIndex(list, 0, essayQ)).toBe(3);
    expect(nextPendingIndex(list, 3, essayQ)).toBe(0);
    expect(nextPendingIndex([graded, blank], 0, essayQ)).toBeNull();
  });

  it("prefills the form from an existing grade and from auto points", () => {
    const g = item({ grade: { points: 4, comment: "ok", rubric_scores: [3, 1], graded_at: "x" } });
    expect(initialForm(g, essayQ)).toEqual({ scores: [3, 1], points: "4", comment: "ok" });
    expect(initialForm(item({ question_id: "q_short", auto_points: 0 }), shortQ).points).toBe("0");
  });

  it("counts pending and graded per question", () => {
    const c = questionCounts([essayQ, shortQ], queue().items);
    expect(c.q_essay).toEqual({ total: 2, pending: 2, graded: 0 });
    expect(c.q_short).toEqual({ total: 1, pending: 1, graded: 0 });
  });
});

describe("GradingPage", () => {
  beforeEach(() => {
    api.grading.mockReset().mockResolvedValue(queue());
    api.getExam.mockReset().mockRejectedValue(new Error("not needed"));
    api.saveGrade.mockReset().mockImplementation((_e: string, aid: string, qid: string, g: GradeInput) => okResponse(aid, qid, g));
  });

  it("hides names by default and can show them", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("Student #A7F2")).toBeInTheDocument();
    expect(screen.queryByText(/Alice Chen/)).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Hide names"));
    expect(screen.getByText("Alice Chen (alice)")).toBeInTheDocument();
  });

  it("sums rubric levels into the total and saves, then moves to the next answer", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Student #A7F2");
    expect(screen.getByText("0 of 2 graded", { exact: false })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reasons: 2 of 3" }));
    await user.click(screen.getByRole("button", { name: "Clarity: 1 of 2" }));
    expect(screen.getByLabelText("Total 3 of 5")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reasons: 2 of 3" })).toHaveAttribute("aria-pressed", "true");

    await user.type(screen.getByLabelText("Comment to student"), "Good start");
    await user.click(screen.getByRole("button", { name: /Save and next/ }));

    await waitFor(() =>
      expect(api.saveGrade).toHaveBeenCalledWith("e1", "a1", "q_essay", {
        points: 3,
        comment: "Good start",
        rubric_scores: [2, 1],
      }),
    );
    expect(await screen.findByText("Student #B3C4")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 graded", { exact: false })).toBeInTheDocument();
    // The new answer starts with an empty form.
    expect(screen.getByLabelText("Total 0 of 5")).toBeInTheDocument();
    expect(screen.getByLabelText("Comment to student")).toHaveValue("");
  });

  it("does not save until every criterion has a level", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Student #A7F2");
    await user.click(screen.getByRole("button", { name: "Reasons: 3 of 3" }));
    await user.click(screen.getByRole("button", { name: /Save and next/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Pick a level for every criterion.");
    expect(api.saveGrade).not.toHaveBeenCalled();
  });

  it("supports number keys for the focused criterion and Enter to save", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Student #A7F2");

    await user.click(screen.getByRole("button", { name: "Reasons: 0 of 3" }));
    await user.keyboard("2");
    await user.click(screen.getByRole("button", { name: "Clarity: 0 of 2" }));
    await user.keyboard("2");
    expect(screen.getByLabelText("Total 4 of 5")).toBeInTheDocument();

    // Enter inside the comment box only types a newline.
    await user.click(screen.getByLabelText("Comment to student"));
    await user.keyboard("{Enter}");
    expect(api.saveGrade).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Clarity: 2 of 2" }));
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(api.saveGrade).toHaveBeenCalledWith("e1", "a1", "q_essay", {
        points: 4,
        comment: "",
        rubric_scores: [2, 2],
      }),
    );
    expect(api.saveGrade).toHaveBeenCalledTimes(1);
  });

  it("grades a short answer with a points box and reports when the question is done", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Student #A7F2");
    await user.click(screen.getByRole("button", { name: /Q3 · Short answer/ }));
    expect(screen.getByText(/no exact match with your accepted answers/)).toBeInTheDocument();
    const box = screen.getByLabelText("Points (0 – 2)");
    expect(box).toHaveValue("0");
    await user.clear(box);
    await user.type(box, "2{Enter}");
    await waitFor(() =>
      expect(api.saveGrade).toHaveBeenCalledWith("e1", "a1", "q_short", { points: 2, comment: "", rubric_scores: null }),
    );
    expect(await screen.findByText(/All answers to Q3 are graded/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to Q2" })).toBeInTheDocument();
  });

  it("shows the server error and keeps the form when saving fails", async () => {
    const { ApiError } = await import("../api/client");
    api.saveGrade.mockRejectedValueOnce(new ApiError(409, "Attempt is not submitted"));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Student #A7F2");
    await user.click(screen.getByRole("button", { name: "Reasons: 1 of 3" }));
    await user.click(screen.getByRole("button", { name: "Clarity: 1 of 2" }));
    await user.click(screen.getByRole("button", { name: /Save and next/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Attempt is not submitted");
    expect(screen.getByText("Student #A7F2")).toBeInTheDocument();
    expect(screen.getByLabelText("Total 2 of 5")).toBeInTheDocument();
  });
});
