// Create account form, login redirect by role, and the superadmin dashboard (mocked API).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminStats, AdminTeacher } from "../api/types";

const authApi = vi.hoisted(() => ({ login: vi.fn(), register: vi.fn(), me: vi.fn() }));
const admin = vi.hoisted(() => ({ stats: vi.fn(), teachers: vi.fn(), setTeacherStatus: vi.fn() }));

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return { ...orig, auth: { ...orig.auth, ...authApi }, adminApi: { ...orig.adminApi, ...admin } };
});

import { ApiError, getToken, setToken } from "../api/client";
import RegisterPage from "../pages/RegisterPage";
import LoginPage from "../pages/LoginPage";
import AdminRoutes from "../pages/admin/routes";
import { validateRegister, type RegisterForm } from "../pages/registerValidation";

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname}</div>;
}

function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/teacher/login" element={<LoginPage role="teacher" />} />
        <Route path="/student/login" element={<LoginPage role="student" />} />
        <Route path="/admin/*" element={<AdminRoutes />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

const good: RegisterForm = {
  name: "Dana Teach",
  email: "dana@uni.edu",
  username: "dana",
  password: "s3cret-pass",
  confirm: "s3cret-pass",
  role: "teacher",
};

describe("validateRegister", () => {
  it("accepts a good form", () => {
    expect(validateRegister(good)).toEqual({});
  });
  it("flags each bad field", () => {
    const e = validateRegister({
      name: " D ",
      email: "nope",
      username: "a b",
      password: "short",
      confirm: "other",
      role: "",
    });
    expect(Object.keys(e).sort()).toEqual(["confirm", "email", "name", "password", "role", "username"]);
  });
  it("checks password strength and confirmation", () => {
    expect(validateRegister({ ...good, password: "allletters", confirm: "allletters" }).password).toMatch(/digit/);
    expect(validateRegister({ ...good, password: "dana-rocks1", confirm: "dana-rocks1" }).password).toMatch(/username/);
    expect(validateRegister({ ...good, confirm: "s3cret-pasS" }).confirm).toMatch(/do not match/);
  });
});

describe("RegisterPage", () => {
  beforeEach(() => {
    authApi.register.mockReset();
    authApi.login.mockReset();
  });

  it("shows field errors and does not call the server for an invalid form", async () => {
    const user = userEvent.setup();
    renderApp("/register");
    await user.type(screen.getByLabelText("Full name"), "Dana Teach");
    await user.type(screen.getByLabelText("Email"), "dana@");
    await user.type(screen.getByLabelText("Username"), "dana");
    await user.type(screen.getByLabelText("Password"), "s3cret-pass");
    await user.type(screen.getByLabelText("Confirm password"), "s3cret-pas");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("Passwords do not match.")).toBeInTheDocument();
    expect(screen.getByText("Choose Teacher or Student.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(authApi.register).not.toHaveBeenCalled();
  });

  it("registers a student (role from the link) and signs in", async () => {
    authApi.register.mockResolvedValue({ status: "active", role: "student", name: "Sam", token: "tok-s" });
    const user = userEvent.setup();
    renderApp("/register?role=student");
    expect(screen.getByRole("radio", { name: /Student/ })).toBeChecked();
    await user.type(screen.getByLabelText("Full name"), "Sam Student");
    await user.type(screen.getByLabelText("Email"), "sam@uni.edu");
    await user.type(screen.getByLabelText("Username"), "sam");
    await user.type(screen.getByLabelText("Password"), "s3cret-pass");
    await user.type(screen.getByLabelText("Confirm password"), "s3cret-pass");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/student"));
    expect(authApi.register).toHaveBeenCalledWith({
      name: "Sam Student",
      email: "sam@uni.edu",
      username: "sam",
      password: "s3cret-pass",
      role: "student",
    });
    expect(getToken("student")).toBe("tok-s");
  });

  it("shows the pending-approval message for teachers in approval mode", async () => {
    authApi.register.mockResolvedValue({
      status: "pending",
      role: "teacher",
      name: "Dana",
      token: null,
      detail: "Your teacher account is waiting for approval by an administrator.",
    });
    const user = userEvent.setup();
    renderApp("/register");
    await user.click(screen.getByRole("radio", { name: /Teacher/ }));
    await user.type(screen.getByLabelText("Full name"), good.name);
    await user.type(screen.getByLabelText("Email"), good.email);
    await user.type(screen.getByLabelText("Username"), good.username);
    await user.type(screen.getByLabelText("Password"), good.password);
    await user.type(screen.getByLabelText("Confirm password"), good.confirm);
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText(/waiting for approval/)).toBeInTheDocument();
    expect(getToken("teacher")).toBeNull();
  });

  it("shows server errors such as a taken username", async () => {
    authApi.register.mockRejectedValue(new ApiError(409, "That username is taken"));
    const user = userEvent.setup();
    renderApp("/register?role=teacher");
    await user.type(screen.getByLabelText("Full name"), good.name);
    await user.type(screen.getByLabelText("Email"), good.email);
    await user.type(screen.getByLabelText("Username"), good.username);
    await user.type(screen.getByLabelText("Password"), good.password);
    await user.type(screen.getByLabelText("Confirm password"), good.confirm);
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That username is taken");
  });
});

describe("LoginPage", () => {
  beforeEach(() => {
    authApi.login.mockReset();
  });

  it("links to Create account and explains Forgot password", async () => {
    const user = userEvent.setup();
    renderApp("/teacher/login");
    expect(screen.getByRole("link", { name: "Create account" })).toHaveAttribute("href", "/register?role=teacher");
    await user.click(screen.getByRole("button", { name: "Forgot password?" }));
    expect(screen.getByRole("note")).toHaveTextContent("Contact your administrator");
  });

  it("redirects by role after sign-in (superadmin on the teacher page goes to /admin)", async () => {
    authApi.login.mockResolvedValue({ token: "tok-a", role: "superadmin", name: "Admin" });
    admin.stats.mockReturnValue(new Promise(() => undefined));
    admin.teachers.mockReturnValue(new Promise(() => undefined));
    authApi.me.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    renderApp("/teacher/login");
    await user.type(screen.getByLabelText("Username or email"), "admin");
    await user.type(screen.getByLabelText("Password"), "admin123");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(getToken("superadmin")).toBe("tok-a"));
    expect(await screen.findByText("Loading dashboard…")).toBeInTheDocument();
  });

  it("shows the server message for a pending or disabled account", async () => {
    authApi.login.mockRejectedValue(new ApiError(403, "This account has been disabled. Contact your administrator."));
    const user = userEvent.setup();
    renderApp("/student/login");
    await user.type(screen.getByLabelText("Username or email"), "alice@uni.edu");
    await user.type(screen.getByLabelText("Password"), "x");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("disabled");
  });
});

