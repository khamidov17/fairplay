// /student/homework/:id — read the questions, photograph each page of your work (phone camera),
// put the pages in order, hand in (again until the due date when allowed), then see feedback.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { studentHomeworkApi, type StudentHomeworkDetail } from "../../../api/homework";
import { QuestionImages } from "../../../components/QuestionImages";
import { StudentLayout, StuLoading } from "../../../components/student/StudentLayout";
import { errorText } from "../../../components/teacher/util";
import "../../../styles/homework.css";
import {
  MAX_PAGES,
  STATE_LABEL,
  addPages,
  dueLabel,
  movePage,
  removePage,
  stateTone,
  submitLabel,
  validatePages,
  type PageItem,
} from "./uploadLogic";

export default function HomeworkUploadPage() {
  const { id = "" } = useParams();
  const [hw, setHw] = useState<StudentHomeworkDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pages, setPages] = useState<PageItem[]>([]);
  const [rejected, setRejected] = useState<string[]>([]);
  const [progress, setProgress] = useState<number | null>(null);
  const [sent, setSent] = useState(false);
  const cameraRef = useRef<HTMLInputElement>(null);
  const filesRef = useRef<HTMLInputElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await studentHomeworkApi.get(id);
      setHw(d);
      if (timer.current) clearTimeout(timer.current);
      if (d.state === "processing") timer.current = setTimeout(load, 2500);
    } catch (e) {
      setError(errorText(e));
    }
  }, [id]);

  useEffect(() => {
    load();
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load]);

  useEffect(() => () => pages.forEach((p) => p.url && URL.revokeObjectURL(p.url)), []); // eslint-disable-line react-hooks/exhaustive-deps

  function onFiles(list: FileList | null) {
    if (!list || list.length === 0) return;
    const r = addPages(pages, Array.from(list));
    setPages(r.pages);
    setRejected(r.rejected);
    setSent(false);
  }

  async function submit() {
    const problem = validatePages(pages);
    if (problem) return setError(problem);
    setError(null);
    setProgress(0);
    try {
      await studentHomeworkApi.submit(id, pages.map((p) => p.file), setProgress);
      pages.forEach((p) => p.url && URL.revokeObjectURL(p.url));
      setPages([]);
      setSent(true);
      await load();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setProgress(null);
    }
  }

  if (!hw)
    return (
      <StudentLayout>
        <main className="stu-main narrow">{error ? <div className="stu-alert error" role="alert">{error}</div> : <StuLoading label="Loading homework…" />}</main>
      </StudentLayout>
    );

  const canAdd = hw.can_submit && pages.length < MAX_PAGES;
  return (
    <StudentLayout>
      <main className="stu-main narrow hw-upload">
        <Link to="/student/homework" className="stu-back">
          ← Homework
        </Link>
        <div className="stu-head">
          <span className="stu-eyebrow">{hw.class_names.join(", ")}</span>
          <h1 className="stu-h1">{hw.title}</h1>
          <p className="stu-sub">
            {dueLabel(hw.due_at)} · <span className={`stu-pill ${stateTone(hw)}`}>{hw.overdue ? "Overdue" : STATE_LABEL[hw.state]}</span>
            {hw.late && <span className="stu-pill warn">Late</span>}
          </p>
        </div>

        {hw.released && (
          <section className="stu-card hw-feedback" aria-labelledby="fb-h">
            <h2 id="fb-h">Your feedback is ready</h2>
            <p className="hw-big">
              {hw.score} / {hw.total_points} <span className="stu-muted">({hw.percent}%)</span>
            </p>
            <p className="stu-muted">See what went wrong in each step and how to fix it.</p>
            <div className="stu-row">
              <Link className="stu-btn primary" to={`/student/grades/${hw.exam_id}`}>
                See feedback
              </Link>
              <Link className="stu-btn" to="/student/progress">
                My progress
              </Link>
            </div>
          </section>
        )}

        {hw.instructions && <p className="stu-card hw-instructions">{hw.instructions}</p>}
        {hw.worksheet && (
          <button type="button" className="stu-btn" onClick={() => studentHomeworkApi.downloadWorksheet(hw.exam_id, hw.worksheet!.name)}>
            Download worksheet ({hw.worksheet.name})
          </button>
        )}

        <section className="stu-card" aria-labelledby="q-h">
          <h2 id="q-h">Questions</h2>
          <ol className="hw-stu-questions">
            {hw.questions.map((q) => (
              <li key={q.question_id}>
                <QuestionImages images={q.images} />
                <span className="stu-serif">{q.prompt}</span>
                {q.options.length > 0 && (
                  <ol type="A" className="hw-stu-options">
                    {q.options.map((o, i) => (
                      <li key={i}>{o}</li>
                    ))}
                  </ol>
                )}
                <span className="stu-muted stu-small"> · {q.points} pt{q.points === 1 ? "" : "s"}</span>
              </li>
            ))}
          </ol>
        </section>

        {hw.state === "processing" && (
          <div className="stu-alert" role="status">
            Reading your pages… you can leave this page; your teacher gets them either way.
          </div>
        )}
        {hw.state === "failed" && hw.error && (
          <div className="stu-alert error" role="alert">
            {hw.error}
          </div>
        )}
        {sent && hw.state !== "failed" && (
          <div className="stu-alert success" role="status">
            Handed in{hw.late ? " (late)" : ""}. Your teacher will review it and release feedback.
          </div>
        )}
        {hw.pages.length > 0 && pages.length === 0 && (
          <section className="stu-card" aria-labelledby="sent-h">
            <h2 id="sent-h">What you handed in</h2>
            <div className="hw-pages">
              {hw.pages.map((u, i) => (
                <a key={u} href={u} target="_blank" rel="noreferrer" className="hw-page">
                  <img src={u} alt={`Page ${i + 1} you handed in`} />
                  <span>Page {i + 1}</span>
                </a>
              ))}
            </div>
          </section>
        )}

        {hw.can_submit ? (
          <section className="stu-card" aria-labelledby="up-h">
            <h2 id="up-h">{hw.state === "not_submitted" || hw.state === "failed" ? "Hand in your work" : "Hand in again"}</h2>
            <p className="stu-muted stu-small">
              One photo per page, flat on a table, in good light. Put them in order before you hand in.
              {hw.state === "handed_in" && " The new photos replace what you handed in."}
            </p>
            <input
              ref={cameraRef}
              type="file"
              accept="image/*"
              capture="environment"
              multiple
              hidden
              data-testid="camera-input"
              onChange={(e) => {
                onFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <input
              ref={filesRef}
              type="file"
              accept="image/*,application/pdf"
              multiple
              hidden
              data-testid="file-input"
              onChange={(e) => {
                onFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <div className="stu-row">
              <button type="button" className="stu-btn primary" disabled={!canAdd} onClick={() => cameraRef.current?.click()}>
                Take photo
              </button>
              <button type="button" className="stu-btn" disabled={!canAdd} onClick={() => filesRef.current?.click()}>
                Choose files
              </button>
            </div>
            {rejected.length > 0 && (
              <p className="stu-alert warn" role="alert">
                Not a photo or PDF: {rejected.join(", ")}
              </p>
            )}
            {pages.length > 0 && (
              <ol className="hw-pages editable" aria-label="Pages to hand in">
                {pages.map((p, i) => (
                  <li key={p.id} className="hw-page">
                    {p.url ? <img src={p.url} alt={`Page ${i + 1}`} /> : <span className="hw-pdf">PDF</span>}
                    <span>Page {i + 1}</span>
                    <span className="hw-page-tools">
                      <button type="button" className="stu-btn sm quiet" aria-label={`Move page ${i + 1} earlier`} disabled={i === 0}
                        onClick={() => setPages(movePage(pages, i, -1))}>
                        ←
                      </button>
                      <button type="button" className="stu-btn sm quiet" aria-label={`Move page ${i + 1} later`} disabled={i === pages.length - 1}
                        onClick={() => setPages(movePage(pages, i, 1))}>
                        →
                      </button>
                      <button type="button" className="stu-btn sm quiet" aria-label={`Delete page ${i + 1}`}
                        onClick={() => {
                          if (p.url) URL.revokeObjectURL(p.url);
                          setPages(removePage(pages, p.id));
                        }}>
                        ✕
                      </button>
                    </span>
                  </li>
                ))}
              </ol>
            )}
            {progress != null && (
              <progress max={1} value={progress} aria-label="Uploading" className="hw-progress" />
            )}
            {error && (
              <div className="stu-alert error" role="alert">
                {error}
              </div>
            )}
            <button type="button" className="stu-btn primary lg block" disabled={pages.length === 0 || progress != null} onClick={submit}>
              {progress != null ? "Handing in…" : submitLabel(hw, pages.length)}
            </button>
          </section>
        ) : (
          !hw.released &&
          hw.block_reason && <p className="stu-muted">{hw.block_reason}.</p>
        )}
      </main>
    </StudentLayout>
  );
}
