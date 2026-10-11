import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { scanApi, type QueueItem, type QueueQuestion, type ScanQueue } from "../../../api/scan";
import { scanAiApi, type AiRun, type Criterion } from "../../../api/scanAi";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, num, plural } from "../../../components/teacher/util";
import "../../../styles/scanAi.css";
import {
  LETTERS,
  bubbleText,
  confidenceText,
  digitKey,
  firstOpenQuestion,
  groupsFor,
  isFlagged,
  itemKey,
  itemsFor,
  keyAction,
  stepIndex,
  withDraft,
  withGrade,
  type QueueFilter,
} from "./queueLogic";
import {
  buildSave,
  firstErrorStep,
  flipStep,
  hasSuggestion,
  initialForm,
  minutes,
  setCriterion,
  setPoints,
  timeSummary,
  toggleCriterion,
  type ReviewForm,
} from "./reviewLogic";

function correctText(q: QueueQuestion): string | null {
  if (q.correct_answer == null) return null;
  if (q.type === "mcq" && typeof q.correct_answer === "number") {
    return `${LETTERS[q.correct_answer]} — ${q.options[q.correct_answer] ?? ""}`;
  }
  return String(q.correct_answer);
}

const FILTERS: [QueueFilter, string][] = [
  ["ungraded", "Not graded"],
  ["flagged", "Needs review"],
  ["low", "Low confidence"],
  ["all", "All"],
];

const STEP_WORD = { correct: "✓ Correct", error: "✗ Error", missing: "… Missing" } as const;

