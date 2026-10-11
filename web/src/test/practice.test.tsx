import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  PracticeItem,
  PracticeSet,
  PracticeStudents,
  StudentPracticeSet,
  StudentPracticeSummary,
} from "../api/types";

const api = vi.hoisted(() => ({
  students: vi.fn(),
  bank: vi.fn(),
  generate: vi.fn(),
  sets: vi.fn(),
  set: vi.fn(),
  deleteSet: vi.fn(),
  editItem: vi.fn(),
  removeItem: vi.fn(),
  addItem: vi.fn(),
  approve: vi.fn(),
  send: vi.fn(),
  mySets: vi.fn(),
  mySet: vi.fn(),
  answer: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return { ...orig, practiceApi: { ...orig.practiceApi, ...api } };
});

import PracticePage, { answerText, itemBadge, readPrefill } from "../pages/teacher/PracticePage";
import StudentPracticePage, { correctAnswerText, firstOpenIndex } from "../pages/student/PracticePage";
import { sectionFor } from "../components/teacher/TeacherLayout";

const students: PracticeStudents = {
  ai_enabled: false,
  topics: [
    { topic: "area", question_count: 2 },
    { topic: "primes", question_count: 2 },
  ],
  students: [
    {
      username: "alice",
      name: "Alice Chen",
      topics: [{ topic: "area", avg_percent: 0, n: 2 }],
      weak: [{ topic: "area", avg_percent: 0, n: 2 }],
      weak_is_fallback: false,
    },
    { username: "bob", name: "Bob Karimov", topics: [], weak: [], weak_is_fallback: false },
  ],
};

function item(over: Partial<PracticeItem> = {}): PracticeItem {
  return {
    id: "pi_1",
    type: "numeric",
    prompt: "A rectangle is 4 cm by 5 cm. What is its area?",
    options: [],
    correct_index: null,
    answer_number: 20,
    tolerance: 0,
    accepted_answers: [],
    math: false,
    rubric: [],
    explanation: "Working: 4 × 5 = 20",
    topics: ["area"],
    images: [],
    status: "verified",
    kind: "perturbed",
    verification: { method: "sympy", how: "template", formula: "n1 * n2" },
    note: "",
    source: { exam_id: "e1", exam_title: "Unit 3 quiz", question_id: "q1" },
    ...over,
  };
}

function draft(items: PracticeItem[], over: Partial<PracticeSet> = {}): PracticeSet {
  return {
    id: "ps_1",
    username: "alice",
    name: "Alice Chen",
    status: "draft",
    title: "Practice: area",
    topics: ["area"],
    formative: true,
    item_count: items.length,
    needs_answer: items.filter((i) => i.status === "needs_answer").length,
    verified: items.filter((i) => i.status === "verified").length,
    created_at: "2026-10-11T09:00:00.000Z",
    approved_at: null,
    sent_at: null,
    progress: { answered: 0, correct: 0, total: items.length },
    items,
    ...over,
  };
}

function renderTeacher(url = "/teacher/practice") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/teacher/practice" element={<PracticePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderStudent(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/student/practice" element={<StudentPracticePage />} />
        <Route path="/student/practice/:setId" element={<StudentPracticePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset();
});

describe("practice helpers", () => {
  it("labels items by verification state", () => {
    expect(itemBadge({ status: "verified", kind: "perturbed" }).label).toBe("Verified with SymPy");
    expect(itemBadge({ status: "needs_answer", kind: "ai_rewrite" })).toEqual({ label: "Needs your answer", tone: "orange" });
    expect(itemBadge({ status: "ready", kind: "ai_rewrite" }).label).toMatch(/AI rewrite/);
    expect(itemBadge({ status: "ready", kind: "original" }).label).toBe("Your original question");
  });

  it("shows answer keys in plain words", () => {
    expect(answerText(item())).toBe("20");
    expect(answerText(item({ tolerance: 0.01, answer_number: 2.5 }))).toBe("2.5 (± 0.01)");
    expect(answerText(item({ type: "mcq", options: ["4", "7"], correct_index: 1 }))).toBe("B. 7");
    expect(answerText(item({ type: "short", accepted_answers: ["2x+2"] }))).toBe("2x+2");
    expect(answerText(item({ type: "numeric", answer_number: null }))).toBe("—");
  });

  it("reads ?student= and ?topic= prefill", () => {
    expect(readPrefill(new URLSearchParams("student=alice&student=bob&topic=Area"))).toEqual({
      students: ["alice", "bob"],
      topics: ["area"],
    });
    expect(readPrefill(new URLSearchParams("student=alice,carol"))).toEqual({ students: ["alice", "carol"], topics: [] });
  });

  it("student helpers", () => {
    expect(correctAnswerText({ index: 1, text: "7" })).toBe("B. 7");
    expect(correctAnswerText("2x+2")).toBe("2x+2");
    expect(correctAnswerText(null)).toBeNull();
    expect(
      firstOpenIndex([
        { id: "a", type: "numeric", prompt: "", options: [], images: [], topics: [], math: false, result: { given: 1, correct: true, tries: 1, ever_correct: true, explanation: "", correct_answer: "1", answered_at: null } },
        { id: "b", type: "numeric", prompt: "", options: [], images: [], topics: [], math: false, result: null },
      ]),
    ).toBe(1);
  });

  it("sidebar marks Practice active", () => {
    expect(sectionFor("/teacher/practice")).toBe("practice");
  });
});

