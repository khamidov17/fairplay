// Phase 3: one benchmark set — its answers, adding answers (from graded exam answers with
// consent, by hand, or CSV + images zip) and AI runs.
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { teacherApi } from "../../../api/client";
import type { Exam } from "../../../api/types";
import {
  fixed,
  qualityApi,
  rate,
  type BenchmarkSetDetail,
  type GradedAnswer,
  type ImportResult,
} from "../../../api/quality";
import { Loading } from "../../../components/Loading";
import { errorText, plural } from "../../../components/teacher/util";
import { shortDate } from "../../../components/teacher/insights/viz";
import { QualityNav } from "./QualityPage";
import { typeLabel } from "./qualityViz";
import "./quality.css";

export default function BenchmarkSetPage() {
  const { id = "" } = useParams();
  const [set, setSet] = useState<BenchmarkSetDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const navigate = useNavigate();

  const load = useCallback(() => {
    qualityApi.set(id).then(setSet, (e) => setError(errorText(e)));
  }, [id]);
  useEffect(load, [load]);

  async function run() {
    setMsg(null);
    try {
      const r = await qualityApi.startRun(id);
      navigate(`/teacher/quality/runs/${encodeURIComponent(r.id)}`);
    } catch (e) {
      setMsg(errorText(e));
    }
  }

  async function removeItem(itemId: string) {
    try {
      await qualityApi.deleteItem(id, itemId);
      load();
    } catch (e) {
      setMsg(errorText(e));
    }
  }

  if (error) {
    return (
      <div className="alert alert-error" role="alert">
        {error}
      </div>
    );
  }
  if (!set) return <Loading label="Loading benchmark…" />;
  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <span className="t-eyebrow">
            <Link to="/teacher/quality/benchmarks">Benchmark</Link>
          </span>
          <h1>{set.name}</h1>
          <p>
            {[set.subject, plural(set.items.length, "answer"), set.consent_basis && `consent: ${set.consent_basis}`]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
        <div className="t-head-actions">
          <button type="button" className="btn btn-primary" onClick={run} disabled={set.items.length === 0}>
            Run the AI on this set
          </button>
          <QualityNav />
        </div>
      </header>
      {msg && (
        <div className="alert alert-info" role="status">
          {msg}
        </div>
      )}
      <div className="q-stack">
        {set.runs.length > 0 && (
          <section className="t-card" aria-labelledby="bset-runs-h">
            <h2 id="bset-runs-h">Runs</h2>
            <div className="t-table-scroll">
              <table className="t-table">
                <thead>
                  <tr>
                    <th scope="col">Run</th>
                    <th scope="col">Status</th>
                    <th scope="col" className="num">Exact</th>
                    <th scope="col" className="num">Within 1 pt</th>
                    <th scope="col" className="num">Kappa</th>
                    <th scope="col" className="num">Severe</th>
                  </tr>
                </thead>
                <tbody>
                  {set.runs.map((r) => (
                    <tr key={r.id}>
                      <td>
                        <Link className="t-row-link" to={`/teacher/quality/runs/${encodeURIComponent(r.id)}`}>
                          {shortDate(r.created_at)} · {r.grader}
                        </Link>
                        <span className="t-muted t-small"> · {[r.provider, r.model, r.prompt_version].filter(Boolean).join(" / ")}</span>
                      </td>
                      <td>{r.status === "running" ? `Running ${r.done_count}/${r.item_count}` : r.status}</td>
                      <td className="num">{rate(r.metrics.overall?.exact)}</td>
                      <td className="num">{rate(r.metrics.overall?.within_1)}</td>
                      <td className="num">{fixed(r.metrics.overall?.kappa)}</td>
                      <td className="num">{rate(r.metrics.overall?.severe_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
        <section className="t-card" aria-labelledby="bset-items-h">
          <h2 id="bset-items-h">Answers</h2>
          {set.items.length === 0 ? (
            <p className="t-muted">No answers yet. Add them below.</p>
          ) : (
            <div className="t-table-scroll">
              <table className="t-table">
                <thead>
                  <tr>
                    <th scope="col">Question</th>
                    <th scope="col">Answer</th>
                    <th scope="col" className="num">Your mark</th>
                    <th scope="col">
                      <span className="sr-only">Remove</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {set.items.map((it) => (
                    <tr key={it.id}>
                      <td>
                        <div className="q-prompt">{it.question_prompt}</div>
                        <span className="t-small t-muted">
                          {typeLabel(it.question_type)}
                          {it.subject ? ` · ${it.subject}` : ""}
                        </span>
                      </td>
                      <td className="t-small">
                        {it.answer_image ? "Picture" : ""}
                        {it.answer_image && it.answer_text ? " + " : ""}
                        {it.answer_text ? <span className="q-prompt">{it.answer_text}</span> : null}
                      </td>
                      <td className="num">
                        {it.human_marks.join(" / ")} of {it.max_points}
                      </td>
                      <td>
                        <button type="button" className="t-link-btn" onClick={() => removeItem(it.id)}>
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <AddFromGraded setId={set.id} onAdded={load} />
        <AddByHand setId={set.id} onAdded={load} />
        <ImportCsv setId={set.id} onAdded={load} />
      </div>
    </>
  );
}

function ConsentBox({ checked, onChange, id }: { checked: boolean; onChange: (v: boolean) => void; id: string }) {
  return (
    <label className="q-check q-consent q-wide" htmlFor={id}>
      <input id={id} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>
        The student(s) agreed that these answers may be used to test grading. No names are copied.
      </span>
    </label>
  );
}

export function AddFromGraded({ setId, onAdded }: { setId: string; onAdded: () => void }) {
  const [exams, setExams] = useState<Exam[]>([]);
  const [examId, setExamId] = useState("");
  const [answers, setAnswers] = useState<GradedAnswer[] | null>(null);
  const [consent, setConsent] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [added, setAdded] = useState<Set<string>>(new Set());

  useEffect(() => {
    teacherApi.listExams().then((xs) => setExams(xs.filter((x) => x.status !== "draft")), () => undefined);
  }, []);
  useEffect(() => {
    if (!examId) {
      setAnswers(null);
      return;
    }
    qualityApi.graded(examId).then(setAnswers, (e) => setMsg(errorText(e)));
  }, [examId]);

  async function add(a: GradedAnswer) {
    setMsg(null);
    try {
      await qualityApi.addFromGrade(setId, { exam_id: examId, attempt_id: a.attempt_id, question_id: a.question_id, consent });
      setAdded((s) => new Set(s).add(`${a.attempt_id}:${a.question_id}`));
      onAdded();
    } catch (e) {
      setMsg(errorText(e));
    }
  }

  return (
    <section className="t-card" aria-labelledby="bset-graded-h">
      <h2 id="bset-graded-h">Add graded answers</h2>
      <div className="q-form">
        <div className="t-field">
          <label htmlFor="bset-exam">Exam</label>
          <select id="bset-exam" className="t-select" value={examId} onChange={(e) => setExamId(e.target.value)}>
            <option value="">Choose an exam…</option>
            {exams.map((e) => (
              <option key={e.id} value={e.id}>
                {e.title}
              </option>
            ))}
          </select>
        </div>
        <ConsentBox id="bset-graded-consent" checked={consent} onChange={setConsent} />
      </div>
      {msg && (
        <p className="t-small" role="alert">
          {msg}
        </p>
      )}
      {answers && answers.length === 0 && <p className="t-muted">No graded answers in this exam yet.</p>}
      {answers && answers.length > 0 && (
        <div className="t-table-scroll">
          <table className="t-table">
            <thead>
              <tr>
                <th scope="col">Answer</th>
                <th scope="col" className="num">Mark</th>
                <th scope="col">
                  <span className="sr-only">Add</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {answers.map((a) => {
                const key = `${a.attempt_id}:${a.question_id}`;
                return (
                  <tr key={key}>
                    <td>
                      {a.anon_id} · Q{a.question_index + 1} <span className="q-prompt">{a.prompt}</span>
                    </td>
                    <td className="num">
                      {a.final}/{a.max_points}
                    </td>
                    <td>
                      {added.has(key) ? (
                        <span className="t-pill green">Added</span>
                      ) : (
                        <button
                          type="button"
                          className="t-btn-outline"
                          disabled={!consent}
                          title={consent ? undefined : "Tick the consent box first"}
                          onClick={() => add(a)}
                        >
                          Add to benchmark
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function AddByHand({ setId, onAdded }: { setId: string; onAdded: () => void }) {
  const [prompt, setPrompt] = useState("");
  const [type, setType] = useState("essay");
  const [maxPoints, setMaxPoints] = useState("10");
  const [answer, setAnswer] = useState("");
  const [marks, setMarks] = useState("");
  const [comment, setComment] = useState("");
  const [consent, setConsent] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    const parsed = marks
      .split(/[;,/\s]+/)
      .filter(Boolean)
      .map(Number);
    try {
      await qualityApi.addItem(setId, {
        question_prompt: prompt,
        question_type: type,
        max_points: Number(maxPoints),
        answer_text: answer,
        human_marks: parsed,
        human_comment: comment,
        consent,
      });
      setPrompt("");
      setAnswer("");
      setMarks("");
      setComment("");
      setMsg("Added.");
      onAdded();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  return (
    <details className="t-card">
      <summary className="t-link-btn">Add an answer by hand</summary>
      <form className="q-form" onSubmit={submit}>
        <div className="t-field q-wide">
          <label htmlFor="bh-prompt">Question</label>
          <textarea id="bh-prompt" className="textarea" rows={2} required value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        </div>
        <div className="t-field">
          <label htmlFor="bh-type">Type</label>
          <select id="bh-type" className="t-select" value={type} onChange={(e) => setType(e.target.value)}>
            <option value="essay">Essay / written</option>
            <option value="short">Short answer</option>
            <option value="numeric">Number</option>
          </select>
        </div>
        <div className="t-field">
          <label htmlFor="bh-max">Max points</label>
          <input id="bh-max" className="input" type="number" min={0.5} step={0.5} value={maxPoints} onChange={(e) => setMaxPoints(e.target.value)} />
        </div>
        <div className="t-field">
          <label htmlFor="bh-marks">Your mark(s), e.g. 7 or 7; 8</label>
          <input id="bh-marks" className="input" required value={marks} onChange={(e) => setMarks(e.target.value)} />
        </div>
        <div className="t-field q-wide">
          <label htmlFor="bh-answer">Student answer</label>
          <textarea id="bh-answer" className="textarea" rows={4} required value={answer} onChange={(e) => setAnswer(e.target.value)} />
        </div>
        <div className="t-field q-wide">
          <label htmlFor="bh-comment">Your comment</label>
          <input id="bh-comment" className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        <ConsentBox id="bh-consent" checked={consent} onChange={setConsent} />
        <div className="q-actions q-wide">
          <button type="submit" className="btn btn-primary" disabled={!consent}>
            Add answer
          </button>
          {msg && (
            <span className="t-small" role="status">
              {msg}
            </span>
          )}
        </div>
      </form>
    </details>
  );
}

function ImportCsv({ setId, onAdded }: { setId: string; onAdded: () => void }) {
  const [csv, setCsv] = useState<File | null>(null);
  const [zip, setZip] = useState<File | null>(null);
  const [consent, setConsent] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!csv) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await qualityApi.importCsv(setId, csv, zip, consent);
      setResult(r);
      onAdded();
    } catch (err) {
      setMsg(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="t-card">
      <summary className="t-link-btn">Import a CSV (and a zip of answer pictures)</summary>
      <p className="t-small t-muted" style={{ margin: 0 }}>
        Columns: prompt, type, max_points, rubric, answer_key, answer_text, image (file name in the zip), human_mark
        (e.g. 7 or 7;8), comment. Name or email columns are ignored.
      </p>
      <form className="q-form" onSubmit={submit}>
        <div className="t-field">
          <label htmlFor="imp-csv">CSV file</label>
          <input id="imp-csv" type="file" accept=".csv,text/csv" onChange={(e) => setCsv(e.target.files?.[0] ?? null)} />
        </div>
        <div className="t-field">
          <label htmlFor="imp-zip">Pictures (zip, optional)</label>
          <input id="imp-zip" type="file" accept=".zip,application/zip" onChange={(e) => setZip(e.target.files?.[0] ?? null)} />
        </div>
        <ConsentBox id="imp-consent" checked={consent} onChange={setConsent} />
        <div className="q-actions q-wide">
          <button type="submit" className="btn btn-primary" disabled={!csv || !consent || busy}>
            {busy ? "Importing…" : "Import"}
          </button>
        </div>
      </form>
      {msg && (
        <div className="alert alert-error" role="alert">
          {msg}
        </div>
      )}
      {result && (
        <div role="status" className="t-small">
          Imported {plural(result.imported, "answer")}.
          {result.dropped_personal_columns.length > 0 && ` Ignored personal columns: ${result.dropped_personal_columns.join(", ")}.`}
          {result.errors.length > 0 && (
            <ul>
              {result.errors.map((er) => (
                <li key={er.row}>
                  Row {er.row}: {er.error}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </details>
  );
}
