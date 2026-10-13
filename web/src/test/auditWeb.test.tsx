// Audit 2026-10-12 web items: W-05 auto-submit, W-06 secret code, W-07 move device,
// W-12 event forwarding, W-01 re-entry.
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LiveStudent } from "../api/types";

const api = vi.hoisted(() => ({ releaseDevice: vi.fn(), events: vi.fn(), listExams: vi.fn() }));
vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return {
    ...real,
    teacherApi: { ...real.teacherApi, releaseDevice: api.releaseDevice },
    studentApi: { ...real.studentApi, events: api.events, listExams: api.listExams },
  };
});
const native = vi.hoisted(() => ({ listeners: [] as ((m: unknown) => void)[] }));
vi.mock("../lib/bridge", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/bridge")>();
  return {
    ...real,
    onNative: (l: (m: unknown) => void) => {
      native.listeners.push(l);
      return () => undefined;
    },
  };
});

import { ApiError } from "../api/client";
import { ReenterExam, isSessionConflict } from "../components/student/ReenterExam";
import { useForwardNativeEvents } from "../lib/useForwardNativeEvents";
import { MoveDevice, SECRET_SHOW_MS, SecretCode } from "../pages/teacher/LiveRoom";

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
  native.listeners = [];
});

describe("W-06 invigilator exit code", () => {
  it("is hidden until asked and hides again by itself", () => {
    vi.useFakeTimers();
    render(<SecretCode code="381204" />);
    expect(screen.queryByText("381204")).toBeNull();
    act(() => screen.getByRole("button", { name: "show" }).click());
    expect(screen.getByText("381204")).toBeTruthy();
    act(() => vi.advanceTimersByTime(SECRET_SHOW_MS + 10));
    expect(screen.queryByText("381204")).toBeNull();
  });
});

describe("W-07 move to another computer from the seat", () => {
  it("asks first, then releases the device", async () => {
    api.releaseDevice.mockResolvedValue({ ok: true });
    const onMoved = vi.fn();
    const s = { attempt_id: "a1", name: "Alice Chen", status: "in_progress" } as LiveStudent;
    render(<MoveDevice examId="e1" student={s} onMoved={onMoved} />);
    await userEvent.click(screen.getByRole("button", { name: "Move Alice Chen to another computer" }));
    expect(api.releaseDevice).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "yes, move" }));
    expect(api.releaseDevice).toHaveBeenCalledWith("e1", "a1");
    expect(onMoved).toHaveBeenCalled();
    expect(await screen.findByRole("status")).toHaveTextContent(/start code on the other computer/);
  });
});

function Forward({ aid }: { aid: string }) {
  useForwardNativeEvents(aid);
  return null;
}

describe("W-12 forwarding app events", () => {
  it("drops a batch the server refuses (4xx) instead of retrying forever", async () => {
    vi.useFakeTimers();
    api.events.mockRejectedValue(new ApiError(409, "This exam was opened somewhere else."));
    render(<Forward aid="a1" />);
    act(() => native.listeners.forEach((l) => l({ type: "event", event: { type: "focus_lost" } })));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(api.events).toHaveBeenCalledTimes(1);
  });

  it("retries after a network or 5xx failure", async () => {
    vi.useFakeTimers();
    api.events.mockRejectedValueOnce(new ApiError(503, "busy")).mockResolvedValue({ ok: true, stored: 1 });
    render(<Forward aid="a1" />);
    act(() => native.listeners.forEach((l) => l({ type: "event", event: { type: "focus_lost" } })));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(api.events).toHaveBeenCalledTimes(2);
  });
});

describe("W-01 back into a moved exam", () => {
  it("recognises the session conflict and offers the start code for the right exam", async () => {
    expect(isSessionConflict(409, "This exam was opened somewhere else. Ask the invigilator for help.")).toBe(true);
    expect(isSessionConflict(409, "Attempt already submitted")).toBe(false);
    api.listExams.mockResolvedValue([{ id: "e7", attempt: { attempt_id: "a1", status: "in_progress" } }]);
    render(
      <MemoryRouter>
        <ReenterExam attemptId="a1" detail="This exam was opened somewhere else." />
      </MemoryRouter>,
    );
    await waitFor(() => expect(api.listExams).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: "Start exam" })).toBeTruthy();
    expect(screen.getByText(/opened somewhere else/)).toBeTruthy();
  });
});
