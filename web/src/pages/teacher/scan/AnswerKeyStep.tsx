import { useCallback, useEffect, useRef, useState } from "react";
import {
  scanAiApi,
  type AnswerKey,
  type CommonMistake,
  type KeyQuestion,
  type KeySuggestion,
} from "../../../api/scanAi";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, num, plural } from "../../../components/teacher/util";
import "../../../styles/scanAi.css";
import { Tex } from "./MathVerify";

interface CritRow {
  id?: string;
  label: string;
  points: string;
  description: string;
  ecf?: boolean;
}

interface Form {
  solution: string;
  criteria: CritRow[];
  partial_credit: string;
  common_mistakes: { label: string; tag: string; deduction: string }[];
  // math check (LaTeX); optional so older form objects still work
  final_latex?: string;
  units?: string;
  solution_latex?: string;
  sig_figs?: string;
}

/** Sum of the criteria points typed in the form (NaN rows count as 0). */
export function criteriaTotal(rows: { points: string }[]): number {
  return rows.reduce((s, r) => s + (Number.isFinite(Number(r.points)) ? Number(r.points) : 0), 0);
}

/** Error message for a question key form, or null when it can be saved. */
export function keyFormError(form: Form, max: number): string | null {
  for (const c of form.criteria) {
    if (!c.label.trim()) return "Each criterion needs a label.";
    const n = Number(c.points);
    if (c.points.trim() === "" || !Number.isFinite(n) || n < 0) return `Criterion “${c.label}” needs points.`;
  }
  if (form.criteria.length > 0 && Math.abs(criteriaTotal(form.criteria) - max) > 1e-6)
    return `Rubric points must add up to ${num(max)} (now ${num(criteriaTotal(form.criteria))}).`;
  const sf = (form.sig_figs ?? "").trim();
  if (sf && !(Number.isInteger(Number(sf)) && Number(sf) >= 1 && Number(sf) <= 15))
    return "Significant figures must be a whole number from 1 to 15.";
  return null;
}

function formFrom(src: {
  solution: string;
  criteria: { id?: string; label: string; points: number; description: string; ecf?: boolean }[];
  partial_credit: string;
  common_mistakes: CommonMistake[];
  final_latex?: string;
  units?: string;
  solution_latex?: string[];
  sig_figs?: number | null;
}): Form {
  return {
    solution: src.solution ?? "",
    final_latex: src.final_latex ?? "",
    units: src.units ?? "",
    solution_latex: (src.solution_latex ?? []).join("\n"),
    sig_figs: src.sig_figs ? String(src.sig_figs) : "",
    criteria: (src.criteria ?? []).map((c) => ({ id: c.id, label: c.label, points: String(c.points), description: c.description ?? "", ecf: !!c.ecf })),
    partial_credit: src.partial_credit ?? "",
    common_mistakes: (src.common_mistakes ?? []).map((m) => ({
      label: m.label,
      tag: m.tag ?? "",
      deduction: m.deduction === null || m.deduction === undefined ? "" : String(m.deduction),
    })),
  };
}

