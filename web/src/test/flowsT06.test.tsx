// Audit 2026-10-12 T-06: remaining web flows -- website-exam hand-in (retry vs 4xx),
// teacher closing the exam (heartbeat "submitted"), submit retry, live-room counts with a
// silent app.
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AttemptView, LiveRoom, LiveStudent } from "../api/types";

const api = vi.hoisted(() => ({ submit: vi.fn(), getAttempt: vi.fn(), heartbeat: vi.fn(), saveAnswers: vi.fn(), events: vi.fn() }));
vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, studentApi: { ...real.studentApi, ...api } };
});

import { ApiError, NetworkError } from "../api/client";
import ExamTakingPage from "../pages/student/ExamTakingPage";
import ExternalDonePage, { EXTERNAL_SUBMIT_RETRY_MS } from "../pages/student/ExternalDonePage";
import { countsOf } from "../pages/teacher/LiveRoom";

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
  localStorage.clear();
});

function view(over: Partial<AttemptView> = {}): AttemptView {
  const now = Date.now();
  return {
    attempt_id: "a_1", exam_id: "e_1", title: "Quiz", course: "", duration_min: 30,
    started_at: new Date(now).toISOString(), deadline_at: new Date(now + 30 * 60_000).toISOString(),
    server_time: new Date(now).toISOString(), status: "in_progress", answers: {}, mode: "questions",
    external_url: null, allowed_domains: [], finish_url_contains: null,
    questions: [{ id: "q1", type: "mcq", prompt: "2+2?", points: 1, options: ["3", "4"] }],
    ...over,
  } as AttemptView;
}

function at(path: string, el: JSX.Element) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/student/exam/:attemptId" element={el} />
        <Route path="/student/external-done/:attemptId" element={el} />
        <Route path="/student/done/:attemptId" element={<p>done page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("website exam hand-in", () => {
  it("keeps retrying while offline, then hands in", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.getAttempt.mockResolvedValue(view({ mode: "external" }));
    api.submit.mockRejectedValueOnce(new NetworkError()).mockResolvedValue({ status: "submitted", submitted_at: "t", receipt: "R1" });
    at("/student/external-done/a_1", <ExternalDonePage />);
    await waitFor(() => expect(api.submit).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(EXTERNAL_SUBMIT_RETRY_MS + 100);
    expect(await screen.findByText("done page")).toBeTruthy();
    expect(api.submit).toHaveBeenCalledTimes(2);
  });

  it("stops on a refusal and says why", async () => {
    api.getAttempt.mockResolvedValue(view({ mode: "external" }));
    api.submit.mockRejectedValue(new ApiError(404, "Attempt not found"));
    at("/student/external-done/a_1", <ExternalDonePage />);
    expect(await screen.findByText(/not found/)).toBeTruthy();
    expect(api.submit).toHaveBeenCalledTimes(1);
  });
});

describe("computer exam", () => {
  it("hands in by itself when the teacher closes the exam (heartbeat says submitted)", async () => {
    api.getAttempt.mockResolvedValue(view());
    api.heartbeat.mockResolvedValue({ ok: true, status: "submitted", server_time: new Date().toISOString(), deadline_at: view().deadline_at });
    api.submit.mockResolvedValue({ status: "submitted", submitted_at: "t", receipt: "R2" });
    api.events.mockResolvedValue({ ok: true, stored: 0 });
    at("/student/exam/a_1", <ExamTakingPage />);
    expect(await screen.findByText("done page", {}, { timeout: 8000 })).toBeTruthy();
    expect(api.submit).toHaveBeenCalled();
  });

  it("retries a hand-in that failed for network reasons", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const past = new Date(Date.now() - 1000).toISOString();
    api.getAttempt.mockResolvedValue(view({ deadline_at: past }));
    api.heartbeat.mockResolvedValue({ ok: true, status: "in_progress", server_time: new Date().toISOString(), deadline_at: past });
    api.submit.mockRejectedValueOnce(new NetworkError()).mockResolvedValue({ status: "submitted", submitted_at: "t", receipt: "R3" });
    at("/student/exam/a_1", <ExamTakingPage />);
    await waitFor(() => expect(api.submit).toHaveBeenCalledTimes(1), { timeout: 5000 });
    await vi.advanceTimersByTimeAsync(3500);
    expect(await screen.findByText("done page", {}, { timeout: 5000 })).toBeTruthy();
  });
});

describe("live room counts", () => {
  it("counts a writing student whose app went silent as 'needs your look'", () => {
    const s = (over: Partial<LiveStudent>) => ({ status: "in_progress", online: true, app_online: true, needs_look: false, ...over }) as LiveStudent;
    const live = {
      students: [s({}), s({ app_online: false }), s({ status: "submitted" })],
      counts: { writing: 2, submitted: 1, offline: 0, needs_look: 0 },
    } as unknown as LiveRoom;
    expect(countsOf(live)).toEqual({ writing: 1, submitted: 1, offline: 0, needs_look: 1 });
  });
});
