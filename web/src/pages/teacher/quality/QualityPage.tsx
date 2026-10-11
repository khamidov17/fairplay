// Phase 3: "Grading quality" across exams — how well AI suggestions agree with the final
// marks, how often teachers correct them, grading time, worst disagreements, benchmark.
// Data: GET /api/quality/overview?course=&exam_ids=
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { fixed, qualityApi, rate, seconds, type QualityOverview } from "../../../api/quality";
import { Loading } from "../../../components/Loading";
import { errorText, plural } from "../../../components/teacher/util";
import { QualityTiles, TimingChart, TypeAgreement, WorstTable, typeLabel } from "./qualityViz";

export function QualityNav() {
  return (
    <nav className="q-subnav" aria-label="Grading quality">
      <Link className="t-btn-outline" to="/teacher/quality">Overview</Link>
      <Link className="t-btn-outline" to="/teacher/quality/benchmarks">Benchmark</Link>
      <Link className="t-btn-outline" to="/teacher/quality/second-marking">My second marking</Link>
    </nav>
  );
}

export default function QualityPage() {
  const [course, setCourse] = useState("");
  const [data, setData] = useState<QualityOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [table, setTable] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    let alive = true;
    setError(null);
    qualityApi.overview(course).then(
      (d) => alive && setData(d),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [course]);

  const weakTypes = Object.entries(data?.weak_areas.types ?? {});
  const weakSubjects = Object.entries(data?.weak_areas.subjects ?? {});

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Grading quality{course ? ` · ${course}` : ""}</h1>
          <p>
            {data
              ? `${plural(data.agreement.n, "AI-suggested grade")} compared with the final mark · ${plural(data.graded, "graded answer")}`
              : "How closely AI suggestions match teachers' final marks."}
          </p>
        </div>
        <div className="t-head-actions">
          <label className="t-gb-filter" htmlFor="q-course">
            Course
            <select id="q-course" className="t-select" value={course} onChange={(e) => setCourse(e.target.value)}>
              <option value="">All courses</option>
              {(data?.courses ?? []).map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <QualityNav />
        </div>
      </header>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {!data && !error && <Loading label="Loading grading quality…" />}
      {data && (
        <div className="q-stack">
          {data.agreement.n === 0 && (
            <div className="t-card">
              <div className="t-empty">
                <strong>No AI-suggested grades yet</strong>
                Numbers appear once answers with an AI suggestion have a final teacher mark. You can still build a
                benchmark of professor-marked answers to test the AI first.
              </div>
            </div>
          )}
          <QualityTiles a={data.agreement} c={data.correction} t={data.timing} />

          <section className="t-card" aria-labelledby="q-type-h">
            <div className="t-card-head">
              <h2 id="q-type-h">Agreement by question type</h2>
              <label className="q-check">
                <input type="checkbox" checked={table} onChange={(e) => setTable(e.target.checked)} />
                Table view
              </label>
            </div>
            <TypeAgreement groups={data.by_type} table={table} />
          </section>

          <div className="q-two">
            <section className="t-card" aria-labelledby="q-time-h">
              <h2 id="q-time-h">Grading time</h2>
              <TimingChart t={data.timing} />
            </section>
            <section className="t-card" aria-labelledby="q-bench-h">
              <div className="t-card-head">
                <h2 id="q-bench-h">Benchmark</h2>
                <Link className="t-link-btn" to="/teacher/quality/benchmarks">
                  Open benchmark
                </Link>
              </div>
              {data.benchmarks.length === 0 ? (
                <p className="t-muted">
                  No benchmark runs yet. Add answers students agreed to share, with your marks, and test the AI on them.
                </p>
              ) : (
                <ul className="t-small" style={{ margin: 0, paddingLeft: 18 }}>
                  {data.benchmarks.map((b) => (
                    <li key={b.run_id}>
                      <Link to={`/teacher/quality/runs/${encodeURIComponent(b.run_id)}`}>{b.name}</Link>:{" "}
                      {rate(b.overall?.exact)} exact · kappa {fixed(b.overall?.kappa)} ·{" "}
                      {plural(b.overall?.n ?? 0, "item")}
                    </li>
                  ))}
                </ul>
              )}
              {(weakTypes.length > 0 || weakSubjects.length > 0) && (
                <div className="alert alert-warning" role="note">
                  Answers in these areas always go to manual review (benchmark kappa below the limit):{" "}
                  {[...weakTypes.map(([t, k]) => `${typeLabel(t)} (${fixed(k)})`), ...weakSubjects.map(([s, k]) => `${s} (${fixed(k)})`)].join(", ")}
                </div>
              )}
            </section>
          </div>

          <section className="t-card" aria-labelledby="q-exams-h">
            <div className="t-card-head">
              <h2 id="q-exams-h">By exam</h2>
              <label className="t-gb-filter" htmlFor="q-open-exam">
                Review routing and second marking for
                <select
                  id="q-open-exam"
                  className="t-select"
                  value=""
                  onChange={(e) => e.target.value && navigate(`/teacher/quality/exams/${encodeURIComponent(e.target.value)}`)}
                >
                  <option value="">Choose an exam…</option>
                  {data.all_exams.map((e) => (
                    <option key={e.exam_id} value={e.exam_id}>
                      {e.title}
                      {e.course ? ` · ${e.course}` : ""}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {data.exams.length === 0 ? (
              <p className="t-muted">No graded answers yet.</p>
            ) : (
              <div className="t-table-scroll">
                <table className="t-table">
                  <thead>
                    <tr>
                      <th scope="col">Exam</th>
                      <th scope="col" className="num">AI-suggested</th>
                      <th scope="col" className="num">Exact</th>
                      <th scope="col" className="num">Kappa</th>
                      <th scope="col" className="num">Severe</th>
                      <th scope="col" className="num">Changed by you</th>
                      <th scope="col" className="num">Time with AI / by hand</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.exams.map((e) => (
                      <tr key={e.exam_id}>
                        <td>
                          <Link className="t-row-link" to={`/teacher/quality/exams/${encodeURIComponent(e.exam_id)}`}>
                            {e.title}
                          </Link>
                          {e.course && <span className="t-muted t-small"> · {e.course}</span>}
                        </td>
                        <td className="num">{e.n}</td>
                        <td className="num">{rate(e.exact)}</td>
                        <td className="num">{fixed(e.kappa)}</td>
                        <td className="num">{rate(e.severe_rate)}</td>
                        <td className="num">{rate(e.correction_rate)}</td>
                        <td className="num">
                          {seconds(e.mean_seconds_ai)} / {seconds(e.mean_seconds_manual)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="t-card" aria-labelledby="q-worst-h">
            <h2 id="q-worst-h">Biggest disagreements</h2>
            <WorstTable rows={data.worst} showExam />
          </section>
        </div>
      )}
    </>
  );
}
