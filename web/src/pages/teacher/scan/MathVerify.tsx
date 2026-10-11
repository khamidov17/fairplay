// Math check UI: LaTeX rendering (KaTeX) and SymPy verification badges for the review screen.
import katex from "katex";
import "katex/dist/katex.min.css";
import { useMemo } from "react";
import type { SuggestionStep, Verification, VerifyStatus } from "../../../api/scanAi";
import "../../../styles/mathVerify.css";
import { disagreementLine, finalLines, verificationSummary, verifyBadge } from "./verifyLogic";

/** Render LaTeX with KaTeX; on any error show the source in monospace. */
export function Tex({ latex, display = false }: { latex: string; display?: boolean }) {
  const html = useMemo(() => {
    try {
      return katex.renderToString(latex, { throwOnError: true, displayMode: display, output: "html", strict: "ignore" });
    } catch {
      return null;
    }
  }, [latex, display]);
  if (html === null) return <code className="mv-src">{latex}</code>;
  return <span className="mv-tex" data-latex={latex} dangerouslySetInnerHTML={{ __html: html }} />;
}

export function VerifyBadge({ status, ecf, reason }: { status?: VerifyStatus | null; ecf?: boolean; reason?: string }) {
  const b = verifyBadge(status, ecf);
  if (!b) return null;
  return (
    <span className={`mv-badge is-${b.tone}`} title={reason ? `${b.title}: ${reason}` : b.title} aria-label={`${b.title}${reason ? `: ${reason}` : ""}`}>
      <span aria-hidden="true">{b.symbol}</span> {b.label}
    </span>
  );
}

/** Extra lines under a step: the LaTeX, the verification badge and a one-line disagreement. */
export function StepCheck({ step }: { step: SuggestionStep }) {
  const why = disagreementLine(step);
  if (!step.latex && !step.verify) return null;
  return (
    <span className="mv-step">
      {step.latex && <Tex latex={step.latex} />}
      <VerifyBadge status={step.verify} ecf={step.ecf} reason={step.verify_reason} />
      {why && (
        <span className="mv-why" role="note">
          {why}
        </span>
      )}
    </span>
  );
}

/** Summary + final answer / units / sig-fig checks under the steps. */
export function VerificationPanel({ v, finalLatex }: { v?: Verification | null; finalLatex?: string | null }) {
  const summary = verificationSummary(v);
  const lines = finalLines(v);
  if (!v || v.checked === false || (!summary && !lines.length)) return null;
  const changed = (v.disagreements || []).filter((d) => d.changes_points);
  return (
    <div className="mv-panel" aria-label="Math check">
      {summary && <p className="mv-summary">{summary}</p>}
      {finalLatex && (
        <p className="mv-final">
          <span className="t-muted">Final answer:</span> <Tex latex={finalLatex} />
          {v.final?.key && (
            <>
              {" "}
              <span className="t-muted">key</span> <Tex latex={v.final.key} />
            </>
          )}
        </p>
      )}
      <ul className="mv-lines">
        {lines.map((l, i) => (
          <li key={i}>
            <VerifyBadge status={l.status} />
            <span>{l.text}</span>
          </li>
        ))}
      </ul>
      {changed.length > 0 && (
        <p className="mv-why" role="note">
          {changed.map((d) => d.reason).join(" · ")}
        </p>
      )}
    </div>
  );
}