/** Step 4 of Scan & Grade: grade by question across students, side by side with the scan. */
export function QueueStep({ examId, onChanged }: { examId: string; onChanged?: () => void }) {
  const [queue, setQueue] = useState<ScanQueue | null>(null);
  const [qid, setQid] = useState<string | null>(null);
  const [filter, setFilter] = useState<QueueFilter>("ungraded");
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const lastIndex = useRef(0);
  const [formState, setFormState] = useState<{ key: string; form: ReviewForm } | null>(null);
  const openedAt = useRef<number>(Date.now());
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [run, setRun] = useState<AiRun | null>(null);
  const commentRef = useRef<HTMLTextAreaElement>(null);

  const reload = useCallback(async () => {
    const q = await scanApi.queue(examId);
    setQueue(q);
    setQid((cur) => cur ?? firstOpenQuestion(q));
    return q;
  }, [examId]);

  useEffect(() => {
    let alive = true;
    scanApi.queue(examId).then(
      (q) => {
        if (!alive) return;
        setQueue(q);
        setQid((cur) => cur ?? firstOpenQuestion(q));
      },
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [examId]);

  // AI run progress (whole exam)
  useEffect(() => {
    if (!run?.running) return;
    const t = setInterval(() => {
      scanAiApi.aiRun(examId).then((r) => {
        setRun(r);
        if (!r.running) reload().catch(() => undefined);
      }, () => undefined);
    }, 1500);
    return () => clearInterval(t);
  }, [run?.running, examId, reload]);

  const question = queue?.questions.find((q) => q.question_id === qid) ?? null;
  const criteria: Criterion[] = useMemo(() => question?.criteria ?? [], [question]);
  const items = useMemo(() => (queue && qid ? itemsFor(queue, qid, filter) : []), [queue, qid, filter]);
  const groups = useMemo(() => (queue && qid ? groupsFor(queue, qid) : []), [queue, qid]);
  const found = activeKey ? items.findIndex((i) => itemKey(i) === activeKey) : -1;
  const active = found >= 0 ? found : Math.min(lastIndex.current, Math.max(0, items.length - 1));
  lastIndex.current = active;
  const current: QueueItem | undefined = items[active];
  const currentKey = current ? itemKey(current) : null;

  const select = useCallback(
    (i: number) => {
      lastIndex.current = i;
      setActiveKey(items[i] ? itemKey(items[i]) : null);
    },
    [items],
  );

  useEffect(() => {
    if (currentKey !== activeKey) setActiveKey(currentKey);
  }, [currentKey, activeKey]);

  // the form belongs to one answer (and to its current draft/grade): a fresh one otherwise
  const formKey = current
    ? `${itemKey(current)}|${current.draft?.created_at ?? ""}|${current.grade?.graded_at ?? ""}`
    : null;
  const form: ReviewForm | null =
    current && formState && formState.key === formKey ? formState.form : current ? initialForm(current, criteria) : null;
  const setForm = useCallback(
    (f: ReviewForm) => {
      if (formKey) setFormState({ key: formKey, form: f });
    },
    [formKey],
  );
  useEffect(() => setError(null), [currentKey]);
  useEffect(() => {
    openedAt.current = Date.now();
    setZoom(1);
  }, [currentKey]);

  useEffect(() => {
    lastIndex.current = 0;
    setActiveKey(null);
  }, [qid, filter]);

  const accept = useCallback(async () => {
    if (!current || !form || !question) return;
    const res = buildSave(current, form, criteria, question.points, (Date.now() - openedAt.current) / 1000);
    if ("error" in res) {
      setError(res.error);
      return;
    }
    const keepsApproved = filter === "all" || filter === "approved";
    const advance = () => {
      if (keepsApproved) select(stepIndex(items.length, active, 1));
    };
    if ("skip" in res) {
      advance();
      return;
    }
    const it = current;
    setBusy(itemKey(it));
    setError(null);
    setNotice(null);
    try {
      const out = await scanApi.approve(examId, it.attempt_id, it.question_id, res.body);
      setQueue((q) => (q ? withGrade(q, it.attempt_id, it.question_id, out.grade) : q));
      advance();
      onChanged?.();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }, [current, form, question, criteria, filter, items.length, active, select, examId, onChanged]);

  const regrade = useCallback(async () => {
    if (!current) return;
    const it = current;
    setActiveKey(itemKey(it));
    setBusy(itemKey(it));
    setError(null);
    try {
      const res = await scanApi.regrade(examId, it.attempt_id, it.question_id);
      setQueue((q) => (q ? withDraft(q, it.attempt_id, it.question_id, res.draft) : q));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }, [current, examId]);

  const approveGroup = async (list: { attempt_id: string; question_id: string }[], key: string) => {
    setBusy(`group:${key}`);
    setError(null);
    try {
      const res = await scanApi.approveBulk(examId, list);
      await reload();
      setNotice(`Approved ${plural(res.approved, "answer")}${res.skipped ? ` (${res.skipped} skipped)` : ""}.`);
      onChanged?.();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  // one document listener; it always calls the handler of the latest render
  const onKeyRef = useRef<(ev: KeyboardEvent) => void>(() => undefined);
  onKeyRef.current = (ev: KeyboardEvent) => {
    if (!current || !form || busy) return;
    const d = digitKey(ev);
    if (d !== null) {
      if (question && d <= question.points) {
        ev.preventDefault();
        setForm(setPoints(form, String(d)));
      }
      return;
    }
    const action = keyAction(ev);
    if (!action) return;
    ev.preventDefault();
    if (action === "next" || action === "prev") select(stepIndex(items.length, active, action === "next" ? 1 : -1));
    else if (action === "approve") accept();
    else if (action === "edit") commentRef.current?.focus();
    else if (action === "regrade") regrade();
  };
  useEffect(() => {
    const h = (ev: KeyboardEvent) => onKeyRef.current(ev);
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, []);

  if (!queue) return error ? <div className="alert alert-error" role="alert">{error}</div> : <Loading label="Loading answers…" />;
  if (queue.items.length === 0)
    return (
      <div className="t-empty">
        <strong>No scanned answers yet</strong>
        Upload the scans first. Answers appear here as soon as pages are read.
      </div>
    );

  const toReview = queue.questions.reduce((s, q) => s + q.to_review, 0);
  const totalAll = queue.questions.reduce((s, q) => s + q.total, 0);
  const qDone = question ? question.approved + question.auto : 0;
  const typeOf = (id: string) => queue.questions.find((q) => q.question_id === id)?.type ?? "essay";
  const time = timeSummary(queue.items, typeOf);

  return (
    <div className="sc-queue">
      <nav className="sc-qlist" aria-label="Questions">
        <p className="t-muted sc-small">{toReview > 0 ? `${plural(toReview, "answer")} to grade` : "Everything is graded"}</p>
        {queue.questions.map((q) => (
          <button
            key={q.question_id}
            type="button"
            className="sc-qbtn"
            aria-current={q.question_id === qid ? "true" : undefined}
            onClick={() => setQid(q.question_id)}
          >
            <span className="sc-qbtn-title">
              Question {q.index + 1} <span className="t-muted">· {q.type === "mcq" ? "bubbles" : q.type}</span>
            </span>
            <span className="sc-qbtn-sub">
              {q.to_review > 0 ? <Pill tone="orange">{q.to_review} to grade</Pill> : <Pill tone="green">Done</Pill>}
              {q.low_confidence > 0 && <span className="sc-low">{q.low_confidence} unsure</span>}
            </span>
          </button>
        ))}
        <div className="sc-keys" aria-label="Keyboard shortcuts">
          <strong>Keys</strong>
          <span>
            <kbd>Enter</kbd> accept and next
          </span>
          <span>
            <kbd>0</kbd>–<kbd>9</kbd> set points
          </span>
          <span>
            <kbd>E</kbd> edit feedback
          </span>
          <span>
            <kbd>←</kbd>/<kbd>→</kbd> previous / next
          </span>
          <span>
            <kbd>R</kbd> AI again
          </span>
        </div>
      </nav>

      <section className="sc-qmain" aria-labelledby="sc-q-h">
        {toReview === 0 && totalAll > 0 && (
          <div className="rv-done" role="status">
            <strong>All {plural(totalAll, "answer")} are graded.</strong>
            {time.graded > 0 ? (
              <p>
                You spent {minutes(time.seconds)} on {plural(time.graded, "answer")} here. Grading them by hand usually takes
                about {minutes(time.estimate)} — roughly <strong>{minutes(time.saved)} saved</strong> (estimate).
              </p>
            ) : (
              <p>Release the grades in the next step.</p>
            )}
          </div>
        )}
        {question && (
          <header className="sc-qhead">
            <div className="sc-card-head">
              <h3 id="sc-q-h">
                Question {question.index + 1} <span className="t-muted">· {plural(question.points, "point")}</span>
              </h3>
              {queue.ai_enabled && (
                <button
                  type="button"
                  className="t-btn-outline"
                  disabled={busy !== null || !!run?.running}
                  onClick={async () => {
                    setError(null);
                    try {
                      const r = await scanAiApi.runAi(examId);
                      setRun(r);
                      setNotice(r.queued ? `AI is grading ${plural(r.queued, "answer")}…` : "Every answer already has an AI suggestion.");
                    } catch (e) {
                      setError(errorText(e));
                    }
                  }}
                >
                  {run?.running ? `AI grading ${run.done} / ${run.total}` : "Grade the rest with AI"}
                </button>
              )}
            </div>
            <p className="sc-prompt">{question.prompt}</p>
            {correctText(question) && (
              <p className="sc-key">
                <span className="t-muted">Answer key:</span> {correctText(question)}
              </p>
            )}
            {question.solution && (
              <details>
                <summary className="t-muted sc-small">Worked solution</summary>
                <p className="sc-prompt">{question.solution}</p>
              </details>
            )}
            {!queue.ai_enabled && (
              <p className="t-muted sc-small">AI is off: type the points (or tick the criteria) for each answer.</p>
            )}
            <div className="rv-progress" aria-label="Progress">
              <div
                className="rv-bar"
                role="progressbar"
                aria-label={`Question ${question.index + 1} graded`}
                aria-valuemin={0}
                aria-valuemax={question.total}
                aria-valuenow={qDone}
              >
                <span style={{ width: `${question.total ? Math.round((qDone / question.total) * 100) : 0}%` }} />
              </div>
              <span>
                {qDone} / {question.total} graded
              </span>
            </div>
          </header>
        )}

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}
        {notice && (
          <div className="alert alert-success" role="status">
            {notice}
          </div>
        )}

        <div className="t-seg sc-filter" role="group" aria-label="Show">
          {FILTERS.map(([f, label]) => {
            const n = queue && qid ? itemsFor(queue, qid, f).length : 0;
            return (
              <button key={f} type="button" className={filter === f ? "on" : ""} aria-pressed={filter === f} onClick={() => setFilter(f)}>
                {label} ({n})
              </button>
            );
          })}
        </div>

        {filter === "ungraded" && groups.length > 0 && (
          <div className="sc-groups" aria-label="Similar answers">
            {groups.map((g) => (
              <div key={g.key} className="sc-group">
                <span>
                  <strong>{g.count} similar answers</strong> · {g.label}
                  {g.key.startsWith("t:") && ` · suggested ${num(g.points)} pts`}
                </span>
                <button type="button" className="btn btn-secondary" disabled={busy !== null} onClick={() => approveGroup(g.items, g.key)}>
                  {busy === `group:${g.key}` ? "Approving…" : `Approve all ${g.count}`}
                </button>
              </div>
            ))}
          </div>
        )}

        {items.length === 0 || !current || !form || !question ? (
          <div className="t-empty">
            <strong>{filter === "all" ? "No answers" : "Nothing here"}</strong>
            {filter === "all" ? "" : "Pick another question or another filter."}
          </div>
        ) : (
          <>
            <ReviewPane
              item={current}
              question={question}
              criteria={criteria}
              form={form}
              setForm={setForm}
              busy={busy === itemKey(current)}
              zoom={zoom}
              setZoom={setZoom}
              commentRef={commentRef}
              onAccept={accept}
              onRegrade={regrade}
              position={`${active + 1} of ${items.length}`}
              onPrev={() => select(stepIndex(items.length, active, -1))}
              onNext={() => select(stepIndex(items.length, active, 1))}
            />
            <ol className="rv-strip" aria-label="Students">
              {items.map((it, i) => (
                <li key={itemKey(it)}>
                  <button
                    type="button"
                    className={`rv-chip${it.status !== "needs_review" ? " is-done" : ""}${isFlagged(it) ? " is-flag" : ""}`}
                    aria-current={i === active ? "true" : undefined}
                    onClick={() => select(i)}
                  >
                    {it.name}
                    {it.status === "approved" && it.grade ? <span>{num(it.grade.points)}</span> : null}
                    {it.status === "needs_review" && isFlagged(it) ? <span aria-label="needs review">⚑</span> : null}
                  </button>
                </li>
              ))}
            </ol>
          </>
        )}
      </section>
    </div>
  );
}

function ReviewPane({
  item,
  question,
  criteria,
  form,
  setForm,
  busy,
  zoom,
  setZoom,
  commentRef,
  onAccept,
  onRegrade,
  position,
  onPrev,
  onNext,
}: {
  item: QueueItem;
  question: QueueQuestion;
  criteria: Criterion[];
  form: ReviewForm;
  setForm: (f: ReviewForm) => void;
  busy: boolean;
  zoom: number;
  setZoom: (z: number) => void;
  commentRef: RefObject<HTMLTextAreaElement>;
  onAccept: () => void;
  onRegrade: () => void;
  position: string;
  onPrev: () => void;
  onNext: () => void;
}) {
  const d = item.draft;
  const sug = hasSuggestion(item);
  const bubbles = bubbleText(item);
  const firstErr = firstErrorStep(form.steps);
  const id = `rv-${item.attempt_id}-${item.question_id}`;
  const max = question.points;

  return (
    <article className="rv-pane" aria-labelledby={`${id}-who`} data-item={itemKey(item)}>
      <div className="rv-image">
        <div className="rv-image-tools">
          <button type="button" className="t-btn-outline" aria-label="Zoom out" onClick={() => setZoom(Math.max(0.5, zoom - 0.25))}>
            −
          </button>
          <span aria-live="polite">{Math.round(zoom * 100)}%</span>
          <button type="button" className="t-btn-outline" aria-label="Zoom in" onClick={() => setZoom(Math.min(3, zoom + 0.25))}>
            +
          </button>
          {item.page_image && (
            <a href={item.page_image} target="_blank" rel="noreferrer">
              Full page{item.page ? ` ${item.page}` : ""}
            </a>
          )}
        </div>
        <div className="rv-image-box">
          {item.image ? (
            <>
              <img src={item.image} alt={`Answer of ${item.name}`} style={{ width: `${zoom * 100}%`, maxWidth: "none" }} />
              {/* homework: an answer can span several pages */}
              {(item.images ?? []).slice(1).map((u, i) => (
                <img key={u} src={u} alt={`Answer of ${item.name}, page ${i + 2}`} style={{ width: `${zoom * 100}%`, maxWidth: "none" }} />
              ))}
            </>
          ) : (
            <span className="t-muted">No picture</span>
          )}
        </div>
      </div>

      <form
        className="rv-side"
        onSubmit={(e) => {
          e.preventDefault();
          onAccept();
        }}
      >
        <div className="rv-who">
          <strong id={`${id}-who`}>
            {item.name} <span className="t-muted">{item.username}</span>
          </strong>
          <span className="t-muted sc-small">{position}</span>
        </div>
        <div className="rv-badges">
          {item.status === "approved" && <Pill tone="green">Graded {num(item.grade?.points)} / {num(max)}</Pill>}
          {item.status === "auto" && <Pill tone="blue">Read automatically</Pill>}
          {item.status === "needs_review" && d?.source === "fallback" && <Pill tone="orange">Grade by hand</Pill>}
          {item.status === "needs_review" && sug && isFlagged(item) && <Pill tone="orange">Needs review</Pill>}
          {d && <span className={`sc-conf${d.low_confidence ? " is-low" : ""}`}>{confidenceText(d)}</span>}
        </div>
        {item.status === "needs_review" && sug && d?.review_reason && <p className="sc-low">{d.review_reason}</p>}
        {bubbles && <p className="sc-bubbles">{bubbles}</p>}

        <div className="rv-points">
          <label htmlFor={`${id}-pts`}>Points</label>
          <input
            id={`${id}-pts`}
            className="input"
            inputMode="decimal"
            value={form.points}
            onChange={(e) => setForm(setPoints(form, e.target.value))}
          />
          <span>/ {num(max)}</span>
          {sug && d && !item.grade && (
            <span className="t-muted sc-small">
              AI suggested {num(d.points)}
            </span>
          )}
        </div>

        {criteria.length > 0 && (
          <div>
            <p className="rv-section-h">Rubric</p>
            <ul className="rv-crit">
              {criteria.map((c) => {
                const v = form.crit[c.id] ?? 0;
                const note = d?.criteria?.find((x) => x.id === c.id)?.note;
                return (
                  <li key={c.id}>
                    <input
                      type="checkbox"
                      id={`${id}-c-${c.id}`}
                      checked={v >= c.points && c.points > 0}
                      onChange={(e) => setForm(toggleCriterion(form, c, e.target.checked))}
                    />
                    <label htmlFor={`${id}-c-${c.id}`}>{c.label}</label>
                    <CritInput
                      label={`Points for ${c.label} (out of ${num(c.points)})`}
                      value={v}
                      onValue={(n) => setForm(setCriterion(form, c, n))}
                    />
                    {note ? <span className="rv-crit-note">{note}</span> : null}
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {form.steps.length > 0 && (
          <div>
            <p className="rv-section-h">Steps{firstErr ? ` · first problem at step ${firstErr}` : ""}</p>
            <ol className="rv-steps">
              {form.steps.map((s) => (
                <li key={s.n} className={`rv-step${s.n === firstErr ? " is-first" : ""}`}>
                  <button
                    type="button"
                    className="rv-step-btn"
                    data-status={s.status}
                    aria-label={`Step ${s.n}: ${s.status}. Change`}
                    onClick={() => setForm(flipStep(form, s.n))}
                  >
                    {STEP_WORD[s.status]}
                  </button>
                  <span className="rv-step-text">
                    {s.text || <em className="t-muted">(not written)</em>}
                    {s.note && <span className="rv-step-note">{s.note}</span>}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        )}
        {form.steps.length === 0 && d?.transcript && (
          <p className="sc-transcript">
            <span className="t-muted">Reads as:</span> “{d.transcript}”
          </p>
        )}

        <div className="t-field">
          <label htmlFor={`${id}-fb`}>Feedback for the student</label>
          <textarea
            id={`${id}-fb`}
            ref={commentRef}
            className="textarea"
            rows={3}
            value={form.comment}
            onChange={(e) => setForm({ ...form, comment: e.target.value, touched: { ...form.touched, comment: true } })}
          />
        </div>
        {d?.teacher_note && (
          <p className="t-muted sc-small">
            <strong>Note for you:</strong> {d.teacher_note}
          </p>
        )}
        {(form.misconception || d?.misconception) && (
          <p className="sc-tags">
            <span className="t-muted sc-small">Misconception:</span>
            {form.misconception ? (
              <span className="sc-tag">
                {form.misconception.tag === "other" ? form.misconception.text : form.misconception.tag}
              </span>
            ) : (
              <span className="t-muted sc-small">none</span>
            )}
            {form.misconception && (
              <button
                type="button"
                className="t-btn-outline"
                onClick={() => setForm({ ...form, misconception: null, touched: { ...form.touched, misconception: true } })}
              >
                Remove tag
              </button>
            )}
          </p>
        )}

        <div className="sc-inline">
          <button type="submit" className="btn btn-primary" disabled={busy} aria-keyshortcuts="Enter">
            {busy ? "Saving…" : item.status === "approved" ? "Save and next" : "Accept and next"}
          </button>
          <button type="button" className="t-btn-outline" onClick={onPrev} aria-keyshortcuts="ArrowLeft">
            Previous
          </button>
          <button type="button" className="t-btn-outline" onClick={onNext} aria-keyshortcuts="ArrowRight">
            Next
          </button>
          <button type="button" className="t-btn-outline" onClick={onRegrade} disabled={busy} aria-keyshortcuts="R">
            AI again
          </button>
        </div>
      </form>
    </article>
  );
}

/** Per-criterion points: free typing ("0.", "") without the value jumping around. */
function CritInput({ label, value, onValue }: { label: string; value: number; onValue: (n: number) => void }) {
  const [text, setText] = useState(String(value));
  useEffect(() => {
    setText((t) => (Number(t) === value && t.trim() !== "" ? t : String(value)));
  }, [value]);
  return (
    <input
      className="input"
      aria-label={label}
      inputMode="decimal"
      value={text}
      onChange={(e) => {
        const v = e.target.value;
        setText(v);
        const n = Number(v);
        if (v.trim() !== "" && Number.isFinite(n) && !v.endsWith(".")) onValue(n);
      }}
    />
  );
}
