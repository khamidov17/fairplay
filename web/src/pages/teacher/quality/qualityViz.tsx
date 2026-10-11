// Phase 3: small charts for the grading-quality pages, in the Insights style: one hue
// (primary blue), numbers always printed next to the bar, hover titles, tables nearby.
import { Link } from "react-router-dom";
import {
  kappaWord,
  rate,
  seconds,
  signedPts,
  fixed,
  type Agreement,
  type AgreementGroup,
  type Correction,
  type Disagreement,
  type Timing,
} from "../../../api/quality";
import { TYPE_LABEL } from "../examDraft";
import "./quality.css";

export interface BarRow {
  label: string;
  value: number | null;
  display: string;
  title?: string;
}

/** Horizontal bars on a 0..max scale. Each row prints its value; empty rows show "—". */
export function HBars({ rows, max, label }: { rows: BarRow[]; max: number; label: string }) {
  return (
    <ul className="q-bars" aria-label={label}>
      {rows.map((r) => {
        const w = r.value == null || max <= 0 ? 0 : Math.max(0, Math.min(100, (r.value / max) * 100));
        return (
          <li key={r.label} className="q-bar-row" title={r.title ?? `${r.label}: ${r.display}`}>
            <span className="q-bar-label">{r.label}</span>
            <span className="q-bar-track" aria-hidden="true">
              {r.value != null && <span className="q-bar-fill" style={{ width: `${w}%` }} />}
            </span>
            <span className="q-bar-value">{r.display}</span>
          </li>
        );
      })}
    </ul>
  );
}

export function typeLabel(t: string): string {
  return (TYPE_LABEL as Record<string, string>)[t] ?? (t ? t[0].toUpperCase() + t.slice(1) : "Other");
}

/** KPI tiles: agreement, kappa, severe errors, correction rate, time per answer. */
export function QualityTiles({ a, c, t }: { a: Agreement; c: Correction; t: Timing }) {
  const severeWarn = (a.severe_rate ?? 0) > 0.05;
  return (
    <div className="t-stats q-tiles">
      <div className="t-stat">
        <span className="t-stat-value blue">{rate(a.exact)}</span>
        <span className="t-stat-label">
          AI matched the final mark exactly · {rate(a.within_1)} within 1 point
        </span>
      </div>
      <div className="t-stat">
        <span className="t-stat-value">{fixed(a.kappa)}</span>
        <span className="t-stat-label">Agreement (weighted kappa): {kappaWord(a.kappa)}</span>
      </div>
      <div className={`t-stat${severeWarn ? " warn" : ""}`}>
        <span className={`t-stat-value${severeWarn ? " orange" : ""}`}>
          {a.severe}
          <small> · {rate(a.severe_rate)}</small>
        </span>
        <span className="t-stat-label">Severe disagreements</span>
      </div>
      <div className="t-stat">
        <span className="t-stat-value">{rate(c.correction_rate)}</span>
        <span className="t-stat-label">
          AI suggestions you changed · average change {c.mean_edit == null ? "—" : `${fixed(c.mean_edit)} pts`}
        </span>
      </div>
      <div className="t-stat">
        <span className="t-stat-value">{seconds(t.ai_assisted.mean)}</span>
        <span className="t-stat-label">Per answer with AI · {seconds(t.manual.mean)} by hand</span>
      </div>
      <div className="t-stat">
        <span className="t-stat-value">{signedPts(a.bias)}</span>
        <span className="t-stat-label">
          AI bias ({(a.bias ?? 0) > 0 ? "more generous" : (a.bias ?? 0) < 0 ? "stricter" : "even"}) · mean error{" "}
          {a.mae == null ? "—" : `${fixed(a.mae)} pts`}
        </span>
      </div>
    </div>
  );
}

/** Exact / within-1 agreement per question type as bars + a table view. */
export function TypeAgreement({ groups, table }: { groups: AgreementGroup[]; table: boolean }) {
  if (groups.length === 0) return <p className="t-muted">No AI-suggested grades yet.</p>;
  if (table) return <AgreementTable groups={groups} keyLabel="Question type" />;
  return (
    <div className="q-type-grid">
      {groups.map((g) => (
        <div key={g.key} className="q-type-block">
          <h3>
            {typeLabel(g.key)} <span className="t-muted t-small">· {g.n} answers · kappa {fixed(g.kappa)}</span>
          </h3>
          <HBars
            label={`${typeLabel(g.key)} agreement`}
            max={1}
            rows={[
              { label: "Exact", value: g.exact, display: rate(g.exact) },
              { label: "Within 1 pt", value: g.within_1, display: rate(g.within_1) },
              { label: "Within 10%", value: g.within_10pct, display: rate(g.within_10pct) },
              { label: "Severe", value: g.severe_rate, display: rate(g.severe_rate) },
            ]}
          />
        </div>
      ))}
    </div>
  );
}

