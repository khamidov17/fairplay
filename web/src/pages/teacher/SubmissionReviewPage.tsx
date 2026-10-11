// One student's whole submission: every question, their answer, the correct answer,
// editable points + comment per question (rubric for essays), overall comment,
// integrity events, previous / next student. Website exams show one "score from the
// exam website" item the teacher fills in.
import { paperImage } from "../../lib/paperAnswer";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, teacherApi } from "../../api/client";
import type { Exam, Question, ResultAnswer, ResultAttempt, Results } from "../../api/types";
import { Loading } from "../../components/Loading";
import { QuestionImages, TopicChips } from "../../components/QuestionImages";
import { Pill } from "../../components/teacher/TeacherLayout";
import { urlHost } from "../../components/teacher/externalValidation";
import { errorText, eventLabel, formatWhen, num, pct } from "../../components/teacher/util";
import { KIND_LABEL, kindOf } from "./examDraft";
import { formatGiven } from "./ResultsView";

const LETTERS = "ABCDEFGH";

function correctText(q: Question | undefined): string | null {
  if (!q) return null;
  if (q.type === "mcq" && q.correct_index != null) return `${LETTERS[q.correct_index]}. ${q.options[q.correct_index]}`;
  if (q.type === "numeric" && q.answer_number != null)
    return q.tolerance ? `${q.answer_number} (± ${q.tolerance})` : String(q.answer_number);
  if (q.type === "short") return q.accepted_answers.join(", ") || null;
  return null;
}

function typeLabel(q: Question | undefined): string {
  if (!q) return "Question";
  if (q.type === "external") return "Exam website";
  return KIND_LABEL[kindOf(q)] ?? q.type;
}