const stats: AdminStats = {
  teachers: { active: 2, pending: 1, disabled: 1, total: 4 },
  students: 37,
  exams_created: 12,
  exams_held: 9,
  attempts_submitted: 214,
  active_teachers_7d: 1,
  active_teachers_30d: 2,
  generated_at: "2026-10-12T10:00:00.000Z",
};

function teacher(over: Partial<AdminTeacher>): AdminTeacher {
  return {
    id: 1,
    username: "teacher",
    name: "Demo Teacher",
    email: null,
    status: "active",
    created_at: "2026-09-01T10:00:00.000Z",
    last_login_at: "2026-10-11T10:00:00.000Z",
    exams: 5,
    students_reached: 30,
    ...over,
  };
}

describe("Admin dashboard", () => {
  beforeEach(() => {
    admin.stats.mockReset().mockResolvedValue(stats);
    admin.teachers.mockReset().mockResolvedValue([
      teacher({ id: 7, username: "pend", name: "Pat Pending", email: "pat@uni.edu", status: "pending", last_login_at: null, exams: 0, students_reached: 0 }),
      teacher({}),
      teacher({ id: 9, username: "gone", name: "Gus Gone", status: "disabled" }),
    ]);
    admin.setTeacherStatus.mockReset();
    authApi.me.mockReset().mockResolvedValue({ username: "admin", name: "Platform Admin", role: "superadmin" });
  });

  it("redirects to /admin/login without a token", () => {
    renderApp("/admin");
    expect(screen.getByRole("heading", { name: "Administrator sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create account" })).not.toBeInTheDocument();
  });

  it("renders tiles, the teacher table and the header", async () => {
    setToken("superadmin", "tok");
    renderApp("/admin");
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(within(screen.getByTestId("tile-Students")).getByText("37")).toBeInTheDocument();
    expect(within(screen.getByTestId("tile-Pending teachers")).getByText("1")).toBeInTheDocument();
    expect(within(screen.getByTestId("tile-Attempts submitted")).getByText("214")).toBeInTheDocument();
    expect(within(screen.getByTestId("tile-Teachers active, 30 days")).getByText("2")).toBeInTheDocument();
    expect(await screen.findByTestId("admin-user")).toHaveTextContent("Platform Admin");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();

    const pend = screen.getByTestId("teacher-row-pend");
    expect(within(pend).getByText("pat@uni.edu")).toBeInTheDocument();
    expect(within(pend).getByText("Pending approval")).toBeInTheDocument();
    expect(within(pend).getByText("Never")).toBeInTheDocument();
    expect(within(pend).getByRole("button", { name: "Approve Pat Pending" })).toBeInTheDocument();
    const active = screen.getByTestId("teacher-row-teacher");
    expect(within(active).getByText("30")).toBeInTheDocument();
    expect(within(active).queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(within(screen.getByTestId("teacher-row-gone")).getByRole("button", { name: "Enable Gus Gone" })).toBeInTheDocument();
  });

  it("approves a pending teacher and refreshes the row", async () => {
    setToken("superadmin", "tok");
    admin.setTeacherStatus.mockResolvedValue(
      teacher({ id: 7, username: "pend", name: "Pat Pending", email: "pat@uni.edu", status: "active", last_login_at: null, exams: 0, students_reached: 0 }),
    );
    const user = userEvent.setup();
    renderApp("/admin");
    await user.click(await screen.findByRole("button", { name: "Approve Pat Pending" }));
    expect(admin.setTeacherStatus).toHaveBeenCalledWith(7, "approve");
    const row = screen.getByTestId("teacher-row-pend");
    await waitFor(() => expect(within(row).getByText("Active")).toBeInTheDocument());
    expect(within(row).getByRole("button", { name: "Disable Pat Pending" })).toBeInTheDocument();
    expect(admin.stats).toHaveBeenCalledTimes(2);
  });

  it("signs out", async () => {
    setToken("superadmin", "tok");
    const user = userEvent.setup();
    renderApp("/admin");
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(getToken("superadmin")).toBeNull();
    expect(screen.getByRole("heading", { name: "Administrator sign in" })).toBeInTheDocument();
  });
});
