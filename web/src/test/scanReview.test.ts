import { describe, expect, it } from "vitest";
import type { QueueItem } from "../api/scan";
import { isFlagged, itemsFor, keyAction, digitKey } from "../pages/teacher/scan/queueLogic";
import {
  buildSave,
  flipStep,
  initialForm,
  setCriterion,
  setPoints,
  timeSummary,
  toggleCriterion,
} from "../pages/teacher/scan/reviewLogic";

const CRITS = [
  { id: "c1", label: "Setup", points: 1, description: "" },
  { id: "c2", label: "Answer", points: 2, description: "" },
];

const qi = (over: Partial<QueueItem> = {}): QueueItem => ({
  attempt_id: "a1", username: "alice", name: "Alice", question_id: "q1", image: "/api/uploads/x", page: 1, mcq: null,
  marks: null, blank: false, status: "needs_review", auto_points: null, low_confidence: false, group: "none", grade: null,
  draft: {
    points: 2, comment: "c", explanation: "Explained.", confidence: 0.9, transcript: "t", mistake_tags: [], source: "ai",
    low_confidence: false, created_at: "x", needs_review: false,
    criteria: [
      { id: "c1", label: "Setup", points: 1, max_points: 1, met: true, note: "" },
      { id: "c2", label: "Answer", points: 1, max_points: 2, met: false, note: "" },
    ],
    steps: [{ n: 1, text: "a", status: "correct", note: "" }, { n: 2, text: "b", status: "error", note: "" }],
  },
  ...over,
});

describe("review logic", () => {
  it("accepts untouched suggestions as is and sends edits otherwise", () => {
    const it0 = qi();
    const f = initialForm(it0, CRITS);
    expect(f.points).toBe("2");
    expect(f.crit).toEqual({ c1: 1, c2: 1 });
    expect(f.comment).toBe("Explained.");
    expect(buildSave(it0, f, CRITS, 3, 4.26)).toEqual({ body: { grading_seconds: 4.3 } });
    const f2 = toggleCriterion(f, CRITS[1], true);
    expect(f2.points).toBe("3");
    const f3 = setCriterion(f2, CRITS[0], 7); // clamped to the criterion max
    expect(f3.crit.c1).toBe(1);
    expect(buildSave(it0, flipStep(f3, 2), CRITS, 3, 1)).toEqual({
      body: {
        points: 3, comment: "Explained.", grading_seconds: 1,
        criteria: [{ id: "c1", points: 1 }, { id: "c2", points: 2 }],
        steps: [{ n: 1, status: "correct" }, { n: 2, status: "missing" }],
      },
    });
    // a typed total replaces the breakdown
    expect(buildSave(it0, setPoints(f2, "1.5"), CRITS, 3, 1)).toEqual({
      body: { points: 1.5, comment: "Explained.", grading_seconds: 1 },
    });
    expect(buildSave(it0, setPoints(f, "x"), CRITS, 3, 1)).toEqual({ error: "Points must be a number." });
    // an approved grade is never re-saved untouched
    const done = qi({ status: "approved", grade: { points: 1, comment: "ok", rubric_scores: null, graded_at: "g" } });
    expect(buildSave(done, initialForm(done, CRITS), CRITS, 3, 1)).toEqual({ skip: true });
    expect(initialForm(done, CRITS).points).toBe("1");
  });

  it("sums time spent against a by-hand estimate", () => {
    const g = (s?: number) => ({ points: 1, comment: "", rubric_scores: null, graded_at: "g", grading_seconds: s });
    const items = [
      qi({ status: "approved", grade: g(20) }),
      qi({ status: "approved", grade: g(10) }),
      qi({ status: "approved", grade: g() }),
      qi({}),
    ];
    expect(timeSummary(items, () => "essay")).toEqual({ graded: 2, seconds: 30, estimate: 240, saved: 210 });
  });

  it("filters flagged and low-confidence answers; keys", () => {
    const flagged = qi({ attempt_id: "a2", draft: { ...qi().draft!, needs_review: true } });
    const low = qi({ attempt_id: "a3", low_confidence: true, draft: { ...qi().draft!, needs_review: true, low_confidence: true } });
    const fine = qi();
    const done = qi({ attempt_id: "a4", status: "approved" });
    const queue = {
      exam_id: "e", title: "", course: "", status: "closed", grades_released: false, ai_enabled: true, low_confidence_below: 0.7,
      questions: [], groups: [], items: [flagged, low, fine, done],
    };
    expect(itemsFor(queue, "q1", "flagged").map((i) => i.attempt_id)).toEqual(["a2", "a3"]);
    expect(itemsFor(queue, "q1", "low").map((i) => i.attempt_id)).toEqual(["a3"]);
    expect(itemsFor(queue, "q1", "ungraded").map((i) => i.attempt_id)).toEqual(["a3", "a2", "a1"]);
    expect(itemsFor(queue, "q1", "all")).toHaveLength(4);
    expect(isFlagged(fine)).toBe(false);
    expect(isFlagged(qi({ draft: null }))).toBe(true);
    expect(keyAction({ key: "Enter" })).toBe("approve");
    expect(keyAction({ key: "ArrowRight" })).toBe("next");
    expect(keyAction({ key: "ArrowLeft" })).toBe("prev");
    expect(keyAction({ key: "Enter", target: document.createElement("button") })).toBeNull();
    expect(digitKey({ key: "3" })).toBe(3);
    expect(digitKey({ key: "3", target: document.createElement("input") })).toBeNull();
    expect(digitKey({ key: "x" })).toBeNull();
  });
});