export function AnswerKeyStep({ examId }: { examId: string }) {
  const [key, setKey] = useState<AnswerKey | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const apply = useCallback((k: AnswerKey) => {
    setKey(k);
    setText(k.text);
  }, []);

  useEffect(() => {
    let alive = true;
    scanAiApi.key(examId).then((k) => alive && apply(k), (e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [examId, apply]);

  async function act(name: string, fn: () => Promise<void>) {
    setBusy(name);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  if (!key) return error ? <div className="alert alert-error" role="alert">{error}</div> : <Loading label="Loading answer key…" />;

  return (
    <div className="sc-step-body">
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="alert alert-success" role="status">
          {notice}
        </div>
      )}
      <section className="t-card" aria-labelledby="ak-h">
        <div className="sc-card-head">
          <div>
            <h3 id="ak-h">Answer key</h3>
            <p className="t-muted sc-small">
              The AI grades every answer against this key and your rubric. Upload the key you already have, or paste it.
            </p>
          </div>
          {key.ai_enabled && (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy !== null}
              onClick={() =>
                act("draft", async () => {
                  const k = await scanAiApi.draftKey(examId);
                  setKey(k);
                  setNotice(`AI drafted ${plural(k.drafted ?? 0, "rubric")}. Review each one and press “Use AI draft”.`);
                })
              }
            >
              {busy === "draft" ? "Drafting…" : "Draft rubrics with AI"}
            </button>
          )}
          {key.ai_enabled && (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy !== null}
              title="Convert each worked solution and final answer to LaTeX once, so the math check can verify steps"
              onClick={() =>
                act("latex", async () => {
                  const k = await scanAiApi.keyLatex(examId);
                  apply(k);
                  const warn = Object.keys(k.latex_warnings ?? {}).length;
                  setNotice(
                    `Converted ${plural(k.converted ?? 0, "answer key")} to LaTeX for the math check.` +
                      (warn ? ` ${plural(warn, "question")} could not be read fully — check the LaTeX fields.` : ""),
                  );
                })
              }
            >
              {busy === "latex" ? "Converting…" : "Key to LaTeX (math check)"}
            </button>
          )}
        </div>
        <div className="ak-top">
          <div className="t-field">
            <label htmlFor="ak-subject">Subject</label>
            <select
              id="ak-subject"
              className="t-select"
              value={key.subject}
              onChange={(e) => act("subject", async () => apply(await scanAiApi.saveKey(examId, { subject: e.target.value })))}
            >
              {key.subjects.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div className="t-field ak-files">
            <span className="ak-label">Key files</span>
            <ul className="ak-filelist">
              {key.files.map((f) => (
                <li key={f.id}>
                  <span>
                    {f.name} <span className="t-muted">· {f.kind}{f.pages.length ? ` · ${plural(f.pages.length, "page")}` : ""}</span>
                  </span>
                  <button
                    type="button"
                    className="t-btn-outline"
                    aria-label={`Remove ${f.name}`}
                    onClick={() => act("rm", async () => apply(await scanAiApi.deleteKeyFile(examId, f.id)))}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <label className="t-btn-outline sc-file-btn">
              {busy === "file" ? "Uploading…" : "Upload key (PDF, image, text)"}
              <input
                ref={fileRef}
                type="file"
                className="sr-only"
                accept="application/pdf,image/png,image/jpeg,.txt,.md,.tex"
                disabled={busy !== null}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f)
                    act("file", async () => {
                      apply(await scanAiApi.uploadKeyFile(examId, f));
                      if (fileRef.current) fileRef.current.value = "";
                    });
                }}
              />
            </label>
          </div>
        </div>
        <div className="t-field">
          <label htmlFor="ak-text">Answer key text (pasted, plus text read from uploaded PDFs)</label>
          <textarea id="ak-text" className="textarea" rows={4} value={text} onChange={(e) => setText(e.target.value)} />
        </div>
        <div className="sc-inline">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy !== null || text === key.text}
            onClick={() =>
              act("text", async () => {
                apply(await scanAiApi.saveKey(examId, { text }));
                setNotice("Answer key saved.");
              })
            }
          >
            Save key text
          </button>
        </div>
      </section>

      {key.questions.map((q) => (
        <QuestionKeyCard key={q.question_id} examId={examId} q={q} misconceptions={key.misconceptions} />
      ))}
    </div>
  );
}

