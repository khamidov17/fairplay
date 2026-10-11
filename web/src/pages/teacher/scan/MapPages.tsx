import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../../api/client";
import { scanAiApi, type MapPage, type PageMap } from "../../../api/scanAi";
import { errorText, plural } from "../../../components/teacher/util";
import "../../../styles/scanAi.css";

export interface Assign {
  student_id: number | null;
  page_no: number;
}

/** Fill every page in upload order: every `pps` pages = the next student of the roster. */
export function autoMap(pages: MapPage[], students: { id: number }[], pps: number): Record<number, Assign> {
  const out: Record<number, Assign> = {};
  pages.forEach((pg, i) => {
    const s = students[Math.floor(i / Math.max(1, pps))];
    out[pg.page_id] = { student_id: s ? s.id : null, page_no: (i % Math.max(1, pps)) + 1 };
  });
  return out;
}

/** Current assignment of each page (what the server has, else nothing). */
export function currentMap(pages: MapPage[]): Record<number, Assign> {
  return Object.fromEntries(pages.map((p) => [p.page_id, { student_id: p.student_id, page_no: p.page_no ?? 1 }]));
}

/** "Map pages" for answer sheets not printed by FairPlay. */
export function MapPages({ examId, refreshKey, onChanged }: { examId: string; refreshKey?: number; onChanged?: () => void }) {
  const [map, setMap] = useState<PageMap | null>(null);
  const [pps, setPps] = useState("1");
  const [qp, setQp] = useState<Record<string, number>>({});
  const [assign, setAssign] = useState<Record<number, Assign>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const apply = useCallback((m: PageMap) => {
    setMap(m);
    setPps(String(m.pages_per_student));
    setQp(m.question_pages);
    setAssign(currentMap(m.pages));
  }, []);

  useEffect(() => {
    let alive = true;
    scanAiApi.pageMap(examId).then((m) => alive && apply(m), (e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [examId, refreshKey, apply]);

  if (!map || map.pages.length === 0) return error ? <p className="t-inline-error">{error}</p> : null;

  const n = Math.max(1, Number(pps) || 1);
  const unassigned = map.pages.filter((p) => !assign[p.page_id]?.student_id).length;

  async function act(name: string, fn: () => Promise<void>) {
    setBusy(name);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error && !(e instanceof ApiError) && e.message.startsWith("Pick") ? e.message : errorText(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="t-card" aria-labelledby="mp-h">
      <h3 id="mp-h">Map pages ({plural(map.pages.length, "page")})</h3>
      <p className="t-muted sc-small">
        These sheets have no FairPlay codes. Say how many pages each student has and where each question is, fill in the
        students, fix single pages if needed, then save.
      </p>
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
      <div className="mp-settings">
        <div className="t-field">
          <label htmlFor="mp-pps">Pages per student</label>
          <input id="mp-pps" className="input" inputMode="numeric" style={{ width: 90 }} value={pps} onChange={(e) => setPps(e.target.value)} />
        </div>
        <button
          type="button"
          className="t-btn-outline"
          disabled={busy !== null}
          onClick={() => setAssign(autoMap(map.pages, map.students, n))}
        >
          Fill in students in order
        </button>
      </div>
      <fieldset className="ak-fieldset" style={{ marginTop: 12 }}>
        <legend>Question is on page</legend>
        <div className="mp-qpages">
          {map.questions.map((q) => (
            <div key={q.question_id} className="t-field">
              <label htmlFor={`mp-q-${q.question_id}`}>Question {q.index + 1}</label>
              <select
                id={`mp-q-${q.question_id}`}
                className="t-select"
                value={Math.min(qp[q.question_id] ?? 1, n)}
                onChange={(e) => setQp({ ...qp, [q.question_id]: Number(e.target.value) })}
              >
                {Array.from({ length: n }, (_, i) => (
                  <option key={i + 1} value={i + 1}>
                    Page {i + 1}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      </fieldset>

      <ol className="mp-grid" aria-label="Pages">
        {map.pages.map((pg, i) => {
          const a = assign[pg.page_id] ?? { student_id: null, page_no: 1 };
          return (
            <li key={pg.page_id} className={`mp-page${a.page_no === 1 ? " is-first" : ""}`}>
              {pg.preview ? (
                <a href={pg.preview} target="_blank" rel="noreferrer">
                  <img src={pg.preview} alt={`Uploaded page ${i + 1}`} loading="lazy" />
                </a>
              ) : (
                <span className="t-muted">No preview</span>
              )}
              <select
                className="t-select"
                aria-label={`Student for page ${i + 1}`}
                value={a.student_id ?? ""}
                onChange={(e) =>
                  setAssign({ ...assign, [pg.page_id]: { ...a, student_id: e.target.value ? Number(e.target.value) : null } })
                }
              >
                <option value="">No student</option>
                {map.students.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              <select
                className="t-select"
                aria-label={`Page number of page ${i + 1}`}
                value={Math.min(a.page_no, n)}
                onChange={(e) => setAssign({ ...assign, [pg.page_id]: { ...a, page_no: Number(e.target.value) } })}
              >
                {Array.from({ length: n }, (_, j) => (
                  <option key={j + 1} value={j + 1}>
                    Page {j + 1}
                  </option>
                ))}
              </select>
            </li>
          );
        })}
      </ol>
      <div className="sc-inline">
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy !== null}
          onClick={() =>
            act("save", async () => {
              const cleanQp = Object.fromEntries(Object.entries(qp).map(([k, v]) => [k, Math.min(v, n)]));
              await scanAiApi.saveMapSettings(examId, { pages_per_student: n, question_pages: cleanQp });
              const list = map.pages
                .filter((p) => assign[p.page_id]?.student_id || p.student_id)
                .map((p) => ({
                  page_id: p.page_id,
                  student_id: assign[p.page_id]?.student_id ?? null,
                  page_no: Math.min(assign[p.page_id]?.page_no ?? 1, n),
                }));
              if (list.length === 0) throw new Error("Pick a student for at least one page.");
              const res = await scanAiApi.applyMap(examId, list);
              apply(await scanAiApi.pageMap(examId));
              setNotice(
                `Saved: ${plural(res.assigned, "page")} for ${plural(res.students, "student")}` +
                  (res.queued ? ` — AI is drafting ${plural(res.queued, "answer")}.` : "."),
              );
              onChanged?.();
            })
          }
        >
          {busy === "save" ? "Saving…" : "Save mapping"}
        </button>
        {unassigned > 0 && <span className="sc-low">{plural(unassigned, "page")} without a student</span>}
      </div>
    </section>
  );
}
