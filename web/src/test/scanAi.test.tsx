import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AnswerKey } from "../api/scanAi";

const api = vi.hoisted(() => ({
  key: vi.fn(),
  saveKey: vi.fn(),
  saveQuestionKey: vi.fn(),
  uploadKeyFile: vi.fn(),
  deleteKeyFile: vi.fn(),
  draftKey: vi.fn(),
  runAi: vi.fn(),
  aiRun: vi.fn(),
  struggles: vi.fn(),
  exportGrades: vi.fn(),
  newCapture: vi.fn(),
  captureInfo: vi.fn(),
  captureUpload: vi.fn(),
  uploadUnprinted: vi.fn(),
  pageMap: vi.fn(),
  saveMapSettings: vi.fn(),
  applyMap: vi.fn(),
}));

vi.mock("../api/scanAi", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../api/scanAi")>();
  return { ...orig, scanAiApi: api };
});

import { AnswerKeyStep, criteriaTotal, keyFormError } from "../pages/teacher/scan/AnswerKeyStep";
import { ExportButtons } from "../pages/teacher/scan/ExportButtons";
import CapturePage from "../pages/teacher/scan/CapturePage";
import { PhoneCapture } from "../pages/teacher/scan/PhoneCapture";
import { MapPages, autoMap } from "../pages/teacher/scan/MapPages";
import { StrugglePanel } from "../pages/teacher/scan/StrugglePanel";
import { FeedbackQuestionCard } from "../components/student/FeedbackQuestion";
import type { FeedbackQuestion } from "../api/types";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ApiError } from "../api/client";

const KEY: AnswerKey = {
  exam_id: "e1",
  subject: "math",
  subjects: ["math", "general"],
  misconceptions: ["sign error", "missing step"],
  text: "",
  files: [],
  ai_enabled: true,
  updated_at: null,
  questions: [
    {
      question_id: "q1", index: 0, type: "essay", prompt: "Integrate x e^x.", points: 4, options: [], correct_answer: null,
      key: { solution: "", criteria: [], partial_credit: "", common_mistakes: [], updated_at: null, updated_by: null,
        effective_criteria: [], criteria_source: "none" },
      ai_suggestion: {
        solution: "Use parts.", criteria: [{ id: "c1", label: "Setup", points: 2, description: "" }, { id: "c2", label: "Answer", points: 2, description: "" }],
        partial_credit: "half", common_mistakes: [{ label: "forgets +C", tag: "missing step", deduction: 1 }],
        provider: "openai", model: "m", prompt_version: "v", created_at: "x",
      },
    },
  ],
};

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset();
});

