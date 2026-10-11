import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClassDetail, ClassSummary, ExamClasses, StudentClass } from "../api/classes";

const api = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  get: vi.fn(),
  update: vi.fn(),
  regenerateCode: vi.fn(),
  addMembers: vi.fn(),
  addCsv: vi.fn(),
  importLms: vi.fn(),
  removeMember: vi.fn(),
  examClasses: vi.fn(),
  setExamClasses: vi.fn(),
  gradebook: vi.fn(),
  gradebookCsv: vi.fn(),
  mine: vi.fn(),
  join: vi.fn(),
}));
const lms = vi.hoisted(() => ({ linkedCourses: vi.fn() }));
const teacher = vi.hoisted(() => ({ createExam: vi.fn(), getExam: vi.fn() }));
const topics = vi.hoisted(() => vi.fn());

vi.mock("../api/classes", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/classes")>();
  return { ...orig, classesApi: api };
});
vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return {
    ...orig,
    lmsApi: { ...orig.lmsApi, ...lms },
    teacherApi: { ...orig.teacherApi, ...teacher },
    slice6Api: { ...orig.slice6Api, topics },
  };
});

import { ApiError } from "../api/client";
import ClassesPage from "../pages/teacher/classes/ClassesPage";
import ClassPage, { addSummary } from "../pages/teacher/classes/ClassPage";
import StudentClassesPage from "../pages/student/ClassesPage";
import { ClassAssignSelect, OPEN_WARNING } from "../components/teacher/classes/ClassAssignSelect";
import { ExamClassesBar } from "../components/teacher/classes/ExamClassesBar";
import { sectionFor } from "../components/teacher/TeacherLayout";
import ExamSetupPage from "../pages/teacher/ExamSetupPage";
import type { Exam } from "../api/types";

const SUMMARY: ClassSummary = {
  id: 7, name: "Calculus I", section: "MATH 101", term: "Fall 2026", description: "",
  join_code: "K7Q2M9X", join_enabled: true, archived: false, created_at: "2026-10-01T00:00:00Z",
  member_count: 2, exam_count: 1,
};

const DETAIL: ClassDetail = {
  ...SUMMARY,
  students: [
    { id: 2, username: "alice", name: "Alice Chen", email: null, joined_at: "2026-10-02T00:00:00Z", added_by: "code" },
    { id: 3, username: "bob", name: "Bob Karimov", email: "bob@x.edu", joined_at: "2026-10-02T00:00:00Z", added_by: "teacher" },
  ],
  exams: [{ id: "e_1", title: "Midterm", status: "live", course: "", delivery: "computer", scheduled_at: null, created_at: "2026-10-03T00:00:00Z" }],
};