export function AgreementTable({ groups, keyLabel }: { groups: AgreementGroup[]; keyLabel: string }) {
  return (
    <div className="t-table-scroll">
      <table className="t-table">
        <thead>
          <tr>
            <th scope="col">{keyLabel}</th>
            <th scope="col" className="num">Answers</th>
            <th scope="col" className="num">Exact</th>
            <th scope="col" className="num">Within 1 pt</th>
            <th scope="col" className="num">Within 10%</th>
            <th scope="col" className="num">Mean error</th>
            <th scope="col" className="num">Bias</th>
            <th scope="col" className="num">Kappa</th>
            <th scope="col" className="num">Severe</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <tr key={g.key}>
              <td className="strong">{keyLabel === "Question type" ? typeLabel(g.key) : g.key || "—"}</td>
              <td className="num">{g.n}</td>
              <td className="num">{rate(g.exact)}</td>
              <td className="num">{rate(g.within_1)}</td>
              <td className="num">{rate(g.within_10pct)}</td>
              <td className="num">{g.mae == null ? "—" : `${fixed(g.mae)} pts`}</td>
              <td className="num">{signedPts(g.bias)}</td>
              <td className="num">{fixed(g.kappa)}</td>
              <td className="num">{rate(g.severe_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Grading time per answer: AI-assisted vs by hand. */
export function TimingChart({ t }: { t: Timing }) {
  const max = Math.max(t.ai_assisted.mean ?? 0, t.manual.mean ?? 0);
  if (!t.ai_assisted.n && !t.manual.n) {
    return <p className="t-muted">No grading times recorded yet.</p>;
  }
  return (
    <>
      <HBars
        label="Average grading time per answer"
        max={max}
        rows={[
          { label: "With AI", value: t.ai_assisted.mean, display: `${seconds(t.ai_assisted.mean)} · ${t.ai_assisted.n}` },
          { label: "By hand", value: t.manual.mean, display: `${seconds(t.manual.mean)} · ${t.manual.n}` },
        ]}
      />
      {t.time_saved_fraction != null && (
        <p className="t-small t-muted">
          {t.time_saved_fraction >= 0
            ? `AI-assisted grading is ${rate(t.time_saved_fraction)} faster per answer.`
            : `AI-assisted grading is ${rate(-t.time_saved_fraction)} slower per answer.`}
        </p>
      )}
    </>
  );
}

export const itemPath = (d: { exam_id: string; attempt_id: string }) =>
  `/teacher/exams/${encodeURIComponent(d.exam_id)}/submissions/${encodeURIComponent(d.attempt_id)}`;

/** Worst disagreements table; each row links to the graded answer. */
export function WorstTable({ rows, showExam }: { rows: Disagreement[]; showExam: boolean }) {
  if (rows.length === 0) return <p className="t-muted">No disagreements: every AI suggestion was kept as is.</p>;
  return (
    <div className="t-table-scroll">
      <table className="t-table">
        <thead>
          <tr>
            <th scope="col">Answer</th>
            <th scope="col" className="num">AI</th>
            <th scope="col" className="num">Final</th>
            <th scope="col" className="num">Difference</th>
            <th scope="col">How graded</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={`${d.attempt_id}:${d.question_id}`}>
              <td>
                <Link className="t-row-link" to={itemPath(d)}>
                  {showExam ? `${d.exam_title} · ` : ""}Q{d.question_index + 1} · {d.anon_id}
                </Link>
                <div className="q-prompt">{d.prompt}</div>
              </td>
              <td className="num">
                {d.ai_points}/{d.max_points}
              </td>
              <td className="num">
                {d.final_points}/{d.max_points}
              </td>
              <td className="num">
                {signedPts(d.diff)}
                {d.severe && <span className="t-pill orange q-sev">Severe</span>}
              </td>
              <td className="t-small">
                {d.source === "ai_edited" ? "AI edited" : d.source === "ai_accepted" ? "AI accepted" : "By hand"}
                {d.graded_by != null && <span className="t-muted"> · {String(d.graded_by)}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
