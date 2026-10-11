// Phase 3: benchmark sets — real, permissioned student answers with the professor's marks,
// used to test the AI grader before trusting it. Data: /api/quality/benchmarks
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { fixed, qualityApi, rate, type BenchmarkSet } from "../../../api/quality";
import { Loading } from "../../../components/Loading";
import { errorText, plural } from "../../../components/teacher/util";
import { shortDate } from "../../../components/teacher/insights/viz";
import { QualityNav } from "./QualityPage";
import "./quality.css";

export default function BenchmarksPage() {
  const [sets, setSets] = useState<BenchmarkSet[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [subject, setSubject] = useState("");
  const [basis, setBasis] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    qualityApi.sets().then(setSets, (e) => setError(errorText(e)));
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const s = await qualityApi.createSet({ name, subject, consent_basis: basis, consent_note: note });
      navigate(`/teacher/quality/benchmarks/${encodeURIComponent(s.id)}`);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Grading benchmark</h1>
          <p>Answers students agreed to share, with your marks. Run the AI on them to see where it can be trusted.</p>
        </div>
        <div className="t-head-actions">
          <QualityNav />
        </div>
      </header>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      <div className="q-stack">
        <section className="t-card" aria-labelledby="bs-list-h">
          <h2 id="bs-list-h">Your sets</h2>
          {!sets ? (
            <Loading label="Loading sets…" />
          ) : sets.length === 0 ? (
            <p className="t-muted">No benchmark sets yet.</p>
          ) : (
            <div className="t-table-scroll">
              <table className="t-table">
                <thead>
                  <tr>
                    <th scope="col">Set</th>
                    <th scope="col" className="num">Answers</th>
                    <th scope="col" className="num">Last run: exact</th>
                    <th scope="col" className="num">Kappa</th>
                    <th scope="col">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {sets.map((s) => (
                    <tr key={s.id}>
                      <td>
                        <Link className="t-row-link" to={`/teacher/quality/benchmarks/${encodeURIComponent(s.id)}`}>
                          {s.name}
                        </Link>
                        {s.subject && <span className="t-muted t-small"> · {s.subject}</span>}
                      </td>
                      <td className="num">{s.item_count}</td>
                      <td className="num">{rate(s.last_run?.metrics.overall?.exact)}</td>
                      <td className="num">{fixed(s.last_run?.metrics.overall?.kappa)}</td>
                      <td>{shortDate(s.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <section className="t-card" aria-labelledby="bs-new-h">
          <h2 id="bs-new-h">New set</h2>
          <form className="q-form" onSubmit={create}>
            <div className="t-field">
              <label htmlFor="bs-name">Name</label>
              <input id="bs-name" className="input" required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="t-field">
              <label htmlFor="bs-subject">Subject</label>
              <input id="bs-subject" className="input" maxLength={100} value={subject} onChange={(e) => setSubject(e.target.value)} />
            </div>
            <div className="t-field">
              <label htmlFor="bs-basis">Consent basis</label>
              <input
                id="bs-basis"
                className="input"
                maxLength={64}
                placeholder="e.g. opt-in form"
                value={basis}
                onChange={(e) => setBasis(e.target.value)}
              />
            </div>
            <div className="t-field q-wide">
              <label htmlFor="bs-note">How students gave permission</label>
              <textarea id="bs-note" className="textarea" rows={2} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
            </div>
            <div className="q-actions q-wide">
              <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
                Create set
              </button>
              <span className="t-small t-muted">
                Student names are never stored in a benchmark. {sets ? plural(sets.length, "set") : ""}
              </span>
            </div>
          </form>
        </section>
      </div>
    </>
  );
}
