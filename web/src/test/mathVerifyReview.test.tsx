import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { QueueItem, ScanQueue } from "../api/scan";

const scan = vi.hoisted(() => ({ queue: vi.fn(), approve: vi.fn(), regrade: vi.fn(), approveBulk: vi.fn() }));
vi.mock("../api/scan", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/scan")>();
  return { ...orig, scanApi: scan };
});

import { QueueStep } from "../pages/teacher/scan/QueueStep";

const item: QueueItem = {
  attempt_id: "a1", username: "alice", name: "Alice Chen", question_id: "q1", image: "/api/uploads/abc", page: 1,
  mcq: null, marks: null, blank: false, grade: null, status: "needs_review", auto_points: null, low_confidence: true,
  group: "none",
  draft: {
    points: 4, comment: "", explanation: "Check step 2.", confidence: 0.5, transcript: "...", mistake_tags: [],
    source: "ai", low_confidence: true, created_at: "x", needs_review: true, review_reasons: ["verifier_disagrees"],
    review_reason: "The math check disagrees with the AI",
    steps: [
      { n: 1, text: "2x + 3 = 7", latex: "2x + 3 = 7", status: "correct", note: "", verify: "unverifiable",
        verify_reason: "first line (nothing to compare with)", ecf: false },
      { n: 2, text: "2x = 4", latex: "2x = 4", status: "correct", note: "", verify: "verified_ok",
        verify_reason: "follows from step 1", ecf: false, model_status: "error",
        verifier_disagrees: "SymPy proves this step follows from the previous one; the AI marked it wrong" },
      { n: 3, text: "x = 2", latex: "x = \\frac{4}{2", status: "correct", note: "", verify: "verified_ok",
        verify_reason: "follows from step 2", ecf: false },
    ],
    final_answer: { latex: "x = 2", units: "", text: "" },
    verification: {
      engine: "sympy", version: "fp-verify-v1",
      steps: [
        { n: 1, status: "unverifiable", reason: "first line", ecf: false, latex: "2x + 3 = 7" },
        { n: 2, status: "verified_ok", reason: "follows from step 1", ecf: false, latex: "2x = 4" },
        { n: 3, status: "verified_ok", reason: "follows from step 2", ecf: false, latex: "x = 2" },
      ],
      final: { status: "verified_ok", reason: "matches the key", student: "x = 2", key: "x = 2", ecf: false },
      units: null, sig_figs: null,
      disagreements: [{ n: 2, model: "error", verifier: "verified_ok", reason: "SymPy proves step 2", changes_points: true }],
    },
  },
};

const queue: ScanQueue = {
  exam_id: "e1", title: "Algebra quiz", course: "MATH 101", status: "closed", grades_released: false, ai_enabled: true,
  low_confidence_below: 0.7,
  questions: [{
    question_id: "q1", index: 0, type: "essay", prompt: "Solve 2x + 3 = 7.", points: 4, images: [], options: [],
    correct_answer: null, total: 1, to_review: 1, approved: 0, auto: 0, low_confidence: 1,
  }],
  items: [item],
  groups: [],
};

describe("review screen with the math check", () => {
  it("shows LaTeX, a verification badge next to each AI mark and the disagreement", async () => {
    scan.queue.mockResolvedValue(queue);
    render(<QueueStep examId="e1" />);
    await screen.findAllByText("Alice Chen");
    const pane = document.querySelector(".rv-pane") as HTMLElement;
    expect(within(pane).getByText("The math check disagrees with the AI")).toBeInTheDocument();
    // AI mark button still there, with the badge next to it
    expect(within(pane).getByRole("button", { name: "Step 2: correct. Change" })).toBeInTheDocument();
    const steps = pane.querySelectorAll(".rv-step");
    expect(within(steps[0] as HTMLElement).getByText("Can't check")).toBeInTheDocument();
    expect(within(steps[1] as HTMLElement).getByText("Proved")).toBeInTheDocument();
    expect(within(steps[1] as HTMLElement).getByRole("note")).toHaveTextContent("the AI marked it wrong");
    expect(steps[0].querySelector(".katex")).not.toBeNull();
    // unreadable LaTeX falls back to the source in monospace
    expect(steps[2].querySelector("code.mv-src")?.textContent).toBe("x = \\frac{4}{2");
    const panel = within(pane).getByLabelText("Math check");
    expect(panel).toHaveTextContent("Math check: 2 proved · 1 can't check");
    expect(panel).toHaveTextContent("Final answer matches the key");
  });
});