export default function SubmissionReviewPage() {
  const { id: examId = "", attemptId = "" } = useParams();
  const navigate = useNavigate();
  const [exam, setExam] = useState<Exam | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [e, r] = await Promise.all([teacherApi.getExam(examId), teacherApi.results(examId)]);
      setExam(e);
      setResults(r);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404 ? "This exam was not found." : errorText(e));
    }
  }, [examId]);

  useEffect(() => {
    void load();
  }, [load]);

  const attempts = results?.attempts ?? [];
  const index = attempts.findIndex((a) => a.attempt_id === attemptId);
  const attempt = index >= 0 ? attempts[index] : undefined;
  const qById = useMemo(() => new Map((exam?.questions ?? []).map((q) => [q.id, q])), [exam]);
  const qIndex = useMemo(() => new Map((exam?.questions ?? []).map((q, i) => [q.id, i])), [exam]);

  if (error)
    return (
      <div className="t-page">
        <Link to={`/teacher/exams/${examId}?tab=results`} className="t-back">
          ← Results
        </Link>
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      </div>
    );
  if (!exam || !results) return <Loading label="Loading submission…" />;
  if (!attempt)
    return (
      <div className="t-page">
        <Link to={`/teacher/exams/${examId}?tab=results`} className="t-back">
          ← Results
        </Link>
        <div className="t-empty">This student has no attempt in this exam.</div>
      </div>
    );

  const go = (i: number) => navigate(`/teacher/exams/${examId}/submissions/${attempts[i].attempt_id}`);
  const answers = [...attempt.answers].sort(
    (x, y) => (qIndex.get(x.question_id) ?? 0) - (qIndex.get(y.question_id) ?? 0),
  );
  const editable = attempt.status === "submitted";
  const isExternal = exam.mode === "external";

  return (
    <div className="t-page t-review">
      <div className="t-review-top">
        <Link to={`/teacher/exams/${examId}?tab=results`} className="t-back">
          ← {exam.title} results
        </Link>
        <div className="t-review-nav" role="group" aria-label="Students">
          <button type="button" className="t-btn-outline" onClick={() => go(index - 1)} disabled={index <= 0}>
            ← Previous student
          </button>
          <span className="t-small t-muted">
            {index + 1} of {attempts.length}
          </span>
          <button
            type="button"
            className="t-btn-outline"
            onClick={() => go(index + 1)}
            disabled={index >= attempts.length - 1}
          >
            Next student →
          </button>
        </div>
      </div>

      <header className="t-review-head">
        <div>
          <h1>{attempt.name}</h1>
          <p className="t-muted" style={{ margin: 0 }}>
            <Link to={`/teacher/students/${encodeURIComponent(attempt.username)}`} className="t-row-link">
              {attempt.username} · profile
            </Link>
            {" · "}
            <Link
              to={`/teacher/exams/${encodeURIComponent(examId)}/submissions/${encodeURIComponent(attempt.attempt_id)}/timeline`}
              className="t-row-link"
            >
              Session timeline
            </Link>
            {attempt.submitted_at ? ` · submitted ${formatWhen(attempt.submitted_at)}` : " · still writing"}
            {attempt.auto_submitted ? " (time ran out or exam closed)" : ""}
            {attempt.receipt ? ` · receipt ${attempt.receipt}` : ""}
          </p>
        </div>
        <div className="t-review-score" aria-live="polite">
          <span className="t-review-score-n">
            {attempt.score == null ? "—" : num(attempt.score)}
            <small> / {num(attempt.total_points)}</small>
          </span>
          <span className="t-small t-muted">
            {attempt.needs_grading > 0
              ? `${attempt.needs_grading} to grade`
              : attempt.percent == null
                ? ""
                : pct(attempt.percent)}
          </span>
        </div>
      </header>

      {!editable && (
        <div className="alert alert-info" role="status">
          {attempt.name} is still writing. You can grade once they submit or you close the exam.
        </div>
      )}
      {isExternal && (
        <div className="alert alert-info" role="note">
          Answers for this exam are on{" "}
          <a href={exam.external_url ?? undefined} target="_blank" rel="noreferrer">
            {urlHost(exam.external_url) ?? "the exam website"}
          </a>
          . Check the student's work there, then enter their score below.
        </div>
      )}

      <ol className="t-review-list">
        {answers.map((ans) => (
          <ReviewItem
            key={`${attempt.attempt_id}-${ans.question_id}`}
            examId={examId}
            attemptId={attempt.attempt_id}
            ans={ans}
            q={qById.get(ans.question_id)}
            n={(qIndex.get(ans.question_id) ?? 0) + 1}
            editable={editable}
            onSaved={load}
          />
        ))}
      </ol>

      <div className="t-columns">
        <section className="t-card t-col-3" aria-labelledby="oc-h" style={{ gap: 12 }}>
          <h2 id="oc-h">Overall comment</h2>
          <OverallComment key={attempt.attempt_id} examId={examId} attempt={attempt} onSaved={load} />
        </section>
        <section className="t-card t-col-2" aria-labelledby="ev-h" style={{ gap: 12 }}>
          <div className="t-card-head">
            <h2 id="ev-h">Lockdown events</h2>
            <Link
              to={`/teacher/exams/${encodeURIComponent(examId)}/submissions/${encodeURIComponent(attempt.attempt_id)}/timeline`}
              className="t-card-link"
            >
              Full session timeline →
            </Link>
          </div>
          {attempt.events.length === 0 ? (
            <p className="t-muted" style={{ margin: 0 }}>
              Nothing recorded.
            </p>
          ) : (
            <ul className="t-review-events">
              {attempt.events.map((ev) => (
                <li key={ev.event_id}>
                  <span>{eventLabel(ev.type)}</span>
                  {ev.detail && <span className="t-muted"> · {ev.detail}</span>}
                  <span className="t-small t-muted"> · {formatWhen(ev.server_time)}</span>
                  {ev.review !== "open" && (
                    <span className="t-small t-muted"> · {ev.review === "dismissed" ? "looked fine" : `note: ${ev.note}`}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

function ReviewItem({
  examId,
  attemptId,
  ans,
  q,
  n,
  editable,
  onSaved,
}: {
  examId: string;
  attemptId: string;
  ans: ResultAnswer;
  q: Question | undefined;
  n: number;
  editable: boolean;
  onSaved: () => Promise<void>;
}) {
  const max = ans.max_points ?? q?.points ?? 0;
  const rubric = q?.type === "essay" ? q.rubric ?? [] : [];
  const [points, setPoints] = useState(ans.pending ? "" : String(ans.points_awarded));
  const [comment, setComment] = useState(ans.comment ?? "");
  const [levels, setLevels] = useState<(number | null)[]>(
    rubric.map((_, i) => (ans.rubric_scores ? ans.rubric_scores[i] ?? null : null)),
  );
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const isExternal = q?.type === "external";
  const right = correctText(q);
  const inputId = `pts-${ans.question_id}`;
  const commentId = `cm-${ans.question_id}`;

  const pickLevel = (ci: number, level: number) => {
    const next = levels.map((v, i) => (i === ci ? level : v));
    setLevels(next);
    if (next.every((v) => v != null)) setPoints(String(next.reduce<number>((s, v) => s + (v ?? 0), 0)));
    setMsg(null);
  };

  async function save() {
    const parsed = Number(points.replace(",", "."));
    if (points.trim() === "" || !Number.isFinite(parsed) || parsed < 0 || parsed > max) {
      setMsg({ ok: false, text: `Enter points from 0 to ${num(max)}.` });
      return;
    }
    const useRubric = rubric.length > 0 && levels.every((v) => v != null);
    if (useRubric) {
      const sum = levels.reduce<number>((s, v) => s + (v ?? 0), 0);
      if (Math.abs(sum - parsed) > 1e-9) {
        setMsg({ ok: false, text: `The rubric adds up to ${num(sum)}. Change a level or clear the rubric.` });
        return;
      }
    }
    setBusy(true);
    setMsg(null);
    try {
      await teacherApi.saveGrade(examId, attemptId, ans.question_id, {
        points: parsed,
        comment,
        rubric_scores: useRubric ? (levels as number[]) : null,
      });
      setMsg({ ok: true, text: "Saved" });
      await onSaved();
    } catch (e) {
      setMsg({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    setMsg(null);
    try {
      await teacherApi.clearGrade(examId, attemptId, ans.question_id);
      setMsg({ ok: true, text: "Back to automatic" });
      await onSaved();
    } catch (e) {
      setMsg({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  const status = ans.pending ? (
    <Pill tone="orange">Needs grading</Pill>
  ) : isExternal ? (
    <Pill tone="green">Scored</Pill>
  ) : ans.correct ? (
    <Pill tone="green">{ans.manual ? "Full marks" : "Correct"}</Pill>
  ) : ans.points_awarded > 0 ? (
    <Pill tone="blue">Partial</Pill>
  ) : (
    <Pill tone="red">{ans.given == null && !isExternal ? "No answer" : "0 points"}</Pill>
  );

  return (
    <li className="t-card t-review-q" aria-labelledby={`q-${ans.question_id}`}>
      <div className="t-review-q-head">
        <span id={`q-${ans.question_id}`} className="t-review-q-title">
          {isExternal ? "Score from the exam website" : `Question ${n}`}
          <span className="t-muted"> · {typeLabel(q)} · {num(max)} {max === 1 ? "point" : "points"}</span>
        </span>
        {status}
      </div>
      {!isExternal && <QuestionImages images={q?.images} compact />}
      {!isExternal && q?.prompt && <p className="t-review-prompt">{q.prompt}</p>}
      {!isExternal && <TopicChips topics={q?.topics} />}
      {!isExternal && (
        <div className="t-review-answers">
          <div className="t-review-given">
            <span className="t-small t-muted">Student answer</span>
            {(ans.image || paperImage(ans.given)) && (
              <img
                className="t-review-scan"
                src={ans.image || paperImage(ans.given) || undefined}
                alt={`Scanned answer for question ${n}`}
              />
            )}
            <div className={q?.type === "essay" ? "t-review-essay" : "t-review-value"}>{formatGiven(q, ans.given)}</div>
          </div>
          {right && (
            <div className="t-review-correct">
              <span className="t-small t-muted">Correct answer</span>
              <div className="t-review-value">{right}</div>
            </div>
          )}
        </div>
      )}

      {editable && (
        <div className="t-review-grade">
          {rubric.length > 0 && (
            <div className="t-review-rubric">
              {rubric.map((r, ci) => (
                <div key={ci} className="t-review-crit">
                  <span className="t-small">
                    {r.label} <span className="t-muted">· {num(r.points)} pts</span>
                  </span>
                  <div className="t-seg" role="group" aria-label={r.label}>
                    {Array.from({ length: Math.floor(r.points) + 1 }, (_, lv) => (
                      <button
                        key={lv}
                        type="button"
                        className={levels[ci] === lv ? "on" : ""}
                        aria-pressed={levels[ci] === lv}
                        aria-label={`${r.label}: ${lv} of ${num(r.points)}`}
                        onClick={() => pickLevel(ci, lv)}
                      >
                        {lv}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="t-review-grade-row">
            <div className="t-field" style={{ width: 200 }}>
              <label htmlFor={inputId}>Points</label>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  id={inputId}
                  className="input"
                  inputMode="decimal"
                  value={points}
                  placeholder="—"
                  onChange={(e) => {
                    setPoints(e.target.value);
                    setMsg(null);
                  }}
                />
                <span className="t-muted" style={{ whiteSpace: "nowrap" }}>/ {num(max)}</span>
              </div>
            </div>
            <div className="t-field" style={{ flex: 1, minWidth: 220 }}>
              <label htmlFor={commentId}>Comment to student</label>
              <textarea
                id={commentId}
                className="textarea"
                rows={2}
                maxLength={5000}
                value={comment}
                onChange={(e) => {
                  setComment(e.target.value);
                  setMsg(null);
                }}
              />
            </div>
          </div>
          <div className="t-review-actions">
            <button type="button" className="btn btn-primary" onClick={() => void save()} disabled={busy}>
              {busy ? "Saving…" : "Save grade"}
            </button>
            {ans.manual && !isExternal && q?.type !== "essay" && (
              <button type="button" className="t-btn-outline" onClick={() => void reset()} disabled={busy}>
                Reset to automatic
              </button>
            )}
            {msg && (
              <span className={msg.ok ? "t-small t-ok" : "t-inline-error"} role={msg.ok ? "status" : "alert"}>
                {msg.text}
              </span>
            )}
          </div>
        </div>
      )}
      {!editable && ans.comment && <p className="t-small">Comment: {ans.comment}</p>}
    </li>
  );
}

function OverallComment({
  examId,
  attempt,
  onSaved,
}: {
  examId: string;
  attempt: ResultAttempt;
  onSaved: () => Promise<void>;
}) {
  const [text, setText] = useState(attempt.overall_comment ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const dirty = text !== (attempt.overall_comment ?? "");

  async function save() {
    setBusy(true);
    setMsg(null);
    try {
      await teacherApi.saveComment(examId, attempt.attempt_id, text);
      setMsg({ ok: true, text: "Saved" });
      await onSaved();
    } catch (e) {
      setMsg({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <label htmlFor="overall-comment" className="sr-only">
        Overall comment to {attempt.name}
      </label>
      <textarea
        id="overall-comment"
        className="textarea"
        rows={4}
        maxLength={2000}
        value={text}
        placeholder={`A note ${attempt.name} sees with their grade`}
        onChange={(e) => {
          setText(e.target.value);
          setMsg(null);
        }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button type="button" className="t-btn-outline" onClick={() => void save()} disabled={busy || !dirty}>
          {busy ? "Saving…" : "Save comment"}
        </button>
        {msg && (
          <span className={msg.ok ? "t-small t-ok" : "t-inline-error"} role={msg.ok ? "status" : "alert"}>
            {msg.text}
          </span>
        )}
      </div>
    </>
  );
}
