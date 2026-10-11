// Homework mode + progress dashboard: student upload page logic, heatmap and progress pages.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ClassProgress, MyProgress, StudentHomeworkDetail, StudentProgress } from "../api/homework";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, auth: { login: vi.fn(), me: vi.fn().mockResolvedValue({ username: "t", name: "T", role: "teacher" }) } };
});

const hw = vi.hoisted(() => ({
  student: { list: vi.fn(), get: vi.fn(), submit: vi.fn(), downloadWorksheet: vi.fn(), notifications: vi.fn(), progress: vi.fn() },
  progress: { classProgress: vi.fn(), student: vi.fn(), addNote: vi.fn(), deleteNote: vi.fn(), practiceDrafts: vi.fn() },
  classes: { list: vi.fn() },
}));

vi.mock("../api/homework", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/homework")>();
  return { ...orig, studentHomeworkApi: hw.student, progressApi: hw.progress };
});
vi.mock("../api/classes", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/classes")>();
  return { ...orig, classesApi: { ...orig.classesApi, ...hw.classes } };
});

import {
  addPages,
  dueLabel,
  movePage,
  removePage,
  submitLabel,
  validatePages,
} from "../pages/student/homework/uploadLogic";
import HomeworkUploadPage from "../pages/student/homework/HomeworkUploadPage";
import MyProgressPage from "../pages/student/homework/MyProgressPage";
import { HomeworkHomeCard } from "../pages/student/homework/HomeworkHomeCard";
import { cellClass, changeText, levelOf, practiceLink, sortStudents, sparkPoints, trendText } from "../pages/teacher/progress/heatmap";
import ProgressPage from "../pages/teacher/progress/ProgressPage";
import StudentProgressPage from "../pages/teacher/progress/StudentProgressPage";
import { dueIso, nextStep, rowStatusText } from "../pages/teacher/homework/homeworkLogic";
import { summaryStatus } from "../pages/teacher/homework/HomeworkListPage";
import { sectionFor } from "../components/teacher/TeacherLayout";

const file = (name: string, type = "image/jpeg", size = 1000) => new File([new Uint8Array(size)], name, { type });

beforeEach(() => {
  Object.values(hw.student).forEach((f) => f.mockReset());
  Object.values(hw.progress).forEach((f) => f.mockReset());
  hw.classes.list.mockReset();
});

// ---------------------------------------------------------------- upload logic
describe("student upload logic", () => {
  it("adds, reorders and deletes pages; rejects non-photos", () => {
    const { pages, rejected } = addPages([], [file("a.jpg"), file("notes.txt", "text/plain"), file("b.heic", ""), file("c.pdf", "application/pdf")], () => "blob:x");
    expect(rejected).toEqual(["notes.txt"]);
    expect(pages.map((p) => p.file.name)).toEqual(["a.jpg", "b.heic", "c.pdf"]);
    const moved = movePage(pages, 2, -1);
    expect(moved.map((p) => p.file.name)).toEqual(["a.jpg", "c.pdf", "b.heic"]);
    expect(movePage(moved, 0, -1)).toBe(moved); // cannot move past the start
    expect(removePage(moved, moved[0].id).map((p) => p.file.name)).toEqual(["c.pdf", "b.heic"]);
  });

  it("validates count and size", () => {
    expect(validatePages([])).toMatch(/Add a photo/);
    const big = addPages([], [file("a.jpg", "image/jpeg", 81 * 1024 * 1024)], () => "").pages;
    expect(validatePages(big)).toMatch(/too large/);
    const many = addPages([], Array.from({ length: 31 }, (_, i) => file(`${i}.jpg`)), () => "").pages;
    expect(validatePages(many)).toMatch(/at most 30/);
    expect(validatePages(many.slice(0, 2))).toBeNull();
  });

  it("labels due dates and the hand-in button", () => {
    const now = new Date(2026, 9, 12, 10, 0);
    expect(dueLabel(new Date(2026, 9, 12, 17, 0).toISOString(), now)).toMatch(/^Due today/);
    expect(dueLabel(new Date(2026, 9, 13, 9, 0).toISOString(), now)).toMatch(/^Due tomorrow/);
    expect(dueLabel(new Date(2026, 9, 15, 9, 0).toISOString(), now)).toBe("Due in 3 days");
    expect(dueLabel(new Date(2026, 9, 9, 9, 0).toISOString(), now)).toBe("Overdue by 3 days");
    expect(submitLabel({ state: "not_submitted", past_due: false }, 2)).toBe("Hand in 2 pages");
    expect(submitLabel({ state: "not_submitted", past_due: true }, 1)).toBe("Hand in late (1 page)");
    expect(submitLabel({ state: "handed_in", past_due: false }, 3)).toBe("Hand in again (3 pages)");
  });
});

