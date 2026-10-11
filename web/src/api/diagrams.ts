// Graph / chart / diagram questions (paper exams): kinds + default checklists, per-exam
// checklist overrides in the answer key, and the shapes the AI draft / grade carry.
// Own module so the shared api files stay small. Contract: CONTRACT.md "Graph and diagram questions".
import { request } from "./client";

const enc = encodeURIComponent;

export type DiagramKind =
  | "function_graph"
  | "data_chart"
  | "geometry"
  | "physics_fbd"
  | "physics_circuit"
  | "physics_ray"
  | "chemistry_structure"
  | "labelled_diagram"
  | "flowchart";

/** One checklist feature of the answer key. Points add up to the question's points. */
export interface DiagramFeature {
  id: string;
  key: string;
  label: string;
  points: number;
  expected: string;
  /** points the drawing must pass through / plot, in axis units */
  points_xy?: [number, number][];
  tolerance?: number;
}

/** Stored on the question (`question.diagram`) and, as an override, in the answer key. */
export interface DiagramSpec {
  kind: DiagramKind;
  features: DiagramFeature[];
  reference_image: string | null;
  notes: string;
}

export interface DiagramKindInfo {
  kind: DiagramKind;
  label: string;
  group: string;
  hint: string;
  /** defaults for a 10-point question (the editor rescales to the question's points) */
  features: DiagramFeature[];
}

export type FeatureStatus = "present" | "wrong" | "missing" | "partial" | "not_assessed";

/** Normalised 0..1 image coordinates from the top-left of the student's answer picture. */
export type Box = [number, number, number, number];
export type Point = [number, number];

export interface DraftFeature {
  id: string;
  key?: string;
  label: string;
  points: number;
  max_points: number;
  status: FeatureStatus;
  note: string;
  box: Box | null;
  point: Point | null;
}

export interface DiagramCheck {
  ran: boolean;
  axes_found: boolean | null;
  dots_found: number | null;
  ticks_even: boolean | null;
  expected_points: number;
  points_matched: number | null;
  readings_on_dots: number | null;
  calibrated: boolean;
  agrees: boolean | null;
  issues: string[];
}

/** `draft.diagram` (AI suggestion) */
export interface DraftDiagram {
  kind: DiagramKind;
  kind_label: string;
  features: DraftFeature[];
  readings: { label: string; x: number; y: number; at: Point | null }[];
  check?: DiagramCheck;
  reference_image: string | null;
  prompt_version?: string;
}

/** `grade.diagram` and the student feedback's `question.diagram` (approved) */
export interface GradeDiagram {
  kind: DiagramKind;
  kind_label: string;
  features: DraftFeature[];
  reference_image: string | null;
}

export interface ExamDiagramQuestion {
  question_id: string;
  index: number;
  type: string;
  prompt: string;
  points: number;
  diagram: DiagramSpec | null;
  /** "key": set in the answer key (overrides the question), "question": from the exam editor */
  source: "key" | "question" | null;
  question_diagram: DiagramSpec | null;
}

let kindsCache: Promise<DiagramKindInfo[]> | null = null;

export const diagramsApi = {
  /** Kinds and their default checklists (cached for the page's life). */
  kinds: (): Promise<DiagramKindInfo[]> => {
    if (!kindsCache) {
      kindsCache = request<{ kinds: DiagramKindInfo[] }>("teacher", "GET", "/api/diagram-kinds").then(
        (r) => r.kinds,
        (e) => {
          kindsCache = null;
          throw e;
        },
      );
    }
    return kindsCache;
  },
  list: (examId: string) =>
    request<{ exam_id: string; kinds: DiagramKindInfo[]; questions: ExamDiagramQuestion[] }>(
      "teacher", "GET", `/api/exams/${enc(examId)}/diagrams`),
  /** null drops the answer-key override (the question's own checklist is used again). */
  save: (examId: string, qid: string, diagram: DiagramSpec | null) =>
    request<ExamDiagramQuestion>("teacher", "PUT", `/api/exams/${enc(examId)}/diagrams/${enc(qid)}`, { diagram }),
};

/** For tests: forget the cached kinds. */
export function resetDiagramKindsCache(): void {
  kindsCache = null;
}
