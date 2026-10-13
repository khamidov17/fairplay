import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Insights, StudentProfile, StudentSummary } from "../api/types";

const api = vi.hoisted(() => ({
  insights: vi.fn(),
  students: vi.fn(),
  profile: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return { ...orig, insightsApi: { ...orig.insightsApi, ...api } };
});

import InsightsPage, { reteachTopics } from "../pages/teacher/InsightsPage";
import StudentsPage from "../pages/teacher/StudentsPage";
import StudentProfilePage, { minutesText } from "../pages/teacher/StudentProfilePage";
import { heatStep } from "../components/teacher/insights/viz";
import { sectionFor } from "../components/teacher/TeacherLayout";

const exams = [
  { exam_id: "e1", title: "Quiz 1", course: "MATH 201", date: "2026-09-01T09:00:00.000Z" },
  { exam_id: "e2", title: "Quiz 2", course: "MATH 201", date: "2026-09-15T09:00:00.000Z" },
];

const insights: Insights = {
  exams,
  all_exams: exams,
  courses: ["MATH 201"],
  attempt_count: 4,
  topics: [
    {
      topic: "integration by parts",
      avg_percent: 57.9,
      n_answers: 5,
      n_students: 2,
      trend: 15,
      by_exam: [
        { exam_id: "e1", avg_percent: 50, n_answers: 3 },
        { exam_id: "e2", avg_percent: 65, n_answers: 2 },
      ],
    },
    { topic: "algebra", avg_percent: 92, n_answers: 4, n_students: 2, trend: null, by_exam: [{ exam_id: "e2", avg_percent: 92, n_answers: 4 }] },
    { topic: "untagged", untagged: true, avg_percent: 62.5, n_answers: 2, n_students: 2, trend: null, by_exam: [{ exam_id: "e1", avg_percent: 62.5, n_answers: 2 }] },
  ],
  hardest: [
    {
      exam_id: "e2",
      exam_title: "Quiz 2",
      course: "MATH 201",
      question_id: "q1",
      question_index: 0,
      type: "numeric",
      prompt: "Integrate x e^x",
      topics: ["integration by parts"],
      avg_percent: 65,
      lost_points: 7,
      n_answers: 2,
    },
  ],
  mistakes: [
    {
      label: "sign error",
      count: 3,
      topics: ["integration by parts"],
      sources: { tag: 2, teacher: 1, ai: 0 },
      examples: [
        {
          exam_id: "e1",
          exam_title: "Quiz 1",
          attempt_id: "a1",
          question_id: "q3",
          question_index: 2,
          username: "alice",
          name: "Alice Chen",
          text: "sign error",
          source: "tag",
        },
      ],
    },
  ],
};

const students: StudentSummary[] = [
  { username: "alice", name: "Alice Chen", exam_count: 3, average_percent: 78.3, weakest_topic: { topic: "integration by parts", avg_percent: 62.5 }, last_exam_at: "2026-09-20T09:00:00.000Z" },
  { username: "bob", name: "Bob Karimov", exam_count: 2, average_percent: 25, weakest_topic: null, last_exam_at: null },
];

const profile: StudentProfile = {
  student: { username: "bob", name: "Bob Karimov" },
  average_percent: 25,
  strengths: ["geometry"],
  weak: ["algebra", "integration by parts"],
  thresholds: { strength_min: 80, weak_below: 60, min_answers: 2 },
  topics: [
    {
      topic: "integration by parts",
      avg_percent: 50,
      n: 2,
      trend: [
        { date: "2026-09-01T09:00:00.000Z", percent: 100, exam_id: "e1", title: "Quiz 1" },
        { date: "2026-09-15T09:00:00.000Z", percent: 30, exam_id: "e2", title: "Quiz 2" },
      ],
    },
    { topic: "untagged", untagged: true, avg_percent: 25, n: 1, trend: [] },
  ],
  exams: [
    { exam_id: "e1", attempt_id: "a1", title: "Quiz 1", course: "MATH 201", delivery: "computer", date: "2026-09-01T09:55:00.000Z", score: 5, total_points: 20, percent: 25, state: "needs_grading", time_on_task_min: 55 },
    { exam_id: "e3", attempt_id: "a3", title: "Paper test", course: "MATH 201", delivery: "paper", date: "2026-09-20T09:00:00.000Z", score: 8, total_points: 10, percent: 80, state: "graded", time_on_task_min: null },
  ],
  practice: [{ id: "ps1", title: "Algebra practice", status: "sent", total: 2, correct: 1, answered: 1, percent: 50, date: "2026-09-21T00:00:00.000Z" }],
};

describe("helpers", () => {
  it("maps percent to a sequential color step", () => {
    expect(heatStep(null)).toBeNull();
    expect(heatStep(10)).toBe(0);
    expect(heatStep(59.9)).toBe(1);
    expect(heatStep(60)).toBe(2);
    expect(heatStep(80)).toBe(3);
    expect(heatStep(100)).toBe(4);
  });
  it("picks lowest tagged topics to reteach", () => {
    expect(reteachTopics(insights.topics).map((t) => t.topic)).toEqual(["integration by parts", "algebra"]);
  });
  it("formats time on task", () => {
    expect(minutesText(null)).toBe("—");
    expect(minutesText(42.4)).toBe("42 min");
    expect(minutesText(75)).toBe("1 h 15 min");
  });
  it("sidebar sections", () => {
    expect(sectionFor("/teacher/insights")).toBe("insights");
    expect(sectionFor("/teacher/students/bob")).toBe("students");
  });
});

describe("InsightsPage", () => {
  beforeEach(() => {
    api.insights.mockReset().mockResolvedValue(insights);
  });

  it("renders the heatmap, reteach list, hardest questions and mistakes", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <InsightsPage />
      </MemoryRouter>,
    );
    const heat = await screen.findByRole("table", { name: /Average score per topic and exam/ });
    const row = within(heat).getByRole("row", { name: /integration by parts/ });
    const cell = within(row).getByText("50%");
    expect(cell).toHaveClass("ins-heat-1");
    expect(within(row).getByText("65%")).toHaveClass("ins-heat-2");
    expect(within(row).getByText(/\+15 pts/)).toBeInTheDocument();
    // untagged hidden by default, toggle shows it
    expect(within(heat).queryByText("No topic")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Show questions without a topic"));
    expect(within(heat).getByText("No topic")).toBeInTheDocument();

    const reteach = screen.getByRole("heading", { name: "What to reteach" }).closest("section")!;
    const links = within(reteach).getAllByRole("link", { name: "Make practice" });
    expect(links[0]).toHaveAttribute("href", "/teacher/practice?topic=integration%20by%20parts");

    expect(screen.getByRole("link", { name: "Quiz 2 · Q1" })).toHaveAttribute("href", "/teacher/exams/e2?tab=results");

    const card = screen.getByRole("article", { name: "sign error" });
    expect(within(card).getByText("3 answers")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Alice Chen" })).toHaveAttribute("href", "/teacher/students/alice");
    expect(within(card).getByRole("link", { name: "Quiz 1 · Q3" })).toHaveAttribute("href", "/teacher/exams/e1/submissions/a1");
    expect(within(card).getByRole("link", { name: "Make practice" })).toHaveAttribute(
      "href",
      "/teacher/practice?topic=integration%20by%20parts",
    );
  });

  it("table view and filters", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <InsightsPage />
      </MemoryRouter>,
    );
    await screen.findByRole("table", { name: /Average score per topic/ });
    expect(api.insights).toHaveBeenLastCalledWith("", []);
    await user.click(screen.getByLabelText("Table view"));
    expect(screen.getByRole("columnheader", { name: /Trend \(last exam vs earlier\)/ })).toBeInTheDocument();
    await user.click(screen.getByText("All exams"));
    await user.click(screen.getByRole("checkbox", { name: /Quiz 2/ }));
    await waitFor(() => expect(api.insights).toHaveBeenLastCalledWith("", ["e2"]));
    await user.selectOptions(screen.getByLabelText("Course"), "MATH 201");
    await waitFor(() => expect(api.insights).toHaveBeenLastCalledWith("MATH 201", []));
  });

  it("shows an empty state", async () => {
    api.insights.mockReset().mockResolvedValue({ ...insights, topics: [], hardest: [], mistakes: [] });
    render(
      <MemoryRouter>
        <InsightsPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Nothing to show yet")).toBeInTheDocument();
  });
});

