import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { paperImage } from "../../lib/paperAnswer";
import { urlHost } from "../../components/teacher/externalValidation";
import { Link, useParams } from "react-router-dom";
import { teacherApi } from "../../api/client";
import type { Exam, GradingItem, GradingQueue, GradingQuestion } from "../../api/types";
import { Loading } from "../../components/Loading";
import { QuestionImages } from "../../components/QuestionImages";
import { errorText, num, plural } from "../../components/teacher/util";
import { TYPE_LABEL } from "./examDraft";
import {
  buildGrade,
  countWords,
  initialForm,
  isBlankAnswer,
  isPending,
  nextPendingIndex,
  questionCounts,
  rubricTotal,
  type GradeForm,
} from "./gradingLogic";

export default function GradingPage() {
  const { id = "" } = useParams();
  const [queue, setQueue] = useState<GradingQueue | null>(null);
  const [exam, setExam] = useState<Exam | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [qid, setQid] = useState<string | null>(null);
  const [pos, setPos] = useState(0);
  const [hideNames, setHideNames] = useState(true);

  useEffect(() => {
    let alive = true;
    setQueue(null);
    setError(null);
    teacherApi.grading(id).then(
      (q) => {
        if (!alive) return;
        setQueue(q);
        // Start on the first question that has pending answers, at its first pending answer.
        const byId = new Map(q.questions.map((x) => [x.question_id, x] as const));
        const firstPending = q.items.find((it) => isPending(it, byId.get(it.question_id)));
        const startQ = firstPending?.question_id ?? q.items[0]?.question_id ?? null;
        setQid(startQ);
        if (startQ) {
          const list = q.items.filter((it) => it.question_id === startQ);
          const p = list.findIndex((it) => isPending(it, byId.get(startQ)));
          setPos(Math.max(0, p));
        }
      },
      (e) => alive && setError(errorText(e)),
    );
    // Optional: the exam gives accepted answers for short questions. Failure is harmless.
    teacherApi.getExam(id).then(
      (e) => alive && setExam(e),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [id]);

  if (error)
    return (
      <>
        <Link to={`/teacher/exams/${id}?tab=results`} className="t-back">
          ← Results
        </Link>
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      </>
    );
  if (!queue) return <Loading label="Loading answers…" />;

  return (
    <GradingWorkspace
      examId={id}
      queue={queue}
      exam={exam}
      qid={qid}
      pos={pos}
      hideNames={hideNames}
      onHideNames={setHideNames}
      onSelect={(q, p) => {
        setQid(q);
        setPos(p);
      }}
      onItemGraded={(item) =>
        setQueue((cur) =>
          cur
            ? {
                ...cur,
                items: cur.items.map((it) =>
                  it.attempt_id === item.attempt_id && it.question_id === item.question_id ? item : it,
                ),
              }
            : cur,
        )
      }
    />
  );
}

interface WorkspaceProps {
  examId: string;
  queue: GradingQueue;
  exam: Exam | null;
  qid: string | null;
  pos: number;
  hideNames: boolean;
  onHideNames: (v: boolean) => void;
  onSelect: (qid: string, pos: number) => void;
  onItemGraded: (item: GradingItem) => void;
}

function GradingWorkspace({ examId, queue, exam, qid, pos, hideNames, onHideNames, onSelect, onItemGraded }: WorkspaceProps) {
  const counts = useMemo(() => questionCounts(queue.questions, queue.items), [queue]);
  const qById = useMemo(() => new Map(queue.questions.map((q) => [q.question_id, q] as const)), [queue.questions]);
  const pickable = queue.questions.filter((q) => (counts[q.question_id]?.total ?? 0) > 0);
  const totalPending = Object.values(counts).reduce((s, c) => s + c.pending, 0);
  const question = qid ? qById.get(qid) : undefined;
  const list = useMemo(() => queue.items.filter((it) => it.question_id === qid), [queue.items, qid]);
  const item = list[Math.min(pos, list.length - 1)];
  const c = qid ? counts[qid] : undefined;
  const title = queue.course ? `${queue.course} ${queue.title}` : queue.title;

  return (
    <>
      <header className="t-grade-head">
        <Link to={`/teacher/exams/${examId}?tab=results`} className="t-back" style={{ alignSelf: "center" }}>
          ← Results
        </Link>
        <div className="t-head-text">
          <h1 style={{ margin: 0, fontSize: 17, fontWeight: 700 }}>Grading · {title}</h1>
          <span className="t-small t-muted">
            {totalPending > 0 ? `${plural(totalPending, "answer")} need you` : "Everything is graded"}
            {queue.grades_released && " · grades are already released, changes show to students"}
          </span>
        </div>
        <label className="t-check">
          <input type="checkbox" checked={hideNames} onChange={(e) => onHideNames(e.target.checked)} />
          Hide names
        </label>
        {question && c && c.total > 0 && (
          <div className="t-progress">
            <span className="t-small t-muted" id="grade-progress-label">
              Q{question.index + 1} progress · {c.graded} of {c.total} graded
            </span>
            <div
              className="t-progress-track"
              role="progressbar"
              aria-labelledby="grade-progress-label"
              aria-valuemin={0}
              aria-valuemax={c.total}
              aria-valuenow={c.graded}
            >
              <div className="t-progress-fill" style={{ width: `${Math.round((c.graded / c.total) * 100)}%` }} />
            </div>
          </div>
        )}
      </header>

      {pickable.length === 0 ? (
        <div className="t-card">
          <div className="t-empty">
            <strong>Nothing to grade</strong>
            Essays and short answers that did not match your accepted answers appear here after students submit.
          </div>
        </div>
      ) : (
        <div className="t-grade-layout">
          <nav aria-labelledby="qpick-h" className="t-qpicker">
            <h2 id="qpick-h">Needs grading</h2>
            {pickable.map((q) => {
              const qc = counts[q.question_id];
              const qList = queue.items.filter((it) => it.question_id === q.question_id);
              const firstPending = qList.findIndex((it) => isPending(it, q));
              return (
                <button
                  key={q.question_id}
                  type="button"
                  className="t-qpick"
                  aria-current={q.question_id === qid ? "true" : undefined}
                  onClick={() => onSelect(q.question_id, Math.max(0, firstPending))}
                >
                  <span className="t-qpick-title">
                    Q{q.index + 1} · {TYPE_LABEL[q.type]}
                  </span>
                  <span className={`t-qpick-sub${qc.pending === 0 ? " done" : ""}`}>
                    {qc.pending === 0
                      ? "All graded"
                      : q.type === "short"
                        ? `${qc.pending} no exact match`
                        : `${qc.pending} left`}
                  </span>
                </button>
              );
            })}
          </nav>
          {question && item ? (
            <GradeItem
              key={`${item.attempt_id}:${item.question_id}`}
              examId={examId}
              question={question}
              item={item}
              exam={exam}
              position={Math.min(pos, list.length - 1)}
              count={list.length}
              hideNames={hideNames}
              onPrev={() => onSelect(question.question_id, Math.max(0, pos - 1))}
              onNext={() => onSelect(question.question_id, Math.min(list.length - 1, pos + 1))}
              onSaved={(graded) => {
                onItemGraded(graded);
                const updated = list.map((it) =>
                  it.attempt_id === graded.attempt_id && it.question_id === graded.question_id ? graded : it,
                );
                const next = nextPendingIndex(updated, Math.min(pos, list.length - 1), question);
                if (next != null) {
                  onSelect(question.question_id, next);
                  return null;
                }
                // This question is done: suggest the next question that still has work.
                const nextQ = pickable.find(
                  (q) =>
                    q.question_id !== question.question_id &&
                    queue.items.some((it) => it.question_id === q.question_id && isPending(it, q)),
                );
                return nextQ ?? undefined;
              }}
              onGoQuestion={(q) => {
                const qList = queue.items.filter((it) => it.question_id === q.question_id);
                onSelect(q.question_id, Math.max(0, qList.findIndex((it) => isPending(it, q))));
              }}
            />
          ) : (
            <div className="t-grade-main">
              <div className="t-empty">Pick a question on the left.</div>
            </div>
          )}
        </div>
      )}
    </>
  );
}

interface ItemProps {
  examId: string;
  question: GradingQuestion;
  item: GradingItem;
  exam: Exam | null;
  position: number;
  count: number;
  hideNames: boolean;
  onPrev: () => void;
  onNext: () => void;
  /** Returns null if it moved on, a question to suggest when this one is finished, or undefined when all done. */
  onSaved: (item: GradingItem) => GradingQuestion | null | undefined;
  onGoQuestion: (q: GradingQuestion) => void;
}

export function GradeItem({
  examId,
  question,
  item,
  exam,
  position,
  count,
  hideNames,
  onPrev,
  onNext,
  onSaved,
  onGoQuestion,
}: ItemProps) {
  const [form, setForm] = useState<GradeForm>(() => initialForm(item, question));
  const [active, setActive] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [finished, setFinished] = useState<null | { next?: GradingQuestion }>(null);
  const rubric = question.rubric ?? [];
  const hasRubric = rubric.length > 0;
  const total = hasRubric ? rubricTotal(form.scores) : Number(form.points.replace(",", ".")) || 0;
  const commentId = `grade-comment`;

  const examQ = exam?.questions.find((q) => q.id === question.question_id);
  const text = typeof item.answer === "string" ? item.answer : item.answer == null ? "" : String(item.answer);
  const words = item.word_count ?? countWords(text);

  const save = useCallback(async () => {
    if (saving) return;
    const built = buildGrade(form, question);
    if (!built.ok) {
      setError(built.error);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await teacherApi.saveGrade(examId, item.attempt_id, item.question_id, built.input);
      const grade = res.grade ?? { ...built.input, graded_at: new Date().toISOString() };
      const result = onSaved({ ...item, grade });
      if (result !== null) setFinished({ next: result ?? undefined });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  }, [examId, form, item, onSaved, question, saving]);

  // Keyboard: Enter saves (not inside the comment box); number keys pick a level for the active criterion.
  const saveRef = useRef(save);
  saveRef.current = save;
  const activeRef = useRef(active);
  activeRef.current = active;
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName;
      if (e.key === "Enter") {
        if (tag === "TEXTAREA") return;
        // Let ordinary buttons/links (Previous, Next, nav) do their own thing.
        if ((tag === "BUTTON" || tag === "A") && !t?.dataset.level) return;
        if (tag === "INPUT" && (t as HTMLInputElement).type === "checkbox") return;
        e.preventDefault();
        void saveRef.current();
        return;
      }
      if (hasRubric && /^[0-9]$/.test(e.key) && tag !== "TEXTAREA" && tag !== "INPUT" && tag !== "SELECT") {
        const level = Number(e.key);
        const ci = activeRef.current;
        if (rubric[ci] && level <= rubric[ci].points) {
          e.preventDefault();
          setForm((f) => ({ ...f, scores: f.scores.map((s, j) => (j === ci ? level : s)) }));
          setError(null);
        }
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [hasRubric, rubric]);

  const who = hideNames ? `Student ${item.anon_id}` : `${item.name} (${item.username})`;
  const autoText =
    question.type === "short"
      ? `Auto-graded ${num(item.auto_points ?? 0)} / ${question.points}: no exact match with your accepted answers.`
      : null;

  return (
    <>
      <article className="t-grade-main" aria-labelledby="grade-prompt">
        <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
          <span className="t-small" style={{ fontWeight: 600, color: "var(--muted)" }}>
            Q{question.index + 1} · {plural(question.points, "point")} · answer {position + 1} of {count}
          </span>
          <span className="t-anon" aria-label={hideNames ? `Anonymous student ${item.anon_id}` : undefined}>
            {who}
          </span>
        </div>
        {question.type !== "external" && <QuestionImages images={question.images ?? examQ?.images} compact />}
        <h2 id="grade-prompt" className="t-grade-prompt">
          {question.type === "external" ? "Score from the exam website" : question.prompt}
        </h2>
        {question.type === "external" ? (
          <p className="t-essay">
            This student's answers are on{" "}
            <a href={exam?.external_url ?? undefined} target="_blank" rel="noreferrer">
              {urlHost(exam?.external_url) ?? "the exam website"}
            </a>
            . Check their work there, then enter the score out of {question.points}.
          </p>
        ) : paperImage(item.answer) ? (
          <img className="t-review-scan" src={paperImage(item.answer) ?? undefined} alt="Scanned answer" />
        ) : isBlankAnswer(item.answer) ? (
          <p className="t-essay empty">No answer was given.</p>
        ) : (
          <p className="t-essay">{text}</p>
        )}
        <span className="t-small t-muted">
          {question.type === "essay" ? `${plural(words, "word")}` : question.type === "external" ? "Exam website" : "Short answer"}
          {examQ?.min_words != null && question.type === "essay" && ` · min ${examQ.min_words}`}
          {examQ?.max_words != null && question.type === "essay" && ` · max ${examQ.max_words}`}
          {" · "}
          {item.flag_count > 0 ? plural(item.flag_count, "integrity flag") : "no integrity flags"}
          {item.grade && " · graded"}
        </span>
        {autoText && (
          <div className="t-autobox">
            {autoText}
            {examQ && examQ.accepted_answers.length > 0 && (
              <>
                {" "}
                Accepted: <strong>{examQ.accepted_answers.join(", ")}</strong>
              </>
            )}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" className="t-btn-outline" onClick={onPrev} disabled={position <= 0}>
            ← Previous
          </button>
          <button type="button" className="t-btn-outline" onClick={onNext} disabled={position >= count - 1}>
            Next →
          </button>
          <Link to={`/teacher/exams/${examId}/submissions/${item.attempt_id}`} className="t-btn-outline">
            Open full submission
          </Link>
        </div>
      </article>

      <aside aria-labelledby="rub-h" className="t-grade-aside">
        <h2 id="rub-h">{hasRubric ? "Rubric" : "Score"}</h2>

        {hasRubric ? (
          rubric.map((r, ci) => (
            <fieldset
              key={ci}
              className="t-criterion"
              onFocus={() => setActive(ci)}
              aria-describedby={ci === active ? "keys-hint" : undefined}
            >
              <legend>
                {r.label} · {plural(r.points, "pt")}
              </legend>
              <div className="t-levels">
                {Array.from({ length: r.points + 1 }, (_, level) => (
                  <button
                    key={level}
                    type="button"
                    className="t-level"
                    data-level={level}
                    aria-pressed={form.scores[ci] === level}
                    aria-label={`${r.label}: ${level} of ${r.points}`}
                    onClick={() => {
                      setActive(ci);
                      setError(null);
                      setForm((f) => ({ ...f, scores: f.scores.map((s, j) => (j === ci ? level : s)) }));
                    }}
                  >
                    {level}
                  </button>
                ))}
              </div>
            </fieldset>
          ))
        ) : (
          <div className="t-field">
            <label htmlFor="grade-points">Points (0 – {question.points})</label>
            <input
              id="grade-points"
              className="input mono"
              inputMode="decimal"
              value={form.points}
              onChange={(e) => {
                setError(null);
                setForm((f) => ({ ...f, points: e.target.value }));
              }}
              style={{ maxWidth: 140 }}
              autoFocus
            />
          </div>
        )}

        <div className="t-field">
          <label htmlFor={commentId}>Comment to student</label>
          <textarea
            id={commentId}
            className="textarea"
            rows={3}
            value={form.comment}
            onChange={(e) => setForm((f) => ({ ...f, comment: e.target.value }))}
          />
        </div>

        {error && (
          <span className="t-inline-error" role="alert">
            {error}
          </span>
        )}
        {finished && (
          <div className="alert alert-success" role="status">
            All answers to Q{question.index + 1} are graded.{" "}
            {finished.next ? (
              <button type="button" className="t-link-btn" onClick={() => onGoQuestion(finished.next!)}>
                Go to Q{finished.next.index + 1}
              </button>
            ) : (
              <Link to={`/teacher/exams/${examId}?tab=results`} className="t-card-link">
                Back to results
              </Link>
            )}
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <span className="t-total" aria-live="polite" aria-label={`Total ${num(total)} of ${question.points}`}>
            {num(total)} <small>/ {question.points}</small>
          </span>
          <button type="button" className="t-save-next" onClick={() => void save()} disabled={saving}>
            {saving ? "Saving…" : "Save and next ↵"}
          </button>
        </div>
        <span className="t-keys" id="keys-hint">
          {hasRubric ? "Keys: 0–9 pick level · Tab next criterion · Enter save" : "Enter saves · Tab moves between fields"}
        </span>
      </aside>
    </>
  );
}
