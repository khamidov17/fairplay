import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AnswerSet, AnswerSets, Region } from "../api/anyPaper";

const api = vi.hoisted(() => ({ sets: vi.fn(), update: vi.fn(), create: vi.fn(), resegment: vi.fn() }));
vi.mock("../api/anyPaper", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/anyPaper")>();
  return { ...orig, anyPaperApi: api };
});

import { RegionCheck } from "../pages/teacher/scan/RegionCheck";
import {
  addRegion,
  boxFromPoints,
  clampBox,
  keyAction,
  markNotAnswered,
  moveBox,
  nudge,
  pagesAfterMove,
  questionState,
  reassign,
  regionOrder,
  regionsDirty,
  resizeBox,
  stepSet,
  toRegionIn,
  visibleSets,
} from "../pages/teacher/scan/regionLogic";

const R = (id: string, q: string | null, page: number, box: [number, number, number, number], extra: Partial<Region> = {}): Region => ({
  id,
  question_id: q,
  page_id: page,
  box,
  confidence: 0.9,
  crossed_out: false,
  label: "",
  source: "ai",
  needs_check: false,
  ...extra,
});

describe("region geometry", () => {
  it("clamps, moves and resizes boxes inside the page", () => {
    expect(clampBox([0.5, 0.6, -0.2, 0.1])).toEqual([0, 0.1, 0.5, 0.6]);
    expect(clampBox([0.995, 0.2, 1, 0.3])).toEqual([0.98, 0.2, 1, 0.3]); // min width at the edge
    expect(moveBox([0.1, 0.1, 0.3, 0.2], 0.9, -0.5)).toEqual([0.8, 0, 1, 0.1]); // size kept
    expect(resizeBox([0.2, 0.2, 0.4, 0.4], "se", 0.1, 0.05)).toEqual([0.2, 0.2, 0.5, 0.45]);
    expect(resizeBox([0.2, 0.2, 0.4, 0.4], "nw", 0.5, 0.5)).toEqual([0.38, 0.38, 0.4, 0.4]); // never flips
    expect(resizeBox([0.2, 0.2, 0.4, 0.4], "ne", 0.1, -0.1)).toEqual([0.2, 0.1, 0.5, 0.4]);
    expect(boxFromPoints(0.6, 0.7, 0.2, 0.3)).toEqual([0.2, 0.3, 0.6, 0.7]);
    expect(nudge([0.2, 0.2, 0.4, 0.4], "ArrowRight", false, 0.01)).toEqual([0.21, 0.2, 0.41, 0.4]);
    expect(nudge([0.2, 0.2, 0.4, 0.4], "ArrowDown", true, 0.01)).toEqual([0.2, 0.2, 0.4, 0.41]);
    expect(nudge([0.2, 0.2, 0.4, 0.4], "x", false)).toBeNull();
  });

  it("edits regions and tracks question states", () => {
    let rs = [R("a", "q1", 1, [0.1, 0.1, 0.5, 0.2]), R("b", "q2", 1, [0.1, 0.3, 0.5, 0.4], { confidence: 0.5, needs_check: true }),
      R("c", "q2", 2, [0.1, 0.1, 0.5, 0.2], { crossed_out: true })];
    expect(questionState("q1", rs)).toEqual({ kind: "found", confidence: 0.9, parts: 1 });
    expect(questionState("q2", rs)).toEqual({ kind: "check", confidence: 0.5, parts: 1 });
    expect(questionState("q3", rs).kind).toBe("missing");
    rs = reassign(rs, "b", "q3");
    expect(rs.find((r) => r.id === "b")).toMatchObject({ question_id: "q3", source: "teacher", confidence: 1, needs_check: false });
    expect(questionState("q3", rs).kind).toBe("found");
    rs = markNotAnswered(rs, "q2");
    expect(rs.map((r) => r.id)).toEqual(["a", "b", "c"]); // crossed-out box stays
    rs = markNotAnswered(rs, "q1");
    expect(rs.map((r) => r.id)).toEqual(["b", "c"]);
    rs = addRegion(rs, "q1", 2, [0.2, 0.5, 0.6, 0.7]);
    expect(rs).toHaveLength(3);
    expect(rs[2]).toMatchObject({ question_id: "q1", page_id: 2, source: "teacher" });
    expect(regionOrder(rs, [1, 2]).map((r) => r.question_id)).toEqual(["q3", "q2", "q1"]);
    expect(toRegionIn(rs)[0]).toEqual({ id: "b", question_id: "q3", page_id: 1, box: [0.1, 0.3, 0.5, 0.4], crossed_out: false, label: "" });
  });

  it("knows when edits differ from what is saved", () => {
    const saved = [R("a", "q1", 1, [0.1, 0.1, 0.5, 0.2])];
    expect(regionsDirty(saved, [...saved])).toBe(false);
    expect(regionsDirty(saved, [{ ...saved[0], confidence: 1, source: "teacher" }])).toBe(false); // same box
    expect(regionsDirty(saved, [{ ...saved[0], box: [0.1, 0.1, 0.5, 0.25] }])).toBe(true);
    expect(regionsDirty(saved, [])).toBe(true);
  });

  it("maps keys and steps through students", () => {
    expect(keyAction({ key: "3" })).toEqual({ type: "assign", n: 3 });
    expect(keyAction({ key: "Tab", shiftKey: true })).toEqual({ type: "prevBox" });
    expect(keyAction({ key: "ArrowLeft", shiftKey: true })).toEqual({ type: "nudge", key: "ArrowLeft", shift: true });
    expect(keyAction({ key: "Enter" })).toEqual({ type: "confirm" });
    expect(keyAction({ key: "s", ctrlKey: true })).toBeNull();
    expect(keyAction({ key: "q" })).toBeNull();
    const sets = [SET_A, SET_B];
    expect(visibleSets(sets, "check").map((s) => s.set_id)).toEqual(["sa"]);
    expect(visibleSets(sets, "all")).toHaveLength(2);
    expect(stepSet(sets, "sa", 1)).toBe("sb");
    expect(stepSet(sets, "sb", 1)).toBe("sa");
    expect(stepSet(sets, "sa", -1)).toBe("sb");
    expect(stepSet([], "sa", 1)).toBeNull();
    expect(pagesAfterMove(sets, 11, "sb")).toEqual([20, 11]);
    expect(pagesAfterMove(sets, 20, "sb")).toBeNull();
  });
});