describe("StudentsPage", () => {
  it("lists students linking to profiles and filters by search", async () => {
    api.students.mockReset().mockResolvedValue(students);
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <StudentsPage />
      </MemoryRouter>,
    );
    const link = await screen.findByRole("link", { name: "Alice Chen" });
    expect(link).toHaveAttribute("href", "/teacher/students/alice");
    expect(screen.getByText("78.3%")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Search"), "bob");
    expect(screen.queryByRole("link", { name: "Alice Chen" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Bob Karimov" })).toBeInTheDocument();
  });
});

describe("StudentProfilePage", () => {
  function renderProfile() {
    return render(
      <MemoryRouter initialEntries={["/teacher/students/bob"]}>
        <Routes>
          <Route path="/teacher/students/:username" element={<StudentProfilePage />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("shows chips, sparklines, exam history with time as context only", async () => {
    api.profile.mockReset().mockResolvedValue(profile);
    renderProfile();
    expect(await screen.findByRole("heading", { level: 1, name: "Bob Karimov" })).toBeInTheDocument();
    expect(api.profile).toHaveBeenCalledWith("bob");
    const weak = screen.getByRole("list", { name: "Weak topics" });
    expect(within(weak).getAllByRole("listitem").map((li) => li.textContent?.trim())).toEqual([
      "algebra",
      "integration by parts",
    ]);
    expect(within(screen.getByRole("list", { name: "Strengths" })).getByText(/geometry/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /integration by parts trend: .*100%.*30%/ })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /context only — not a cheating signal/ })).toBeInTheDocument();
    expect(screen.getByText("55 min")).toBeInTheDocument();
    expect(screen.getByText("so far · needs grading")).toBeInTheDocument();
    // 2026-10-13: Practice is off unless the server switches it on
    expect(screen.queryByRole("link", { name: "Make practice for weak topics" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Quiz 1" })).toHaveAttribute("href", "/teacher/exams/e1/submissions/a1");
    expect(screen.getByText("Algebra practice")).toBeInTheDocument();
    // untagged topic not listed in the trend table
    expect(screen.queryByText("untagged")).not.toBeInTheDocument();
  });

  it("shows not found for other teachers' students", async () => {
    const { ApiError } = await import("../api/client");
    api.profile.mockReset().mockRejectedValue(new ApiError(404, "Student not found"));
    renderProfile();
    expect(await screen.findByText("Student not found")).toBeInTheDocument();
  });
});
