// Phase 3: grading quality for one exam — AI vs final marks, review routing (policy +
// routed answers), second marking (assign, track) and moderation.
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  REASON_LABEL,
  STATUS_LABEL,
  fixed,
  qualityApi,
  rate,
  type ExamQuality,
  type ExamRouting,
  type PolicyResponse,
  type RoutingPolicy,
  type SecondMarkRow,
  type SecondMarkingList,
} from "../../../api/quality";
import { Loading } from "../../../components/Loading";
import { errorText, plural } from "../../../components/teacher/util";
import { AgreementTable, QualityTiles, TimingChart, TypeAgreement, WorstTable, itemPath } from "./qualityViz";
import { QualityNav } from "./QualityPage";

export default function ExamQualityPage() {
  const { id = "" } = useParams();
  const [data, setData] = useState<ExamQuality | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    qualityApi.exam(id).then(setData, (e) => setError(errorText(e)));
  }, [id]);
  useEffect(load, [load]);

  if (error) {
    return (
      <div className="alert alert-error" role="alert">
        {error}
      </div>
    );
  }
  if (!data) return <Loading label="Loading grading quality…" />;
  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <span className="t-eyebrow">
            <Link to="/teacher/quality">Grading quality</Link>
          </span>
          <h1>{data.title}</h1>
          <p>
            {data.course ? `${data.course} · ` : ""}
            {plural(data.agreement.n, "AI-suggested grade")} compared · {plural(data.graded, "graded answer")}
          </p>
        </div>
        <div className="t-head-actions">
          <Link className="t-btn-outline" to={`/teacher/exams/${encodeURIComponent(data.exam_id)}/grading`}>
            Open grading
          </Link>
          <QualityNav />
        </div>
      </header>
      <div className="q-stack">
        <QualityTiles a={data.agreement} c={data.correction} t={data.timing} />
        <div className="q-two">
          <section className="t-card" aria-labelledby="eq-type-h">
            <h2 id="eq-type-h">Agreement by question type</h2>
            <TypeAgreement groups={data.by_type} table={false} />
          </section>
          <section className="t-card" aria-labelledby="eq-time-h">
            <h2 id="eq-time-h">Grading time</h2>
            <TimingChart t={data.timing} />
          </section>
        </div>
        {data.by_question.length > 0 && (
          <section className="t-card" aria-labelledby="eq-q-h">
            <h2 id="eq-q-h">By question</h2>
            <AgreementTable
              keyLabel="Question"
              groups={data.by_question.map((q) => ({ ...q, key: `Q${q.question_index + 1} · ${q.prompt.slice(0, 60)}` }))}
            />
          </section>
        )}
        <section className="t-card" aria-labelledby="eq-worst-h">
          <h2 id="eq-worst-h">Biggest disagreements</h2>
          <WorstTable rows={data.worst} showExam={false} />
        </section>
        <RoutingSection examId={data.exam_id} />
        <SecondMarkingSection exam={data} onChange={load} />
      </div>
    </>
  );
}

