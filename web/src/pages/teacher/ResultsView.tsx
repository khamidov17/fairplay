import { Fragment, useCallback, useEffect, useState } from "react";
import { paperImage, paperValue } from "../../lib/paperAnswer";
import { urlHost } from "../../components/teacher/externalValidation";
import { Link } from "react-router-dom";
import { ApiError, teacherApi } from "../../api/client";
import type { AnswerValue, Exam, Question, ResultAnswer, ResultAttempt, Results } from "../../api/types";
import { Loading } from "../../components/Loading";
import { Pill } from "../../components/teacher/TeacherLayout";
import { SendGradesButton } from "../../components/teacher/lms/SendGradesButton";
import { errorText, eventLabel, formatWhen, num, pct, plural } from "../../components/teacher/util";
import { formatClock } from "../../lib/timer";
import { KIND_LABEL, kindOf } from "./examDraft";
import { ExportButtons } from "./scan/ExportButtons"; // phase 2: CSV / Excel export
import { StrugglePanel } from "./scan/StrugglePanel"; // phase 2: where students struggled

const LETTERS = "ABCDEFGH";

export function formatGiven(q: Question | undefined, raw: AnswerValue | null): string {
  const given = paperValue(raw) as AnswerValue | null;
  if (paperImage(raw) && (given === null || given === undefined)) return "Written on paper (see scan)";
  if (given === null || given === undefined || given === "") return "No answer";
  if (q?.type === "mcq" && typeof given === "number") {
    const text = q.options[given];
    return text !== undefined ? `${LETTERS[given] ?? given + 1}. ${text}` : `Option ${given + 1}`;
  }
  return String(given);
}

function correctText(q: Question | undefined): string | null {
  if (!q) return null;
  if (q.type === "mcq" && q.correct_index != null) return `${LETTERS[q.correct_index]}. ${q.options[q.correct_index]}`;
  if (q.type === "numeric" && q.answer_number != null)
    return q.tolerance ? `${q.answer_number} (± ${q.tolerance})` : String(q.answer_number);
  if (q.type === "short") return q.accepted_answers.join(", ");
  return null;
}

type ReleaseState = null | { step: "confirm" } | { step: "pending"; detail: string };

