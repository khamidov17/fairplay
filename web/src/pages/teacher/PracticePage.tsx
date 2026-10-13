// Slice 6 H: practice assignments. The teacher picks students, sees their weak topics,
// generates a draft set per student (variations of the teacher's own questions, math
// answers verified with SymPy on the server), reviews/edits items, then approves & sends.
// Practice never counts toward grades.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { practiceApi } from "../../api/client";
import type {
  PracticeBankQuestion,
  PracticeItem,
  PracticeItemEdit,
  PracticeSet,
  PracticeSetSummary,
  PracticeStudent,
  PracticeStudents,
} from "../../api/types";
import { Loading } from "../../components/Loading";
import { QuestionImages } from "../../components/QuestionImages";
import { Pill } from "../../components/teacher/TeacherLayout";
import { errorText, pct, plural } from "../../components/teacher/util";
import "../../styles/practice.css";

// ---------------------------------------------------------------- helpers (exported for tests)

export function itemBadge(item: Pick<PracticeItem, "status" | "kind">): { label: string; tone: string } {
  if (item.status === "needs_answer") return { label: "Needs your answer", tone: "orange" };
  if (item.status === "verified") return { label: "Verified with SymPy", tone: "green" };
  if (item.kind === "ai_rewrite") return { label: "AI rewrite — check it", tone: "blue" };
  if (item.kind === "teacher_edit") return { label: "Edited by you", tone: "grey" };
  if (item.kind === "shuffled") return { label: "Options shuffled", tone: "grey" };
  return { label: "Your original question", tone: "grey" };
}

/** The item's answer key in plain words. */
export function answerText(item: PracticeItem): string {
  switch (item.type) {
    case "mcq": {
      const ci = item.correct_index;
      return ci != null && item.options[ci] != null ? `${String.fromCharCode(65 + ci)}. ${item.options[ci]}` : "—";
    }
    case "numeric":
      if (item.answer_number == null) return "—";
      return item.tolerance ? `${item.answer_number} (± ${item.tolerance})` : String(item.answer_number);
    case "short":
      return item.accepted_answers.length ? item.accepted_answers.join(" · ") : "—";
    default:
      return "Self-check (not marked automatically)";
  }
}

const TYPE_LABEL: Record<string, string> = {
  mcq: "Multiple choice",
  numeric: "Number",
  short: "Short answer",
  essay: "Essay",
};

/** ?student=alice&student=bob or ?student=alice,bob ; ?topic=x likewise. */
export function readPrefill(params: URLSearchParams): { students: string[]; topics: string[] } {
  const split = (key: string) =>
    params
      .getAll(key)
      .flatMap((v) => v.split(","))
      .map((v) => v.trim())
      .filter(Boolean);
  return { students: split("student"), topics: split("topic").map((t) => t.toLowerCase()) };
}

function progressText(p: { answered: number; correct: number; total: number }): string {
  if (p.answered === 0) return `Not started · ${plural(p.total, "item")}`;
  return `${p.answered} of ${p.total} answered · ${p.correct} correct`;
}

// ---------------------------------------------------------------- page