describe("teacher practice page", () => {
  it("prefills students and topics from the URL, shows weak topics and generates", async () => {
    api.students.mockResolvedValue(students);
    api.sets.mockResolvedValue([]);
    const generated = draft([item(), item({ id: "pi_2", status: "needs_answer", kind: "ai_rewrite", answer_number: null, note: "Please enter the answer." })]);
    api.generate.mockResolvedValue({ sets: [generated], skipped: [{ username: "bob", reason: "No graded answers yet — pick topics to practise" }] });
    renderTeacher("/teacher/practice?student=alice&topic=area");

    const alice = await screen.findByLabelText(/Alice Chen/);
    expect(alice).toBeChecked();
    expect(screen.getByLabelText(/Bob Karimov/)).not.toBeChecked();
    expect(screen.getByText(/No graded answers yet/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Choose topics/ })).toBeChecked();
    expect(screen.getByRole("button", { name: /area/ })).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(screen.getByLabelText(/Bob Karimov/));
    api.sets.mockResolvedValue([generated]);
    await userEvent.click(screen.getByRole("button", { name: /Make practice for 2 students/ }));
    expect(api.generate).toHaveBeenCalledWith({ usernames: ["alice", "bob"], topics: ["area"], count: 5 });

    expect(await screen.findByText(/bob/, { selector: "strong" })).toBeInTheDocument();
    const card = await screen.findByRole("article", { name: /Alice Chen · Practice: area/ });
    expect(within(card).getByText("Verified with SymPy")).toBeInTheDocument();
    expect(within(card).getByText("Needs your answer")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: /Approve & send/ })).toBeDisabled();
    expect(within(card).getByText(/1 item needs your answer/)).toBeInTheDocument();
  });

  it("lets the teacher answer a needs-answer item, then approve & send", async () => {
    const needs = item({ id: "pi_2", status: "needs_answer", kind: "ai_rewrite", answer_number: null });
    const d = draft([item(), needs]);
    api.students.mockResolvedValue(students);
    api.sets.mockResolvedValue([d]);
    api.set.mockResolvedValue(d);
    const fixed = draft([item(), { ...needs, status: "ready", kind: "teacher_edit", answer_number: 42 }]);
    api.editItem.mockResolvedValue(fixed);
    api.approve.mockResolvedValue({ ...fixed, status: "approved" });
    api.send.mockResolvedValue({ ...fixed, status: "sent", sent_at: "2026-10-11T10:00:00.000Z" });
    renderTeacher();

    await userEvent.click(await screen.findByRole("button", { name: "Review" }));
    const card = await screen.findByRole("article");
    // needs-answer items open in edit mode
    const answer = within(card).getByLabelText("Answer");
    await userEvent.type(answer, "42");
    await userEvent.click(within(card).getByRole("button", { name: "Save item" }));
    await waitFor(() => expect(api.editItem).toHaveBeenCalledWith("ps_1", "pi_2", expect.objectContaining({ answer_number: 42 })));
    const send = within(card).getByRole("button", { name: /Approve & send/ });
    await waitFor(() => expect(send).toBeEnabled());
    await userEvent.click(send);
    await waitFor(() => expect(api.send).toHaveBeenCalledWith("ps_1"));
    expect(api.approve).toHaveBeenCalledWith("ps_1");
  });

  it("removes an item and lists sent sets with progress", async () => {
    const d = draft([item(), item({ id: "pi_3", type: "mcq", options: ["4", "7"], correct_index: 1, kind: "shuffled", status: "ready", answer_number: null })]);
    api.students.mockResolvedValue(students);
    api.sets.mockResolvedValue([
      d,
      { ...draft([item()]), id: "ps_9", status: "sent", sent_at: "2026-10-10T10:00:00.000Z", progress: { answered: 1, correct: 1, total: 1 } },
    ]);
    api.set.mockResolvedValue(d);
    api.removeItem.mockResolvedValue(draft([item()]));
    renderTeacher();

    expect(await screen.findByText("1 of 1 answered · 1 correct")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: /Alice Chen progress/ })).toHaveAttribute("aria-valuenow", "1");
    await userEvent.click(screen.getByRole("button", { name: "Review" }));
    const card = await screen.findByRole("article");
    expect(within(card).getByText("Options shuffled")).toBeInTheDocument();
    const removes = within(card).getAllByRole("button", { name: "Remove" });
    await userEvent.click(removes[1]);
    await waitFor(() => expect(api.removeItem).toHaveBeenCalledWith("ps_1", "pi_3"));
    await waitFor(() => expect(within(card).getAllByRole("button", { name: "Remove" })).toHaveLength(1));
  });
});