export function ResultsView({ exam, onExamChange }: { exam: Exam; onExamChange: (e: Exam) => void }) {
  const isExternal = exam.mode === "external";
  const externalHost = urlHost(exam.external_url) ?? "the exam website";
  const [results, setResults] = useState<Results | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [release, setRelease] = useState<ReleaseState>(null);
  const [releaseBusy, setReleaseBusy] = useState(false);
  const [releaseError, setReleaseError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await teacherApi.results(exam.id);
      setResults(r);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [exam.id]);

  useEffect(() => {
    setResults(null);
    void load();
  }, [load, exam.status]);

  async function doRelease(force: boolean) {
    setReleaseBusy(true);
    setReleaseError(null);
    try {
      const updated = await teacherApi.release(exam.id, force);
      setRelease(null);
      onExamChange(updated);
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && !force && /grad/i.test(e.detail)) {
        setRelease({ step: "pending", detail: e.detail });
      } else {
        setReleaseError(errorText(e));
      }
    } finally {
      setReleaseBusy(false);
    }
  }

  async function doUnrelease() {
    setReleaseBusy(true);
    setReleaseError(null);
    try {
      const updated = await teacherApi.unrelease(exam.id);
      onExamChange(updated);
      await load();
    } catch (e) {
      setReleaseError(errorText(e));
    } finally {
      setReleaseBusy(false);
    }
  }

  if (error && !results)
    return (
      <div className="alert alert-error" role="alert">
        {error}
      </div>
    );
  if (!results) return <Loading label="Loading results…" />;

  const qById = new Map(exam.questions.map((q) => [q.id, q] as const));
  const qIndex = new Map(exam.questions.map((q, i) => [q.id, i] as const));
  const released = results.grades_released ?? exam.grades_released;
  const pending = results.pending_grading ?? 0;
  const submitted = results.submitted_count ?? results.attempts.filter((a) => a.status === "submitted").length;
  const attemptCount = results.attempt_count ?? results.attempts.length;
  const flagsTotal = results.flags_total ?? 0;
  const flagsOpen = results.flags_open ?? 0;
  const closed = exam.status === "closed";

  const statusLine = [
    exam.closed_at ? `Closed ${formatWhen(exam.closed_at)}` : exam.status === "live" ? "Still live" : null,
    plural(attemptCount, "student"),
    released ? `grades released${results.released_at ? ` ${formatWhen(results.released_at)}` : ""}` : "not released yet",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      <section className="t-results-bar" aria-label="Grades">
        <div className="t-head-text">
          <strong>Results{results.course ? ` · ${results.course}` : ""}</strong>
          <span className="t-small t-muted">{statusLine}</span>
        </div>
        {isExternal && (
          <span className="t-small t-muted" style={{ maxWidth: 360, textAlign: "right" }}>
            Answers are on {externalHost}. Open each student with Review to enter their score.
          </span>
        )}
        {(<>
        {pending > 0 && (
          <Link to={`/teacher/exams/${exam.id}/grading`} className="t-btn-outline">
            Grade {plural(pending, "answer")}
          </Link>
        )}
        {released ? (
          <button type="button" className="t-btn-outline" onClick={doUnrelease} disabled={releaseBusy}>
            {releaseBusy ? "Hiding…" : "Hide grades"}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setRelease({ step: "confirm" })}
            disabled={!closed || releaseBusy || release !== null}
            aria-describedby={!closed ? "release-hint" : undefined}
          >
            Release grades
          </button>
        )}
        <SendGradesButton exam={exam} released={released} />
        <ExportButtons examId={exam.id} />
        {!closed && !released && (
          <span id="release-hint" className="t-small t-muted" style={{ flexBasis: "100%", textAlign: "right" }}>
            Close the exam to release grades.
          </span>
        )}
        </>)}
      </section>

      {release?.step === "confirm" && (
        <div className="t-confirm" role="alertdialog" aria-labelledby="rel-q">
          <p id="rel-q">
            <strong>Release grades to students?</strong> They will see their score, each answer and your comments. You
            can hide them again later.
          </p>
          <button type="button" className="btn btn-primary" onClick={() => doRelease(false)} disabled={releaseBusy}>
            {releaseBusy ? "Releasing…" : "Yes, release"}
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => setRelease(null)} disabled={releaseBusy}>
            Cancel
          </button>
        </div>
      )}
      {release?.step === "pending" && (
        <div className="t-confirm" role="alertdialog" aria-labelledby="rel-p">
          <p id="rel-p">
            <strong>{release.detail}.</strong> Ungraded essays count as 0 points if you release now.
          </p>
          <Link to={`/teacher/exams/${exam.id}/grading`} className="btn btn-primary">
            Grade them first
          </Link>
          <button type="button" className="btn btn-secondary" onClick={() => doRelease(true)} disabled={releaseBusy}>
            {releaseBusy ? "Releasing…" : "Release anyway"}
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => setRelease(null)} disabled={releaseBusy}>
            Cancel
          </button>
        </div>
      )}
      {releaseError && (
        <div className="alert alert-error" role="alert">
          {releaseError}
        </div>
      )}
      {exam.delivery === "paper" && <StrugglePanel examId={exam.id} />}
      {exam.status === "live" && (
        <div className="alert alert-info">The exam is still live. Results update as students submit.</div>
      )}

      <section aria-label="Summary" className="t-stats results">
        {(<>
        <div className="t-stat">
          <span className="t-stat-label">Average</span>
          <span className="t-stat-value">{pct(results.average_percent)}</span>
        </div>
        <div className="t-stat">
          <span className="t-stat-label">Median</span>
          <span className="t-stat-value">{pct(results.median_percent)}</span>
        </div>
        </>)}
        <div className="t-stat">
          <span className="t-stat-label">Submitted</span>
          <span className="t-stat-value">
            {submitted} <small>/ {attemptCount}</small>
          </span>
        </div>
        <div className="t-stat">
          <span className="t-stat-label">Flags reviewed</span>
          <span className="t-stat-value">
            {flagsTotal - flagsOpen} <small>/ {flagsTotal}</small>
          </span>
        </div>
      </section>

      <div className="t-columns">
        {<ScoreSpread buckets={results.score_buckets ?? []} median={results.median_percent} />}
        <IntegritySummary results={results} />
      </div>

      {!isExternal && (
      <section aria-labelledby="qa-h" className="t-card" style={{ gap: 14 }}>
        <h2 id="qa-h">Question by question</h2>
        <div className="t-table-scroll">
          <table className="t-table" style={{ minWidth: 620 }}>
            <thead>
              <tr>
                <th scope="col">Question</th>
                <th scope="col">Type</th>
                <th scope="col">Got it right</th>
                <th scope="col" className="num">
                  Avg points
                </th>
              </tr>
            </thead>
            <tbody>
              {results.questions.map((rq) => {
                const q = qById.get(rq.question_id);
                const i = qIndex.get(rq.question_id) ?? 0;
                const kind = q ? kindOf(q) : rq.type;
                const essayPending =
                  rq.type === "essay"
                    ? results.attempts.filter((a) => a.answers.some((x) => x.question_id === rq.question_id && x.pending)).length
                    : 0;
                return (
                  <tr key={rq.question_id}>
                    <td className="strong" style={{ maxWidth: 380 }}>
                      Q{i + 1} · {rq.prompt.length > 70 ? `${rq.prompt.slice(0, 70)}…` : rq.prompt}
                    </td>
                    <td>{KIND_LABEL[kind]}</td>
                    <td>
                      {rq.percent_correct == null ? (
                        essayPending > 0 ? (
                          <span className="t-muted">Grading · {essayPending} left</span>
                        ) : (
                          "—"
                        )
                      ) : (
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                          <span style={{ minWidth: 48, fontWeight: 600 }}>{pct(rq.percent_correct)}</span>
                          <span className="t-progress-track" style={{ width: 90 }} aria-hidden="true">
                            <span
                              className="t-progress-fill"
                              style={{ display: "block", width: `${Math.max(0, Math.min(100, rq.percent_correct))}%` }}
                            />
                          </span>
                          {essayPending > 0 && <span className="t-small t-muted">· {essayPending} left</span>}
                        </span>
                      )}
                    </td>
                    <td className="num">
                      {num(rq.avg_points)} <span className="t-muted">/ {rq.points ?? q?.points ?? "—"}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
      )}

      <section aria-labelledby="st-h" className="t-card" style={{ gap: 14 }}>
        <h2 id="st-h">Students</h2>
        {results.attempts.length === 0 ? (
          <div className="t-empty">
            <strong>No students yet</strong>
            Results appear as soon as students start the exam.
          </div>
        ) : (
          <div className="t-table-scroll">
            <table className="t-table" style={{ minWidth: 760 }}>
              <thead>
                <tr>
                  <th scope="col">Student</th>
                  <th scope="col" className="num">
                    Score
                  </th>
                  <th scope="col" className="num">
                    Percent
                  </th>
                  <th scope="col">Status</th>
                  <th scope="col">Receipt</th>
                  <th scope="col">Flags</th>
                  <th scope="col">
                    <span className="sr-only">Details</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {results.attempts.map((a) => {
                  const isOpen = !!open[a.attempt_id];
                  const detailsId = `details-${a.attempt_id}`;
                  return (
                    <Fragment key={a.attempt_id}>
                      <tr>
                        <td>
                          <div style={{ fontWeight: 600 }}>
                            <Link to={`/teacher/students/${encodeURIComponent(a.username)}`} className="t-row-link">
                              {a.name}
                            </Link>
                          </div>
                          <div className="t-small t-muted">{a.username}</div>
                        </td>
                        <td className="num mono">
                          {num(a.score)}/{a.total_points}
                        </td>
                        <td className="num" style={{ fontWeight: 700 }}>
                          {pct(a.percent)}
                        </td>
                        <td>
                          <AttemptStatus a={a} />
                        </td>
                        <td className="mono t-small">{a.receipt ?? "—"}</td>
                        <td>
                          {(a.flag_count ?? a.events.length) === 0 ? (
                            <span className="t-muted">None</span>
                          ) : (
                            <Pill tone="orange">{a.flag_count ?? a.events.length}</Pill>
                          )}
                        </td>
                        <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                          <Link
                            to={`/teacher/exams/${exam.id}/submissions/${a.attempt_id}`}
                            className="t-mini-btn primary"
                            style={{ marginRight: 6 }}
                          >
                            {a.needs_grading > 0 ? "Grade" : "Review"}
                          </Link>
                          {!isExternal && (
                          <button
                            type="button"
                            className="t-mini-btn"
                            aria-expanded={isOpen}
                            aria-controls={detailsId}
                            onClick={() => setOpen((o) => ({ ...o, [a.attempt_id]: !isOpen }))}
                          >
                            {isOpen ? "Hide answers" : "Show answers"}
                          </button>
                          )}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="t-expand" id={detailsId}>
                          <td colSpan={7}>
                            <AttemptDetails
                              examId={exam.id}
                              attempt={a}
                              qById={qById}
                              qIndex={qIndex}
                              onChanged={load}
                            />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function AttemptStatus({ a }: { a: ResultAttempt }) {
  if (a.status !== "submitted") return <Pill tone="green">Still writing</Pill>;
  if ((a.needs_grading ?? 0) > 0) return <Pill tone="orange">{a.needs_grading} to grade</Pill>;
  return (
    <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
      <Pill>Submitted</Pill>
      {a.auto_submitted && <Pill tone="orange">Auto-submitted</Pill>}
    </span>
  );
}

export function ScoreSpread({ buckets, median }: { buckets: { label: string; count: number }[]; median: number | null }) {
  const max = Math.max(1, ...buckets.map((b) => b.count));
  const total = buckets.reduce((s, b) => s + b.count, 0);
  const medianIdx = median == null ? -1 : Math.min(9, Math.floor(median / 10));
  const peak = buckets.reduce((best, b, i) => (b.count > (buckets[best]?.count ?? -1) ? i : best), 0);
  const summary =
    total === 0
      ? "No submitted attempts yet"
      : `Score distribution of ${plural(total, "student")}; most common range ${buckets[peak]?.label ?? ""} percent`;
  return (
    <section aria-labelledby="dist-h" className="t-card t-col-3" style={{ flex: "2 1 380px" }}>
      <h2 id="dist-h" style={{ fontSize: 17 }}>
        Score spread
      </h2>
      {total === 0 ? (
        <div className="t-empty">The chart fills in as students submit.</div>
      ) : (
        <>
          <div className="t-chart" role="img" aria-label={summary}>
            {buckets.map((b, i) => (
              <div className="t-chart-col" key={b.label}>
                <span className="t-chart-n">{b.count}</span>
                <div
                  className={`t-chart-bar${i === medianIdx ? " hi" : ""}`}
                  style={{ height: Math.max(4, Math.round((b.count / max) * 160)) }}
                />
              </div>
            ))}
          </div>
          <div className="t-chart-labels" aria-hidden="true">
            {buckets.map((b) => (
              <span key={b.label}>{b.label.split(/[–-]/)[0]}+</span>
            ))}
          </div>
          {medianIdx >= 0 && (
            <span className="t-small t-muted" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <i className="t-swatch" style={{ background: "var(--primary)" }} /> Range with the median score
            </span>
          )}
        </>
      )}
    </section>
  );
}

function IntegritySummary({ results }: { results: Results }) {
  const byType = new Map<string, { total: number; open: number; dismissed: number; noted: number }>();
  for (const a of results.attempts)
    for (const ev of a.events) {
      if (ev.type === "lock_started" || ev.type === "lock_ended") continue;
      const row = byType.get(ev.type) ?? { total: 0, open: 0, dismissed: 0, noted: 0 };
      row.total += 1;
      if (ev.review === "dismissed") row.dismissed += 1;
      else if (ev.review === "noted") row.noted += 1;
      else row.open += 1;
      byType.set(ev.type, row);
    }
  const autoSubmitted = results.attempts.filter((a) => a.auto_submitted).length;
  const rows = [...byType.entries()].sort((x, y) => y[1].total - x[1].total);
  return (
    <section aria-labelledby="int-h" className="t-card t-col-2" style={{ flex: "1 1 300px", gap: 4 }}>
      <h2 id="int-h" style={{ fontSize: 17, marginBottom: 8 }}>
        Integrity log
      </h2>
      {rows.length === 0 && autoSubmitted === 0 ? (
        <div className="t-empty">No integrity events recorded.</div>
      ) : (
        <>
          {rows.map(([type, r]) => {
            const parts = [r.dismissed && `${r.dismissed} fine`, r.noted && `${r.noted} noted`, r.open && `${r.open} open`].filter(
              Boolean,
            );
            return (
              <div className="t-kv" key={type}>
                <span>{eventLabel(type)}</span>
                <span style={{ color: r.open ? "var(--warn)" : undefined }}>
                  {r.total} · {parts.join(", ")}
                </span>
              </div>
            );
          })}
          {autoSubmitted > 0 && (
            <div className="t-kv">
              <span>Submitted automatically</span>
              <span>{plural(autoSubmitted, "student")}</span>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function AttemptDetails({
  examId,
  attempt: a,
  qById,
  qIndex,
  onChanged,
}: {
  examId: string;
  attempt: ResultAttempt;
  qById: Map<string, Question>;
  qIndex: Map<string, number>;
  onChanged: () => Promise<void>;
}) {
  const answers = [...a.answers].sort((x, y) => (qIndex.get(x.question_id) ?? 0) - (qIndex.get(y.question_id) ?? 0));
  const editable = a.status === "submitted";
  return (
    <div className="t-expand-grid">
      <div>
        <h3>Answers</h3>
        <ul className="t-answers">
          {answers.map((ans) => (
            <AnswerRow
              key={ans.question_id}
              examId={examId}
              attemptId={a.attempt_id}
              ans={ans}
              q={qById.get(ans.question_id)}
              n={(qIndex.get(ans.question_id) ?? 0) + 1}
              editable={editable}
              onChanged={onChanged}
            />
          ))}
        </ul>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {editable && <OverallComment examId={examId} attempt={a} onChanged={onChanged} />}
        <div>
          <h3>Integrity events</h3>
          {a.events.length === 0 ? (
            <p className="t-small t-muted">No events recorded.</p>
          ) : (
            <ul className="t-answers">
              {a.events.map((ev, i) => (
                <li key={ev.event_id ?? i} className="t-answer" style={{ gridTemplateColumns: "auto 1fr auto" }}>
                  <span className="mono t-small">{formatClock(ev.client_time ?? ev.server_time)}</span>
                  <span className="t-small">
                    {eventLabel(ev.type)} <span className="mono t-muted">{ev.detail}</span>
                    {ev.note && <div className="t-muted">Note: {ev.note}</div>}
                  </span>
                  {ev.review && ev.review !== "open" ? (
                    <Pill>{ev.review === "dismissed" ? "Fine" : "Noted"}</Pill>
                  ) : (
                    <Pill tone="orange">Open</Pill>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function AnswerRow({
  examId,
  attemptId,
  ans,
  q,
  n,
  editable,
  onChanged,
}: {
  examId: string;
  attemptId: string;
  ans: ResultAnswer;
  q: Question | undefined;
  n: number;
  editable: boolean;
  onChanged: () => Promise<void>;
}) {
  const [points, setPoints] = useState(String(ans.points_awarded));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setPoints(String(ans.points_awarded)), [ans.points_awarded]);

  const max = ans.max_points ?? q?.points ?? 0;
  const right = correctText(q);
  const inputId = `pts-${attemptId}-${ans.question_id}`;
  const parsed = Number(points.replace(",", "."));
  const valid = points.trim() !== "" && Number.isFinite(parsed) && parsed >= 0 && parsed <= max;

  async function save() {
    if (!valid) {
      setError(`Enter 0 to ${max}.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await teacherApi.saveGrade(examId, attemptId, ans.question_id, {
        points: parsed,
        comment: ans.comment ?? "",
        rubric_scores: null,
      });
      await onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    setError(null);
    try {
      await teacherApi.clearGrade(examId, attemptId, ans.question_id);
      await onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="t-answer">
      <span className="mono" style={{ fontWeight: 700 }}>
        Q{n}
      </span>
      <div style={{ minWidth: 0 }}>
        <div className="t-small t-muted">{q?.prompt}</div>
        <div className="t-answer-given">{formatGiven(q, ans.given)}</div>
        {!ans.correct && !ans.pending && right && <div className="t-small t-muted">Correct: {right}</div>}
        {ans.comment && <div className="t-small" style={{ marginTop: 4 }}>Comment: {ans.comment}</div>}
      </div>
      <div className="t-answer-side">
        {ans.pending ? (
          <Pill tone="orange">Needs grading</Pill>
        ) : ans.correct ? (
          <Pill tone="green">{ans.manual ? "Full marks · manual" : "Correct"}</Pill>
        ) : ans.points_awarded > 0 ? (
          <Pill tone="blue">{ans.manual ? "Partial · manual" : "Partial"}</Pill>
        ) : (
          <Pill tone="red">{ans.manual ? "0 · manual" : "Incorrect"}</Pill>
        )}
        {editable ? (
          <div className="t-override">
            <label htmlFor={inputId} className="sr-only">
              Points for question {n}
            </label>
            <input
              id={inputId}
              className="input mono"
              inputMode="decimal"
              value={points}
              onChange={(e) => setPoints(e.target.value)}
              aria-invalid={!valid}
            />
            <span className="t-small t-muted">/ {max}</span>
            <button
              type="button"
              className="t-mini-btn primary"
              onClick={save}
              disabled={busy || (String(ans.points_awarded) === points.trim() && !ans.pending)}
            >
              Save
            </button>
            {ans.manual && (
              <button type="button" className="t-mini-btn" onClick={reset} disabled={busy}>
                Reset to automatic
              </button>
            )}
          </div>
        ) : (
          <span className="t-small t-muted mono">
            {num(ans.points_awarded)} / {max}
          </span>
        )}
        {error && <span className="t-inline-error">{error}</span>}
      </div>
    </li>
  );
}

function OverallComment({
  examId,
  attempt,
  onChanged,
}: {
  examId: string;
  attempt: ResultAttempt;
  onChanged: () => Promise<void>;
}) {
  const [text, setText] = useState(attempt.overall_comment ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const id = `comment-${attempt.attempt_id}`;
  const dirty = text !== (attempt.overall_comment ?? "");

  async function save() {
    setBusy(true);
    setMsg(null);
    try {
      await teacherApi.saveComment(examId, attempt.attempt_id, text);
      setMsg({ ok: true, text: "Saved" });
      await onChanged();
    } catch (e) {
      setMsg({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="t-field">
      <label htmlFor={id}>Overall comment to {attempt.name}</label>
      <textarea
        id={id}
        className="textarea"
        rows={3}
        maxLength={2000}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setMsg(null);
        }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button type="button" className="t-mini-btn primary" onClick={save} disabled={busy || !dirty}>
          {busy ? "Saving…" : "Save comment"}
        </button>
        {msg && (
          <span className={msg.ok ? "t-small" : "t-inline-error"} style={msg.ok ? { color: "var(--success)" } : undefined} role="status">
            {msg.text}
          </span>
        )}
      </div>
    </div>
  );
}