// ---------------------------------------------------------------- student upload page
function detail(over: Partial<StudentHomeworkDetail> = {}): StudentHomeworkDetail {
  return {
    exam_id: "e1", title: "Week 3", course: "", due_at: new Date(Date.now() + 86_400_000 * 3).toISOString(),
    past_due: false, state: "not_submitted", late: false, overdue: false, submitted_at: null, can_submit: true,
    block_reason: null, allow_late: true, allow_resubmit: true, released: false, feedback_new: false, score: null,
    total_points: 6, percent: null, question_count: 1, class_names: ["Algebra 1"], instructions: "Show every step.",
    worksheet: null, questions: [{ question_id: "q1", index: 0, type: "essay", prompt: "Factor x^2-5x+6", points: 4, options: [], images: [] }],
    pages: [], error: null, ai_enabled: true, ...over,
  };
}

function renderAt(path: string, routes: [string, JSX.Element][]) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {routes.map(([p, el]) => (
          <Route key={p} path={p} element={el} />
        ))}
      </Routes>
    </MemoryRouter>,
  );
}

describe("HomeworkUploadPage", () => {
  beforeEach(() => {
    globalThis.URL.createObjectURL = vi.fn(() => "blob:thumb");
    globalThis.URL.revokeObjectURL = vi.fn();
  });

  it("takes photos with the back camera, reorders, deletes and hands in in order", async () => {
    hw.student.get.mockResolvedValueOnce(detail()).mockResolvedValue(detail({ state: "handed_in", pages: ["/api/uploads/p1"], submitted_at: "x" }));
    hw.student.submit.mockResolvedValue({ submission_id: "s", pages: 2, late: false, state: "processing" });
    renderAt("/student/homework/e1", [["/student/homework/:id", <HomeworkUploadPage />]]);
    await screen.findByText("Factor x^2-5x+6");
    const camera = screen.getByTestId("camera-input") as HTMLInputElement;
    expect(camera).toHaveAttribute("capture", "environment");
    expect(camera.multiple).toBe(true);
    fireEvent.change(camera, { target: { files: [file("one.jpg"), file("two.jpg")] } });
    fireEvent.change(screen.getByTestId("file-input"), { target: { files: [file("three.jpg")] } });
    const list = screen.getByRole("list", { name: "Pages to hand in" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    await userEvent.click(screen.getByRole("button", { name: "Move page 3 earlier" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete page 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Hand in 2 pages" }));
    await waitFor(() => expect(hw.student.submit).toHaveBeenCalled());
    const [id, files] = hw.student.submit.mock.calls[0];
    expect(id).toBe("e1");
    expect((files as File[]).map((f) => f.name)).toEqual(["three.jpg", "two.jpg"]);
    await screen.findByText(/Handed in\. Your teacher/);
    expect(await screen.findByRole("img", { name: "Page 1 you handed in" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Hand in again" })).toBeInTheDocument();
  });

  it("shows why handing in is closed, and the feedback when released", async () => {
    hw.student.get.mockResolvedValue(detail({ can_submit: false, block_reason: "The due date has passed", overdue: true, past_due: true }));
    renderAt("/student/homework/e1", [["/student/homework/:id", <HomeworkUploadPage />]]);
    expect(await screen.findByText("The due date has passed.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Take photo" })).toBeNull();
  });

  it("released feedback links to the step feedback page", async () => {
    hw.student.get.mockResolvedValue(detail({ state: "feedback", released: true, score: 4, percent: 66.7, can_submit: false, block_reason: "Feedback is already released" }));
    renderAt("/student/homework/e1", [["/student/homework/:id", <HomeworkUploadPage />]]);
    expect(await screen.findByText("Your feedback is ready")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See feedback" })).toHaveAttribute("href", "/student/grades/e1");
  });
});

describe("student home badge + my progress", () => {
  it("shows Feedback ready and homework to do", async () => {
    hw.student.notifications.mockResolvedValue({ feedback_ready: 1, items: [{ exam_id: "e1", title: "Week 3", released_at: null }] });
    hw.student.list.mockResolvedValue([{ ...detail({ exam_id: "e2", title: "Week 4" }) }]);
    renderAt("/", [["/", <HomeworkHomeCard />]]);
    expect(await screen.findByRole("status")).toHaveTextContent("Feedback ready");
    expect(screen.getByRole("link", { name: "Week 3" })).toHaveAttribute("href", "/student/homework/e1");
    expect(await screen.findByRole("link", { name: "Week 4" })).toBeInTheDocument();
  });

  it("renders nothing without news", async () => {
    hw.student.notifications.mockResolvedValue({ feedback_ready: 0, items: [] });
    hw.student.list.mockResolvedValue([]);
    const { container } = renderAt("/", [["/", <HomeworkHomeCard />]]);
    await waitFor(() => expect(hw.student.list).toHaveBeenCalled());
    expect(container.querySelector(".hw-home")).toBeNull();
  });

  it("lists topics improving / to work on and recent feedback", async () => {
    const t = (topic: string, mastery: number, trend_label: "improving" | "steady" = "steady") => ({ topic, mastery, trend: null, trend_label, n: 2 });
    const data: MyProgress = {
      mastery: 70, level: "developing", trend_label: "improving", improving: [t("linear equations", 85, "improving")],
      work_on: [t("factoring", 40)], strong: [], topics: [t("linear equations", 85), t("factoring", 40)],
      mistakes: [{ label: "sign error", count: 2, topics: ["factoring"] }],
      recent: [{ exam_id: "e1", title: "Week 3", date: "2026-10-10T00:00:00", percent: 66.7, homework: true }],
    };
    hw.student.progress.mockResolvedValue(data);
    renderAt("/", [["/", <MyProgressPage />]]);
    const workOn = await screen.findByRole("region", { name: "To work on" });
    expect(within(workOn).getByText("factoring")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Getting better" })).getByText("linear equations")).toBeInTheDocument();
    expect(screen.getByText("sign error")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Week 3/ })).toHaveAttribute("href", "/student/grades/e1");
  });
});

// ---------------------------------------------------------------- heatmap + progress pages
const progressFixture: ClassProgress = {
  class: { id: 1, name: "Algebra 1", section: "" },
  generated_at: "2026-10-12T10:00:00",
  assignments: 3,
  topics: [
    { topic: "factoring", mastery: 45, level: "struggling", students: 2, struggling: [{ username: "ann", name: "Ann", mastery: 30 }] },
    { topic: "linear equations", mastery: 85, level: "strong", students: 2, struggling: [] },
  ],
  students: [
    { student_id: 2, username: "ben", name: "Ben", mastery: 75, level: "developing", n: 4, assignments: 2, trend: 20, trend_label: "improving",
      cells: { factoring: { mastery: 60, n: 2, level: "developing", trend: null }, "linear equations": { mastery: 90, n: 2, level: "strong", trend: null } } },
    { student_id: 1, username: "ann", name: "Ann", mastery: 50, level: "struggling", n: 3, assignments: 3, trend: -25, trend_label: "slipping",
      cells: { factoring: { mastery: 30, n: 2, level: "struggling", trend: -20 } } },
  ],
  misconceptions: [{ label: "sign error", this_week: 3, last_week: 1, change: 2, approved: 2, students: [{ username: "ann", name: "Ann" }], topics: ["factoring"] }],
  actions: [{ topic: "factoring", kind: "reteach", mastery: 45, text: "Re-teach factoring to the class — common mistake this week: sign error", students: ["ann"], misconception: "sign error" }],
  slipping: [{ username: "ann", name: "Ann", trend: -25, mastery: 50 }],
  improving: [{ username: "ben", name: "Ben", trend: 20, mastery: 75 }],
};

describe("heatmap helpers", () => {
  it("colours by the server thresholds and sorts", () => {
    expect([levelOf(80), levelOf(79.9), levelOf(60), levelOf(59.9), levelOf(null)]).toEqual(["strong", "developing", "developing", "struggling", null]);
    expect(cellClass(30)).toBe("hm-cell hm-struggling");
    expect(cellClass(undefined)).toBe("hm-cell hm-none");
    expect(sortStudents(progressFixture.students, "name").map((r) => r.username)).toEqual(["ann", "ben"]);
    expect(sortStudents(progressFixture.students, "mastery").map((r) => r.username)).toEqual(["ann", "ben"]);
    expect(sortStudents(progressFixture.students, "trend").map((r) => r.username)).toEqual(["ann", "ben"]);
    expect(trendText(-24.6)).toBe("▼ 25");
    expect(trendText(null)).toBe("new");
    expect(changeText(2)).toBe("+2");
    expect(changeText(-1)).toBe("−1");
    expect(sparkPoints([{ percent: 100 }, { percent: 0 }], 100, 20, 0)).toBe("0,0 100,20");
    expect(practiceLink(["ann", "ben"], "factoring")).toBe("/teacher/practice?student=ann%2Cben&topic=factoring");
  });

  it("teacher homework helpers", () => {
    expect(rowStatusText({ status: "submitted", processing: "processing" })).toBe("Reading pages…");
    expect(rowStatusText({ status: "ai_graded", processing: null })).toBe("AI graded");
    expect(dueIso("2026-10-20", "")).toBe(new Date("2026-10-20T23:59:00").toISOString());
    expect(dueIso("bad", "10:00")).toBeNull();
    const c = { handed_in: 2, submitted: 0, ai_graded: 2, reviewed: 0, released: 0 };
    expect(nextStep(c, false)).toBe("2 students ready for your review.");
    expect(summaryStatus({ grades_released: false, past_due: false, counts: { ai_graded: 1, submitted: 1, handed_in: 2, reviewed: 0 } as never }).label).toBe("2 to review");
    expect(sectionFor("/teacher/homework/e1")).toBe("homework");
    expect(sectionFor("/teacher/progress/students/ann")).toBe("progress");
  });
});

describe("ProgressPage", () => {
  it("renders the heatmap, misconceptions, slipping students and a one-click practice action", async () => {
    hw.classes.list.mockResolvedValue([{ id: 1, name: "Algebra 1", section: "", term: "", description: "", join_code: "X", join_enabled: true, archived: false, created_at: "", member_count: 2, exam_count: 1 }]);
    hw.progress.classProgress.mockResolvedValue(progressFixture);
    hw.progress.practiceDrafts.mockResolvedValue({ sets: [{ id: "ps1" }], skipped: [] });
    renderAt("/teacher/progress", [["/teacher/progress", <ProgressPage />], ["/teacher/practice", <p>practice page</p>]]);
    const table = await screen.findByRole("table", { name: /Mastery per student/ });
    expect(hw.progress.classProgress).toHaveBeenCalledWith(1);
    const cell = within(table).getByLabelText("Ann, factoring: 30%");
    expect(cell).toHaveClass("hm-struggling");
    expect(within(table).getByLabelText("Ann, linear equations: no work")).toHaveClass("hm-none");
    expect(within(table).getByLabelText("Ben, linear equations: 90%")).toHaveClass("hm-strong");
    // sort weakest first
    await userEvent.click(screen.getByRole("button", { name: "Weakest first" }));
    const names = within(table).getAllByRole("rowheader").map((th) => th.textContent);
    expect(names).toEqual(["Class", "Ann", "Ben"]);
    expect(within(screen.getByRole("region", { name: "Slipping" })).getByText("Ann")).toBeInTheDocument();
    expect(screen.getByText("sign error")).toBeInTheDocument();
    expect(screen.getByText("+2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Choose in Practice" })).toHaveAttribute("href", "/teacher/practice?student=ann&topic=factoring");
    await userEvent.click(screen.getByRole("button", { name: "Assign practice to 1 student" }));
    expect(hw.progress.practiceDrafts).toHaveBeenCalledWith(["ann"], ["factoring"]);
    expect(await screen.findByText("practice page")).toBeInTheDocument();
  });

  it("explains an empty class", async () => {
    hw.classes.list.mockResolvedValue([]);
    renderAt("/teacher/progress", [["/teacher/progress", <ProgressPage />]]);
    expect(await screen.findByText("No classes yet")).toBeInTheDocument();
  });
});

describe("StudentProgressPage", () => {
  it("shows topic lines, recurring mistakes with their answers and notes", async () => {
    const data: StudentProgress = {
      student: { id: 1, username: "ann", name: "Ann" }, classes: [{ id: 1, name: "Algebra 1" }],
      mastery: 50, level: "struggling", n: 3, trend: -25, trend_label: "slipping",
      assignments: [{ exam_id: "e1", title: "W1", date: "2026-10-01T00:00:00", percent: 80 }],
      topics: [{ topic: "factoring", mastery: 30, level: "struggling", n: 2, trend: -20, trend_label: "slipping",
        series: [{ exam_id: "e1", title: "W1", date: "2026-10-01T00:00:00", percent: 60 }, { exam_id: "e2", title: "W2", date: "2026-10-08T00:00:00", percent: 20 }] }],
      mistakes: [{ label: "sign error", count: 2, recurring: true, topics: ["factoring"], last_seen: "2026-10-08T00:00:00",
        examples: [{ exam_id: "e2", title: "W2", question_id: "q1", prompt: "Factor", image: "/api/uploads/abc", date: "2026-10-08T00:00:00", comment: "", approved: true }] }],
      notes: [{ id: 5, text: "Talked about signs", created_at: "2026-10-09T00:00:00" }],
    };
    hw.progress.student.mockResolvedValue(data);
    hw.progress.addNote.mockResolvedValue({ id: 6, text: "x", created_at: "" });
    renderAt("/teacher/progress/students/ann", [["/teacher/progress/students/:username", <StudentProgressPage />]]);
    expect(await screen.findByRole("img", { name: "factoring: 60%, 20%" })).toBeInTheDocument();
    expect(screen.getByText("Recurring")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Answer to: Factor" })).toHaveAttribute("src", "/api/uploads/abc");
    expect(screen.getByText("Talked about signs")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Practice weak topics" })).toHaveAttribute("href", "/teacher/practice?student=ann&topic=factoring");
    await userEvent.type(screen.getByLabelText("New note"), "Needs a check-in");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    expect(hw.progress.addNote).toHaveBeenCalledWith("ann", "Needs a check-in");
  });
});
