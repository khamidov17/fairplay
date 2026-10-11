// Phase 3: one benchmark run — AI marks vs the professor's marks, by type and subject.
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { fixed, qualityApi, rate, signedPts, type BenchmarkRunDetail } from "../../../api/quality";
import { Loading } from "../../../components/Loading";
import { errorText, plural } from "../../../components/teacher/util";
import { QualityNav } from "./QualityPage";
import { AgreementTable, HBars, typeLabel } from "./qualityViz";
import "./quality.css";

const POLL_MS = 1500;

export default function BenchmarkRunPage() {
  const { id = "" } = useParams();
  const [run, setRun] = useState<BenchmarkRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = () =>
      qualityApi.run(id).then(
        (r) => {
          if (!alive) return;
          setRun(r);
          if (r.status === "running") timer = setTimeout(tick, POLL_MS);
        },
        (e) => alive && setError(errorText(e)),
      );
    void tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [id]);

  if (error) {
    return (
      <div className="alert alert-error" role="alert">
        {error}
      </div>
    );
  }
  if (!run) return <Loading label="Loading run…" />;
  const o = run.metrics.overall;
  const hh = run.metrics.human_vs_human;
  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <span className="t-eyebrow">
            {run.set && <Link to={`/teacher/quality/benchmarks/${encodeURIComponent(run.set.id)}`}>{run.set.name}</Link>}
          </span>
          <h1>Benchmark run</h1>
          <p>
            {[run.grader, run.provider, run.model, run.prompt_version].filter(Boolean).join(" · ")} ·{" "}
            {run.status === "running" ? `running ${run.done_count}/${run.item_count}` : run.status}
          </p>
        </div>
        <div className="t-head-actions">
          <QualityNav />
        </div>
      </header>
      {run.status === "failed" && (
        <div className="alert alert-error" role="alert">
          The run failed: {run.error}
        </div>
      )}
      {run.status === "running" && <Loading label={`AI is marking ${run.done_count} of ${run.item_count}…`} />}
      {o && (
        <div className="q-stack">
          <div className="q-two">
            <section className="t-card" aria-labelledby="run-sum-h">
              <h2 id="run-sum-h">AI vs your marks</h2>
              <HBars
                label="Agreement"
                max={1}
                rows={[
                  { label: "Exact", value: o.exact, display: rate(o.exact) },
                  { label: "Within 1 pt", value: o.within_1, display: rate(o.within_1) },
                  { label: "Within 10%", value: o.within_10pct, display: rate(o.within_10pct) },
                  { label: "Severe", value: o.severe_rate, display: rate(o.severe_rate) },
                ]}
              />
              <p className="t-small" style={{ margin: 0 }}>
                {plural(o.n, "answer")} · kappa {fixed(o.kappa)} · mean error {fixed(o.mae)} pts · bias{" "}
                {signedPts(o.bias)}
                {run.metrics.failed ? ` · ${run.metrics.failed} could not be marked` : ""}
              </p>
            </section>
            <section className="t-card" aria-labelledby="run-hh-h">
              <h2 id="run-hh-h">Teachers vs each other</h2>
              {hh ? (
                <p className="t-small" style={{ margin: 0 }}>
                  On {plural(hh.n, "answer")} with two human marks, teachers agreed exactly {rate(hh.exact)} of the time
                  (kappa {fixed(hh.kappa)}, mean difference {fixed(hh.mae)} pts). Use this as the bar the AI should reach.
                </p>
              ) : (
                <p className="t-muted">Add a second mark to some answers (e.g. “7; 8”) to compare teachers with each other.</p>
              )}
            </section>
          </div>
          <section className="t-card" aria-labelledby="run-type-h">
            <h2 id="run-type-h">By question type</h2>
            <AgreementTable groups={run.metrics.by_type ?? []} keyLabel="Question type" />
          </section>
          <section className="t-card" aria-labelledby="run-sub-h">
            <h2 id="run-sub-h">By subject</h2>
            <AgreementTable groups={run.metrics.by_subject ?? []} keyLabel="Subject" />
          </section>
          <section className="t-card" aria-labelledby="run-items-h">
            <h2 id="run-items-h">Answers, biggest difference first</h2>
            <div className="t-table-scroll">
              <table className="t-table">
                <thead>
                  <tr>
                    <th scope="col">Question</th>
                    <th scope="col" className="num">Yours</th>
                    <th scope="col" className="num">AI</th>
                    <th scope="col" className="num">Difference</th>
                    <th scope="col">AI said</th>
                  </tr>
                </thead>
                <tbody>
                  {run.items.map((it) => (
                    <tr key={it.item_id}>
                      <td>
                        <div className="q-prompt">{it.prompt}</div>
                        <span className="t-small t-muted">{typeLabel(it.question_type)}</span>
                      </td>
                      <td className="num">
                        {it.human_points}/{it.max_points}
                      </td>
                      <td className="num">{it.ai_points == null ? "—" : `${it.ai_points}/${it.max_points}`}</td>
                      <td className="num">
                        {it.diff == null ? "—" : signedPts(it.diff)}
                        {it.severe && <span className="t-pill orange q-sev">Severe</span>}
                      </td>
                      <td className="t-small">
                        {it.error ? <span className="t-muted">Failed: {it.error}</span> : it.explanation}
                        {it.needs_review && <span className="q-chip"> asked for review</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}
    </>
  );
}
