import { useCallback, useEffect, useRef, useState } from "react";
import { scanApi, type BatchStatus, type ScanBatch, type ScanStatus, type ScanStudent, type UnmatchedPage } from "../../../api/scan";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, formatWhen } from "../../../components/teacher/util";

const ACTIVE: BatchStatus[] = ["queued", "reading", "grading"];

export function batchLabel(b: ScanBatch): { text: string; tone: string } {
  switch (b.status) {
    case "queued":
      return { text: "Waiting", tone: "grey" };
    case "reading":
      return { text: b.page_count ? `Reading page ${Math.min(b.processed + 1, b.page_count)} of ${b.page_count}` : "Reading…", tone: "blue" };
    case "grading":
      return { text: b.ai_total ? `AI drafting ${b.ai_done} of ${b.ai_total}` : "AI drafting…", tone: "blue" };
    case "done":
      return { text: "Done", tone: "green" };
    case "stalled":
      return { text: "Stopped — upload again", tone: "orange" };
    default:
      return { text: "Failed", tone: "red" };
  }
}

export function UploadStep({ examId, onChanged }: { examId: string; onChanged?: () => void }) {
  const [status, setStatus] = useState<ScanStatus | null>(null);
  const [pages, setPages] = useState<UnmatchedPage[]>([]);
  const [students, setStudents] = useState<ScanStudent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    const [s, u] = await Promise.all([scanApi.status(examId), scanApi.unmatched(examId)]);
    setStatus(s);
    setPages(u);
    return s;
  }, [examId]);

  useEffect(() => {
    let alive = true;
    refresh().catch((e) => alive && setError(errorText(e)));
    scanApi.roster(examId).then((r) => alive && setStudents(r.all_students), () => undefined);
    return () => {
      alive = false;
    };
  }, [examId, refresh]);

  const active = status?.batches.some((b) => ACTIVE.includes(b.status)) ?? false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => {
      refresh().then((s) => {
        if (!s.batches.some((b) => ACTIVE.includes(b.status))) onChanged?.();
      }, () => undefined);
    }, 2000);
    return () => clearInterval(t);
  }, [active, refresh, onChanged]);

  async function upload(files: File[]) {
    if (files.length === 0) return;
    setError(null);
    setProgress(0);
    try {
      await scanApi.upload(examId, files, setProgress);
      await refresh();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setProgress(null);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  if (!status) return error ? <div className="alert alert-error" role="alert">{error}</div> : <Loading label="Loading scans…" />;

  return (
    <div className="sc-step-body">
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      <div
        className={`sc-drop${dragging ? " is-over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          upload(Array.from(e.dataTransfer.files));
        }}
      >
        <p className="sc-drop-title">Drop the scanned PDF here</p>
        <p className="t-muted">One big PDF from the copier is fine — pages can be in any order or upside down. Photos (JPEG/PNG) work too.</p>
        <label className="btn btn-primary sc-file-btn">
          Choose files
          <input
            ref={inputRef}
            type="file"
            className="sr-only"
            multiple
            accept="application/pdf,image/png,image/jpeg,image/tiff,image/webp"
            onChange={(e) => upload(Array.from(e.target.files ?? []))}
            disabled={progress !== null}
          />
        </label>
        {progress !== null && (
          <div className="sc-progress" role="progressbar" aria-label="Uploading" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
            <span style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
        )}
      </div>

      <div className="t-stats">
        <div className="t-stat">
          <span className="t-stat-label">Students scanned</span>
          <span className="t-stat-value">
            {status.students_scanned} / {status.students_expected}
          </span>
        </div>
        <div className="t-stat">
          <span className="t-stat-label">Pages to fix</span>
          <span className="t-stat-value">{status.unmatched}</span>
        </div>
        <div className="t-stat">
          <span className="t-stat-label">AI drafts</span>
          <span className="t-stat-value">{status.ai_enabled ? "On" : "Off"}</span>
        </div>
      </div>
      {!status.ai_enabled && (
        <p className="t-muted sc-small">
          AI is off on this server, so you will grade written answers by hand. Bubbles are still read automatically.
        </p>
      )}

      <section className="t-card" aria-labelledby="sc-batches-h">
        <h3 id="sc-batches-h">Uploads</h3>
        {status.batches.length === 0 ? (
          <div className="t-empty">
            <strong>No scans yet</strong>
            Scan the papers on a copier (200 dpi or more) and upload the PDF.
          </div>
        ) : (
          <div className="t-table-scroll">
            <table className="t-table">
              <thead>
                <tr>
                  <th scope="col">File</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">Pages</th>
                  <th scope="col" className="num">Matched</th>
                  <th scope="col" className="num">To fix</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {status.batches.map((b) => {
                  const l = batchLabel(b);
                  return (
                    <tr key={b.batch_id}>
                      <td>
                        <div className="sc-file-name">{b.filename || "Scan"}</div>
                        <div className="t-muted sc-small">{formatWhen(b.created_at)}</div>
                      </td>
                      <td>
                        <Pill tone={l.tone}>{l.text}</Pill>
                        {b.error && <div className="t-muted sc-small">{b.error}</div>}
                      </td>
                      <td className="num">{b.page_count}</td>
                      <td className="num">{b.matched}</td>
                      <td className="num">{b.unmatched}</td>
                      <td style={{ textAlign: "right" }}>
                        {b.status === "done" && status.ai_enabled && (
                          <button
                            type="button"
                            className="t-btn-outline"
                            onClick={() => scanApi.rerunAi(examId, b.batch_id).then(refresh, (e) => setError(errorText(e)))}
                          >
                            Run AI again
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {pages.length > 0 && (
        <section className="t-card" aria-labelledby="sc-unmatched-h">
          <h3 id="sc-unmatched-h">Pages to fix ({pages.length})</h3>
          <p className="t-muted">FairPlay could not tell whose page this is. Pick the student and page number.</p>
          <ul className="sc-unmatched">
            {pages.map((p) => (
              <UnmatchedCard
                key={p.page_id}
                page={p}
                students={students}
                pagesPerStudent={status.pages_per_student ?? 1}
                onAssign={async (sid, pageNo) => {
                  await scanApi.assign(examId, p.page_id, sid, pageNo);
                  await refresh();
                  onChanged?.();
                }}
                onIgnore={async () => {
                  await scanApi.ignore(examId, p.page_id);
                  await refresh();
                }}
              />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function UnmatchedCard({
  page,
  students,
  pagesPerStudent,
  onAssign,
  onIgnore,
}: {
  page: UnmatchedPage;
  students: ScanStudent[];
  pagesPerStudent: number;
  onAssign: (studentId: number, pageNo: number) => Promise<void>;
  onIgnore: () => Promise<void>;
}) {
  const [sid, setSid] = useState<string>(page.suggested_student_id ? String(page.suggested_student_id) : "");
  const [pageNo, setPageNo] = useState<string>(String(page.page_no ?? 1));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = `um-${page.page_id}`;

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <li className="sc-um-card">
      {page.preview ? (
        <a href={page.preview} target="_blank" rel="noreferrer" className="sc-um-preview">
          <img src={page.preview} alt={`Scanned page ${page.seq + 1}`} loading="lazy" />
        </a>
      ) : (
        <div className="sc-um-preview sc-um-missing">No preview</div>
      )}
      <div className="sc-um-body">
        <p className="sc-um-reason">
          <strong>Page {page.seq + 1} of the upload.</strong> {page.reason}
        </p>
        {page.name_crop && (
          <figure className="sc-um-name">
            <img src={page.name_crop} alt="Name box as written" />
            <figcaption className="t-muted">
              Name box{page.suggested_name ? ` — AI read “${page.suggested_name}”` : ""}
            </figcaption>
          </figure>
        )}
        <div className="sc-um-form">
          <div className="t-field">
            <label htmlFor={`${id}-s`}>Student</label>
            <select id={`${id}-s`} className="t-select" value={sid} onChange={(e) => setSid(e.target.value)}>
              <option value="">Choose…</option>
              {students.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.username})
                </option>
              ))}
            </select>
          </div>
          <div className="t-field">
            <label htmlFor={`${id}-p`}>Page</label>
            <select id={`${id}-p`} className="t-select" value={pageNo} onChange={(e) => setPageNo(e.target.value)}>
              {Array.from({ length: Math.max(1, pagesPerStudent) }, (_, i) => (
                <option key={i + 1} value={i + 1}>
                  {i + 1}
                </option>
              ))}
            </select>
          </div>
        </div>
        {error && (
          <p className="t-inline-error" role="alert">
            {error}
          </p>
        )}
        <div className="sc-inline">
          <button type="button" className="btn btn-primary" disabled={!sid || busy} onClick={() => run(() => onAssign(Number(sid), Number(pageNo)))}>
            {busy ? "Saving…" : "Assign"}
          </button>
          <button type="button" className="t-btn-outline" disabled={busy} onClick={() => run(onIgnore)}>
            Ignore page
          </button>
        </div>
        {page.qr_found > 0 && page.page_no === 1 && (
          <p className="t-muted sc-small">The next pages of this blank booklet in the same upload are assigned too.</p>
        )}
        {page.qr_found === 0 && <p className="t-muted sc-small">No code: cropped assuming an upright, full-page scan — check the answers after assigning.</p>}
      </div>
    </li>
  );
}

