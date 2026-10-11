import { describe, expect, it } from "vitest";
import type { Exam } from "../api/types";
import {
  draftFromExam,
  joinSchedule,
  newQuestion,
  newRubricRow,
  splitSchedule,
  toExamInput,
  validateDraft,
} from "../pages/teacher/examDraft";

function draftWith(q: ReturnType<typeof newQuestion>) {
  return { ...draftFromExam(), title: "Quiz", questions: [q] };
}

describe("exam draft (slice 2)", () => {
  it("creates true/false as an mcq with True/False options", () => {
    const q = { ...newQuestion("tf"), prompt: "Every bounded sequence converges.", correct_index: 1 };
    const input = toExamInput(draftWith(q));
    expect(input.questions[0]).toMatchObject({
      type: "mcq",
      options: ["True", "False"],
      correct_index: 1,
      rubric: [],
      min_words: null,
      max_words: null,
    });
  });

  it("requires essay rubric points to add up to the question points", () => {
    const q = {
      ...newQuestion("essay"),
      prompt: "Explain.",
      points: "10",
      rubric: [newRubricRow("Reasons", "6"), newRubricRow("Clarity", "3")],
    };
    const errs = validateDraft(draftWith(q));
    expect(errs.byKey[q.key]?.rubric).toMatch(/add up to 9/);

    const fixed = {
      ...q,
      rubric: [newRubricRow("Reasons", "6"), newRubricRow("Clarity", "4")],
      min_words: "50",
      max_words: "300",
      topics: ["argument"],
    };
    expect(validateDraft(draftWith(fixed)).byKey[fixed.key]).toBeUndefined();
    expect(toExamInput(draftWith(fixed)).questions[0]).toMatchObject({
      type: "essay",
      points: 10,
      rubric: [
        { label: "Reasons", points: 6 },
        { label: "Clarity", points: 4 },
      ],
      min_words: 50,
      max_words: 300,
      options: [],
      accepted_answers: [],
    });
  });

  it("rejects min words above max words", () => {
    const q = { ...newQuestion("essay"), prompt: "Explain.", min_words: "300", max_words: "50" };
    expect(validateDraft(draftWith(q)).byKey[q.key]?.words).toBeTruthy();
  });

  it("round-trips course, room, schedule and shuffle", () => {
    const iso = joinSchedule("2026-10-20", "14:30")!;
    expect(splitSchedule(iso)).toEqual({ date: "2026-10-20", time: "14:30" });
    expect(joinSchedule("", "14:30")).toBeNull();

    const exam = {
      id: "e1",
      title: "Midterm",
      duration_min: 90,
      status: "draft",
      start_code: null,
      created_at: "2026-10-01T00:00:00Z",
      question_count: 1,
      total_points: 2,
      course: "MATH 201",
      room: "AB 1087",
      scheduled_at: iso,
      shuffle_questions: true,
      grades_released: false,
      released_at: null,
      closed_at: null,
      pending_grading: 0,
      questions: [
        {
          id: "q1",
          type: "mcq",
          prompt: "T/F?",
          points: 2,
          options: ["True", "False"],
          correct_index: 0,
          answer_number: null,
          tolerance: null,
          accepted_answers: [],
          rubric: [],
          min_words: null,
          max_words: null,
        },
      ],
    } satisfies Exam;
    const d = draftFromExam(exam);
    expect(d.questions[0].tf).toBe(true);
    const input = toExamInput(d);
    expect(input).toMatchObject({ course: "MATH 201", room: "AB 1087", shuffle_questions: true, duration_min: 90 });
    expect(input.scheduled_at).toBe(iso);
  });
});