describe("answer key", () => {
  it("validates criteria totals", () => {
    expect(criteriaTotal([{ points: "1.5" }, { points: "x" }, { points: "2" }])).toBe(3.5);
    const form = { solution: "", partial_credit: "", common_mistakes: [], criteria: [{ label: "a", points: "1", description: "" }] };
    expect(keyFormError(form, 2)).toMatch(/add up to 2/);
    expect(keyFormError({ ...form, criteria: [{ label: "", points: "2", description: "" }] }, 2)).toMatch(/label/);
    expect(keyFormError({ ...form, criteria: [{ label: "a", points: "2", description: "" }] }, 2)).toBeNull();
    expect(keyFormError({ ...form, criteria: [] }, 2)).toBeNull();
  });

  it("uses the AI draft as a starting point and saves the question key", async () => {
    api.key.mockResolvedValue(KEY);
    api.saveQuestionKey.mockImplementation(async (_e: string, _q: string, body: { criteria: unknown[] }) => ({
      question_id: "q1",
      key: { ...KEY.questions[0].key, ...body, criteria: body.criteria, criteria_source: "key" },
      ai_suggestion: null,
    }));
    render(<AnswerKeyStep examId="e1" />);
    expect(await screen.findByText("Integrate x e^x.")).toBeInTheDocument();
    expect(screen.getByText("No rubric")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Use AI draft" }));
    expect(screen.getByLabelText("Criterion 1")).toHaveValue("Setup");
    expect(screen.getByLabelText("Worked solution / final answer")).toHaveValue("Use parts.");
    // break the total -> validation, nothing saved
    const p2 = screen.getByLabelText("Points for criterion 2");
    await userEvent.clear(p2);
    await userEvent.type(p2, "1");
    await userEvent.click(screen.getByRole("button", { name: "Save question 1" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Rubric points must add up to 4");
    expect(api.saveQuestionKey).not.toHaveBeenCalled();
    await userEvent.clear(p2);
    await userEvent.type(p2, "2");
    await userEvent.click(screen.getByRole("button", { name: "Save question 1" }));
    await waitFor(() => expect(api.saveQuestionKey).toHaveBeenCalled());
    const body = api.saveQuestionKey.mock.calls[0][2];
    expect(body.criteria.map((c: { points: number }) => c.points)).toEqual([2, 2]);
    expect(body.common_mistakes).toEqual([{ label: "forgets +C", tag: "missing step", deduction: 1 }]);
    expect(await screen.findByText("Rubric set")).toBeInTheDocument();
  });
});

describe("phone capture", () => {
  it("shows a QR link on the upload step", async () => {
    api.newCapture.mockResolvedValue({
      token: "tok", path: "/teacher/capture/tok", url: "http://fp.example/teacher/capture/tok",
      expires_at: "2026-10-12T10:30:00Z", qr_svg: "<svg data-testid='qr'></svg>",
    });
    render(<PhoneCapture examId="e1" unprinted={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Show QR code" }));
    expect(await screen.findByRole("img", { name: "QR code for the phone link" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "http://fp.example/teacher/capture/tok" })).toHaveAttribute("href", "/teacher/capture/tok");
    expect(api.newCapture).toHaveBeenCalledWith("e1", false);
  });

  it("phone page: take photos, remove one, send", async () => {
    Object.assign(URL, { createObjectURL: vi.fn(() => "blob:p"), revokeObjectURL: vi.fn() });
    api.captureInfo.mockResolvedValue({ exam_title: "Paper Midterm", course: "MATH 201", expires_at: "x", photos: 0, uploads: 0, unprinted: false });
    api.captureUpload.mockResolvedValue({ batch_id: "b1", photos: 1 });
    render(
      <MemoryRouter initialEntries={["/teacher/capture/tok"]}>
        <Routes>
          <Route path="/teacher/capture/:token" element={<CapturePage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Paper Midterm" })).toBeInTheDocument();
    const input = screen.getByLabelText("Take photo") as HTMLInputElement;
    expect(input).toHaveAttribute("capture", "environment");
    expect(input).toHaveAttribute("accept", "image/*");
    const a = new File(["a"], "a.jpg", { type: "image/jpeg" });
    const b = new File(["b"], "b.jpg", { type: "image/jpeg" });
    await userEvent.upload(input, [a, b]);
    expect(screen.getAllByRole("img")).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Remove photo 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Send 1 photo" }));
    await waitFor(() => expect(api.captureUpload).toHaveBeenCalled());
    expect(api.captureUpload.mock.calls[0][0]).toBe("tok");
    expect(api.captureUpload.mock.calls[0][1]).toEqual([b]);
    expect(await screen.findByText(/Sent 1 photo\./)).toBeInTheDocument();
  });

  it("phone page: expired link", async () => {
    api.captureInfo.mockRejectedValue(new ApiError(410, "expired"));
    render(
      <MemoryRouter initialEntries={["/teacher/capture/old"]}>
        <Routes>
          <Route path="/teacher/capture/:token" element={<CapturePage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("This link has expired");
  });
});

describe("map pages", () => {
  const pg = (id: number, seq: number) => ({ page_id: id, batch_id: "b", seq, status: "unmatched", preview: `/api/uploads/p${id}`, student_id: null, page_no: null });
  const MAP = {
    exam_id: "e1", pages_per_student: 1, question_pages: { q1: 1, q2: 1 },
    questions: [{ question_id: "q1", index: 0, prompt: "A", type: "essay" }, { question_id: "q2", index: 1, prompt: "B", type: "essay" }],
    students: [{ id: 2, username: "alice", name: "Alice Chen" }, { id: 3, username: "bob", name: "Bob Karimov" }],
    pages: [pg(11, 0), pg(12, 1), pg(13, 2), pg(14, 3), pg(15, 4)],
  };

  it("groups pages per student in roster order", () => {
    expect(autoMap(MAP.pages, MAP.students, 2)).toEqual({
      11: { student_id: 2, page_no: 1 }, 12: { student_id: 2, page_no: 2 },
      13: { student_id: 3, page_no: 1 }, 14: { student_id: 3, page_no: 2 },
      15: { student_id: null, page_no: 1 },
    });
  });

  it("sets pages per student, question pages, fixes one page and saves", async () => {
    api.pageMap.mockResolvedValue(MAP);
    api.saveMapSettings.mockResolvedValue(MAP);
    api.applyMap.mockResolvedValue({ assigned: 4, students: 2, queued: 4 });
    render(<MapPages examId="e1" />);
    expect(await screen.findByRole("heading", { name: "Map pages (5 pages)" })).toBeInTheDocument();
    const pps = screen.getByLabelText("Pages per student");
    await userEvent.clear(pps);
    await userEvent.type(pps, "2");
    await userEvent.selectOptions(screen.getByLabelText("Question 2"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Fill in students in order" }));
    expect(screen.getByLabelText("Student for page 3")).toHaveValue("3");
    // bob's two pages came in swapped
    await userEvent.selectOptions(screen.getByLabelText("Page number of page 3"), "2");
    await userEvent.selectOptions(screen.getByLabelText("Page number of page 4"), "1");
    expect(screen.getByText("1 page without a student")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save mapping" }));
    await waitFor(() => expect(api.applyMap).toHaveBeenCalled());
    expect(api.saveMapSettings).toHaveBeenCalledWith("e1", { pages_per_student: 2, question_pages: { q1: 1, q2: 2 } });
    expect(api.applyMap.mock.calls[0][1]).toEqual([
      { page_id: 11, student_id: 2, page_no: 1 }, { page_id: 12, student_id: 2, page_no: 2 },
      { page_id: 13, student_id: 3, page_no: 2 }, { page_id: 14, student_id: 3, page_no: 1 },
    ]);
    expect(await screen.findByText(/AI is drafting 4 answers/)).toBeInTheDocument();
  });
});

describe("struggles and student feedback", () => {
  it("lists questions by average with misconception counts", async () => {
    api.struggles.mockResolvedValue({
      exam_id: "e1",
      time: { graded: 2, seconds: 30, estimate_manual_seconds: 240 },
      questions: [
        { question_id: "q1", index: 0, prompt: "Easy one", points: 2, graded: 2, total: 2, avg_points: 2, avg_percent: 100, misconceptions: [] },
        { question_id: "q2", index: 1, prompt: "Integrate", points: 4, graded: 2, total: 3, avg_points: 1, avg_percent: 25,
          misconceptions: [{ tag: "missing step", count: 3, approved: 2, students: [
            { username: "alice", name: "Alice Chen" }, { username: "bob", name: "Bob Karimov" }, { username: "carol", name: "Carol Wang" }] }] },
        { question_id: "q3", index: 2, prompt: "Nobody", points: 1, graded: 0, total: 0, avg_points: null, avg_percent: null, misconceptions: [] },
      ],
    });
    render(<StrugglePanel examId="e1" />);
    expect(await screen.findByRole("heading", { name: "Where students struggled" })).toBeInTheDocument();
    const items = screen.getAllByRole("listitem").filter((li) => li.classList.contains("st-q"));
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Question 2");
    expect(items[0]).toHaveTextContent("1 / 4 avg · 25%");
    expect(items[0]).toHaveTextContent("missing step · 3 students (1 not approved yet) — Alice Chen, Bob Karimov, Carol Wang");
  });

  it("student feedback shows the scan, the explanation and the step marks", () => {
    const q = {
      question_id: "q2", index: 1, type: "essay", prompt: "Integrate x e^x.", options: [], points: 4, points_awarded: 3,
      status: "partial", given: "(written on paper)", correct_answer: null, tolerance: null, comment: "Add the constant.",
      rubric: [], rubric_scores: null, images: [], image: "/api/uploads/scan",
      steps: [{ n: 1, text: "u = x", status: "correct", note: "" }, { n: 2, text: "", status: "missing", note: "+C" }],
      first_error_step: 2,
      criteria: [{ label: "Setup", points: 1, max_points: 1 }, { label: "Constant", points: 0, max_points: 1 }],
    } as unknown as FeedbackQuestion;
    render(<FeedbackQuestionCard q={q} />);
    expect(screen.getByRole("img", { name: "Your written answer for question 2" })).toHaveAttribute("src", "/api/uploads/scan");
    expect(screen.getByText("Add the constant.")).toBeInTheDocument();
    const steps = screen.getByRole("region", { name: "Your steps for question 2" });
    expect(steps).toHaveTextContent("Correct");
    expect(steps).toHaveTextContent("Missing step");
    expect(steps).toHaveTextContent("This is where it first goes wrong.");
    expect(screen.getByText("Constant").parentElement).toHaveTextContent("0 / 1");
  });
});

describe("export", () => {
  it("downloads the file and warns about ungraded answers", async () => {
    const create = vi.fn(() => "blob:x");
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() });
    api.exportGrades.mockResolvedValue({ blob: new Blob(["x"]), filename: "Quiz-grades.xlsx", ungraded: 3 });
    render(<ExportButtons examId="e1" />);
    await userEvent.click(screen.getByRole("button", { name: "Export Excel" }));
    await waitFor(() => expect(api.exportGrades).toHaveBeenCalledWith("e1", "xlsx"));
    expect(await screen.findByRole("status")).toHaveTextContent("3 answers are not graded yet");
    expect(create).toHaveBeenCalled();
  });
});
