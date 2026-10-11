// Phase 1a: session timeline page, invigilator exit code in the live room, the Mac-app
// exam-session bridge message.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Exam, LiveRoom, SessionTimeline } from "../api/types";

const api = vi.hoisted(() => ({ getExam: vi.fn(), live: vi.fn(), results: vi.fn(), gradingSummary: vi.fn() }));
const sec = vi.hoisted(() => ({ timeline: vi.fn(), packageKey: vi.fn() }));
const me = vi.hoisted(() => vi.fn());

vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return {
    ...orig,
    auth: { ...orig.auth, me },
    teacherApi: { ...orig.teacherApi, ...api },
    securityApi: { ...orig.securityApi, ...sec },
  };
});

import { ApiError, setToken } from "../api/client";
import * as bridge from "../lib/bridge";
import TeacherRoutes from "../pages/teacher/routes";
import { timelineCounts } from "../pages/teacher/SessionTimelinePage";

function renderAt(path: string) {
  setToken("teacher", "tok");
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/teacher/*" element={<TeacherRoutes />} />
      </Routes>
    </MemoryRouter>,
  );
}

const TL: SessionTimeline = {
  exam_id: "e_abc",
  attempt_id: "a1",
  exam_title: "Calculus Quiz",
  student: { username: "alice", name: "Alice Chen" },
  status: "submitted",
  started_at: "2026-10-12T09:01:00Z",
  deadline_at: "2026-10-12T09:31:00Z",
  submitted_at: "2026-10-12T09:29:00Z",
  receipt: "K7Q2-M9XD",
  late: false,
  auto_submitted: false,
  events: [
    { at: "2026-10-12T08:58:00Z", source: "audit", action: "login_ok", summary: "Signed in", actor: "student", ip: "10.0.0.7", detail: {}, tone: "info" },
    { at: "2026-10-12T09:01:00Z", source: "audit", action: "attempt_started", summary: "Started the exam", actor: "student", ip: "10.0.0.7", detail: {}, tone: "good" },
    { at: "2026-10-12T09:05:00Z", source: "integrity", action: "other_app_foreground", summary: "Another app came to the front: Safari", actor: "student", ip: null, detail: {}, tone: "warn" },
    { at: "2026-10-12T09:10:00Z", source: "audit", action: "connection_lost", summary: "Connection lost (no heartbeat from the exam page)", actor: "student", ip: null, detail: {}, tone: "warn" },
    { at: "2026-10-12T09:12:00Z", source: "audit", action: "connection_restored", summary: "Connection restored after 2 min", actor: "student", ip: "10.0.0.7", detail: {}, tone: "good" },
    { at: "2026-10-12T09:29:00Z", source: "audit", action: "submitted", summary: "Submitted (receipt K7Q2-M9XD)", actor: "student", ip: "10.0.0.7", detail: {}, tone: "good" },
  ],
};

beforeEach(() => {
  for (const f of [...Object.values(api), ...Object.values(sec)]) f.mockReset();
  me.mockReset().mockResolvedValue({ username: "teacher", name: "Demo Teacher", role: "teacher" });
  api.gradingSummary.mockResolvedValue([]);
});

describe("session timeline page", () => {
  it("lists events in order with who / where, and filters to the ones worth a look", async () => {
    const user = userEvent.setup();
    sec.timeline.mockResolvedValue(TL);
    renderAt("/teacher/exams/e_abc/submissions/a1/timeline");
    expect(await screen.findByRole("heading", { name: "Session timeline" })).toBeInTheDocument();
    expect(sec.timeline).toHaveBeenCalledWith("e_abc", "a1");
    expect(screen.getByText(/Alice Chen · alice · Calculus Quiz/)).toBeInTheDocument();
    expect(within(screen.getByLabelText("Session status")).getByText("Submitted")).toBeInTheDocument();
    const facts = screen.getByLabelText("Session facts");
    expect(within(facts).getByText("K7Q2-M9XD")).toBeInTheDocument();

    const items = screen.getAllByRole("listitem");
    expect(items.map((li) => li.querySelector(".tl-summary")?.textContent)).toEqual(TL.events.map((e) => e.summary));
    expect(items[2]).toHaveClass("warn");
    expect(within(items[0]).getByText(/Student · Server log · 10\.0\.0\.7/)).toBeInTheDocument();
    expect(within(items[2]).getByText(/Lockdown app/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Alice Chen's submission/ })).toHaveAttribute(
      "href",
      "/teacher/exams/e_abc/submissions/a1",
    );

    await user.click(screen.getByRole("button", { name: "Worth a look (2)" }));
    expect(screen.getAllByRole("listitem").map((li) => li.querySelector(".tl-summary")?.textContent)).toEqual([
      "Another app came to the front: Safari",
      "Connection lost (no heartbeat from the exam page)",
    ]);
  });

  it("shows a clear message for a session that is not this teacher's", async () => {
    sec.timeline.mockRejectedValue(new ApiError(404, "Exam not found"));
    renderAt("/teacher/exams/e_other/submissions/a9/timeline");
    expect(await screen.findByRole("alert")).toHaveTextContent("This exam session was not found.");
  });

  it("counts flags and connection drops", () => {
    expect(timelineCounts(TL.events)).toEqual({ look: 2, lockdown: 1, gaps: 1 });
  });
});

describe("live room exit code", () => {
  const exam: Exam = {
    id: "e_abc", title: "Calculus Quiz", duration_min: 20, status: "live", start_code: "4729", questions: [],
    created_at: "2026-10-01T10:00:00Z", question_count: 1, total_points: 1, course: "MATH 201", room: "",
    scheduled_at: null, shuffle_questions: false, grades_released: false, released_at: null, closed_at: null,
    pending_grading: 0, exit_code: "K7Q2-M9XD",
  };
  const live: LiveRoom = {
    exam_id: "e_abc", status: "live", start_code: "4729", duration_min: 20, students: [], flags: [],
    counts: { writing: 0, submitted: 0, offline: 0, needs_look: 0 }, exit_code: "K7Q2-M9XD",
  };

  it("shows the per-exam invigilator code next to the start code", async () => {
    api.getExam.mockResolvedValue(exam);
    api.live.mockResolvedValue(live);
    renderAt("/teacher/exams/e_abc");
    expect(await screen.findByTestId("exit-code")).toHaveTextContent("K7Q2-M9XD");
    expect(screen.getByText(/Invigilator exit code/)).toBeInTheDocument();
    expect(screen.queryByText("1234")).toBeNull();
  });
});

describe("exam session for the Mac app", () => {
  afterEach(() => {
    delete (window as unknown as { webkit?: unknown }).webkit;
    bridge._resetBridgeForTests();
  });

  it("posts examSession (attempt, token, exit verifier) only inside the Mac app", () => {
    const check = { alg: "PBKDF2-SHA256" as const, iterations: 100000, salt: "c2FsdA==", hash: "aGFzaA==" };
    bridge.setExamSession({ attempt_id: "a_1", token: "tok", exit_check: check }); // no-op outside
    const postMessage = vi.fn();
    (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { proctora: { postMessage } } };
    bridge.setExamSession({ attempt_id: "a_1", token: "tok", exit_check: check });
    expect(postMessage).toHaveBeenCalledWith({ action: "examSession", attempt_id: "a_1", token: "tok", exit_check: check });
    bridge.setExamSession({ attempt_id: null, token: null, exit_check: null });
    expect(postMessage).toHaveBeenLastCalledWith({ action: "examSession", attempt_id: null, token: null, exit_check: null });
  });
});