describe("student practice page", () => {
  const summary: StudentPracticeSummary = {
    id: "ps_1",
    title: "Practice: area",
    topics: ["area"],
    formative: true,
    item_count: 2,
    answered: 0,
    correct: 0,
    progress: { answered: 0, correct: 0, total: 2 },
    sent_at: "2026-10-11T10:00:00.000Z",
    teacher_name: "Demo Teacher",
  };
  const set: StudentPracticeSet = {
    id: "ps_1",
    title: "Practice: area",
    topics: ["area"],
    formative: true,
    sent_at: "2026-10-11T10:00:00.000Z",
    progress: { answered: 0, correct: 0, total: 2 },
    items: [
      { id: "pi_1", type: "numeric", prompt: "A rectangle is 4 cm by 5 cm. Area?", options: [], images: [], topics: ["area"], math: false, result: null },
      { id: "pi_2", type: "mcq", prompt: "Which is prime?", options: ["4", "7"], images: [], topics: ["primes"], math: false, result: null },
    ],
  };

  it("lists sent sets and says practice is not graded", async () => {
    api.mySets.mockResolvedValue([summary]);
    renderStudent("/student/practice");
    expect(await screen.findByText("Practice: area")).toBeInTheDocument();
    expect(screen.getByText(/doesn’t count toward your grade/)).toBeInTheDocument();
    expect(screen.getByText("0 of 2 answered · 0 correct")).toBeInTheDocument();
  });

  it("answers items one by one with instant feedback", async () => {
    api.mySet.mockResolvedValue(set);
    api.answer
      .mockResolvedValueOnce({
        item_id: "pi_1",
        correct: false,
        readable: true,
        feedback: "Not quite — try again.",
        explanation: "",
        correct_answer: null,
        tries: 1,
        result: { given: "21", correct: false, tries: 1, ever_correct: false, explanation: "", correct_answer: null, answered_at: null },
        progress: { answered: 1, correct: 0, total: 2 },
      })
      .mockResolvedValueOnce({
        item_id: "pi_1",
        correct: true,
        readable: true,
        feedback: "Correct!",
        explanation: "Working: 4 × 5 = 20",
        correct_answer: "20",
        tries: 2,
        result: { given: "20", correct: true, tries: 2, ever_correct: true, explanation: "Working: 4 × 5 = 20", correct_answer: "20", answered_at: null },
        progress: { answered: 1, correct: 1, total: 2 },
      })
      .mockResolvedValueOnce({
        item_id: "pi_2",
        correct: true,
        readable: true,
        feedback: "Correct!",
        explanation: "",
        correct_answer: { index: 1, text: "7" },
        tries: 1,
        result: { given: 1, correct: true, tries: 1, ever_correct: true, explanation: "", correct_answer: { index: 1, text: "7" }, answered_at: null },
        progress: { answered: 2, correct: 2, total: 2 },
      });
    renderStudent("/student/practice/ps_1");

    const input = await screen.findByLabelText(/Your answer/);
    await userEvent.type(input, "21");
    await userEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("Not quite — try again.")).toBeInTheDocument();
    expect(api.answer).toHaveBeenCalledWith("ps_1", "pi_1", "21");

    await userEvent.clear(input);
    await userEvent.type(input, "20");
    await userEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("Correct!")).toBeInTheDocument();
    expect(screen.getByText("Working: 4 × 5 = 20")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 answered · 1 correct")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Which is prime?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: /7/ }));
    await userEvent.click(screen.getByRole("button", { name: "Check" }));
    await waitFor(() => expect(api.answer).toHaveBeenLastCalledWith("ps_1", "pi_2", 1));
    expect(await screen.findByText("2 of 2 answered · 2 correct")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Item 1, correct" })).toBeInTheDocument();
  });
});
