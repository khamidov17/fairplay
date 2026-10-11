// Slice 6 H: student practice. Lists the sets a teacher sent, then one item at a time with
// instant feedback (checked on the server). Practice never counts toward grades.
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, practiceApi } from "../../api/client";
import type {
  PracticeProgress,
  StudentPracticeItem,
  StudentPracticeResult,
  StudentPracticeSet,
  StudentPracticeSummary,
} from "../../api/types";
import { QuestionImages } from "../../components/QuestionImages";
import { StudentLayout, StuLoading } from "../../components/student/StudentLayout";
import "../../styles/practice.css";

const NOT_GRADED = "Practice — doesn’t count toward your grade";

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail : "Cannot reach the server. Check the connection and try again.";
}

/** Text for an answer key returned by the server. */
export function correctAnswerText(ca: StudentPracticeResult["correct_answer"] | undefined): string | null {
  if (ca == null) return null;
  if (typeof ca === "string") return ca;
  if (ca.text == null) return null;
  return ca.index != null ? `${String.fromCharCode(65 + ca.index)}. ${ca.text}` : ca.text;
}

/** First item without a correct answer yet (or the first item). */
export function firstOpenIndex(items: StudentPracticeItem[]): number {
  const i = items.findIndex((it) => !it.result || !(it.result.ever_correct || it.result.correct));
  return i < 0 ? 0 : i;
}

export default function StudentPracticePage() {
  const { setId } = useParams();
  return setId ? <PracticeSetView id={setId} /> : <PracticeList />;
}

function Progress({ p }: { p: PracticeProgress }) {
  const pctDone = p.total ? Math.round((p.answered / p.total) * 100) : 0;
  return (
    <div className="pr-stu-progress">
      <div
        className="pr-bar"
        role="progressbar"
        aria-label="Practice progress"
        aria-valuemin={0}
        aria-valuemax={p.total}
        aria-valuenow={p.answered}
      >
        <span style={{ width: `${pctDone}%` }} />
      </div>
      <span className="stu-small">
        {p.answered} of {p.total} answered · {p.correct} correct
      </span>
    </div>
  );
}