// ---------------------------------------------------------------- review routing
function RoutingSection({ examId }: { examId: string }) {
  const [routing, setRouting] = useState<ExamRouting | null>(null);
  const [policy, setPolicy] = useState<PolicyResponse | null>(null);
  const [form, setForm] = useState<RoutingPolicy | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    qualityApi.routing(examId, true).then(setRouting, (e) => setMsg(errorText(e)));
    qualityApi.examPolicy(examId).then(
      (p) => {
        setPolicy(p);
        setForm(p.policy);
      },
      (e) => setMsg(errorText(e)),
    );
  }, [examId]);
  useEffect(load, [load]);

  async function save() {
    if (!form) return;
    setBusy(true);
    setMsg(null);
    try {
      const p = await qualityApi.saveExamPolicy(examId, form);
      setPolicy(p);
      setForm(p.policy);
      setMsg("Saved. Routing updated.");
      setRouting(await qualityApi.routing(examId, true));
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    try {
      const p = await qualityApi.resetExamPolicy(examId);
      setPolicy(p);
      setForm(p.policy);
      setRouting(await qualityApi.routing(examId, true));
      setMsg("Using your default settings again.");
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const set = <K extends keyof RoutingPolicy>(k: K, v: RoutingPolicy[K]) => setForm((f) => (f ? { ...f, [k]: v } : f));

  return (
    <section className="t-card" aria-labelledby="eq-route-h">
      <div className="t-card-head">
        <h2 id="eq-route-h">Manual review routing</h2>
        {routing && (
          <span className="t-pill orange">
            {routing.summary.routed} of {routing.summary.total} answers go to review
          </span>
        )}
      </div>
      <p className="t-small t-muted" style={{ margin: 0 }}>
        Answers matching any rule below are marked “Check by hand” in the review screen instead of being bulk-approved.
      </p>
      {routing && Object.keys(routing.summary.by_reason).length > 0 && (
        <div className="q-reasons" aria-label="Reasons">
          {Object.entries(routing.summary.by_reason).map(([code, n]) => (
            <span key={code} className="q-chip">
              {REASON_LABEL[code] ?? code}: {n}
            </span>
          ))}
        </div>
      )}
      {form && (
        <details>
          <summary className="t-link-btn">
            Routing rules {policy?.inherits ? "(your defaults)" : "(set for this exam)"}
          </summary>
          <div className="q-form" style={{ marginTop: 12 }}>
            <div className="t-field">
              <label htmlFor="rp-conf">Review when AI confidence is below</label>
              <input
                id="rp-conf"
                className="input"
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={form.confidence_threshold}
                onChange={(e) => set("confidence_threshold", Number(e.target.value))}
              />
            </div>
            <div className="t-field">
              <label htmlFor="rp-margin">Points from a grade boundary</label>
              <input
                id="rp-margin"
                className="input"
                type="number"
                min={0}
                step={0.5}
                value={form.boundary_margin}
                onChange={(e) => set("boundary_margin", Number(e.target.value))}
              />
            </div>
            <div className="t-field">
              <label htmlFor="rp-bounds">Grade boundaries (% of total)</label>
              <input
                id="rp-bounds"
                className="input"
                value={form.grade_boundaries.join(", ")}
                onChange={(e) =>
                  set(
                    "grade_boundaries",
                    e.target.value
                      .split(/[,\s]+/)
                      .filter(Boolean)
                      .map(Number)
                      .filter((n) => !Number.isNaN(n)),
                  )
                }
              />
            </div>
            <div className="t-field">
              <label htmlFor="rp-kappa">Weak area when benchmark kappa is below</label>
              <input
                id="rp-kappa"
                className="input"
                type="number"
                min={-1}
                max={1}
                step={0.05}
                value={form.weak_kappa}
                onChange={(e) => set("weak_kappa", Number(e.target.value))}
              />
            </div>
            <label className="q-check">
              <input type="checkbox" checked={form.review_illegible} onChange={(e) => set("review_illegible", e.target.checked)} />
              Hard-to-read handwriting
            </label>
            <label className="q-check">
              <input type="checkbox" checked={form.review_ai_flagged} onChange={(e) => set("review_ai_flagged", e.target.checked)} />
              The AI asks for a review
            </label>
            <label className="q-check">
              <input type="checkbox" checked={form.check_criteria} onChange={(e) => set("check_criteria", e.target.checked)} />
              Criteria don&apos;t add up to the total
            </label>
            <label className="q-check">
              <input type="checkbox" checked={form.review_without_ai} onChange={(e) => set("review_without_ai", e.target.checked)} />
              Answers without an AI suggestion
            </label>
            <div className="q-actions q-wide">
              <button type="button" className="btn btn-primary" onClick={save} disabled={busy}>
                Save for this exam
              </button>
              {!policy?.inherits && (
                <button type="button" className="btn btn-secondary" onClick={reset} disabled={busy}>
                  Use my defaults
                </button>
              )}
            </div>
          </div>
        </details>
      )}
      {msg && (
        <p className="t-small" role="status">
          {msg}
        </p>
      )}
      {routing && routing.items.length > 0 && (
        <div className="t-table-scroll">
          <table className="t-table">
            <thead>
              <tr>
                <th scope="col">Answer</th>
                <th scope="col" className="num">AI points</th>
                <th scope="col">Why</th>
                <th scope="col">Graded</th>
              </tr>
            </thead>
            <tbody>
              {routing.items.slice(0, 50).map((it) => (
                <tr key={`${it.attempt_id}:${it.question_id}`}>
                  <td>
                    <Link className="t-row-link" to={itemPath({ exam_id: examId, attempt_id: it.attempt_id })}>
                      {it.anon_id} · {it.question_id}
                    </Link>
                  </td>
                  <td className="num">{it.ai_points ?? "—"}</td>
                  <td>
                    <div className="q-reasons">
                      {it.reasons.map((r) => (
                        <span key={r.code} className="q-chip" title={r.message}>
                          {REASON_LABEL[r.code] ?? r.code}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td>{it.graded ? "Yes" : "Not yet"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- second marking
type Mode = "questions" | "sample" | "routed";

function SecondMarkingSection({ exam, onChange }: { exam: ExamQuality; onChange: () => void }) {
  const [list, setList] = useState<SecondMarkingList | null>(null);
  const [teachers, setTeachers] = useState<{ username: string; name: string }[]>([]);
  const [marker, setMarker] = useState("");
  const [mode, setMode] = useState<Mode>("questions");
  const [qids, setQids] = useState<string[]>([]);
  const [percent, setPercent] = useState(10);
  const [tolerance, setTolerance] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    qualityApi.secondMarking(exam.exam_id).then(setList, (e) => setMsg(errorText(e)));
  }, [exam.exam_id]);
  useEffect(() => {
    load();
    qualityApi.teachers().then(
      (t) => {
        setTeachers(t);
        if (t[0]) setMarker((m) => m || t[0].username);
      },
      () => undefined,
    );
  }, [load]);

  async function assign() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await qualityApi.assign(exam.exam_id, {
        marker_username: marker,
        question_ids: mode === "questions" ? qids : null,
        sample_percent: mode === "sample" ? percent : null,
        routed_only: mode === "routed",
        tolerance: tolerance === "" ? null : Number(tolerance),
      });
      setMsg(`${plural(r.created, "answer")} sent for second marking${r.skipped ? ` (${r.skipped} already were)` : ""}.`);
      load();
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const canAssign = marker && (mode !== "questions" || qids.length > 0);
  const toModerate = (list?.items ?? []).filter((x) => x.status === "needs_moderation");

  return (
    <section className="t-card" aria-labelledby="eq-sm-h">
      <div className="t-card-head">
        <h2 id="eq-sm-h">Second marking and moderation</h2>
        {list && list.items.length > 0 && (
          <span className={`t-pill ${list.open ? "orange" : "green"}`}>
            {list.open ? `${list.open} not final yet` : "All agreed or moderated"}
          </span>
        )}
      </div>
      <p className="t-small t-muted" style={{ margin: 0 }}>
        Another teacher marks the chosen answers blind (without your mark or the AI suggestion). When the two marks
        differ by more than the tolerance, you set the agreed mark here.
      </p>
      {teachers.length === 0 ? (
        <p className="t-muted">There is no other teacher account to act as second marker yet.</p>
      ) : (
        <div className="q-form">
          <div className="t-field">
            <label htmlFor="sm-marker">Second marker</label>
            <select id="sm-marker" className="t-select" value={marker} onChange={(e) => setMarker(e.target.value)}>
              {teachers.map((t) => (
                <option key={t.username} value={t.username}>
                  {t.name} ({t.username})
                </option>
              ))}
            </select>
          </div>
          <div className="t-field">
            <label htmlFor="sm-mode">Which answers</label>
            <select id="sm-mode" className="t-select" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
              <option value="questions">All answers to chosen questions</option>
              <option value="sample">A random sample</option>
              <option value="routed">Answers routed to review</option>
            </select>
          </div>
          {mode === "sample" && (
            <div className="t-field">
              <label htmlFor="sm-pct">Sample size (%)</label>
              <input
                id="sm-pct"
                className="input"
                type="number"
                min={1}
                max={100}
                value={percent}
                onChange={(e) => setPercent(Number(e.target.value))}
              />
            </div>
          )}
          <div className="t-field">
            <label htmlFor="sm-tol">Tolerance (points)</label>
            <input
              id="sm-tol"
              className="input"
              type="number"
              min={0}
              step={0.5}
              placeholder="10% of the question"
              value={tolerance}
              onChange={(e) => setTolerance(e.target.value)}
            />
          </div>
          {mode === "questions" && (
            <fieldset className="q-wide" style={{ border: 0, padding: 0, margin: 0 }}>
              <legend className="t-label">Questions</legend>
              {exam.questions.map((q) => (
                <label key={q.question_id} className="q-check" style={{ marginRight: 16 }}>
                  <input
                    type="checkbox"
                    checked={qids.includes(q.question_id)}
                    onChange={() =>
                      setQids((cur) =>
                        cur.includes(q.question_id) ? cur.filter((x) => x !== q.question_id) : [...cur, q.question_id],
                      )
                    }
                  />
                  Q{q.index + 1} · {q.prompt.slice(0, 50)}
                </label>
              ))}
            </fieldset>
          )}
          <div className="q-actions q-wide">
            <button type="button" className="btn btn-primary" onClick={assign} disabled={busy || !canAssign}>
              Send for second marking
            </button>
          </div>
        </div>
      )}
      {msg && (
        <p className="t-small" role="status">
          {msg}
        </p>
      )}
      {toModerate.length > 0 && (
        <div className="q-stack">
          <h3 style={{ margin: 0, fontSize: 16 }}>Needs moderation ({toModerate.length})</h3>
          {toModerate.map((row) => (
            <ModerationCard
              key={row.id}
              examId={exam.exam_id}
              row={row}
              onDone={() => {
                load();
                onChange();
              }}
            />
          ))}
        </div>
      )}
      {list && list.items.length > 0 && (
        <div className="t-table-scroll">
          <table className="t-table">
            <thead>
              <tr>
                <th scope="col">Answer</th>
                <th scope="col">Marker</th>
                <th scope="col" className="num">First</th>
                <th scope="col" className="num">Second</th>
                <th scope="col" className="num">Agreed</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {list.items.map((x) => (
                <tr key={x.id}>
                  <td>
                    <Link className="t-row-link" to={itemPath({ exam_id: exam.exam_id, attempt_id: x.attempt_id })}>
                      {x.name ?? x.anon_id} · Q{x.question_index + 1}
                    </Link>
                  </td>
                  <td>{x.marker?.name ?? "—"}</td>
                  <td className="num">{x.first?.points ?? "—"}</td>
                  <td className="num">{x.second?.points ?? "—"}</td>
                  <td className="num">{x.moderated?.points ?? (x.status === "agreed" ? x.first?.points : "—")}</td>
                  <td>{STATUS_LABEL[x.status]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function ModerationCard({ examId, row, onDone }: { examId: string; row: SecondMarkRow; onDone: () => void }) {
  const [points, setPoints] = useState(String(row.first?.points ?? ""));
  const [comment, setComment] = useState(row.first?.comment ?? "");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      await qualityApi.moderate(examId, row.id, Number(points), comment);
      onDone();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="t-card q-mark-card" aria-label={`Moderate ${row.anon_id} question ${row.question_index + 1}`}>
      <div className="q-stack">
        <strong>
          {row.name ?? row.anon_id} · Q{row.question_index + 1} · {row.prompt.slice(0, 80)}
        </strong>
        <div className="q-answer">
          {row.answer.image ? <img src={row.answer.image} alt="Scanned answer" /> : row.answer.text ?? "(blank)"}
        </div>
      </div>
      <div className="q-stack">
        <div className="q-marks">
          <div className="q-mark-box">
            <span className="t-small t-muted">First mark</span>
            <strong>
              {row.first?.points ?? "—"}/{row.max_points}
            </strong>
            {row.first?.comment && <span className="t-small">{row.first.comment}</span>}
          </div>
          <div className="q-mark-box">
            <span className="t-small t-muted">Second mark · {row.marker?.name}</span>
            <strong>
              {row.second?.points ?? "—"}/{row.max_points}
            </strong>
            {row.second?.comment && <span className="t-small">{row.second.comment}</span>}
          </div>
          <div className="q-mark-box">
            <span className="t-small t-muted">AI suggestion</span>
            <strong>{row.ai ? `${row.ai.points}/${row.max_points}` : "—"}</strong>
            {row.ai?.confidence != null && <span className="t-small">confidence {rate(row.ai.confidence)}</span>}
          </div>
        </div>
        <div className="t-field">
          <label htmlFor={`mod-p-${row.id}`}>Agreed mark (tolerance {fixed(row.tolerance)} pts)</label>
          <input
            id={`mod-p-${row.id}`}
            className="input q-points-input"
            type="number"
            min={0}
            max={row.max_points}
            step={0.5}
            value={points}
            onChange={(e) => setPoints(e.target.value)}
          />
        </div>
        <div className="t-field">
          <label htmlFor={`mod-c-${row.id}`}>Comment for the student</label>
          <textarea id={`mod-c-${row.id}`} className="textarea" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        {err && (
          <div className="alert alert-error" role="alert">
            {err}
          </div>
        )}
        <div className="q-actions">
          <button type="button" className="btn btn-primary" onClick={save} disabled={busy || points === ""}>
            Set agreed mark
          </button>
        </div>
      </div>
    </article>
  );
}
