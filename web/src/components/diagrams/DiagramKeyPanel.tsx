import { useEffect, useState } from "react";
import { diagramsApi, type DiagramSpec, type ExamDiagramQuestion } from "../../api/diagrams";
import { errorText } from "../teacher/util";
import { DiagramEditor } from "./DiagramEditor";
import { checklistError } from "./diagramLogic";

/**
 * Scan & Grade answer-key step, written-answer questions: mark the question as a graph / chart /
 * diagram for grading and edit its checklist. Saved in the answer key (works after the exam was
 * given); it overrides the checklist set in the exam editor.
 */
export function DiagramKeyPanel({
  examId,
  questionId,
  points,
  info,
  onSaved,
}: {
  examId: string;
  questionId: string;
  points: number;
  info: ExamDiagramQuestion | undefined;
  onSaved: (q: ExamDiagramQuestion) => void;
}) {
  const [spec, setSpec] = useState<DiagramSpec | null>(info?.diagram ?? null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  useEffect(() => setSpec(info?.diagram ?? null), [info]);

  const dirty = JSON.stringify(spec) !== JSON.stringify(info?.diagram ?? null);
  const fromQuestion = info?.source === "question" || (info?.source === "key" && !!info.question_diagram);

  async function save(next: DiagramSpec | null) {
    if (next) {
      const msg = checklistError(next, points);
      if (msg) {
        setErr(msg);
        return;
      }
    }
    setBusy(true);
    setErr(null);
    try {
      const res = await diagramsApi.save(examId, questionId, next);
      onSaved(res);
      setOk(next ? "Checklist saved." : "Saved.");
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dg-keypanel">
      <DiagramEditor
        idPrefix={`akq-${questionId}`}
        value={spec}
        points={points}
        offLabel={fromQuestion ? "Written answer (keep the exam's checklist)" : "Written answer (text, working)"}
        onChange={(s) => {
          setOk(null);
          setSpec(s);
        }}
      />
      {(dirty || err || ok) && (
        <div className="sc-inline">
          {dirty && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => save(spec)}
            >
              {busy ? "Saving…" : spec ? "Save checklist" : "Use a written answer"}
            </button>
          )}
          {err && (
            <span className="field-error" role="alert">
              {err}
            </span>
          )}
          {ok && !dirty && (
            <span className="t-muted sc-small" role="status">
              {ok}
            </span>
          )}
        </div>
      )}
      {info?.diagram && !dirty && (
        <span className="hint">The AI grades this drawing with the checklist; rubric criteria below are not used.</span>
      )}
      {info?.source === "key" && info.question_diagram && !dirty && (
        <button type="button" className="t-link-btn" disabled={busy} onClick={() => save(null)}>
          Use the checklist from the exam editor
        </button>
      )}
    </div>
  );
}
