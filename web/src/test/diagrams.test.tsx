import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DiagramKindInfo, DiagramSpec, DraftDiagram, DraftFeature, GradeDiagram } from "../api/diagrams";
import type { AiDraft, QueueItem, ScanQueue } from "../api/scan";
import type { Criterion } from "../api/scanAi";
import type { FeedbackQuestion } from "../api/types";

const scan = vi.hoisted(() => ({ queue: vi.fn(), approve: vi.fn(), regrade: vi.fn(), approveBulk: vi.fn() }));
const dg = vi.hoisted(() => ({ kinds: vi.fn(), list: vi.fn(), save: vi.fn() }));
const s6 = vi.hoisted(() => ({ uploadImage: vi.fn() }));

vi.mock("../api/scan", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/scan")>();
  return { ...orig, scanApi: { ...orig.scanApi, ...scan } };
});
vi.mock("../api/diagrams", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/diagrams")>();
  return { ...orig, diagramsApi: dg };
});
vi.mock("../api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/client")>();
  return { ...orig, slice6Api: { ...orig.slice6Api, ...s6 } };
});

import { DiagramChecklist } from "../components/diagrams/DiagramChecklist";
import { DiagramEditor } from "../components/diagrams/DiagramEditor";
import { DiagramOverlay } from "../components/diagrams/DiagramOverlay";
import {
  checklistError,
  checklistTotal,
  defaultSpec,
  markStyle,
  overlayMarks,
  parsePointList,
  rescaleFeatures,
  splitPoints,
  statusFor,
} from "../components/diagrams/diagramLogic";
import { FeedbackQuestionCard } from "../components/student/FeedbackQuestion";
import { initialForm, type ReviewForm } from "../pages/teacher/scan/reviewLogic";
import { QueueStep } from "../pages/teacher/scan/QueueStep";
import { toQuestionInput, validateDraft, draftFromExam, newQuestion } from "../pages/teacher/examDraft";

const KINDS: DiagramKindInfo[] = [
  {
    kind: "function_graph", label: "Function graph (sketch)", group: "Graphs and charts", hint: "Sketch of y = f(x).",
    features: [
      { id: "f1", key: "axes", label: "Axes drawn and labelled (x, y)", points: 1, expected: "" },
      { id: "f2", key: "intercepts", label: "Intercepts correct", points: 2.5, expected: "" },
      { id: "f3", key: "shape", label: "Shape and end behaviour", points: 2.5, expected: "" },
      { id: "f4", key: "key_points", label: "Passes through the key points", points: 4, expected: "", points_xy: [], tolerance: 0.5 },
    ],
  },
  {
    kind: "physics_fbd", label: "Free-body diagram", group: "Physics diagrams", hint: "Forces on one object.",
    features: [
      { id: "f1", key: "gravity", label: "Weight straight down", points: 5, expected: "" },
      { id: "f2", key: "normal", label: "Normal force perpendicular", points: 5, expected: "" },
    ],
  },
];

const FEATURES: DraftFeature[] = [
  { id: "f1", label: "Axes labelled", points: 1, max_points: 1, status: "present", note: "", box: [0.1, 0.2, 0.9, 0.8], point: null },
  { id: "f2", label: "Units", points: 0, max_points: 1, status: "missing", note: "No units", box: null, point: [0.5, 0.95] },
  { id: "f3", label: "Points plotted", points: 2, max_points: 3, status: "wrong", note: "(3, 5) is off", box: [0.25, 0.3, 0.6, 0.7], point: null },
  { id: "f4", label: "Title", points: 0, max_points: 1, status: "not_assessed", note: "", box: null, point: null },
];
const CRITERIA: Criterion[] = [
  { id: "f1", label: "Axes labelled", points: 1, description: "" },
  { id: "f2", label: "Units", points: 1, description: "SI units" },
  { id: "f3", label: "Points plotted", points: 3, description: "" },
  { id: "f4", label: "Title", points: 1, description: "" },
];
const DRAFT_DIAGRAM: DraftDiagram = {
  kind: "data_chart", kind_label: "Data chart", features: FEATURES,
  readings: [{ label: "", x: 1, y: 2, at: [0.2, 0.7] }],
  check: { ran: true, axes_found: true, dots_found: 4, ticks_even: true, expected_points: 5, points_matched: 3,
    readings_on_dots: 1, calibrated: true, agrees: false, issues: ["“Points plotted”: 3 of 5 expected points found"] },
  reference_image: "/api/uploads/ref",
};

beforeEach(() => {
  vi.clearAllMocks();
  dg.kinds.mockResolvedValue(KINDS);
});