function QuestionKeyCard({ examId, q, misconceptions }: { examId: string; q: KeyQuestion; misconceptions: string[] }) {
  const [form, setForm] = useState<Form>(() => formFrom(q.key));
  const [suggestion, setSuggestion] = useState<KeySuggestion | null>(q.ai_suggestion);
  const [saved, setSaved] = useState(q.key);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const id = `akq-${q.question_id}`;
  const hasEcf = form.criteria.length > 0;

  useEffect(() => setSuggestion(q.ai_suggestion), [q.ai_suggestion]);

  const set = (patch: Partial<Form>) => {
    setOk(null);
    setForm((f) => ({ ...f, ...patch }));
  };
  const total = criteriaTotal(form.criteria);

  async function save() {
    const msg = keyFormError(form, q.points);
    if (msg) {
      setErr(msg);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const res = await scanAiApi.saveQuestionKey(examId, q.question_id, {
        solution: form.solution,
        criteria: form.criteria.map((c) => ({
          id: c.id,
          label: c.label.trim(),
          points: Number(c.points),
          description: c.description,
          ecf: !!c.ecf,
        })),
        final_latex: (form.final_latex ?? "").trim(),
        units: (form.units ?? "").trim(),
        solution_latex: (form.solution_latex ?? "").split("\n").map((l) => l.trim()).filter(Boolean),
        sig_figs: (form.sig_figs ?? "").trim() ? Number(form.sig_figs) : null,
        partial_credit: form.partial_credit,
        common_mistakes: form.common_mistakes
          .filter((m) => m.label.trim())
          .map((m) => ({ label: m.label.trim(), tag: m.tag || null, deduction: m.deduction.trim() === "" ? null : Number(m.deduction) })),
      });
      setSaved(res.key);
      setForm(formFrom(res.key));
      setWarnings(res.latex_warnings ?? []);
      setOk("Saved.");
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="t-card ak-q" aria-labelledby={`${id}-h`}>
      <div className="sc-card-head">
        <div>
          <h3 id={`${id}-h`}>
            Question {q.index + 1} <span className="t-muted">· {plural(q.points, "point")} · {q.type}</span>
          </h3>
          <p className="sc-prompt">{q.prompt}</p>
          {q.correct_answer && (
            <p className="sc-key">
              <span className="t-muted">Correct answer:</span> {q.correct_answer}
            </p>
          )}
        </div>
        {saved.criteria_source === "key" ? (
          <Pill tone="green">Rubric set</Pill>
        ) : saved.criteria_source === "question" ? (
          <Pill tone="blue">Uses the essay rubric</Pill>
        ) : (
          <Pill tone="grey">No rubric</Pill>
        )}
      </div>

      {suggestion && (
        <div className="ak-suggest" role="note">
          <span>
            <strong>AI draft ready</strong> — {plural(suggestion.criteria.length, "criterion", "criteria")}
            {suggestion.solution ? ", worked solution" : ""}. Nothing changes until you save.
          </span>
          <button
            type="button"
            className="t-btn-outline"
            onClick={() => {
              setForm(formFrom(suggestion));
              setOk(null);
            }}
          >
            Use AI draft
          </button>
        </div>
      )}

      <div className="t-field">
        <label htmlFor={`${id}-sol`}>Worked solution / final answer</label>
        <textarea id={`${id}-sol`} className="textarea" rows={3} value={form.solution} onChange={(e) => set({ solution: e.target.value })} />
      </div>

      <fieldset className="ak-fieldset">
        <legend>
          Math check (LaTeX){" "}
          {saved.latex_source && <span className="t-muted sc-small">· {saved.latex_source === "ai" ? "converted by AI" : "typed by you"}</span>}
        </legend>
        <p className="t-muted sc-small">
          Optional. With the final answer as LaTeX, every step and the final answer are checked with SymPy (units with
          conversion). Examples: <code>x = 2</code>, <code>{"\\frac{1}{3}"}</code>, <code>19.6</code> with unit <code>m/s</code>.
        </p>
        <div className="ak-latex">
          <div className="t-field">
            <label htmlFor={`${id}-fl`}>Final answer (LaTeX)</label>
            <input id={`${id}-fl`} className="input" value={form.final_latex ?? ""} onChange={(e) => set({ final_latex: e.target.value })} />
          </div>
          <div className="t-field">
            <label htmlFor={`${id}-un`}>Units</label>
            <input id={`${id}-un`} className="input" placeholder="e.g. m/s" value={form.units ?? ""} onChange={(e) => set({ units: e.target.value })} />
          </div>
          <div className="t-field">
            <label htmlFor={`${id}-sf`}>Sig. figures</label>
            <input
              id={`${id}-sf`}
              className="input"
              inputMode="numeric"
              placeholder="any"
              value={form.sig_figs ?? ""}
              onChange={(e) => set({ sig_figs: e.target.value })}
            />
          </div>
        </div>
        {(form.final_latex ?? "").trim() && (
          <p className="ak-latex-preview" aria-label="Preview of the final answer">
            <Tex latex={form.final_latex ?? ""} /> {form.units && <span className="t-muted">{form.units}</span>}
          </p>
        )}
        <div className="t-field">
          <label htmlFor={`${id}-sl`}>Worked solution in LaTeX (one step per line)</label>
          <textarea
            id={`${id}-sl`}
            className="textarea"
            rows={3}
            value={form.solution_latex ?? ""}
            onChange={(e) => set({ solution_latex: e.target.value })}
          />
        </div>
        {warnings.length > 0 && (
          <ul className="t-inline-error" role="alert">
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        )}
      </fieldset>

      <fieldset className="ak-fieldset">
        <legend>
          Rubric criteria{" "}
          <span className={`ak-total${form.criteria.length && Math.abs(total - q.points) > 1e-6 ? " is-bad" : ""}`}>
            {num(total)} / {num(q.points)}
          </span>
        </legend>
        {form.criteria.length === 0 && (
          <p className="t-muted sc-small">
            {saved.criteria_source === "question"
              ? "No key criteria: the essay rubric of the question is used."
              : "No criteria: the AI judges the whole answer. Add criteria for partial credit."}
          </p>
        )}
        {form.criteria.map((c, i) => (
          <div key={i} className={`ak-crit${hasEcf ? " has-ecf" : ""}`}>
            <input
              className="input"
              aria-label={`Criterion ${i + 1}`}
              placeholder="What earns the points"
              value={c.label}
              onChange={(e) => set({ criteria: form.criteria.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) })}
            />
            <input
              className="input ak-pts"
              aria-label={`Points for criterion ${i + 1}`}
              inputMode="decimal"
              value={c.points}
              onChange={(e) => set({ criteria: form.criteria.map((x, j) => (j === i ? { ...x, points: e.target.value } : x)) })}
            />
            <input
              className="input"
              aria-label={`How to judge criterion ${i + 1}`}
              placeholder="How to judge (optional)"
              value={c.description}
              onChange={(e) => set({ criteria: form.criteria.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)) })}
            />
            <label className="ak-ecf" title="Error carried forward: award this criterion when the method is right on a wrong earlier value">
              <input
                type="checkbox"
                aria-label={`ECF for criterion ${i + 1}`}
                checked={!!c.ecf}
                onChange={(e) => set({ criteria: form.criteria.map((x, j) => (j === i ? { ...x, ecf: e.target.checked } : x)) })}
              />
              ECF
            </label>
            <button
              type="button"
              className="t-btn-outline"
              aria-label={`Remove criterion ${i + 1}`}
              onClick={() => set({ criteria: form.criteria.filter((_, j) => j !== i) })}
            >
              ✕
            </button>
          </div>
        ))}
        <button
          type="button"
          className="t-btn-outline"
          onClick={() => set({ criteria: [...form.criteria, { label: "", points: "", description: "" }] })}
        >
          Add criterion
        </button>
      </fieldset>

      <div className="t-field">
        <label htmlFor={`${id}-pc`}>Partial credit rules</label>
        <textarea id={`${id}-pc`} className="textarea" rows={2} value={form.partial_credit} onChange={(e) => set({ partial_credit: e.target.value })} />
      </div>

      <fieldset className="ak-fieldset">
        <legend>Common mistakes</legend>
        {form.common_mistakes.map((m, i) => (
          <div key={i} className="ak-crit ak-mist">
            <input
              className="input"
              aria-label={`Mistake ${i + 1}`}
              placeholder="e.g. forgets +C"
              value={m.label}
              onChange={(e) => set({ common_mistakes: form.common_mistakes.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) })}
            />
            <select
              className="t-select"
              aria-label={`Tag for mistake ${i + 1}`}
              value={m.tag}
              onChange={(e) => set({ common_mistakes: form.common_mistakes.map((x, j) => (j === i ? { ...x, tag: e.target.value } : x)) })}
            >
              <option value="">No tag</option>
              {misconceptions.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <input
              className="input ak-pts"
              aria-label={`Deduction for mistake ${i + 1}`}
              placeholder="−pts"
              inputMode="decimal"
              value={m.deduction}
              onChange={(e) => set({ common_mistakes: form.common_mistakes.map((x, j) => (j === i ? { ...x, deduction: e.target.value } : x)) })}
            />
            <button
              type="button"
              className="t-btn-outline"
              aria-label={`Remove mistake ${i + 1}`}
              onClick={() => set({ common_mistakes: form.common_mistakes.filter((_, j) => j !== i) })}
            >
              ✕
            </button>
          </div>
        ))}
        <button
          type="button"
          className="t-btn-outline"
          onClick={() => set({ common_mistakes: [...form.common_mistakes, { label: "", tag: "", deduction: "" }] })}
        >
          Add mistake
        </button>
      </fieldset>

      {err && (
        <p className="t-inline-error" role="alert">
          {err}
        </p>
      )}
      <div className="sc-inline">
        <button type="button" className="btn btn-primary" disabled={busy} onClick={save}>
          {busy ? "Saving…" : `Save question ${q.index + 1}`}
        </button>
        {ok && (
          <span className="t-muted" role="status">
            {ok}
          </span>
        )}
      </div>
    </section>
  );
}
