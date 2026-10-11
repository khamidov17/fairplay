import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Gradebook } from "../api/types";

const api = vi.hoisted(() => ({
  gradebook: vi.fn(),
  gradebookCsv: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return { ...orig, teacherApi: { ...orig.teacherApi, ...api } };
});

import GradebookPage, { cellText } from "../pages/teacher/GradebookPage";

const book: Gradebook = {
  courses: ["MATH 201", "STATS 302"],
  exams: [
    { exam_id: "e1", title: "Quiz 1", course: "MATH 201", total_points: 10, status: "closed", grades_released: true },
    { exam_id: "e2", title: "Midterm", course: "MATH 201", total_points: 50, status: "live", grades_released: false },
  ],
  students: [
    {
      username: "alice",
      name: "Alice Chen",
      average_percent: 86,
      cells: {
        e1: { state: "graded", score: 8.6, percent: 86 },
        e2: { state: "needs_grading", score: 30, percent: 60 },
      },
    },
    {
      username: "bob",
      name: "Bob Karimov",
      average_percent: null,
      cells: {
        e1: { state: "missing", score: null, percent: null },
        e2: { state: "in_progress", score: null, percent: null },
      },
    },
    {
      username: "carol",
      name: "Carol Wang",
      average_percent: null,
      cells: { e1: { state: "missing", score: null, percent: null }, e2: { state: "not_taken", score: null, percent: null } },
    },
  ],
};

function renderPage() {
  return render(
    <MemoryRouter>
      <GradebookPage />
    </MemoryRouter>,
  );
}

describe("cellText", () => {
  it("shows percent for graded cells and a word otherwise", () => {
    expect(cellText({ state: "graded", score: 9, percent: 90 })).toBe("90%");
    expect(cellText({ state: "needs_grading", score: 3, percent: 30 })).toBe("Needs grading");
    expect(cellText({ state: "not_taken", score: null, percent: null })).toBe("Not taken");
    expect(cellText(undefined)).toBe("—");
  });
});

describe("GradebookPage", () => {
  const createObjectURL = vi.fn(() => "blob:fake");
  const revokeObjectURL = vi.fn();

  beforeEach(() => {
    api.gradebook.mockReset().mockResolvedValue(book);
    api.gradebookCsv.mockReset().mockResolvedValue(new Blob(["Student,Username\n"], { type: "text/csv" }));
    Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, configurable: true, writable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: revokeObjectURL, configurable: true, writable: true });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders students by exams with cell states and averages", async () => {
    renderPage();
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("columnheader", { name: /Quiz 1/ })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: /Midterm/ })).toBeInTheDocument();

    const alice = within(table).getByRole("row", { name: /Alice Chen/ });
    expect(within(alice).getAllByText("86%")).toHaveLength(2); // cell + average
    expect(within(alice).getByText("Needs grading")).toHaveClass("t-cell", "needs_grading");

    const bob = within(table).getByRole("row", { name: /Bob Karimov/ });
    expect(within(bob).getByText("Missing")).toHaveClass("missing");
    expect(within(bob).getByText("In progress")).toHaveClass("in_progress");
    expect(within(bob).getByText("—")).toBeInTheDocument();

    const carol = within(table).getByRole("row", { name: /Carol Wang/ });
    expect(within(carol).getByText("Not taken")).toHaveClass("not_taken");

    expect(screen.getByText("3 students · 2 exams")).toBeInTheDocument();
  });

  it("filters by course", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("table");
    expect(api.gradebook).toHaveBeenLastCalledWith("");
    await user.selectOptions(screen.getByLabelText("Course"), "STATS 302");
    await waitFor(() => expect(api.gradebook).toHaveBeenLastCalledWith("STATS 302"));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Gradebook · STATS 302");
  });

  it("exports the CSV as proctora-gradebook.csv", async () => {
    const user = userEvent.setup();
    const clicked: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this);
    });
    renderPage();
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: /Export CSV/ }));
    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(api.gradebookCsv).toHaveBeenCalledWith("");
    expect(clicked[0].download).toBe("proctora-gradebook.csv");
    expect(clicked[0].getAttribute("href")).toBe("blob:fake");
  });

  it("shows the API error", async () => {
    const { ApiError } = await import("../api/client");
    api.gradebook.mockReset().mockRejectedValue(new ApiError(500, "Gradebook is broken"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("Gradebook is broken");
  });
});
