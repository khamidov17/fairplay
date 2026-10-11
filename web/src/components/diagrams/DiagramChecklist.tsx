import { useEffect, useState } from "react";
import type { DraftDiagram } from "../../api/diagrams";
import type { Criterion } from "../../api/scanAi";
import { setCriterion, toggleCriterion, type ReviewForm } from "../../pages/teacher/scan/reviewLogic";
import { STATUS_ICON, STATUS_WORD, statusFor } from "./diagramLogic";
import "../../styles/diagrams.css";

/**
 * Review screen, graph / diagram questions: the feature checklist with ✓ toggles (same points
 * logic as rubric criteria — ticking a feature recomputes the total), the AI's note per feature
 * and the picture check's warnings.
 */
export function DiagramChecklist({
  idPrefix,
  criteria,
  diagram,
  form,
  setForm,
}: {
  idPrefix: string;
  criteria: Criterion[];
  diagram: DraftDiagram | null | undefined;
  form: ReviewForm;
  setForm: (f: ReviewForm) => void;
}) {
  const ai = new Map((diagram?.features ?? []).map((f) => [f.id, f]));
  const issues = diagram?.check?.issues ?? [];
  return (
    <div className="dg-check">
      <p className="rv-section-h">
        Checklist{diagram?.kind_label ? ` · ${diagram.kind_label}` : ""}
      </p>
      <ol className="dg-list">
        {criteria.map((c, i) => {
          const v = form.crit[c.id] ?? 0;
          const a = ai.get(c.id);
          const status = statusFor(v, c.points, a?.status);
          return (
            <li key={c.id} className={`dg-row is-${status}`} data-feature={c.id}>
              <span className="dg-num" aria-hidden="true">
                {i + 1}
              </span>
              <input
                type="checkbox"
                id={`${idPrefix}-f-${c.id}`}
                checked={v >= c.points && c.points > 0}
                onChange={(e) => setForm(toggleCriterion(form, c, e.target.checked))}
              />
              <label htmlFor={`${idPrefix}-f-${c.id}`}>{c.label}</label>
              <FeaturePoints
                label={`Points for ${c.label} (out of ${c.points})`}
                value={v}
                onValue={(n) => setForm(setCriterion(form, c, n))}
              />
              <span className={`dg-status is-${status}`} data-status={status}>
                <span aria-hidden="true">{STATUS_ICON[status]}</span> {STATUS_WORD[status]}
              </span>
              {(a?.note || c.description) && (
                <span className="dg-note">
                  {a?.note ? <span>{a.note}</span> : null}
                  {a?.note && c.description ? " · " : ""}
                  {c.description ? <span className="t-muted">expected: {c.description}</span> : null}
                </span>
              )}
            </li>
          );
        })}
      </ol>
      {issues.length > 0 && (
        <div className="dg-issues" role="note">
          <strong>Picture check:</strong>
          <ul>
            {issues.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}
      {(diagram?.readings?.length ?? 0) > 0 && (
        <p className="t-muted sc-small">
          AI read: {diagram!.readings.map((r) => `(${r.x}, ${r.y})`).join(" ")}
        </p>
      )}
    </div>
  );
}

function FeaturePoints({ label, value, onValue }: { label: string; value: number; onValue: (n: number) => void }) {
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
