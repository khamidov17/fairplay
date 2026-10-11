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
