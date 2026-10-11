// Pure helpers for the math check (SymPy step verification) shown on the review screen.
import type { SuggestionStep, Verification, VerifyStatus } from "../../../api/scanAi";

export interface Badge {
  tone: "ok" | "error" | "unknown";
  symbol: string;
  label: string;
  title: string;
}

/** Badge for one verification status ("proved ✓ / error ✗ / can't check ?"). */
export function verifyBadge(status: VerifyStatus | undefined | null, ecf = false): Badge | null {
  if (!status) return null;
  if (status === "verified_ok")
    return ecf
      ? { tone: "ok", symbol: "✓", label: "ECF", title: "Math check: correct given an earlier error (error carried forward)" }
      : { tone: "ok", symbol: "✓", label: "Proved", title: "Math check: this step follows (checked with SymPy)" };
  if (status === "verified_error")
    return { tone: "error", symbol: "✗", label: "Error", title: "Math check: this step does not follow" };
  return { tone: "unknown", symbol: "?", label: "Can't check", title: "Math check: this line could not be checked" };
}

/** One line explaining why the math check and the AI disagree on a step (null when they agree). */
export function disagreementLine(step: SuggestionStep): string | null {
  if (step.teacher_flipped) return null;
  if (step.verifier_disagrees) return step.verifier_disagrees;
  if (!step.verify) return null;
  if (step.verify === "verified_error" && step.status === "correct")
    return `Math check finds a problem${step.verify_reason ? `: ${step.verify_reason}` : ""}`;
  if (step.verify === "verified_ok" && step.status === "error" && !step.ecf)
    return `Math check says this step follows${step.verify_reason ? ` (${step.verify_reason})` : ""}`;
  return null;
}

/** Short summary of the whole check, e.g. "Math check: 3 proved · 1 error · 1 can't check". */
export function verificationSummary(v: Verification | undefined | null): string | null {
  if (!v || !Array.isArray(v.steps)) return null;
  const c = { ok: 0, err: 0, unk: 0 };
  for (const s of v.steps) {
    if (s.status === "verified_ok") c.ok += 1;
    else if (s.status === "verified_error") c.err += 1;
    else c.unk += 1;
  }
  if (!v.steps.length && !v.final) return null;
  const parts = [];
  if (c.ok) parts.push(`${c.ok} proved`);
  if (c.err) parts.push(`${c.err} ${c.err === 1 ? "error" : "errors"}`);
  if (c.unk) parts.push(`${c.unk} can't check`);
  return `Math check: ${parts.join(" · ") || "nothing to check"}`;
}

/** Lines for the final answer / units / significant figures checks. */
export function finalLines(v: Verification | undefined | null): { status: VerifyStatus; text: string }[] {
  if (!v) return [];
  const out: { status: VerifyStatus; text: string }[] = [];
  if (v.final && v.final.status !== "unverifiable")
    out.push({ status: v.final.status, text: `Final answer ${v.final.reason}${v.final.ecf ? " — ECF" : ""}` });
  else if (v.final) out.push({ status: "unverifiable", text: `Final answer: ${v.final.reason}` });
  if (v.units) out.push({ status: v.units.status, text: `Units: ${v.units.reason}` });
  if (v.sig_figs && v.sig_figs.ok !== null)
    out.push({
      status: v.sig_figs.ok ? "verified_ok" : "verified_error",
      text: `Significant figures: ${Array.isArray(v.sig_figs.given) ? v.sig_figs.given.join("–") : v.sig_figs.given} (needs ${v.sig_figs.required})`,
    });
  return out;
}
