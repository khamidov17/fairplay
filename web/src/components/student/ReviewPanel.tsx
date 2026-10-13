import { useEffect, useRef, useState } from "react";
import type { AnswerMap, StudentQuestion } from "../../api/types";
import { QuestionImages } from "../QuestionImages";

export function reviewCounts(questions: StudentQuestion[], answers: AnswerMap, marked: ReadonlySet<string>) {
  let answered = 0;
  let markedCount = 0;
  let empty = 0;
  for (const q of questions) {
    if (answers[q.id] !== undefined) answered += 1;
    else empty += 1;
    if (marked.has(q.id)) markedCount += 1;
  }
  return { answered, marked: markedCount, empty };
}

interface Props {
  questions: StudentQuestion[];
  answers: AnswerMap;
  marked: ReadonlySet<string>;
  onGoTo(index: number): void;
  onKeepWorking(): void;
  /** Called after the student confirmed. */
  onSubmit(): void;
  submitting: boolean;
  error: string | null;
}

/** "Ready to submit?" — counts, marked/empty questions with Go to, then a confirm step. */
export function ReviewPanel({ questions, answers, marked, onGoTo, onKeepWorking, onSubmit, submitting, error }: Props) {
  const counts = reviewCounts(questions, answers, marked);
  const [confirming, setConfirming] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const headRef = useRef<HTMLHeadingElement>(null);

  const attention = questions
    .map((q, i) => ({ q, i, isMarked: marked.has(q.id), isEmpty: answers[q.id] === undefined }))
    .filter((x) => x.isMarked || x.isEmpty);

  useEffect(() => {
    headRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!confirming) return;
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setConfirming(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirming]);

  useEffect(() => {
    if (error) setConfirming(false);
  }, [error]);

  return (
    <div className="stu-review">
      <main className="stu-paper stu-review-main">
        <h1 ref={headRef} tabIndex={-1}>
          Ready to submit?
        </h1>
        <p className="stu-review-lede">Check the questions below before you hand in your exam.</p>
        <div className="stu-counts">
          <div className="stu-count" data-testid="count-answered">
            <b>{counts.answered}</b>
            <span>Answered</span>
          </div>
          <div className={`stu-count marked${counts.marked === 0 ? " zero" : ""}`} data-testid="count-marked">
            <b>{counts.marked}</b>
            <span>Marked for review</span>
          </div>
          <div className={`stu-count none${counts.empty === 0 ? " zero" : ""}`} data-testid="count-empty">
            <b>{counts.empty}</b>
            <span>Not answered</span>
          </div>
        </div>

        {attention.length > 0 ? (
          <>
            <h2 className="stu-review-sub">Worth another look</h2>
            <ul className="stu-review-list" aria-label="Questions to check">
              {attention.map(({ q, i, isMarked, isEmpty }) => (
                <li key={q.id}>
                  <span className="qn">Q{i + 1}</span>
                  <span className="qp" title={q.prompt}>
                    {q.prompt}
                  </span>
                  <QuestionImages images={q.images} compact label={`Pictures in question ${i + 1}`} />
                  {isMarked && <span className="st marked">Marked</span>}
                  {isEmpty && <span className="st none">Empty</span>}
                  <button
                    type="button"
                    className="stu-btn sm"
                    aria-label={`Go to question ${i + 1}`}
                    onClick={() => onGoTo(i)}
                    disabled={submitting}
                  >
                    Go to
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <div className="stu-alert success">All questions are answered.</div>
        )}

        <section className="stu-after" aria-labelledby="after-h">
          <h2 id="after-h">After you submit</h2>
          <ol>
            <li>You can&apos;t change answers any more. If time runs out, everything you&apos;ve written is submitted automatically.</li>
            <li>You get a receipt code. It proves your exam reached the server.</li>
            <li>Your laptop unlocks. Grades appear in Grades when your teacher releases them.</li>
          </ol>
        </section>

        {error && (
          <div className="stu-alert error" role="alert">
            {error}
          </div>
        )}

        <div className="stu-review-actions">
          <button type="button" className="stu-btn lg" onClick={onKeepWorking} disabled={submitting}>
            Keep working
          </button>
          <button type="button" className="stu-btn primary lg" onClick={() => setConfirming(true)} disabled={submitting}>
            {submitting ? "Submitting…" : "Submit exam"}
          </button>
        </div>
      </main>

      {confirming && (
        <div className="stu-backdrop" onClick={(e) => e.target === e.currentTarget && setConfirming(false)}>
          <div
            className="stu-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="confirm-h"
            aria-describedby="confirm-d"
            onKeyDown={(e) => {
              // keep Tab inside the dialog
              if (e.key !== "Tab") return;
              const btns = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>("button"));
              const first = btns[0];
              const last = btns[btns.length - 1];
              if (!first || !last) return;
              if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
              } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
              }
            }}
          >
            <h2 id="confirm-h">Submit your exam now?</h2>
            <p id="confirm-d" style={{ margin: 0, color: "var(--s-text-2)", lineHeight: 1.6, fontSize: 15 }}>
              {counts.empty > 0
                ? `${counts.empty} ${counts.empty === 1 ? "question is" : "questions are"} not answered. `
                : "All questions are answered. "}
              You can&apos;t change answers after this.
            </p>
            <div className="stu-review-actions split">
              <button ref={cancelRef} type="button" className="stu-btn lg" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="stu-btn primary lg"
                onClick={() => {
                  setConfirming(false);
                  onSubmit();
                }}
              >
                Yes, submit
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
