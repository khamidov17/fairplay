import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Agreement, MarkerItem, QualityOverview, SecondMarkRow } from "../api/quality";

const api = vi.hoisted(() => ({
  overview: vi.fn(),
  queue: vi.fn(),
  mark: vi.fn(),
  moderate: vi.fn(),
}));

vi.mock("../api/quality", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/quality")>();
  return { ...orig, qualityApi: { ...orig.qualityApi, ...api } };
});

import QualityPage from "../pages/teacher/quality/QualityPage";
import SecondMarkingQueuePage from "../pages/teacher/quality/SecondMarkingQueuePage";
import { ModerationCard } from "../pages/teacher/quality/ExamQualityPage";
import { HBars } from "../pages/teacher/quality/qualityViz";
import { kappaWord, rate, seconds, signedPts } from "../api/quality";
import { sectionFor } from "../components/teacher/TeacherLayout";

const agreement = (over: Partial<Agreement> = {}): Agreement => ({
  n: 4,
  exact: 0.5,
  within_1: 0.75,
  within_10pct: 0.5,
  mae: 1.5,
  mae_pct: 20,
  bias: 0.75,
  kappa: 0.64,
  severe_rate: 0.5,
  severe: 2,
  mean_ai: 5.5,
  mean_human: 4,
  ...over,
});

const overview: QualityOverview = {
  agreement: agreement(),
  by_type: [{ ...agreement({ n: 3 }), key: "essay" }, { ...agreement({ n: 1, exact: 1, severe_rate: 0 }), key: "short" }],
  by_question: [],
  correction: { ai_suggested: 4, accepted: 2, edited: 2, correction_rate: 0.5, points_changed: 2, mean_edit: 3, mean_edit_pct: 30 },
  timing: {
    ai_assisted: { n: 2, mean: 30, median: 30, total: 60 },
    manual: { n: 1, mean: 120, median: 120, total: 120 },
    time_saved_fraction: 0.75,
    batches: [],
  },
  worst: [
    {
      exam_id: "e1",
      exam_title: "Essays",
      attempt_id: "a1",
      anon_id: "#AB12",
      question_id: "q2",
      question_index: 1,
      prompt: "Define feudalism.",
      type: "essay",
      source: "ai_edited",
      graded_by: "teacher",
      confidence: 0.8,
      ai_points: 4,
      final_points: 1,
      max_points: 4,
      diff: 3,
      severe: true,
    },
  ],
  graded: 5,
  severe_fraction: 0.25,
  exams: [
    {
      exam_id: "e1",
      title: "Essays",
      course: "HIST 101",
      graded: 5,
      n: 4,
      exact: 0.5,
      within_1: 0.75,
      mae: 1.5,
      kappa: 0.64,
      severe_rate: 0.5,
      bias: 0.75,
      correction_rate: 0.5,
      mean_seconds_ai: 30,
      mean_seconds_manual: 120,
    },
  ],
  courses: ["HIST 101"],
  all_exams: [{ exam_id: "e1", title: "Essays", course: "HIST 101" }],
  weak_areas: { types: { essay: 0.41 }, subjects: {} },
  benchmarks: [{ set_id: "bs1", name: "History set", run_id: "br1", finished_at: null, overall: agreement({ n: 20 }) }],
};

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
});

describe("quality helpers", () => {
  it("formats rates, points, time and kappa", () => {
    expect(rate(0.734)).toBe("73%");
    expect(rate(null)).toBe("—");
    expect(signedPts(-1.234)).toBe("−1.23 pts");
    expect(signedPts(0)).toBe("0 pts");
    expect(seconds(95)).toBe("1 min 35 s");
    expect(kappaWord(0.65)).toBe("good");
    expect(kappaWord(null)).toBe("not enough data");
    expect(sectionFor("/teacher/quality/benchmarks/x")).toBe("quality");
  });

  it("draws bars that print their values", () => {
    render(<HBars label="Agreement" max={1} rows={[{ label: "Exact", value: 0.5, display: "50%" }, { label: "Severe", value: null, display: "—" }]} />);
    const list = screen.getByRole("list", { name: "Agreement" });
    expect(within(list).getByText("50%")).toBeInTheDocument();
    expect(within(list).getByText("—")).toBeInTheDocument();
  });
});