describe("diagram logic", () => {
  it("shares points exactly and rescales checklists", () => {
    // same numbers as the server's split_points (app/scan/diagram_spec.py)
    expect(splitPoints(10, [1, 2, 2, 1, 2, 1, 1, 1])).toEqual([1, 2, 1.5, 1, 1.5, 1, 1, 1]);
    expect(splitPoints(6, [1, 2.5, 2.5, 4])).toEqual([0.5, 1.5, 1.5, 2.5]);
    for (const total of [1, 3, 4, 7, 25]) {
      const s = splitPoints(total, [1, 2, 2, 1, 2]);
      expect(s.reduce((a, b) => a + b, 0)).toBeCloseTo(total, 9);
      expect(s.every((v) => v >= 0 && (v * 2) % 1 === 0)).toBe(true);
    }
    const spec = defaultSpec(KINDS[0], 6);
    expect(checklistTotal(spec.features)).toBe(6);
    expect(spec.features[3].points_xy).toEqual([]);
    expect(rescaleFeatures(spec.features, 12).map((f) => f.points)).toEqual([1, 3, 3, 5]);
    expect(checklistError(spec, 6)).toBeNull();
    expect(checklistError(spec, 8)).toBe("Checklist points must add up to 8 (now 6).");
    expect(checklistError({ ...spec, features: [{ ...spec.features[0], label: " " }] }, 1)).toMatch(/label/);
    expect(checklistError({ ...spec, features: [] }, 1)).toMatch(/at least one/);
  });

  it("reads point lists", () => {
    expect(parsePointList("1, 2; 2, 4.5")).toEqual([[1, 2], [2, 4.5]]);
    expect(parsePointList("(1,2)\n(3, -1)")).toEqual([[1, 2], [3, -1]]);
    expect(parsePointList("")).toEqual([]);
    expect(parsePointList("1, two")).toBeNull();
    expect(parsePointList("1 2 3")).toBeNull();
  });

  it("places overlay marks and recomputes their status from the points", () => {
    expect(markStyle({ box: [0.1, 0.2, 0.9, 0.8] })).toEqual({ left: "10%", top: "20%", width: "80%", height: "60%" });
    expect(markStyle({ point: [0.5, 0.95] })).toEqual({ left: "50%", top: "95%" });
    expect(markStyle({ box: null, point: null })).toBeNull();
    const marks = overlayMarks(FEATURES);
    expect(marks.map((m) => [m.id, m.n, m.kind, m.status, m.ok])).toEqual([
      ["f1", 1, "box", "present", true],
      ["f2", 2, "point", "missing", false],
      ["f3", 3, "box", "wrong", false],
    ]); // f4 has no place on the picture
    const toggled = overlayMarks(FEATURES, { f1: 0, f2: 1, f3: 2 });
    expect(toggled.map((m) => m.status)).toEqual(["wrong", "present", "partial"]);
    expect(statusFor(0, 1, "missing")).toBe("missing");
    expect(statusFor(0, 1, "present")).toBe("wrong");
    expect(statusFor(1, 1)).toBe("present");
  });

  it("exam draft keeps the diagram and validates its checklist", () => {
    const q = { ...newQuestion("essay"), prompt: "Sketch y = x^2", points: "6", topics: ["graphs"], diagram: defaultSpec(KINDS[0], 6) };
    const d = { ...draftFromExam(), title: "T", questions: [q] };
    expect(validateDraft(d).byKey[q.key]).toBeUndefined();
    const input = toQuestionInput(q);
    expect(input.diagram?.kind).toBe("function_graph");
    expect(input.rubric).toEqual([]);
    const bad = { ...q, points: "8" };
    expect(validateDraft({ ...d, questions: [bad] }).byKey[q.key]?.diagram).toMatch(/add up to 8/);
    expect(toQuestionInput({ ...q, diagram: null }).diagram).toBeUndefined();
  });
});

function Harness({ draft }: { draft: DraftDiagram }) {
  const it = {
    attempt_id: "a1", username: "alice", name: "Alice", question_id: "q1", image: "/api/uploads/x", page: 1, mcq: null,
    marks: null, blank: false, status: "needs_review", auto_points: null, low_confidence: false, group: "none", grade: null,
    draft: { points: 3, comment: "", confidence: 0.9, transcript: "", mistake_tags: [], source: "ai", low_confidence: false,
      created_at: "x", criteria: FEATURES.map((f) => ({ ...f, met: f.points === f.max_points })), diagram: draft } as AiDraft,
  } as QueueItem;
  const [form, setForm] = useState<ReviewForm>(() => initialForm(it, CRITERIA));
  return (
    <>
      <output aria-label="total">{form.points}</output>
      <DiagramOverlay src="/api/uploads/x" alt="drawing" marks={overlayMarks(draft.features, form.crit)} />
      <DiagramChecklist idPrefix="t" criteria={CRITERIA} diagram={draft} form={form} setForm={setForm} />
    </>
  );
}

