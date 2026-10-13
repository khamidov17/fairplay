import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { scanApi, type QueueItem, type QueueQuestion, type ScanQueue } from "../../../api/scan";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, num, plural } from "../../../components/teacher/util";
import {
  LETTERS,
  bubbleText,
  confidenceText,
  editError,
  firstOpenQuestion,
  groupsFor,
  itemKey,
  itemsFor,
  keyAction,
  shownPoints,
  stepIndex,
  withDraft,
  withGrade,
  type QueueFilter,
} from "./queueLogic";

/** An item can be approved as-is when there is a real draft (not the AI-off placeholder)
 *  or an automatic bubble read. */
function canApprove(it: QueueItem): boolean {
  if (it.status === "approved") return false;
  if (it.status === "auto") return true;
  return !!it.draft && it.draft.source !== "fallback";
}

function correctText(q: QueueQuestion): string | null {
  if (q.correct_answer == null) return null;
  if (q.type === "mcq" && typeof q.correct_answer === "number") {
    return `${LETTERS[q.correct_answer]} — ${q.options[q.correct_answer] ?? ""}`;
  }
  return String(q.correct_answer);
}

export function QueueStep({ examId, onChanged }: { examId: string; onChanged?: () => void }) {
  const [queue, setQueue] = useState<ScanQueue | null>(null);
  const [qid, setQid] = useState<string | null>(null);
  const [filter, setFilter] = useState<QueueFilter>("review");
  // the active card is tracked by key so re-sorting (after a regrade) keeps it selected
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const lastIndex = useRef(0);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const listRef = useRef<HTMLOListElement>(null);

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

  const question = queue?.questions.find((q) => q.question_id === qid) ?? null;
  const items = useMemo(() => (queue && qid ? itemsFor(queue, qid, filter) : []), [queue, qid, filter]);
  const groups = useMemo(() => (queue && qid ? groupsFor(queue, qid) : []), [queue, qid]);
  const found = activeKey ? items.findIndex((i) => itemKey(i) === activeKey) : -1;
  const active = found >= 0 ? found : Math.min(lastIndex.current, Math.max(0, items.length - 1));
  lastIndex.current = active;
  const current: QueueItem | undefined = items[active];
  const setActive = useCallback(
    (fn: number | ((i: number) => number)) => {
      const i = typeof fn === "function" ? fn(lastIndex.current) : fn;
      lastIndex.current = i;
      setActiveKey(items[i] ? itemKey(items[i]) : null);
    },
    [items],
  );

  const currentKey = current ? itemKey(current) : null;
  useEffect(() => {
    if (currentKey !== activeKey) setActiveKey(currentKey);
  }, [currentKey, activeKey]);

  useEffect(() => {
    lastIndex.current = 0;
    setActiveKey(null);
    setEditing(null);
  }, [qid, filter]);

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest" });
  }, [active]);

  const run = useCallback(async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }, []);

  const approve = useCallback(
    (it: QueueItem, edit?: { points: number; comment: string }) =>
      run(itemKey(it), async () => {
        const res = await scanApi.approve(examId, it.attempt_id, it.question_id, edit);
        setQueue((q) => (q ? withGrade(q, it.attempt_id, it.question_id, res.grade) : q));
        setEditing(null);
        onChanged?.();
      }),
    [examId, run, onChanged],
  );

  const regrade = useCallback(
    (it: QueueItem) => {
      setActiveKey(itemKey(it)); // keep this card selected when the list re-sorts
      return run(itemKey(it), async () => {
        const res = await scanApi.regrade(examId, it.attempt_id, it.question_id);
        setQueue((q) => (q ? withDraft(q, it.attempt_id, it.question_id, res.draft) : q));
      });
    },
    [examId, run],
  );

  const approveGroup = (items: { attempt_id: string; question_id: string }[], label: string) =>
    run(`group:${label}`, async () => {
      const res = await scanApi.approveBulk(examId, items);
      const fresh = await scanApi.queue(examId);
      setQueue(fresh);
      setNotice(`Approved ${plural(res.approved, "answer")}${res.skipped ? ` (${res.skipped} skipped)` : ""}.`);
      onChanged?.();
    });

  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const action = keyAction(ev);
      if (!action || !current || busy) return;
      ev.preventDefault();
      if (action === "next" || action === "prev") {
        setEditing(null);
        setActive((i) => stepIndex(items.length, i, action === "next" ? 1 : -1));
      } else if (action === "approve") {
        if (canApprove(current)) approve(current);
      } else if (action === "edit") {
        setEditing(itemKey(current));
      } else if (action === "regrade") {
        regrade(current);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [current, items.length, busy, approve, regrade, setActive]);

  if (!queue) return error ? <div className="alert alert-error" role="alert">{error}</div> : <Loading label="Loading answers…" />;
  if (queue.items.length === 0)
    return (
      <div className="t-empty">
        <strong>No scanned answers yet</strong>
        Upload the scans first. Answers appear here as soon as pages are read.
      </div>
    );

  const toReview = queue.questions.reduce((s, q) => s + q.to_review, 0);

  return (
    <div className="sc-queue">
      <nav className="sc-qlist" aria-label="Questions">
        <p className="t-muted sc-small">{toReview > 0 ? `${plural(toReview, "answer")} to review` : "Everything is approved"}</p>
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
              {q.to_review > 0 ? <Pill tone="orange">{q.to_review} to review</Pill> : <Pill tone="green">Done</Pill>}
              {q.low_confidence > 0 && <span className="sc-low">{q.low_confidence} unsure</span>}
            </span>
          </button>
        ))}
        <div className="sc-keys" aria-label="Keyboard shortcuts">
          <strong>Keys</strong>
          <span><kbd>A</kbd> approve</span>
          <span><kbd>E</kbd> edit</span>
          <span><kbd>R</kbd> AI again</span>
          <span><kbd>J</kbd>/<kbd>K</kbd> next / previous</span>
        </div>
      </nav>

      <section className="sc-qmain" aria-labelledby="sc-q-h">
        {question && (
          <header className="sc-qhead">
            <h3 id="sc-q-h">
              Question {question.index + 1} <span className="t-muted">· {plural(question.points, "point")}</span>
            </h3>
            <p className="sc-prompt">{question.prompt}</p>
            {correctText(question) && (
              <p className="sc-key">
                <span className="t-muted">Answer key:</span> {correctText(question)}
              </p>
            )}
            {!queue.ai_enabled && (
              <p className="t-muted sc-small">AI is off: drafts show 0 points — enter each grade with Edit.</p>
            )}
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
          {(
            [
              ["review", `To review (${question?.to_review ?? 0})`],
              ["all", `All (${question?.total ?? 0})`],
              ["approved", `Approved (${question?.approved ?? 0})`],
            ] as [QueueFilter, string][]
          ).map(([f, label]) => (
            <button key={f} type="button" className={filter === f ? "on" : ""} aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {label}
            </button>
          ))}
        </div>

        {filter === "review" && groups.length > 0 && (
          <div className="sc-groups" aria-label="Similar answers">
            {groups.map((g) => (
              <div key={g.key} className="sc-group">
                <span>
                  <strong>{g.count} similar answers</strong> · {g.label}
                  {g.key.startsWith("t:") && ` · draft ${num(g.points)} pts`}
                </span>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busy !== null}
                  onClick={() => approveGroup(g.items, g.key)}
                >
                  {busy === `group:${g.key}` ? "Approving…" : `Approve all ${g.count}`}
                </button>
              </div>
            ))}
          </div>
        )}

        {items.length === 0 ? (
          <div className="t-empty">
            <strong>{filter === "review" ? "Nothing left to review here" : "No answers"}</strong>
            {filter === "review" ? "Pick another question or look at All." : ""}
          </div>
        ) : (
          <ol className="sc-items" ref={listRef}>
            {items.map((it, i) => (
              <ItemCard
                key={itemKey(it)}
                index={i}
                item={it}
                max={question?.points ?? 0}
                active={i === active}
                editing={editing === itemKey(it)}
                busy={busy === itemKey(it)}
                onFocus={() => setActive(i)}
                onApprove={() => approve(it)}
                onEdit={() => setEditing(itemKey(it))}
                onCancelEdit={() => setEditing(null)}
                onSave={(points, comment) => approve(it, { points, comment })}
                onRegrade={() => regrade(it)}
              />
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

function ItemCard({
  index,
  item,
  max,
  active,
  editing,
  busy,
  onFocus,
  onApprove,
  onEdit,
  onCancelEdit,
  onSave,
  onRegrade,
}: {
  index: number;
  item: QueueItem;
  max: number;
  active: boolean;
  editing: boolean;
  busy: boolean;
  onFocus: () => void;
  onApprove: () => void;
  onEdit: () => void;
  onCancelEdit: () => void;
  onSave: (points: number, comment: string) => void;
  onRegrade: () => void;
}) {
  const d = item.draft;
  const shown = shownPoints(item);
  const [points, setPoints] = useState(String(shown.value ?? ""));
  const [comment, setComment] = useState(item.grade?.comment ?? d?.comment ?? "");
  const [err, setErr] = useState<string | null>(null);
  const pointsRef = useRef<HTMLInputElement>(null);
  const id = `qi-${item.attempt_id}-${item.question_id}`;

  useEffect(() => {
    if (editing) {
      setPoints(String(shown.value ?? ""));
      setComment(item.grade?.comment ?? d?.comment ?? "");
      setErr(null);
      pointsRef.current?.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  const bubbles = bubbleText(item);
  return (
    <li
      className={`sc-item${active ? " is-active" : ""}${item.low_confidence && item.status === "needs_review" ? " is-low" : ""}`}
      data-index={index}
      onClick={onFocus}
      aria-labelledby={`${id}-who`}
    >
      <div className="sc-item-head">
        <span id={`${id}-who`} className="sc-who">
          {item.name} <span className="t-muted">{item.username}</span>
        </span>
        <span className="sc-item-status">
          {item.status === "approved" && <Pill tone="green">Approved {num(item.grade?.points)} / {num(max)}</Pill>}
          {item.status === "auto" && <Pill tone="blue">Read automatically</Pill>}
          {item.status === "needs_review" && d?.source === "fallback" && <Pill tone="orange">Grade by hand</Pill>}
          {item.status === "needs_review" && item.low_confidence && d?.source !== "fallback" && (
            <Pill tone="orange">Check: AI unsure</Pill>
          )}
          {item.status === "needs_review" && d?.key_match && (
            <Pill tone="blue">Reading matches the key — check the picture</Pill>
          )}
        </span>
      </div>
      <div className="sc-item-body">
        <div className="sc-crop">
          {item.image ? (
            <img src={item.image} alt={`Answer of ${item.name}`} loading="lazy" />
          ) : (
            <span className="t-muted">No picture</span>
          )}
        </div>
        <div className="sc-draft">
          {bubbles && <p className="sc-bubbles">{bubbles}</p>}
          {d && (
            <>
              <p className="sc-draft-points">
                <span className="t-muted">AI draft</span> <strong>{num(d.points)}</strong> / {num(max)}{" "}
                <span className={`sc-conf${d.low_confidence ? " is-low" : ""}`}>{confidenceText(d)}</span>
              </p>
              {d.transcript && (
                <p className="sc-transcript">
                  <span className="t-muted">Reads as:</span> “{d.transcript}”
                </p>
              )}
              {d.comment && <p className="sc-comment">{d.comment}</p>}
              {d.mistake_tags.length > 0 && (
                <p className="sc-tags">
                  {d.mistake_tags.map((t) => (
                    <span key={t} className="sc-tag">
                      {t}
                    </span>
                  ))}
                </p>
              )}
            </>
          )}
          {item.grade && item.grade.comment && item.status === "approved" && (
            <p className="sc-comment">
              <span className="t-muted">Comment to student:</span> {item.grade.comment}
            </p>
          )}

          {editing ? (
            <form
              className="sc-edit"
              onSubmit={(e) => {
                e.preventDefault();
                const msg = editError(points, max);
                if (msg) {
                  setErr(msg);
                  return;
                }
                onSave(Number(points), comment);
              }}
            >
              <div className="t-field">
                <label htmlFor={`${id}-pts`}>Points (out of {num(max)})</label>
                <input
                  id={`${id}-pts`}
                  ref={pointsRef}
                  className="input"
                  inputMode="decimal"
                  value={points}
                  onChange={(e) => setPoints(e.target.value)}
                  aria-invalid={err ? true : undefined}
                />
              </div>
              <div className="t-field">
                <label htmlFor={`${id}-c`}>Comment for the student</label>
                <textarea id={`${id}-c`} className="textarea" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} />
              </div>
              {err && (
                <p className="t-inline-error" role="alert">
                  {err}
                </p>
              )}
              <div className="sc-inline">
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  Save grade
                </button>
                <button type="button" className="t-btn-outline" onClick={onCancelEdit}>
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <div className="sc-inline sc-actions">
              {item.status !== "approved" && (
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={onApprove}
                  disabled={busy || !canApprove(item)}
                  aria-keyshortcuts="A"
                >
                  {busy ? "Saving…" : item.status === "auto" ? "Confirm" : "Approve"}
                </button>
              )}
              <button type="button" className="t-btn-outline" onClick={onEdit} disabled={busy} aria-keyshortcuts="E">
                {item.status === "approved" ? "Change" : "Edit"}
              </button>
              <button type="button" className="t-btn-outline" onClick={onRegrade} disabled={busy} aria-keyshortcuts="R">
                AI again
              </button>
            </div>
          )}
        </div>
      </div>
    </li>
  );
}