export default function PracticePage() {
  const [params] = useSearchParams();
  const prefill = useMemo(() => readPrefill(params), [params]);

  const [data, setData] = useState<PracticeStudents | null>(null);
  const [sets, setSets] = useState<PracticeSetSummary[] | null>(null);
  const [open, setOpen] = useState<Record<string, PracticeSet>>({});
  const [error, setError] = useState<string | null>(null);

  const [picked, setPicked] = useState<string[]>(prefill.students);
  const [useWeak, setUseWeak] = useState(prefill.topics.length === 0);
  const [topics, setTopics] = useState<string[]>(prefill.topics);
  const [count, setCount] = useState(5);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [skipped, setSkipped] = useState<{ username: string; reason: string }[]>([]);

  const reloadSets = useCallback(async () => {
    try {
      setSets(await practiceApi.sets());
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    let alive = true;
    practiceApi.students().then(
      (d) => alive && setData(d),
      (e) => alive && setError(errorText(e)),
    );
    practiceApi.sets().then(
      (s) => alive && setSets(s),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, []);

  const students = useMemo(() => {
    const q = search.trim().toLowerCase();
    const all = data?.students ?? [];
    return q ? all.filter((s) => s.name.toLowerCase().includes(q) || s.username.toLowerCase().includes(q)) : all;
  }, [data, search]);

  const toggleStudent = (u: string) =>
    setPicked((cur) => (cur.includes(u) ? cur.filter((x) => x !== u) : [...cur, u]));
  const toggleTopic = (t: string) => setTopics((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));

  const canGenerate = picked.length > 0 && (useWeak || topics.length > 0) && !busy;

  const generate = async () => {
    setBusy(true);
    setError(null);
    setSkipped([]);
    try {
      const res = await practiceApi.generate({
        usernames: picked,
        topics: useWeak ? undefined : topics,
        count: Math.min(20, Math.max(1, count || 1)),
      });
      setSkipped(res.skipped);
      setOpen((cur) => {
        const next = { ...cur };
        for (const s of res.sets) next[s.id] = s;
        return next;
      });
      await reloadSets();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const updateSet = (s: PracticeSet) => {
    setOpen((cur) => ({ ...cur, [s.id]: s }));
    setSets((cur) => (cur ? cur.map((x) => (x.id === s.id ? { ...x, ...s } : x)) : cur));
  };

  const review = async (id: string) => {
    try {
      updateSet(await practiceApi.set(id));
    } catch (e) {
      setError(errorText(e));
    }
  };

  const removeSet = async (id: string) => {
    if (!window.confirm("Delete this practice set?")) return;
    try {
      await practiceApi.deleteSet(id);
      setOpen((cur) => {
        const next = { ...cur };
        delete next[id];
        return next;
      });
      setSets((cur) => (cur ? cur.filter((x) => x.id !== id) : cur));
    } catch (e) {
      setError(errorText(e));
    }
  };

  const drafts = (sets ?? []).filter((s) => s.status !== "sent");
  const sent = (sets ?? []).filter((s) => s.status === "sent");

  return (
    <>
      <header className="t-page-head">
        <div className="t-head-text">
          <h1>Practice</h1>
          <p>Extra practice built from your own questions, in each student’s weak topics. Practice never counts toward grades.</p>
        </div>
      </header>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      {!data ? (
        !error && <Loading label="Loading students…" />
      ) : (
        <section className="t-card pr-make" aria-labelledby="pr-make-title">
          <div className="t-card-head">
            <h2 id="pr-make-title">Make practice</h2>
            <span className="t-muted t-small">
              {data.ai_enabled
                ? "AI rewrites questions; every math answer is checked with SymPy."
                : "AI is off: number questions get new numbers (checked with SymPy); other questions come back as review."}
            </span>
          </div>
          <div className="pr-make-grid">
            <fieldset className="pr-students">
              <legend>Students</legend>
              <label className="pr-search">
                <span className="sr-only">Search students</span>
                <input
                  className="input"
                  type="search"
                  placeholder="Search name or username"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
              {students.length === 0 ? (
                <p className="t-muted t-small">No students found.</p>
              ) : (
                <ul className="pr-student-list">
                  {students.map((s) => (
                    <StudentRow key={s.username} s={s} checked={picked.includes(s.username)} onToggle={toggleStudent} />
                  ))}
                </ul>
              )}
            </fieldset>

            <div className="pr-options">
              <fieldset className="pr-topic-mode">
                <legend>Topics</legend>
                <label className="pr-radio">
                  <input type="radio" name="pr-topic-mode" checked={useWeak} onChange={() => setUseWeak(true)} />
                  Each student’s weak topics
                </label>
                <label className="pr-radio">
                  <input type="radio" name="pr-topic-mode" checked={!useWeak} onChange={() => setUseWeak(false)} />
                  Choose topics
                </label>
                {!useWeak && (
                  <div className="pr-chips" role="group" aria-label="Topics to practise">
                    {data.topics.length === 0 && <span className="t-muted t-small">Your questions have no topics yet.</span>}
                    {data.topics.map((t) => (
                      <button
                        key={t.topic}
                        type="button"
                        className="pr-chip"
                        aria-pressed={topics.includes(t.topic)}
                        onClick={() => toggleTopic(t.topic)}
                      >
                        {t.topic} <span className="pr-chip-n">{t.question_count}</span>
                      </button>
                    ))}
                  </div>
                )}
              </fieldset>
              <div className="t-field">
                <label htmlFor="pr-count">Items per student</label>
                <input
                  id="pr-count"
                  className="input pr-count"
                  type="number"
                  min={1}
                  max={20}
                  value={count}
                  onChange={(e) => setCount(Number(e.target.value))}
                />
              </div>
              <button type="button" className="btn btn-primary" disabled={!canGenerate} onClick={generate}>
                {busy ? "Making practice…" : `Make practice for ${plural(picked.length, "student")}`}
              </button>
              {skipped.length > 0 && (
                <ul className="pr-skipped" aria-label="Students skipped">
                  {skipped.map((s) => (
                    <li key={s.username}>
                      <strong>{s.username}</strong>: {s.reason}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </section>
      )}

      <section aria-labelledby="pr-drafts-title" className="pr-section">
        <h2 id="pr-drafts-title" className="pr-h2">
          To review {drafts.length > 0 && <span className="t-muted">({drafts.length})</span>}
        </h2>
        {sets === null ? (
          <Loading label="Loading practice sets…" />
        ) : drafts.length === 0 ? (
          <div className="t-empty">
            <strong>No drafts</strong>
            Pick students above and make practice. You check every item before anything is sent.
          </div>
        ) : (
          drafts.map((s) =>
            open[s.id] ? (
              <SetEditor key={s.id} set={open[s.id]!} onChange={updateSet} onDelete={removeSet} onError={setError} />
            ) : (
              <div key={s.id} className="t-card pr-set-row">
                <div>
                  <strong>{s.name ?? s.username}</strong> · {s.title}
                  <div className="t-small t-muted">
                    {plural(s.item_count, "item")}
                    {s.needs_answer > 0 && ` · ${s.needs_answer} need your answer`}
                  </div>
                </div>
                <button type="button" className="t-btn-outline" onClick={() => review(s.id)}>
                  Review
                </button>
              </div>
            ),
          )
        )}
      </section>

      <section aria-labelledby="pr-sent-title" className="pr-section">
        <h2 id="pr-sent-title" className="pr-h2">
          Sent
        </h2>
        {sets !== null && sent.length === 0 ? (
          <div className="t-empty">
            <strong>Nothing sent yet</strong>
            Sent sets appear here with each student’s progress.
          </div>
        ) : sent.length > 0 ? (
          <div className="t-card">
            <div className="t-table-scroll">
              <table className="t-table">
                <thead>
                  <tr>
                    <th scope="col">Student</th>
                    <th scope="col">Practice</th>
                    <th scope="col">Sent</th>
                    <th scope="col">Progress</th>
                  </tr>
                </thead>
                <tbody>
                  {sent.map((s) => (
                    <tr key={s.id}>
                      <td>{s.name ?? s.username}</td>
                      <td>{s.title}</td>
                      <td>{s.sent_at ? new Date(s.sent_at).toLocaleDateString() : "—"}</td>
                      <td>
                        <div className="pr-progress-cell">
                          <span>{progressText(s.progress)}</span>
                          <ProgressBar value={s.progress.answered} max={s.progress.total} label={`${s.name ?? s.username} progress`} />
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </section>
    </>
  );
}

function ProgressBar({ value, max, label }: { value: number; max: number; label: string }) {
  const p = max ? Math.round((value / max) * 100) : 0;
  return (
    <div className="pr-bar" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
      <span style={{ width: `${p}%` }} />
    </div>
  );
}

function StudentRow({ s, checked, onToggle }: { s: PracticeStudent; checked: boolean; onToggle: (u: string) => void }) {
  const id = `pr-stu-${s.username}`;
  return (
    <li className={`pr-student${checked ? " on" : ""}`}>
      <input id={id} type="checkbox" checked={checked} onChange={() => onToggle(s.username)} />
      <label htmlFor={id}>
        <span className="pr-student-name">{s.name}</span>
        <span className="pr-weak">
          {s.weak.length === 0 ? (
            <span className="t-muted">No graded answers yet</span>
          ) : (
            <>
              <span className="t-muted">{s.weak_is_fallback ? "Lowest:" : "Weak:"}</span>
              {s.weak.map((w) => (
                <span key={w.topic} className="pr-topic">
                  {w.topic} <span className="pr-topic-pct">{pct(w.avg_percent)}</span>
                </span>
              ))}
            </>
          )}
        </span>
      </label>
    </li>
  );
}

// ---------------------------------------------------------------- draft editor

function SetEditor({
  set,
  onChange,
  onDelete,
  onError,
}: {
  set: PracticeSet;
  onChange: (s: PracticeSet) => void;
  onDelete: (id: string) => void;
  onError: (msg: string | null) => void;
}) {
  const [bank, setBank] = useState<PracticeBankQuestion[] | null>(null);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const needs = set.items.filter((i) => i.status === "needs_answer").length;

  const run = async (fn: () => Promise<PracticeSet>) => {
    setBusy(true);
    onError(null);
    try {
      onChange(await fn());
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const loadBank = async () => {
    try {
      const all = await practiceApi.bank();
      const have = new Set(set.items.map((i) => `${i.source.exam_id}/${i.source.question_id}`));
      setBank(all.filter((q) => !have.has(`${q.exam_id}/${q.question_id}`)));
    } catch (e) {
      onError(errorText(e));
    }
  };

  const approveAndSend = () =>
    run(async () => {
      if (set.status !== "approved") await practiceApi.approve(set.id);
      return practiceApi.send(set.id);
    });

  const titleId = `pr-set-${set.id}`;
  return (
    <article className="t-card pr-set" aria-labelledby={titleId}>
      <div className="t-card-head">
        <div>
          <h3 id={titleId} className="pr-set-title">
            {set.name ?? set.username} · {set.title}
          </h3>
          <div className="t-small t-muted">
            {plural(set.items.length, "item")} · {set.verified} verified with SymPy · Practice — not graded
          </div>
        </div>
        <Pill tone={set.status === "approved" ? "blue" : "grey"}>{set.status === "approved" ? "Approved" : "Draft"}</Pill>
      </div>

      {set.dropped && set.dropped.length > 0 && (
        <details className="pr-dropped">
          <summary>{plural(set.dropped.length, "variation")} failed the check and were left out</summary>
          <ul>
            {set.dropped.map((d, i) => (
              <li key={i}>{d.reason}</li>
            ))}
          </ul>
        </details>
      )}

      <ol className="pr-items">
        {set.items.map((it, i) => (
          <ItemCard
            key={it.id}
            n={i + 1}
            item={it}
            busy={busy}
            onSave={(body) => run(() => practiceApi.editItem(set.id, it.id, body))}
            onRemove={() => run(() => practiceApi.removeItem(set.id, it.id))}
          />
        ))}
      </ol>

      <div className="pr-add">
        {bank === null ? (
          <button type="button" className="t-btn-outline" onClick={loadBank}>
            Add one of your questions
          </button>
        ) : (
          <>
            <label htmlFor={`pr-bank-${set.id}`} className="t-small">
              Add from your questions
            </label>
            <select id={`pr-bank-${set.id}`} className="t-select" value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">Choose a question…</option>
              {bank.map((q) => (
                <option key={`${q.exam_id}/${q.question_id}`} value={`${q.exam_id}/${q.question_id}`}>
                  {q.exam_title}: {q.prompt.slice(0, 80)}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="t-btn-outline"
              disabled={!pick || busy}
              onClick={() => {
                const [examId = "", qid = ""] = pick.split("/");
                setPick("");
                setBank((b) => (b ? b.filter((q) => `${q.exam_id}/${q.question_id}` !== `${examId}/${qid}`) : b));
                void run(() => practiceApi.addItem(set.id, examId, qid));
              }}
            >
              Add
            </button>
          </>
        )}
      </div>

      <div className="pr-set-actions">
        {needs > 0 && (
          <span className="pr-needs" role="status">
            {needs === 1 ? "1 item needs" : `${needs} items need`} your answer before sending.
          </span>
        )}
        <button type="button" className="t-btn-outline" disabled={busy} onClick={() => onDelete(set.id)}>
          Delete
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy || needs > 0 || set.items.length === 0}
          onClick={approveAndSend}
        >
          Approve &amp; send
        </button>
      </div>
    </article>
  );
}

function ItemCard({
  n,
  item,
  busy,
  onSave,
  onRemove,
}: {
  n: number;
  item: PracticeItem;
  busy: boolean;
  onSave: (body: PracticeItemEdit) => Promise<void>;
  onRemove: () => void;
}) {
  const [editing, setEditing] = useState(item.status === "needs_answer");
  const badge = itemBadge(item);
  return (
    <li className={`pr-item${item.status === "needs_answer" ? " needs" : ""}`}>
      <div className="pr-item-head">
        <span className="pr-item-n">{n}</span>
        <span className="t-small t-muted">
          {TYPE_LABEL[item.type] ?? item.type} · {item.topics.join(", ")}
        </span>
        <Pill tone={badge.tone}>{badge.label}</Pill>
      </div>
      <QuestionImages images={item.images} compact />
      {editing ? (
        <ItemForm
          item={item}
          busy={busy}
          onCancel={item.status === "needs_answer" ? undefined : () => setEditing(false)}
          onSave={async (body) => {
            await onSave(body);
            setEditing(false);
          }}
        />
      ) : (
        <>
          <p className="pr-prompt">{item.prompt}</p>
          {item.type === "mcq" && (
            <ol className="pr-options" type="A">
              {item.options.map((o, i) => (
                <li key={i} className={i === item.correct_index ? "correct" : undefined}>
                  {o}
                  {i === item.correct_index && <span className="sr-only"> (correct)</span>}
                </li>
              ))}
            </ol>
          )}
          <dl className="pr-key">
            <dt>Answer</dt>
            <dd>{answerText(item)}</dd>
            {item.explanation && (
              <>
                <dt>Explanation</dt>
                <dd>{item.explanation}</dd>
              </>
            )}
          </dl>
          {item.verification?.formula && (
            <p className="t-small t-muted">Computed from {item.verification.formula.replace(/\((n\d)\)/g, "$1")} with the new numbers (n1, n2… are the numbers in the question, in order).</p>
          )}
          <div className="pr-item-actions">
            <button type="button" className="t-link-btn" onClick={() => setEditing(true)} disabled={busy}>
              Edit
            </button>
            <button type="button" className="t-link-btn pr-danger" onClick={onRemove} disabled={busy}>
              Remove
            </button>
          </div>
        </>
      )}
      {item.note && <p className="pr-note">{item.note}</p>}
      <p className="t-small t-muted pr-source">From: {item.source.exam_title || "your questions"}</p>
    </li>
  );
}

function ItemForm({
  item,
  busy,
  onSave,
  onCancel,
}: {
  item: PracticeItem;
  busy: boolean;
  onSave: (body: PracticeItemEdit) => Promise<void>;
  onCancel?: () => void;
}) {
  const [prompt, setPrompt] = useState(item.prompt);
  const [options, setOptions] = useState<string[]>(item.options);
  const [correct, setCorrect] = useState<number>(item.correct_index ?? 0);
  const [answer, setAnswer] = useState(item.answer_number == null ? "" : String(item.answer_number));
  const [tolerance, setTolerance] = useState(item.tolerance == null ? "" : String(item.tolerance));
  const [accepted, setAccepted] = useState(
    (item.accepted_answers.length ? item.accepted_answers : item.suggested_answer ? [item.suggested_answer] : []).join("\n"),
  );
  const [explanation, setExplanation] = useState(item.explanation);
  const [formError, setFormError] = useState<string | null>(null);
  const uid = item.id;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    const body: PracticeItemEdit = { prompt: prompt.trim(), explanation: explanation.trim() };
    if (!body.prompt) return setFormError("Write the question.");
    if (item.type === "mcq") {
      const opts = options.map((o) => o.trim());
      if (opts.some((o) => !o)) return setFormError("Fill in every option.");
      body.options = opts;
      body.correct_index = correct;
    }
    if (item.type === "numeric") {
      const v = Number(answer.replace(",", "."));
      if (answer.trim() === "" || !Number.isFinite(v)) return setFormError("Enter the answer as a number.");
      body.answer_number = v;
      if (tolerance.trim() !== "") body.tolerance = Math.max(0, Number(tolerance) || 0);
    }
    if (item.type === "short") {
      const acc = accepted
        .split("\n")
        .map((a) => a.trim())
        .filter(Boolean);
      if (!acc.length) return setFormError("Enter at least one accepted answer.");
      body.accepted_answers = acc;
    }
    await onSave(body);
  };

  return (
    <form className="pr-form" onSubmit={submit}>
      <div className="t-field">
        <label htmlFor={`pr-p-${uid}`}>Question</label>
        <textarea id={`pr-p-${uid}`} className="textarea" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      </div>
      {item.type === "mcq" && (
        <fieldset className="pr-opt-edit">
          <legend>Options (pick the correct one)</legend>
          {options.map((o, i) => (
            <div key={i} className="pr-opt-row">
              <input
                type="radio"
                name={`pr-c-${uid}`}
                checked={correct === i}
                onChange={() => setCorrect(i)}
                aria-label={`Option ${String.fromCharCode(65 + i)} is correct`}
              />
              <input
                className="input"
                value={o}
                aria-label={`Option ${String.fromCharCode(65 + i)}`}
                onChange={(e) => setOptions((cur) => cur.map((x, j) => (j === i ? e.target.value : x)))}
              />
            </div>
          ))}
        </fieldset>
      )}
      {item.type === "numeric" && (
        <div className="pr-form-row">
          <div className="t-field">
            <label htmlFor={`pr-a-${uid}`}>Answer</label>
            <input id={`pr-a-${uid}`} className="input" inputMode="decimal" value={answer} onChange={(e) => setAnswer(e.target.value)} />
          </div>
          <div className="t-field">
            <label htmlFor={`pr-t-${uid}`}>Allowed difference (±)</label>
            <input id={`pr-t-${uid}`} className="input" inputMode="decimal" value={tolerance} onChange={(e) => setTolerance(e.target.value)} />
          </div>
        </div>
      )}
      {item.type === "short" && (
        <div className="t-field">
          <label htmlFor={`pr-acc-${uid}`}>Accepted answers (one per line){item.math ? " — math is checked with SymPy, so 2(x+1) also matches 2x+2" : ""}</label>
          <textarea id={`pr-acc-${uid}`} className="textarea" rows={2} value={accepted} onChange={(e) => setAccepted(e.target.value)} />
        </div>
      )}
      <div className="t-field">
        <label htmlFor={`pr-e-${uid}`}>Explanation shown after answering (optional)</label>
        <textarea id={`pr-e-${uid}`} className="textarea" rows={2} value={explanation} onChange={(e) => setExplanation(e.target.value)} />
      </div>
      {formError && (
        <p className="t-inline-error" role="alert">
          {formError}
        </p>
      )}
      <div className="pr-item-actions">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          Save item
        </button>
        {onCancel && (
          <button type="button" className="t-btn-outline" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
