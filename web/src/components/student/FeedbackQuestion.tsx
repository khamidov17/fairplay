import type { AnswerValue, FeedbackQuestion as FQ } from "../../api/types";
import { paperImage, paperValue } from "../../lib/paperAnswer";
import { LETTERS, fmtNum } from "./format";
import { QuestionImages } from "../QuestionImages";
import "../../styles/feedbackSteps.css";
import { DiagramFeedback } from "../diagrams/DiagramFeedback";

export function typeLabel(q: Pick<FQ, "type" | "options">): string {
  if (q.type === "mcq") {
    const o = q.options.map((x) => x.trim().toLowerCase());
    return o.length === 2 && o[0] === "true" && o[1] === "false" ? "True or false" : "Multiple choice";
  }
  if (q.type === "numeric") return "Number";
  if (q.type === "short") return "Short answer";
  return "Essay";
}

const STATUS: Record<FQ["status"], { cls: string; word: string }> = {
  correct: { cls: "success", word: "correct" },
  partial: { cls: "warn", word: "partial" },
  wrong: { cls: "danger", word: "wrong" },
  unanswered: { cls: "", word: "not answered" },
};

export function StatusPill({ q }: { q: Pick<FQ, "status" | "points" | "points_awarded"> }) {
  const s = STATUS[q.status] ?? STATUS.wrong;
  const score = `${fmtNum(q.points_awarded)} / ${fmtNum(q.points)}`;
  return (
    <span className={`stu-pill ${s.cls}`} data-status={q.status}>
      {q.status === "correct" ? (
        <>
          {score} <span aria-hidden="true">✓</span>
          <span className="sr-only">correct</span>
        </>
      ) : (
        `${score} · ${s.word}`
      )}
    </span>
  );
}

function show(q: FQ, raw: AnswerValue | null): string {
  const v = paperValue(raw) as AnswerValue | null;
  if (paperImage(raw) && (v === null || v === undefined)) return "Written on paper";
  if (v === null || v === undefined || v === "") return "No answer";
  if (q.type === "mcq" && typeof v === "number") {
    const text = q.options[v];
    return text === undefined ? `Option ${v + 1}` : `${LETTERS[v] ?? v + 1} · ${text}`;
  }
  return String(v);
}

function correctText(q: FQ): string | null {
  if (q.correct_answer === null || q.correct_answer === undefined) return null;
  if (q.type === "numeric" && typeof q.correct_answer === "number") {
    return q.tolerance ? `${fmtNum(q.correct_answer)} ± ${fmtNum(q.tolerance)}` : fmtNum(q.correct_answer);
  }
  return show(q, q.correct_answer);
}

/** Phase 2 (paper exams): approved step marks and rubric breakdown sent with the feedback. */
interface PaperExtras {
  index: number;
  steps?: { n: number; text: string; status: "correct" | "error" | "missing"; note: string }[];
  first_error_step?: number | null;
  criteria?: { label: string; points: number; max_points: number }[];
}

const STEP_TEXT = { correct: "Correct", error: "Mistake", missing: "Missing step" } as const;

function PaperSteps({ q }: { q: PaperExtras }) {
  const steps = q.steps ?? [];
  const criteria = q.criteria ?? [];
  if (steps.length === 0 && criteria.length === 0) return null;
  return (
    <>
      {steps.length > 0 && (
        <section className="stu-steps" aria-label={`Your steps for question ${q.index + 1}`}>
          <h2>Your work, step by step</h2>
          <ol>
            {steps.map((s) => (
              <li key={s.n} className={`stu-step ${s.status}${s.n === q.first_error_step ? " first" : ""}`}>
                <span className="stu-step-mark">{STEP_TEXT[s.status] ?? s.status}</span>
                <span>
                  {s.text || <em>(not written)</em>}
                  {s.note && <span className="stu-step-note">{s.note}</span>}
                  {s.n === q.first_error_step && <span className="stu-step-note">This is where it first goes wrong.</span>}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}
      {criteria.length > 0 && (
        <section className="stu-rubric" aria-label="Rubric">
          <h2>Rubric</h2>
          {criteria.map((c, i) => (
            <div key={i} className="stu-rubric-row">
              <span>{c.label}</span>
              <b>
                {fmtNum(c.points)} / {fmtNum(c.max_points)}
              </b>
            </div>
          ))}
        </section>
      )}
    </>
  );
}

/** One question on the feedback page: status, your answer vs correct, rubric, teacher comment. */
export function FeedbackQuestionCard({ q }: { q: FQ }) {
  const headId = `fbq-${q.question_id}`;
  const mono = q.type === "numeric";
  const yours = show(q, q.given);
  const correct = q.type === "essay" ? null : correctText(q);
  const rubric = q.rubric ?? [];

  return (
    <article className="stu-fbq" aria-labelledby={headId}>
      <div className="stu-fbq-head">
        <h2 id={headId}>
          Question {q.index + 1} · {typeLabel(q)}
        </h2>
        <StatusPill q={q} />
      </div>
      <QuestionImages images={q.images} compact />
      <div className="stu-fbq-prompt">{q.prompt}</div>
      {q.diagram && (
        <DiagramFeedback
          index={q.index}
          image={(q as { image?: string | null }).image || paperImage(q.given) || null}
          diagram={q.diagram}
        />
      )}
      {!q.diagram && ((q as { image?: string | null }).image || paperImage(q.given)) && (
        <img
          className="stu-fbq-scan"
          src={(q as { image?: string | null }).image || paperImage(q.given) || undefined}
          alt={`Your written answer for question ${q.index + 1}`}
        />
      )}
      <PaperSteps q={(q.diagram ? { ...q, criteria: undefined } : q) as PaperExtras} />

      {q.type === "essay" ? (
        <>
          {!((q as { image?: string | null }).image || paperImage(q.given)) && (
            <div className={`stu-ans essay`}>
              <span>YOUR ANSWER</span>
              <span>{yours}</span>
            </div>
          )}
          {rubric.length > 0 && (
            <section className="stu-rubric" aria-label="Rubric">
              <h2>Rubric</h2>
              {rubric.map((r, i) => {
                const got = q.rubric_scores?.[i];
                return (
                  <div key={i} className="stu-rubric-row">
                    <span>{r.label}</span>
                    <b>{got === undefined || got === null ? `– / ${fmtNum(r.points)}` : `${fmtNum(got)} / ${fmtNum(r.points)}`}</b>
                  </div>
                );
              })}
            </section>
          )}
        </>
      ) : q.status === "correct" || correct === null ? (
        <div className={`stu-ans ${q.status === "correct" ? "good" : q.status === "unanswered" ? "" : "bad"}`}>
          <span>YOUR ANSWER</span>
          <span className={mono ? "stu-mono" : undefined}>{yours}</span>
        </div>
      ) : (
        <div className="stu-ans-grid">
          <div className={`stu-ans ${q.status === "unanswered" ? "" : "bad"}`}>
            <span>YOUR ANSWER</span>
            <span className={mono ? "stu-mono" : undefined}>{yours}</span>
          </div>
          <div className="stu-ans good">
            <span>CORRECT ANSWER</span>
            <span className={mono ? "stu-mono" : undefined}>{correct}</span>
          </div>
        </div>
      )}

      {q.comment?.trim() && (
        <div className="stu-teacher-note">
          <strong>Teacher:</strong> {q.comment}
        </div>
      )}
    </article>
  );
}