// ---------------------------------------------------------------- component
const SET_A: AnswerSet = {
  set_id: "sa", batch_id: "b1", student_id: 1, student: { id: 1, username: "alice", name: "Alice Chen" },
  name_read: "Alice Chen", id_read: "",
  match: { confidence: 0.95, candidates: [], reasons: [], teacher: false },
  status: "needs_check", needs_check: true, reasons: ["low_confidence"],
  pages: [
    { page_id: 10, seq: 0, preview: "/p10", image: "/i10", candidates: [] },
    { page_id: 11, seq: 1, preview: "/p11", image: "/i11", candidates: [] },
  ],
  regions: [
    R("r1", "q1", 10, [0.1, 0.2, 0.6, 0.3]),
    R("r2", "q2", 11, [0.1, 0.5, 0.6, 0.6], { confidence: 0.55, needs_check: true }),
  ],
  missing: ["q3"], segment: {}, updated_at: null,
};
const SET_B: AnswerSet = {
  ...SET_A, set_id: "sb", student_id: null, student: null, name_read: "Bob Karimow",
  match: { confidence: 0.5, candidates: [{ student_id: 2, name: "Bob Karimov", username: "bob", score: 0.7 }], reasons: [], teacher: false },
  status: "ok", needs_check: false, reasons: [], pages: [{ page_id: 20, seq: 2, preview: "/p20", image: "/i20", candidates: [] }],
  regions: [], missing: ["q1", "q2", "q3"],
};
const DATA: AnswerSets = {
  exam_id: "e1", ai_enabled: true, low_confidence_below: 0.7,
  questions: [
    { question_id: "q1", index: 0, prompt: "2+2?", type: "numeric", points: 2 },
    { question_id: "q2", index: 1, prompt: "Capital?", type: "short", points: 2 },
    { question_id: "q3", index: 2, prompt: "Explain", type: "essay", points: 4 },
  ],
  students: [{ id: 1, username: "alice", name: "Alice Chen" }, { id: 2, username: "bob", name: "Bob Karimov" }],
  sets: [SET_A, SET_B], needs_check: 1, loose_pages: [],
};

/** jsdom has no PointerEvent: send mouse events under pointer event names (React listens by name). */
function ptr(target: EventTarget, type: "pointerdown" | "pointermove" | "pointerup", x = 0, y = 0) {
  fireEvent(target as Element, new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true }));
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset();
  api.sets.mockResolvedValue(DATA);
  api.update.mockResolvedValue({ ...DATA, queued: 1 });
  api.create.mockResolvedValue({ ...DATA, queued: 0 });
  api.resegment.mockResolvedValue(DATA);
});

