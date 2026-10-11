import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { SuggestionStep, Verification } from "../api/scanAi";
import { StepCheck, Tex, VerificationPanel, VerifyBadge } from "../pages/teacher/scan/MathVerify";
import { disagreementLine, finalLines, verificationSummary, verifyBadge } from "../pages/teacher/scan/verifyLogic";

const step = (over: Partial<SuggestionStep> = {}): SuggestionStep => ({
  n: 2, text: "2x = 4", status: "correct", note: "", ...over,
});

const V: Verification = {
  engine: "sympy+pint",
  version: "fp-verify-v1",
  steps: [
    { n: 1, status: "unverifiable", reason: "first line", ecf: false, latex: "2x + 3 = 7" },
    { n: 2, status: "verified_error", reason: "does not follow", ecf: false, latex: "2x = 10" },
    { n: 3, status: "verified_ok", reason: "correct given the error in step 2", ecf: true, latex: "x = 5" },
  ],
  final: { status: "verified_error", reason: "does not match the key", student: "x = 5", key: "x = 2", ecf: true },
  units: { status: "verified_error", reason: "no units (expected m/s)", student: null, key: "m/s" },
  sig_figs: { required: 3, given: [1, 2], ok: false },
  disagreements: [
    { n: 2, model: "correct", verifier: "verified_error", reason: "SymPy finds a mistake here", changes_points: true },
    { n: 3, model: "error", verifier: "verified_ok", reason: "minor", changes_points: false },
  ],
};

describe("verifyLogic", () => {
  it("maps statuses to proved / error / can't check badges", () => {
    expect(verifyBadge("verified_ok")).toMatchObject({ tone: "ok", symbol: "✓", label: "Proved" });
    expect(verifyBadge("verified_ok", true)).toMatchObject({ tone: "ok", label: "ECF" });
    expect(verifyBadge("verified_error")).toMatchObject({ tone: "error", symbol: "✗" });
    expect(verifyBadge("unverifiable")).toMatchObject({ tone: "unknown", symbol: "?", label: "Can't check" });
    expect(verifyBadge(undefined)).toBeNull();
  });

  it("explains disagreements in one line", () => {
    expect(disagreementLine(step({ verifier_disagrees: "SymPy proves this step follows" }))).toBe(
      "SymPy proves this step follows",
    );
    expect(disagreementLine(step({ verify: "verified_error", verify_reason: "x = 2 becomes x = 5" }))).toBe(
      "Math check finds a problem: x = 2 becomes x = 5",
    );
    expect(disagreementLine(step({ status: "error", verify: "verified_ok" }))).toMatch(/follows/);
    expect(disagreementLine(step({ status: "error", verify: "verified_ok", ecf: true }))).toBeNull();
    expect(disagreementLine(step({ verify: "verified_ok" }))).toBeNull();
    expect(disagreementLine(step({ verifier_disagrees: "x", teacher_flipped: true }))).toBeNull();
  });

  it("summarises the whole check and the final answer", () => {
    expect(verificationSummary(V)).toBe("Math check: 1 proved · 1 error · 1 can't check");
    expect(verificationSummary(undefined)).toBeNull();
    const lines = finalLines(V);
    expect(lines.map((l) => l.status)).toEqual(["verified_error", "verified_error", "verified_error"]);
    expect(lines[0].text).toBe("Final answer does not match the key — ECF");
    expect(lines[2].text).toBe("Significant figures: 1–2 (needs 3)");
  });
});

describe("MathVerify components", () => {
  it("renders LaTeX with KaTeX, and the source in monospace when it cannot", () => {
    const { container } = render(
      <div>
        <Tex latex="\frac{1}{2}" />
        <Tex latex="\frac{1" />
      </div>,
    );
    expect(container.querySelector(".mv-tex .katex")).not.toBeNull();
    expect(container.querySelector(".mv-tex")?.getAttribute("data-latex")).toBe("\\frac{1}{2}");
    expect(container.querySelector("code.mv-src")?.textContent).toBe("\\frac{1");
  });

  it("shows a badge per step next to the model's mark, with the disagreement", () => {
    render(
      <ol>
        <li>
          <StepCheck
            step={step({
              latex: "2x = 4",
              verify: "verified_ok",
              verify_reason: "follows from step 1",
              model_status: "error",
              verifier_disagrees: "SymPy proves this step follows from the previous one; the AI marked it wrong",
            })}
          />
        </li>
        <li>
          <StepCheck step={step({ n: 3, latex: "x = 5", verify: "verified_error", verify_reason: "not equal" })} />
        </li>
        <li>
          <StepCheck step={step({ n: 4, text: "count atoms", verify: "unverifiable", verify_reason: "could not read" })} />
        </li>
        <li>
          <StepCheck step={step({ n: 5 })} />
        </li>
      </ol>,
    );
    expect(screen.getByLabelText(/this step follows \(checked with SymPy\): follows from step 1/)).toHaveTextContent("Proved");
    expect(screen.getByLabelText(/does not follow: not equal/)).toHaveTextContent("✗ Error");
    expect(screen.getByLabelText(/could not be checked/)).toHaveTextContent("? Can't check");
    expect(screen.getByText(/the AI marked it wrong/)).toBeInTheDocument();
    expect(screen.getByText(/Math check finds a problem: not equal/)).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")[3]).toBeEmptyDOMElement();
  });

  it("shows the final answer, units and sig-fig checks with point-changing disagreements", () => {
    render(<VerificationPanel v={V} finalLatex="x = 5" />);
    expect(screen.getByText("Math check: 1 proved · 1 error · 1 can't check")).toBeInTheDocument();
    expect(screen.getByText("Units: no units (expected m/s)")).toBeInTheDocument();
    expect(screen.getByText("SymPy finds a mistake here")).toBeInTheDocument();
    expect(screen.queryByText("minor")).toBeNull();
    expect(screen.getByLabelText("Math check").querySelectorAll(".katex").length).toBe(2);
  });

  it("renders nothing without a verification", () => {
    const { container } = render(
      <div>
        <VerificationPanel v={undefined} />
        <VerificationPanel v={{ ...V, checked: false }} />
        <VerifyBadge status={null} />
      </div>,
    );
    expect(container.firstChild).toBeEmptyDOMElement();
  });
});
