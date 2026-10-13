// 2026-10-13: Google Classroom-style classes, the invite link, optional features.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const k = vi.hoisted(() => ({
  list: vi.fn(),
  get: vi.fn(),
  create: vi.fn(),
  listExams: vi.fn(),
  gradebook: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return {
    ...orig,
    classesApi: { ...orig.classesApi, list: k.list, get: k.get, create: k.create },
    teacherApi: { ...orig.teacherApi, listExams: k.listExams, gradebook: k.gradebook },
  };
});

import ClassesPage from "../pages/teacher/ClassesPage";
import ClassPage from "../pages/teacher/ClassPage";
import JoinLinkPage, { PENDING_JOIN_KEY, takeJoinCode } from "../pages/student/JoinLinkPage";
import { clearToken, setToken } from "../api/client";

const calc = {
  id: 3, name: "Calculus I", term: "Fall 2026 · Section 2", teacher_name: "Demo Teacher",
  join_code: "K7QM-3XPA", member_count: 2, exam_count: 1, created_at: "2026-10-12T00:00:00Z",
};

beforeEach(() => {
  k.list.mockReset().mockResolvedValue([calc]);
  k.get.mockReset().mockResolvedValue({
    ...calc,
    members: [
      { username: "alice", name: "Alice Chen", joined_at: "2026-10-12T00:00:00Z" },
      { username: "bob", name: "Bob Karimov", joined_at: "2026-10-12T00:00:00Z" },
    ],
  });
  k.listExams.mockReset().mockResolvedValue([
    { id: "e1", title: "Midterm", status: "live", class_id: 3, question_count: 4, duration_min: 30 },
    { id: "e2", title: "Other class quiz", status: "draft", class_id: 9, question_count: 1, duration_min: 10 },
  ]);
  k.gradebook.mockReset().mockResolvedValue({ courses: [], exams: [], students: [] });
});

describe("classes as cards", () => {
  it("shows name, term, teacher and counts, and links to the class", async () => {
    render(
      <MemoryRouter>
        <ClassesPage />
      </MemoryRouter>,
    );
    const card = (await screen.findByText("Calculus I")).closest("li")!;
    expect(within(card).getByText("Fall 2026 · Section 2")).toBeInTheDocument();
    expect(within(card).getByText("Demo Teacher")).toBeInTheDocument();
    expect(within(card).getByText("2 students")).toBeInTheDocument();
    expect(within(card).getByText("1 exam")).toBeInTheDocument();
    expect(within(card).getByText("Calculus I").closest("a")).toHaveAttribute("href", "/teacher/classes/3");
  });

  it("creates a class with a term", async () => {
    const user = userEvent.setup();
    k.create.mockResolvedValue({ ...calc, id: 5 });
    render(
      <MemoryRouter initialEntries={["/teacher/classes"]}>
        <Routes>
          <Route path="/teacher/classes" element={<ClassesPage />} />
          <Route path="/teacher/classes/:classId" element={<p>class page</p>} />
        </Routes>
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: /New class/ }));
    await user.type(screen.getByLabelText("Class name"), "Stats");
    await user.type(screen.getByLabelText(/Term or section/), "Spring 2027");
    await user.click(screen.getByRole("button", { name: "Create class" }));
    await waitFor(() => expect(k.create).toHaveBeenCalledWith("Stats", "Spring 2027"));
    expect(await screen.findByText("class page")).toBeInTheDocument();
  });
});

describe("class page", () => {
  function renderClass(tab = "") {
    return render(
      <MemoryRouter initialEntries={[`/teacher/classes/3${tab}`]}>
        <Routes>
          <Route path="/teacher/classes/:classId" element={<ClassPage />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("has the banner, code, and only this class's exams", async () => {
    renderClass();
    expect(await screen.findByRole("heading", { level: 1, name: "Calculus I" })).toBeInTheDocument();
    expect(screen.getByText(/Fall 2026 · Section 2 · Demo Teacher · 2 students/)).toBeInTheDocument();
    expect(screen.getByText("K7QM-3XPA")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Exams" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Midterm")).toBeInTheDocument();
    expect(screen.queryByText("Other class quiz")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /New exam/ })).toHaveAttribute("href", "/teacher/exams/new?class=3");
  });

  it("lists students and loads the class's marks", async () => {
    const user = userEvent.setup();
    renderClass("?tab=students");
    expect(await screen.findByText("Alice Chen")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Marks" }));
    await waitFor(() => expect(k.gradebook).toHaveBeenCalledWith("", 3));
  });
});

describe("invite link", () => {
  it("remembers the code and sends a signed-out student to sign in", async () => {
    clearToken("student");
    sessionStorage.removeItem(PENDING_JOIN_KEY);
    render(
      <MemoryRouter initialEntries={["/student/join/k7qm3xpa"]}>
        <Routes>
          <Route path="/student/join/:code" element={<JoinLinkPage />} />
          <Route path="/student/login" element={<p>login page</p>} />
          <Route path="/student" element={<p>home page</p>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("login page")).toBeInTheDocument();
    expect(takeJoinCode()).toBe("K7QM3XPA");
    expect(takeJoinCode()).toBeNull();   // used once
    setToken("student", "tok");
    clearToken("student");
  });
});