describe("Check answer regions", () => {
  it("shows the students needing a check, boxes by question, and missing answers", async () => {
    render(<RegionCheck examId="e1" />);
    await screen.findByRole("heading", { name: "Check answer regions" });
    expect(screen.getByRole("button", { name: /Needs check \(1\)/ })).toHaveAttribute("aria-pressed", "true");
    // only alice in the "needs check" list
    expect(screen.getByRole("button", { name: /Alice Chen/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Bob Karimow/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Box Q1 · 90%, page 1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Box Q2 · 55%, page 2/ })).toBeInTheDocument();
    expect(screen.getByText("Check 55%")).toBeInTheDocument();
    expect(screen.getByText("Not found")).toBeInTheDocument();
    expect(screen.getByText("Some boxes are unsure")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /All \(2\)/ }));
    expect(screen.getByRole("button", { name: /Bob Karimow/ })).toBeInTheDocument();
  });

  it("keyboard: select a box, reassign with a number, nudge, confirm with Enter", async () => {
    render(<RegionCheck examId="e1" />);
    const box = await screen.findByRole("button", { name: /Box Q2 · 55%/ });
    ptr(box, "pointerdown", 10, 10);
    ptr(window, "pointerup");
    expect(box).toHaveAttribute("aria-pressed", "true");
    fireEvent.keyDown(document.body, { key: "3" });
    expect(await screen.findByRole("button", { name: /Box Q3, page 2/ })).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "ArrowDown" });
    fireEvent.keyDown(document.body, { key: "Enter" });
    await waitFor(() => expect(api.update).toHaveBeenCalled());
    const [examId, setId, body] = api.update.mock.calls[0];
    expect([examId, setId, body.confirm]).toEqual(["e1", "sa", true]);
    const moved = body.regions.find((r: Region) => r.id === "r2");
    expect(moved.question_id).toBe("q3");
    expect(moved.box[1]).toBeCloseTo(0.505);
    expect(await screen.findByText(/Confirmed Alice Chen\. AI is grading 1 answer again\./)).toBeInTheDocument();
  });

  it("marks a question not answered, draws a box, deletes a box", async () => {
    render(<RegionCheck examId="e1" />);
    await screen.findByRole("button", { name: /Box Q1/ });
    await userEvent.click(screen.getByRole("button", { name: "Question 1 not answered" }));
    expect(screen.queryByRole("button", { name: /Box Q1/ })).toBeNull();
    expect(screen.getAllByText("Not found")).toHaveLength(2);
    // draw for question 3 on page 1
    await userEvent.click(screen.getByRole("button", { name: "Draw a box for question 3" }));
    const canvas = document.querySelector('[data-page-id="10"]') as HTMLElement;
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 200, right: 100, bottom: 200, x: 0, y: 0, toJSON: () => ({}) });
    ptr(canvas, "pointerdown", 10, 20);
    ptr(window, "pointermove", 60, 80);
    ptr(window, "pointerup");
    const drawn = await screen.findByRole("button", { name: /Box Q3, page 1/ });
    expect(drawn).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "Delete box" }));
    expect(screen.queryByRole("button", { name: /Box Q3, page 1/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save boxes" }));
    await waitFor(() => expect(api.update).toHaveBeenCalled());
    const body = api.update.mock.calls[0][2];
    expect(body.regions.map((r: Region) => r.id)).toEqual(["r2"]);
    expect(body.confirm).toBeUndefined();
  });

  it("drags a box corner to resize it", async () => {
    render(<RegionCheck examId="e1" />);
    const box = await screen.findByRole("button", { name: /Box Q1 · 90%/ });
    const canvas = document.querySelector('[data-page-id="10"]') as HTMLElement;
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    ptr(box, "pointerdown", 20, 25);
    ptr(window, "pointerup");
    const handle = (await screen.findByRole("button", { name: /Box Q1/ })).querySelector('[data-handle="se"]') as HTMLElement;
    ptr(handle, "pointerdown", 60, 30);
    ptr(window, "pointermove", 70, 40);
    ptr(window, "pointerup");
    await userEvent.click(screen.getByRole("button", { name: "Save boxes" }));
    await waitFor(() => expect(api.update).toHaveBeenCalled());
    const r1 = api.update.mock.calls[0][2].regions.find((r: Region) => r.id === "r1");
    expect(r1.box).toEqual([0.1, 0.2, 0.7, 0.4]);
  });

  it("picks the student, moves a page to another student or a new one", async () => {
    render(<RegionCheck examId="e1" />);
    await screen.findByRole("heading", { name: "Check answer regions" });
    await userEvent.selectOptions(screen.getByLabelText("Student"), "2");
    await waitFor(() => expect(api.update).toHaveBeenCalledWith("e1", "sa", { student_id: 2 }));
    await screen.findByText("Student saved. AI is grading 1 answer again.");
    await userEvent.selectOptions(screen.getByLabelText("Move page 2 to"), "sb");
    await waitFor(() => expect(api.update).toHaveBeenCalledWith("e1", "sb", { page_ids: [20, 11] }));
    await screen.findByText(/^Page moved\./);
    await userEvent.selectOptions(screen.getByLabelText("Move page 1 to"), "new");
    await waitFor(() => expect(api.create).toHaveBeenCalledWith("e1", [10]));
    await screen.findByText("Page moved to a new student.");
    await userEvent.click(screen.getByRole("button", { name: "Find again with AI" }));
    await waitFor(() => expect(api.resegment).toHaveBeenCalledWith("e1", "sa"));
  });

  it("suggests roster matches for an unmatched student", async () => {
    render(<RegionCheck examId="e1" />);
    await userEvent.click(await screen.findByRole("button", { name: /All \(2\)/ }));
    await userEvent.click(screen.getByRole("button", { name: /Bob Karimow/ }));
    expect(screen.getByText(/Wrote “Bob Karimow”/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Bob Karimov · 70%" }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith("e1", "sb", { student_id: 2 }));
  });
});