describe("review checklist + overlay", () => {
  it("toggling a feature recomputes the points and the mark on the drawing", async () => {
    render(<Harness draft={DRAFT_DIAGRAM} />);
    expect(screen.getByLabelText("total")).toHaveTextContent("3");
    const overlay = screen.getByTestId("diagram-overlay");
    const mark = (id: string) => overlay.querySelector(`[data-feature="${id}"]`) as HTMLElement;
    expect(mark("f1")).toHaveStyle({ left: "10%", top: "20%", width: "80%", height: "60%" });
    expect(mark("f2")).toHaveAttribute("data-status", "missing");
    expect(mark("f2")).toHaveStyle({ left: "50%", top: "95%" });
    expect(mark("f1")).toHaveClass("is-ok");
    expect(screen.getByText("No units")).toBeInTheDocument();
    expect(screen.getByText(/expected: SI units/)).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("3 of 5 expected points found");
    expect(screen.getByText("AI read: (1, 2)")).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("Units"));
    expect(screen.getByLabelText("total")).toHaveTextContent("4");
    expect(mark("f2")).toHaveAttribute("data-status", "present");
    expect(mark("f2")).toHaveClass("is-ok");
    await userEvent.click(screen.getByLabelText("Axes labelled"));
    expect(screen.getByLabelText("total")).toHaveTextContent("3");
    expect(mark("f1")).toHaveAttribute("data-status", "wrong");
    const pts = screen.getByLabelText("Points for Points plotted (out of 3)");
    await userEvent.clear(pts);
    await userEvent.type(pts, "3");
    expect(screen.getByLabelText("total")).toHaveTextContent("4");
    expect(mark("f3")).toHaveAttribute("data-status", "present");
    expect(screen.getByLabelText("Points plotted")).toBeChecked();
  });
});

const pane = () => document.querySelector(".rv-pane") as HTMLElement;

describe("QueueStep with a graph / diagram question", () => {
  it("shows the marked drawing, the checklist, and sends feature points", async () => {
    const spec: DiagramSpec = { kind: "data_chart", features: [], reference_image: "/api/uploads/ref", notes: "" };
    const q: ScanQueue = {
      exam_id: "e1", title: "Lab", course: "", status: "closed", grades_released: false, ai_enabled: true,
      low_confidence_below: 0.7, groups: [],
      questions: [{ question_id: "q1", index: 0, type: "essay", prompt: "Plot the data.", points: 6, images: [], options: [],
        correct_answer: null, total: 1, to_review: 1, approved: 0, auto: 0, low_confidence: 0, criteria: CRITERIA, diagram: spec }],
      items: [{
        attempt_id: "a1", username: "alice", name: "Alice Chen", question_id: "q1", image: "/api/uploads/x", page: 1, mcq: null,
        marks: null, blank: false, status: "needs_review", auto_points: null, low_confidence: false, group: "none", grade: null,
        draft: { points: 3, comment: "Add units.", explanation: "Add units.", confidence: 0.9, transcript: "graph", mistake_tags: [],
          source: "ai", low_confidence: false, created_at: "x", needs_review: false,
          criteria: FEATURES.map((f) => ({ id: f.id, label: f.label, points: f.points, max_points: f.max_points, met: f.status === "present", note: f.note })),
          diagram: DRAFT_DIAGRAM } as AiDraft,
      }],
    } as ScanQueue;
    scan.queue.mockResolvedValue(q);
    scan.approve.mockResolvedValue({ attempt_id: "a1", question_id: "q1", grade: { points: 4, comment: "Add units.", rubric_scores: null, graded_at: "x" },
      score: 4, total_points: 6, percent: 66.7, needs_grading: 0 });
    render(<QueueStep examId="e1" />);
    await screen.findAllByText("Alice Chen");
    const p = pane();
    expect(within(p).getByRole("img", { name: "Answer of Alice Chen" })).toHaveAttribute("src", "/api/uploads/x");
    expect(within(p).getByTestId("diagram-overlay").querySelectorAll(".dg-mark")).toHaveLength(3);
    expect(within(p).getByRole("link", { name: "Reference drawing" })).toHaveAttribute("href", "/api/uploads/ref");
    expect(within(p).getByText(/Checklist · Data chart/)).toBeInTheDocument();
    expect(within(p).queryByText("Rubric")).not.toBeInTheDocument();
    await userEvent.click(within(p).getByLabelText("Units"));
    expect(within(p).getByLabelText("Points")).toHaveValue("4");
    fireEvent.keyDown(document, { key: "Enter" });
    await waitFor(() => expect(scan.approve).toHaveBeenCalled());
    const body = scan.approve.mock.calls[0][3];
    expect(body.points).toBe(4);
    expect(body.criteria).toEqual([{ id: "f1", points: 1 }, { id: "f2", points: 1 }, { id: "f3", points: 2 }, { id: "f4", points: 0 }]);
  });
});