describe("QualityPage", () => {
  it("shows tiles, type chart, exams, weak areas and the worst disagreements with links", async () => {
    api.overview.mockResolvedValue(overview);
    render(
      <MemoryRouter>
        <QualityPage />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Grading quality" })).toBeInTheDocument();
    expect(screen.getByText(/AI matched the final mark exactly/)).toBeInTheDocument();
    expect(screen.getAllByText("50%").length).toBeGreaterThan(0);
    expect(screen.getByText(/Agreement \(weighted kappa\): good/)).toBeInTheDocument();
    expect(screen.getByText("AI-assisted grading is 75% faster per answer.")).toBeInTheDocument();
    expect(screen.getByText(/always go to manual review/)).toHaveTextContent("Essay (0.41)");
    const worst = screen.getByRole("heading", { name: "Biggest disagreements" }).closest("section")!;
    const link = within(worst).getByRole("link", { name: /Essays · Q2 · #AB12/ });
    expect(link).toHaveAttribute("href", "/teacher/exams/e1/submissions/a1");
    expect(within(worst).getByText("Severe")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Essays" })).toHaveAttribute("href", "/teacher/quality/exams/e1");
    // table view of the type chart
    await userEvent.click(screen.getByLabelText("Table view"));
    const typeCard = screen.getByRole("heading", { name: "Agreement by question type" }).closest("section")!;
    expect(within(typeCard).getByRole("columnheader", { name: "Kappa" })).toBeInTheDocument();
    expect(within(typeCard).getByRole("cell", { name: "Short answer" })).toBeInTheDocument();
    // course filter reloads
    api.overview.mockResolvedValue({ ...overview, agreement: agreement({ n: 0 }), exams: [], worst: [] });
    await userEvent.selectOptions(screen.getByLabelText("Course"), "HIST 101");
    await waitFor(() => expect(api.overview).toHaveBeenLastCalledWith("HIST 101"));
    expect(await screen.findByText("No AI-suggested grades yet")).toBeInTheDocument();
  });
});

const markerItems: MarkerItem[] = [
  {
    id: "sm1",
    exam_id: "e1",
    exam_title: "Essays",
    course: "HIST 101",
    anon_id: "#AB12",
    question: {
      question_id: "q1",
      index: 0,
      type: "essay",
      prompt: "Why did the empire fall?",
      points: 10,
      rubric: [{ label: "Two causes", points: 6 }],
      options: [],
      images: [],
    },
    answer: { text: "Debt and invasions.", image: null },
    status: "to_mark",
    my_mark: null,
  },
  {
    id: "sm2",
    exam_id: "e1",
    exam_title: "Essays",
    course: "",
    anon_id: "#CD34",
    question: null,
    answer: { text: "Old", image: null },
    status: "marked",
    my_mark: { points: 3, comment: "", marked_at: null },
  },
];

describe("SecondMarkingQueuePage", () => {
  it("lists answers blind and saves a mark", async () => {
    api.queue.mockResolvedValue({ items: markerItems, to_mark: 1 });
    api.mark.mockResolvedValue({ ...markerItems[0], status: "marked", my_mark: { points: 7, comment: "ok", marked_at: null } });
    render(
      <MemoryRouter>
        <SecondMarkingQueuePage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Debt and invasions.")).toBeInTheDocument();
    expect(screen.getByText(/1 answer to mark/)).toBeInTheDocument();
    expect(screen.queryByText("Old")).not.toBeInTheDocument(); // marked ones hidden by default
    expect(screen.getByText("Two causes · 6 pts")).toBeInTheDocument();
    expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/first mark:/i)).not.toBeInTheDocument();
    const save = screen.getByRole("button", { name: "Save mark" });
    expect(save).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Your mark (out of 10)"), "7");
    await userEvent.type(screen.getByLabelText("Comment (for the lead teacher)"), "ok");
    await userEvent.click(save);
    expect(api.mark).toHaveBeenCalledWith("sm1", 7, "ok");
    expect(await screen.findByText("Marked")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Show marked answers"));
    expect(screen.getByText("Old")).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    api.queue.mockResolvedValue({ items: [], to_mark: 0 });
    render(
      <MemoryRouter>
        <Routes>
          <Route path="*" element={<SecondMarkingQueuePage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("No answers are assigned to you.")).toBeInTheDocument();
  });
});

describe("ModerationCard", () => {
  it("shows both marks and the AI suggestion and sets the agreed mark", async () => {
    const row: SecondMarkRow = {
      id: "sm1",
      attempt_id: "a1",
      anon_id: "#AB12",
      username: "alice",
      name: "Alice Chen",
      question_id: "q1",
      question_index: 0,
      prompt: "Why did the empire fall?",
      type: "essay",
      max_points: 10,
      answer: { text: "Debt.", image: null },
      marker: { username: "marker2", name: "Second Marker" },
      tolerance: 1,
      reason: "selected",
      status: "needs_moderation",
      first: { points: 8, comment: "Good", graded_by: "teacher", graded_at: null },
      second: { points: 4, comment: "Thin", marked_at: null },
      ai: { points: 9, confidence: 0.8, comment: "" },
      moderated: null,
      created_at: "2026-10-01T00:00:00Z",
    };
    api.moderate.mockResolvedValue({ id: "sm1", status: "moderated" });
    const done = vi.fn();
    render(<ModerationCard examId="e1" row={row} onDone={done} />);
    expect(screen.getByText("8/10")).toBeInTheDocument();
    expect(screen.getByText("4/10")).toBeInTheDocument();
    expect(screen.getByText("9/10")).toBeInTheDocument();
    expect(screen.getByText("Thin")).toBeInTheDocument();
    const input = screen.getByLabelText(/Agreed mark/);
    await userEvent.clear(input);
    await userEvent.type(input, "6");
    await userEvent.click(screen.getByRole("button", { name: "Set agreed mark" }));
    expect(api.moderate).toHaveBeenCalledWith("e1", "sm1", 6, "Good");
    await waitFor(() => expect(done).toHaveBeenCalled());
  });
});