function renderClassPage() {
  return render(
    <MemoryRouter initialEntries={["/teacher/classes/7"]}>
      <Routes>
        <Route path="/teacher/classes/:classId" element={<ClassPage />} />
        <Route path="/teacher/exams/new" element={<p>exam setup</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset();
  lms.linkedCourses.mockReset();
  lms.linkedCourses.mockResolvedValue([]);
  topics.mockReset();
  topics.mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

describe("helpers", () => {
  it("marks the Classes nav item active", () => {
    expect(sectionFor("/teacher/classes")).toBe("classes");
    expect(sectionFor("/teacher/classes/7")).toBe("classes");
  });
  it("summarises an add", () => {
    expect(addSummary({ added: [DETAIL.students[0]], already: [], not_found: ["zed"] })).toBe("Added 1 student. 1 not found.");
    expect(addSummary({ added: [], already: DETAIL.students, not_found: [] })).toBe(
      "Added 0 students. 2 were already in the class.",
    );
  });
});

describe("teacher ClassesPage", () => {
  it("lists classes and creates a new one", async () => {
    api.list.mockResolvedValue([SUMMARY]);
    api.create.mockResolvedValue({ ...SUMMARY, id: 9, name: "Physics", member_count: 0, exam_count: 0 });
    render(
      <MemoryRouter initialEntries={["/teacher/classes"]}>
        <Routes>
          <Route path="/teacher/classes" element={<ClassesPage />} />
          <Route path="/teacher/classes/:classId" element={<p>class page</p>} />
        </Routes>
      </MemoryRouter>,
    );
    const tile = await screen.findByRole("link", { name: /Calculus I/ });
    expect(tile).toHaveAttribute("href", "/teacher/classes/7");
    expect(within(tile).getByText("2 students")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "New class" }));
    await userEvent.click(screen.getByRole("button", { name: "Create class" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Give the class a name.");
    await userEvent.type(screen.getByLabelText("Class name"), "Physics");
    await userEvent.type(screen.getByLabelText("Term"), "Spring");
    await userEvent.click(screen.getByRole("button", { name: "Create class" }));
    expect(api.create).toHaveBeenCalledWith({ name: "Physics", section: "", term: "Spring" });
    expect(await screen.findByText("class page")).toBeInTheDocument();
  });

  it("shows archived classes on request", async () => {
    api.list.mockResolvedValue([]);
    render(
      <MemoryRouter>
        <ClassesPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("No classes yet")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Archived" }));
    await waitFor(() => expect(api.list).toHaveBeenLastCalledWith(true));
    expect(await screen.findByText("No archived classes")).toBeInTheDocument();
  });
});

describe("teacher ClassPage", () => {
  it("shows the join code large, copies, regenerates and turns joining off", async () => {
    api.get.mockResolvedValue(DETAIL);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    api.regenerateCode.mockResolvedValue({ ...SUMMARY, join_code: "NEWC0DE" });
    api.update.mockResolvedValue({ ...SUMMARY, join_code: "NEWC0DE", join_enabled: false });
    renderClassPage();

    expect(await screen.findByRole("heading", { name: "Calculus I" })).toBeInTheDocument();
    expect(screen.getByText("K7Q2M9X")).toHaveClass("cl-code");
    await userEvent.click(screen.getByRole("button", { name: "Copy code" }));
    expect(writeText).toHaveBeenCalledWith("K7Q2M9X");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await userEvent.click(screen.getByRole("button", { name: "Make new code" }));
    expect(api.regenerateCode).toHaveBeenCalledWith(7);
    expect(await screen.findByText("NEWC0DE")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("switch", { name: "Students can join with the code" }));
    expect(api.update).toHaveBeenCalledWith(7, { join_enabled: false });
    expect(await screen.findByText(/Joining is off/)).toBeInTheDocument();

    expect(screen.getByRole("link", { name: "New exam for this class" })).toHaveAttribute("href", "/teacher/exams/new?class=7");
    expect(screen.getByRole("link", { name: "Midterm" })).toHaveAttribute("href", "/teacher/exams/e_1");
  });

  it("adds students by username and lists those not found", async () => {
    api.get.mockResolvedValue(DETAIL);
    api.addMembers.mockResolvedValue({
      added: [{ id: 4, username: "carol", name: "Carol Wang", email: null }],
      already: [],
      not_found: ["zed"],
      class: { ...SUMMARY, member_count: 3 },
    });
    renderClassPage();
    await screen.findByRole("heading", { name: "Calculus I" });
    await userEvent.type(screen.getByLabelText("One username or email per line"), "carol{enter}zed");
    expect(screen.getByText("2 entries")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add students" }));
    expect(api.addMembers).toHaveBeenCalledWith(7, "carol\nzed");
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Added 1 student. 1 not found.");
    expect(within(screen.getByRole("list", { name: "Not found" })).getByText("zed")).toBeInTheDocument();
    // what was not found stays in the box to fix
    expect(screen.getByLabelText("One username or email per line")).toHaveValue("zed");
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2)); // reloaded after adding
  });

  it("uploads a CSV and imports from an LMS course", async () => {
    api.get.mockResolvedValue(DETAIL);
    lms.linkedCourses.mockResolvedValue([
      { id: 5, connection_id: 1, provider: "canvas", provider_name: "Canvas", lms_course_id: "101", name: "Calc Canvas",
        code: "C1", member_count: 30, exam_count: 0, last_sync_at: null, last_error: null },
    ]);
    const result = { added: [], already: [], not_found: ["Zed Moon (zed@x.edu)"], class: SUMMARY };
    api.addCsv.mockResolvedValue(result);
    api.importLms.mockResolvedValue({ ...result, not_found: [] });
    renderClassPage();
    await screen.findByRole("heading", { name: "Calculus I" });

    await userEvent.click(screen.getByRole("button", { name: "CSV file" }));
    const file = new File(["name,username\nZed Moon,zed\n"], "roster.csv", { type: "text/csv" });
    await userEvent.upload(screen.getByLabelText("CSV file"), file);
    await userEvent.click(screen.getByRole("button", { name: "Add students" }));
    await waitFor(() => expect(api.addCsv).toHaveBeenCalledWith(7, "name,username\nZed Moon,zed\n"));
    expect(await screen.findByText("Zed Moon (zed@x.edu)")).toBeInTheDocument();

    await userEvent.click(await screen.findByRole("button", { name: "From LMS course" }));
    await userEvent.selectOptions(screen.getByLabelText("Linked LMS course"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Import students" }));
    expect(api.importLms).toHaveBeenCalledWith(7, 5);
  });

  it("searches and removes a student", async () => {
    api.get.mockResolvedValue(DETAIL);
    api.removeMember.mockResolvedValue(undefined);
    renderClassPage();
    await screen.findByRole("heading", { name: "Calculus I" });
    await userEvent.type(screen.getByLabelText("Search"), "bob@");
    expect(screen.queryByText("Alice Chen")).not.toBeInTheDocument();
    expect(screen.getByText("Bob Karimov")).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Search"));

    await userEvent.click(screen.getByRole("button", { name: "Remove Alice Chen" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove Alice Chen" })); // confirm
    expect(api.removeMember).toHaveBeenCalledWith(7, 2);
    await waitFor(() => expect(screen.queryByText("Alice Chen")).not.toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Students · 1" })).toBeInTheDocument();
  });

  it("says when the class does not exist", async () => {
    api.get.mockRejectedValue(new ApiError(404, "Class not found"));
    renderClassPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("This class does not exist.");
  });
});

describe("assign to classes", () => {
  function Harness({ examId, initial = null }: { examId?: string; initial?: number[] | null }) {
    const [ids, setIds] = useState<number[] | null>(initial);
    return (
      <MemoryRouter>
        <ClassAssignSelect examId={examId} value={ids} onChange={(v) => setIds(v)} />
        <output data-testid="ids">{JSON.stringify(ids)}</output>
      </MemoryRouter>
    );
  }

  it("warns when no class is selected and toggles classes", async () => {
    api.list.mockResolvedValue([SUMMARY, { ...SUMMARY, id: 8, name: "Group B", section: "", member_count: 1 }]);
    render(<Harness initial={[]} />);
    expect(await screen.findByText(OPEN_WARNING)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: /Group B/ }));
    expect(screen.getByTestId("ids")).toHaveTextContent("[8]");
    expect(screen.queryByText(OPEN_WARNING)).not.toBeInTheDocument();
    expect(screen.getByText(/Only students in the selected class can/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: /Calculus I/ }));
    expect(screen.getByTestId("ids")).toHaveTextContent("[8,7]");
    await userEvent.click(screen.getByRole("checkbox", { name: /Group B/ }));
    expect(screen.getByTestId("ids")).toHaveTextContent("[7]");
  });

  it("loads the exam's current classes, including archived ones", async () => {
    api.list.mockResolvedValue([SUMMARY]);
    const current: ExamClasses = {
      exam_id: "e_1", student_count: 3, roster_override: false, audience: "classes",
      classes: [{ id: 11, name: "Old group", section: "", term: "", archived: true, member_count: 3 }],
    };
    api.examClasses.mockResolvedValue(current);
    render(<Harness examId="e_1" />);
    expect(await screen.findByRole("checkbox", { name: /Old group \(archived\)/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Calculus I/ })).not.toBeChecked();
    expect(screen.getByTestId("ids")).toHaveTextContent("[11]");
  });

  it("exam page strip: warning when open, chips + count when assigned, edit in place", async () => {
    api.examClasses.mockResolvedValue({ exam_id: "e_1", classes: [], student_count: 0, roster_override: false, audience: "open" });
    api.list.mockResolvedValue([SUMMARY]);
    api.setExamClasses.mockResolvedValue({
      exam_id: "e_1", student_count: 2, roster_override: false, audience: "classes",
      classes: [{ id: 7, name: "Calculus I", section: "MATH 101", term: "", archived: false, member_count: 2 }],
    });
    render(
      <MemoryRouter>
        <ExamClassesBar examId="e_1" />
      </MemoryRouter>,
    );
    expect(await screen.findByText(OPEN_WARNING)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Assign to classes" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: /Calculus I/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save classes" }));
    expect(api.setExamClasses).toHaveBeenCalledWith("e_1", [7]);
    expect(await screen.findByRole("link", { name: "Calculus I · MATH 101" })).toHaveAttribute("href", "/teacher/classes/7");
    expect(screen.getByText("2 students")).toBeInTheDocument();
  });
});

describe("new exam for a class", () => {
  it("preselects the class and assigns it on save", { timeout: 20000 }, async () => {
    const user = userEvent.setup();
    api.list.mockResolvedValue([SUMMARY]);
    api.setExamClasses.mockResolvedValue({ exam_id: "e_new", classes: [], student_count: 2, roster_override: false, audience: "classes" });
    teacher.createExam.mockResolvedValue({ id: "e_new", course_link_id: null } as unknown as Exam);
    render(
      <MemoryRouter initialEntries={["/teacher/exams/new?class=7"]}>
        <Routes>
          <Route path="/teacher/exams/new" element={<ExamSetupPage />} />
          <Route path="/teacher/exams/:id" element={<p>exam page</p>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("checkbox", { name: /Calculus I/ })).toBeChecked();
    expect(screen.queryByText(OPEN_WARNING)).not.toBeInTheDocument();
    await user.type(document.querySelector<HTMLInputElement>("#exam-title")!, "Class quiz");
    await user.click(screen.getByRole("button", { name: "+ Short answer" }));
    await user.type(document.querySelector<HTMLTextAreaElement>("[id$='-prompt']")!, "Capital of France?");
    await user.type(document.querySelector<HTMLInputElement>("[id$='-accepted']")!, "Paris");
    await user.type(document.querySelector<HTMLInputElement>("[id$='-topics']")!, "geo{Enter}");
    await user.click(screen.getByRole("button", { name: "Save exam" }));
    await waitFor(() => expect(api.setExamClasses).toHaveBeenCalledWith("e_new", [7]));
    expect(await screen.findByText("exam page")).toBeInTheDocument();
  });
});

describe("student ClassesPage", () => {
  const MINE: StudentClass[] = [
    {
      id: 7, name: "Calculus I", section: "MATH 101", term: "", description: "", teacher_name: "Demo Teacher",
      joined_at: "2026-10-02T00:00:00Z",
      exams: [
        { id: "e_1", title: "Midterm", course: "", status: "live", scheduled_at: null, duration_min: 30, delivery: "computer", attempt: null },
        { id: "e_2", title: "Quiz", course: "", status: "closed", scheduled_at: null, duration_min: 10, delivery: "computer",
          attempt: { attempt_id: "a_1", status: "submitted" } },
      ],
    },
  ];

  it("lists classes with their exams", async () => {
    api.mine.mockResolvedValue(MINE);
    render(
      <MemoryRouter>
        <StudentClassesPage />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Calculus I" })).toBeInTheDocument();
    expect(screen.getByText("MATH 101 · Demo Teacher")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Check in" })).toHaveAttribute("href", "/student/exams/e_1/checkin");
    expect(screen.getByText("Submitted")).toBeInTheDocument();
  });

  it("joins a class with a code", async () => {
    api.mine.mockResolvedValueOnce([]).mockResolvedValue(MINE);
    api.join.mockResolvedValue({ id: 7, name: "Calculus I", section: "", term: "", teacher_name: "Demo Teacher", already_member: false });
    render(
      <MemoryRouter>
        <StudentClassesPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("No classes yet")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Class code"), "k7q-2m9x");
    await userEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(api.join).toHaveBeenCalledWith("K7Q2M9X");
    expect(await screen.findByRole("status")).toHaveTextContent("You joined Calculus I (Demo Teacher).");
    expect(await screen.findByRole("heading", { name: "Calculus I" })).toBeInTheDocument();
  });

  it("shows why a code was refused", async () => {
    api.mine.mockResolvedValue([]);
    api.join.mockRejectedValue(new ApiError(403, "Joining this class is turned off. Ask your teacher."));
    render(
      <MemoryRouter>
        <StudentClassesPage />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Join" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Type the class code from your teacher.");
    expect(api.join).not.toHaveBeenCalled();
    await userEvent.type(screen.getByLabelText("Class code"), "ABC2345");
    await userEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Joining this class is turned off. Ask your teacher.");
  });
});