function PracticeList() {
  const [sets, setSets] = useState<StudentPracticeSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    practiceApi.mySets().then(
      (s) => alive && setSets(s),
      (e) => {
        if (!alive) return;
        setError(errText(e));
        setSets([]);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  return (
    <StudentLayout>
      <main className="stu-main narrow">
        <div>
          <h1 className="stu-h1">Practice</h1>
          <p className="pr-stu-note">{NOT_GRADED}. Try as often as you like.</p>
        </div>
        {error && (
          <div className="stu-alert error" role="alert">
            {error}
          </div>
        )}
        {sets === null ? (
          <StuLoading label="Loading practice…" />
        ) : sets.length === 0 ? (
          !error && (
            <div className="stu-card">
              <h2>No practice yet</h2>
              <p className="stu-muted">When your teacher sends you practice, it shows up here.</p>
            </div>
          )
        ) : (
          <ul className="pr-stu-list" aria-label="Practice sets">
            {sets.map((s) => (
              <li key={s.id}>
                <Link to={`/student/practice/${encodeURIComponent(s.id)}`} className="stu-card pr-stu-set">
                  <span className="pr-stu-set-title">{s.title}</span>
                  <span className="stu-small stu-muted">
                    {s.teacher_name ? `From ${s.teacher_name}` : "From your teacher"}
                    {s.sent_at ? ` · ${new Date(s.sent_at).toLocaleDateString()}` : ""}
                  </span>
                  <Progress p={s.progress ?? { answered: s.answered, correct: s.correct, total: s.item_count }} />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </main>
    </StudentLayout>
  );
}

function PracticeSetView({ id }: { id: string }) {
  const navigate = useNavigate();
  const [set, setSet] = useState<StudentPracticeSet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [index, setIndex] = useState(0);
  const [value, setValue] = useState<string | number | null>(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ correct: boolean | null; text: string; explanation: string; answer: string | null } | null>(null);

  useEffect(() => {
    let alive = true;
    practiceApi.mySet(id).then(
      (s) => {
        if (!alive) return;
        setSet(s);
        setIndex(firstOpenIndex(s.items));
      },
      (e) => alive && setError(errText(e)),
    );
    return () => {
      alive = false;
    };
  }, [id]);

  const item = set?.items[index];

  useEffect(() => {
    setValue(null);
    setFeedback(null);
  }, [index]);

  const check = async () => {
    if (!set || !item || value === null || value === "") return;
    setBusy(true);
    setError(null);
    try {
      const r = await practiceApi.answer(set.id, item.id, value);
      setFeedback({
        correct: r.correct,
        text: r.feedback,
        explanation: r.explanation ?? r.result?.explanation ?? "",
        answer: correctAnswerText(r.correct_answer ?? r.result?.correct_answer),
      });
      setSet((cur) =>
        cur
          ? {
              ...cur,
              progress: r.progress,
              items: cur.items.map((it) => (it.id === item.id && r.result ? { ...it, result: r.result } : it)),
            }
          : cur,
      );
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  if (error && !set) {
    return (
      <StudentLayout>
        <main className="stu-main narrow">
          <div className="stu-alert error" role="alert">
            {error}
          </div>
          <Link to="/student/practice" className="stu-link">
            Back to practice
          </Link>
        </main>
      </StudentLayout>
    );
  }
  if (!set || !item) {
    return (
      <StudentLayout>
        <main className="stu-main narrow">{set ? <p>This set has no items.</p> : <StuLoading label="Loading practice…" />}</main>
      </StudentLayout>
    );
  }

  const last = index === set.items.length - 1;
  const done = item.result && (item.result.ever_correct || item.result.correct);

  return (
    <StudentLayout>
      <main className="stu-main narrow">
        <Link to="/student/practice" className="stu-link">
          ← All practice
        </Link>
        <div>
          <p className="pr-stu-note">{NOT_GRADED}</p>
          <h1 className="stu-h1">{set.title}</h1>
        </div>
        <Progress p={set.progress} />

        <nav className="pr-dots" aria-label="Practice items">
          {set.items.map((it, i) => {
            const state = !it.result ? "" : it.result.ever_correct || it.result.correct ? " ok" : it.result.correct === null ? " saved" : " miss";
            return (
              <button
                key={it.id}
                type="button"
                className={`pr-dot${state}`}
                aria-current={i === index ? "step" : undefined}
                aria-label={`Item ${i + 1}${state === " ok" ? ", correct" : state === " miss" ? ", not yet correct" : state === " saved" ? ", answered" : ""}`}
                onClick={() => setIndex(i)}
              >
                {i + 1}
              </button>
            );
          })}
        </nav>

        <section className="stu-card pr-stu-item" aria-labelledby="pr-item-title">
          <p className="stu-small stu-muted" id="pr-item-title">
            Item {index + 1} of {set.items.length}
            {item.topics.length > 0 && ` · ${item.topics.join(", ")}`}
          </p>
          <QuestionImages images={item.images} />
          <p className="pr-stu-prompt">{item.prompt}</p>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              void check();
            }}
          >
            <AnswerField item={item} value={value} onChange={setValue} />
            {error && (
              <p className="stu-field-error" role="alert">
                {error}
              </p>
            )}
            <div className="pr-stu-actions">
              <button type="submit" className="stu-btn primary" disabled={busy || value === null || value === ""}>
                {busy ? "Checking…" : item.type === "essay" ? "Save" : "Check"}
              </button>
            </div>
          </form>

          <div aria-live="polite">
            {feedback && (
              <div className={`pr-feedback${feedback.correct === true ? " ok" : feedback.correct === false ? " miss" : ""}`}>
                <strong>{feedback.text}</strong>
                {feedback.answer && feedback.correct !== true && <p>Answer: {feedback.answer}</p>}
                {feedback.explanation && <p>{feedback.explanation}</p>}
              </div>
            )}
            {!feedback && done && <p className="pr-feedback ok">You already got this one right.</p>}
          </div>

          {item.type === "essay" && item.rubric && item.rubric.length > 0 && (
            <div className="pr-rubric">
              <p className="stu-small stu-muted">What a good answer covers:</p>
              <ul>
                {item.rubric.map((r, i) => (
                  <li key={i}>{r.label}</li>
                ))}
              </ul>
            </div>
          )}
        </section>

        <div className="pr-stu-nav">
          <button type="button" className="stu-btn" disabled={index === 0} onClick={() => setIndex((i) => i - 1)}>
            Previous
          </button>
          {last ? (
            <button type="button" className="stu-btn" onClick={() => navigate("/student/practice")}>
              Finish
            </button>
          ) : (
            <button type="button" className="stu-btn" onClick={() => setIndex((i) => i + 1)}>
              Next
            </button>
          )}
        </div>
      </main>
    </StudentLayout>
  );
}

function AnswerField({
  item,
  value,
  onChange,
}: {
  item: StudentPracticeItem;
  value: string | number | null;
  onChange: (v: string | number | null) => void;
}) {
  if (item.type === "mcq") {
    return (
      <fieldset className="pr-stu-options">
        <legend className="sr-only">Choose one answer</legend>
        {item.options.map((o, i) => (
          <label key={i} className={`pr-stu-option${value === i ? " on" : ""}`}>
            <input type="radio" name={`pr-${item.id}`} checked={value === i} onChange={() => onChange(i)} />
            <span className="pr-stu-letter" aria-hidden="true">
              {String.fromCharCode(65 + i)}
            </span>
            <span>{o}</span>
          </label>
        ))}
      </fieldset>
    );
  }
  if (item.type === "essay") {
    return (
      <label className="pr-stu-field">
        <span className="sr-only">Your answer</span>
        <textarea className="pr-stu-text" rows={8} value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value)} />
      </label>
    );
  }
  const hint =
    item.type === "numeric"
      ? "Numbers like 12, 0.5 or 3/4 are fine."
      : item.math
        ? "Write math like 2x+2, x^2 or sqrt(2) — equivalent forms count."
        : "";
  return (
    <label className="pr-stu-field">
      <span className="stu-small">Your answer</span>
      <input
        className="pr-stu-input"
        inputMode={item.type === "numeric" ? "decimal" : "text"}
        autoComplete="off"
        value={(value as string) ?? ""}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && <span className="stu-small stu-muted">{hint}</span>}
    </label>
  );
}