const last = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls[fn.mock.calls.length - 1][0];

function EditorHarness({ points, onSpec }: { points: number; onSpec: (s: DiagramSpec | null) => void }) {
  const [v, setV] = useState<DiagramSpec | null>(null);
  return (
    <DiagramEditor
      idPrefix="q"
      value={v}
      points={points}
      onChange={(s) => {
        setV(s);
        onSpec(s);
      }}
    />
  );
}

describe("DiagramEditor", () => {
  it("picks a subtype with default checklist, edits it, and uploads a reference drawing", async () => {
    const onSpec = vi.fn();
    s6.uploadImage.mockResolvedValue({ id: "r", url: "/api/uploads/refimg", width: 10, height: 10 });
    render(<EditorHarness points={5} onSpec={onSpec} />);
    const sel = await screen.findByLabelText("Answer kind");
    await waitFor(() => expect(within(sel).getByRole("option", { name: "Free-body diagram" })).toBeInTheDocument());
    await userEvent.selectOptions(sel, "function_graph");
    let spec: DiagramSpec = last(onSpec);
    expect(spec.kind).toBe("function_graph");
    expect(checklistTotal(spec.features)).toBe(5);
    expect(screen.getByLabelText("Feature 1")).toHaveValue("Axes drawn and labelled (x, y)");
    expect(screen.getByText("5 / 5 pts")).toBeInTheDocument();
    // key points take a list of (x, y)
    await userEvent.type(screen.getByLabelText("Feature 4 points to plot"), "0, -3; 3, 0");
    spec = last(onSpec);
    expect(spec.features[3].points_xy).toEqual([[0, -3], [3, 0]]);
    // removing a feature breaks the total -> error + "Fit to"
    await userEvent.click(screen.getByLabelText("Remove feature 1"));
    expect(screen.getByText(/must add up to 5/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Fit to 5 pts" }));
    spec = last(onSpec);
    expect(checklistTotal(spec.features)).toBe(5);
    expect(screen.queryByText(/must add up to/)).not.toBeInTheDocument();
    const file = new File(["x"], "ref.png", { type: "image/png" });
    await userEvent.upload(screen.getByLabelText("Upload reference drawing"), file);
    await waitFor(() => expect(last(onSpec).reference_image).toBe("/api/uploads/refimg"));
    expect(screen.getByRole("img", { name: "Reference drawing" })).toBeInTheDocument();
    await userEvent.selectOptions(sel, "");
    expect(last(onSpec)).toBeNull();
  });
});

describe("student feedback", () => {
  it("shows the annotated drawing and the checklist results", () => {
    const diagram: GradeDiagram = { kind: "data_chart", kind_label: "Data chart", reference_image: "/api/uploads/ref",
      features: FEATURES.map((f) => (f.id === "f3" ? { ...f, status: "partial" } : f)) };
    const q = {
      question_id: "q1", index: 0, type: "essay", prompt: "Plot the data.", options: [], points: 6, points_awarded: 3,
      status: "partial", given: null, correct_answer: null, tolerance: null, comment: "Add units.", rubric: [], images: [],
      rubric_scores: null, image: "/api/uploads/x",
      criteria: [{ label: "Axes labelled", points: 1, max_points: 1 }], diagram,
    } as unknown as FeedbackQuestion;
    render(<FeedbackQuestionCard q={q} />);
    expect(screen.getByRole("img", { name: /Your drawing for question 1, marked/ })).toHaveAttribute("src", "/api/uploads/x");
    const sec = screen.getByRole("region", { name: "Checklist for question 1" });
    expect(within(sec).getByText("No units")).toBeInTheDocument();
    expect(within(sec).getByText("Partly right")).toBeInTheDocument();
    expect(within(sec).getByText("2 / 3")).toBeInTheDocument();
    expect(sec.querySelectorAll(".dg-mark")).toHaveLength(3);
    expect(screen.getByText("See the correct drawing")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Rubric" })).not.toBeInTheDocument(); // checklist replaces it
  });
});
