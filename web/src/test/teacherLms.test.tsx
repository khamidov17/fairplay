import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Exam, LmsConnection, LmsCourseLink, LmsProvider } from "../api/types";

const lms = vi.hoisted(() => ({
  providers: vi.fn(),
  connections: vi.fn(),
  connect: vi.fn(),
  disconnect: vi.fn(),
  courses: vi.fn(),
  linkCourse: vi.fn(),
  resync: vi.fn(),
  linkedCourses: vi.fn(),
  linkExam: vi.fn(),
  pushGrades: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return { ...orig, lmsApi: lms };
});

import { ApiError } from "../api/client";
import LmsPage, { syncSummary } from "../pages/teacher/LmsPage";
import { LmsCourseSelect } from "../components/teacher/lms/LmsCourseSelect";
import { SendGradesButton, pushSummary } from "../components/teacher/lms/SendGradesButton";
import { sectionFor } from "../components/teacher/TeacherLayout";

const PROVIDERS: LmsProvider[] = [
  { id: "canvas", name: "Canvas", auth: "token", status: "ready", token_help: "canvas help", base_url_hint: "https://yourschool.instructure.com" },
  { id: "moodle", name: "Moodle", auth: "token", status: "beta", token_help: "Moodle: Preferences → Security keys", base_url_hint: "https://moodle.x" },
  { id: "blackboard", name: "Blackboard Learn", auth: "oauth_token", status: "beta", token_help: "bb", base_url_hint: "" },
  { id: "brightspace", name: "D2L Brightspace", auth: "oauth_token", status: "beta", token_help: "d2l", base_url_hint: "" },
];

const CONN: LmsConnection = {
  id: 1, provider: "canvas", provider_name: "Canvas", status: "ready", base_url: "https://canvas.test",
  label: "DKU Canvas", lms_user_name: "Dr Teacher", created_at: "2026-10-01T00:00:00Z",
  last_sync_at: null, last_error: null, course_count: 0,
};

const LINK: LmsCourseLink = {
  id: 5, connection_id: 1, provider: "canvas", provider_name: "Canvas", lms_course_id: "101",
  name: "Calculus I", code: "MATH 101", member_count: 3, exam_count: 1,
  last_sync_at: "2026-10-01T10:00:00Z", last_error: null,
};

function renderPage() {
  return render(
    <MemoryRouter>
      <LmsPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  for (const f of Object.values(lms)) f.mockReset();
  lms.providers.mockResolvedValue(PROVIDERS);
  lms.connections.mockResolvedValue([]);
  lms.linkedCourses.mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

describe("helpers", () => {
  it("summarises a roster sync and a push", () => {
    expect(syncSummary({ added: 3, updated: 0, removed: 0, total: 3 })).toBe("3 students in the LMS course · 3 added");
    expect(syncSummary({ added: 0, updated: 1, removed: 2, total: 1 })).toBe(
      "1 student in the LMS course · 0 added, 1 renamed, 2 removed",
    );
    expect(pushSummary({ pushed: 2, skipped: 0, errors: [] }, "Canvas")).toBe("Sent 2 grades to Canvas.");
  });
  it("marks the LMS sync nav item active", () => {
    expect(sectionFor("/teacher/lms")).toBe("lms");
  });
});

describe("LmsPage", () => {
  it("shows the connect form with Canvas token help when nothing is connected", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Connect your LMS" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Canvas/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getAllByText("Beta")).toHaveLength(3);
    expect(screen.getByText("How to get a token")).toBeInTheDocument();
    expect(screen.getByText("Account → Settings")).toBeInTheDocument();
    expect(screen.getByText(/developer key or LTI 1\.3/)).toBeInTheDocument();
    expect(screen.getByText("No linked courses yet")).toBeInTheDocument();
  });

  it("connects, clears the token and lists courses", async () => {
    const user = userEvent.setup();
    lms.connect.mockResolvedValue(CONN);
    lms.connections.mockResolvedValueOnce([]).mockResolvedValue([CONN]);
    lms.courses.mockResolvedValue([
      { lms_course_id: "101", name: "Calculus I", code: "MATH 101", link_id: null },
      { lms_course_id: "102", name: "Physics", code: "", link_id: null },
    ]);
    renderPage();
    await user.type(await screen.findByLabelText("Canvas web address"), "https://canvas.test");
    await user.type(screen.getByLabelText("Access token"), "secret-token");
    await user.click(screen.getByRole("button", { name: "Connect Canvas" }));
    await waitFor(() =>
      expect(lms.connect).toHaveBeenCalledWith({
        provider: "canvas", base_url: "https://canvas.test", token: "secret-token", label: "",
      }),
    );
    expect(await screen.findByText("Connected to Canvas as Dr Teacher.")).toBeInTheDocument();
    expect(await screen.findByText("Physics")).toBeInTheDocument();
    expect(lms.courses).toHaveBeenCalledWith(1);
    expect(screen.queryByText(/secret-token/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Access token")).not.toBeInTheDocument();
  });

  it("shows why a connection failed", async () => {
    const user = userEvent.setup();
    lms.connect.mockRejectedValue(new ApiError(422, "Canvas did not accept the access token (401)."));
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^Moodle/ }));
    expect(screen.getByText(/Moodle support is in beta/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Moodle web address"), "https://m.test");
    await user.type(screen.getByLabelText("Access token"), "x");
    await user.click(screen.getByRole("button", { name: "Connect Moodle" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("did not accept the access token");
    expect(lms.connect.mock.calls[0][0].provider).toBe("moodle");
  });

  it("links a course and re-syncs a roster", async () => {
    const user = userEvent.setup();
    lms.connections.mockResolvedValue([CONN]);
    lms.courses.mockResolvedValue([{ lms_course_id: "101", name: "Calculus I", code: "MATH 101", link_id: null }]);
    lms.linkCourse.mockResolvedValue({ ...LINK, sync: { added: 3, updated: 0, removed: 0, total: 3 } });
    renderPage();
    expect(await screen.findByText("DKU Canvas")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Connect your LMS" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show courses" }));
    lms.linkedCourses.mockResolvedValue([LINK]);
    await user.click(await screen.findByRole("button", { name: "Link Calculus I" }));
    expect(lms.linkCourse).toHaveBeenCalledWith(1, "101");
    expect(await screen.findByText(/Linked Calculus I\. 3 students in the LMS course · 3 added\./)).toBeInTheDocument();

    const table = screen.getByRole("heading", { name: "Linked courses" }).closest("section")!;
    const row = within(table).getByText("Calculus I").closest("tr")!;
    expect(within(row).getByText("3")).toBeInTheDocument();

    lms.resync.mockResolvedValue({ ...LINK, sync: { added: 0, updated: 0, removed: 1, total: 2 } });
    await user.click(within(table).getByRole("button", { name: "Re-sync roster of Calculus I" }));
    expect(lms.resync).toHaveBeenCalledWith(5);
    expect(await screen.findByText(/Roster of Calculus I updated\./)).toBeInTheDocument();
  });

  it("shows an LMS error when courses cannot be loaded", async () => {
    const user = userEvent.setup();
    lms.connections.mockResolvedValue([CONN]);
    lms.courses.mockRejectedValue(new ApiError(502, "Canvas did not accept the access token (401). Remove the connection and connect again with a new token."));
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Show courses" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("connect again");
  });

  it("removes a connection after confirming", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    lms.connections.mockResolvedValueOnce([CONN]).mockResolvedValue([]);
    lms.disconnect.mockResolvedValue(undefined);
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Remove DKU Canvas" }));
    expect(lms.disconnect).toHaveBeenCalledWith(1);
    expect(await screen.findByText("Canvas connection removed.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Connect your LMS" })).toBeInTheDocument();
  });
});

describe("LmsCourseSelect", () => {
  it("lists linked courses and reports the choice", async () => {
    const user = userEvent.setup();
    lms.linkedCourses.mockResolvedValue([LINK]);
    const onChange = vi.fn();
    render(
      <MemoryRouter>
        <LmsCourseSelect value={null} onChange={onChange} />
      </MemoryRouter>,
    );
    const select = await screen.findByLabelText("Course in your LMS");
    await user.selectOptions(select, "5");
    expect(onChange).toHaveBeenCalledWith(5, LINK);
  });

  it("points to the LMS page when nothing is linked", async () => {
    render(
      <MemoryRouter>
        <LmsCourseSelect value={null} onChange={() => undefined} />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("link", { name: "Link a course" })).toHaveAttribute("href", "/teacher/lms");
  });
});

describe("SendGradesButton", () => {
  const exam = { id: "e1", course_link_id: 5 } as Exam;

  it("is hidden until grades are released or when not linked", () => {
    const { container, rerender } = render(<SendGradesButton exam={exam} released={false} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<SendGradesButton exam={{ ...exam, course_link_id: null }} released />);
    expect(container).toBeEmptyDOMElement();
  });

  it("sends grades and shows the result", async () => {
    const user = userEvent.setup();
    lms.linkedCourses.mockResolvedValue([LINK]);
    lms.pushGrades.mockResolvedValue({ pushed: 2, skipped: 1, errors: [{ student: "Dan Park", detail: "Grades are locked" }] });
    render(<SendGradesButton exam={exam} released />);
    await user.click(await screen.findByRole("button", { name: "Send grades to Canvas" }));
    expect(lms.pushGrades).toHaveBeenCalledWith("e1");
    const msg = await screen.findByRole("alert");
    expect(msg).toHaveTextContent("Sent 2 grades to Canvas");
    expect(msg).toHaveTextContent("1 skipped");
    expect(msg).toHaveTextContent("Dan Park: Grades are locked");
  });

  it("shows a 409 reason", async () => {
    const user = userEvent.setup();
    lms.pushGrades.mockRejectedValue(new ApiError(409, "Release the grades first"));
    render(<SendGradesButton exam={exam} released />);
    await user.click(screen.getByRole("button", { name: /Send grades to/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Release the grades first");
  });
});
