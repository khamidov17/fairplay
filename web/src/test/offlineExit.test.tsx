import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { LiveStudent } from "../api/types";

const api = vi.hoisted(() => ({ offlineExit: vi.fn() }));
vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, teacherApi: { ...real.teacherApi, offlineExit: api.offlineExit } };
});

import { OfflineExitBox } from "../pages/teacher/LiveRoom";

const student = (over: Partial<LiveStudent>): LiveStudent =>
  ({ attempt_id: "a1", username: "alice", name: "Alice Chen", status: "in_progress", ...over }) as LiveStudent;

describe("offline exit box", () => {
  it("asks for the student and the Mac code, then shows the exit code", async () => {
    api.offlineExit.mockResolvedValue({ code: "06710479" });
    render(<OfflineExitBox examId="e1" students={[student({}), student({ attempt_id: "a2", name: "Bob", status: "submitted" })]} />);
    await userEvent.click(screen.getByRole("button", { name: "offline exit" }));
    const get = screen.getByRole("button", { name: "get code" });
    expect(screen.queryByRole("option", { name: "Bob" })).toBeNull();      // only students still writing
    await userEvent.type(screen.getByLabelText("Mac code"), "k7qm-3xpa");
    expect(get).toBeDisabled();                                             // no student picked yet
    await userEvent.selectOptions(screen.getByLabelText("Student"), "a1");
    await userEvent.click(get);
    expect(api.offlineExit).toHaveBeenCalledWith("e1", "a1", "K7QM-3XPA");
    expect(await screen.findByText("06710479")).toBeTruthy();
  });
});
