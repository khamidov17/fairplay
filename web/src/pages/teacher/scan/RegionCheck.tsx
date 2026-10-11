import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { anyPaperApi, type AnswerSet, type AnswerSets, type Box, type Region, type SetPage } from "../../../api/anyPaper";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, plural } from "../../../components/teacher/util";
import "../../../styles/anyPaper.css";
import {
  REASON_TEXT,
  boxFromPoints,
  keyAction,
  markNotAnswered,
  moveBox,
  nudge,
  pagesAfterMove,
  questionByNumber,
  questionColor,
  questionState,
  reassign,
  regionOrder,
  regionsDirty,
  removeRegion,
  resizeBox,
  setTitle,
  stepSet,
  toRegionIn,
  updateRegion,
  visibleSets,
  addRegion,
  type Handle,
  type SetFilter,
} from "./regionLogic";

type Drag =
  | { kind: "move" | Handle; id: string; x: number; y: number; box: Box; w: number; h: number }
  | { kind: "draw"; pageId: number; x0: number; y0: number; x: number; y: number; left: number; top: number; w: number; h: number };

const pct = (v: number) => `${Math.round(v * 100)}%`;

/** "Check answer regions": the AI's student grouping and answer boxes on pages without
 *  FairPlay codes. Only sets that need a check are shown by default. */
export function RegionCheck({ examId, refreshKey, onChanged }: { examId: string; refreshKey?: number; onChanged?: () => void }) {
  const [data, setData] = useState<AnswerSets | null>(null);
  const [filter, setFilter] = useState<SetFilter>("check");
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [edited, setEdited] = useState<Region[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [drawFor, setDrawFor] = useState<string | null>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dragPage, setDragPage] = useState<number | null>(null);
  const rootRef = useRef<HTMLElement>(null);

  const apply = useCallback((d: AnswerSets, keep?: string | null) => {
    setData(d);
    setCurrentId((cur) => {
      const want = keep ?? cur;
      if (want && d.sets.some((s) => s.set_id === want)) return want;
      return (d.sets.find((s) => s.needs_check) ?? d.sets[0])?.set_id ?? null;
    });
  }, []);

  useEffect(() => {
    let alive = true;
    anyPaperApi.sets(examId).then(
      (d) => alive && apply(d),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [examId, refreshKey, apply]);

  const current = data?.sets.find((s) => s.set_id === currentId) ?? null;
  // editing copy of the current set's boxes
  useEffect(() => {
    setEdited(current ? current.regions : []);
    setSelected(null);
    setDrawFor(null);
  }, [current]);

  const shown = useMemo(() => (data ? visibleSets(data.sets, filter) : []), [data, filter]);
  const dirty = current ? regionsDirty(current.regions, edited) : false;
  const pageIds = current ? current.pages.map((p) => p.page_id) : [];
  const ordered = regionOrder(edited, pageIds);
  const sel = edited.find((r) => r.id === selected) ?? null;
  const qIndex = useMemo(() => new Map((data?.questions ?? []).map((q) => [q.question_id, q.index])), [data]);

  const run = useCallback(
    async (name: string, fn: () => Promise<AnswerSets | void>, keep?: string | null, msg?: (d: AnswerSets) => string) => {
      setBusy(name);
      setError(null);
      setNotice(null);
      try {
        const d = await fn();
        if (d) {
          apply(d, keep);
          if (msg) setNotice(msg(d));
          onChanged?.();
        }
      } catch (e) {
        setError(errorText(e));
      } finally {
        setBusy(null);
      }
    },
    [apply, onChanged],
  );

  const queuedText = (d: AnswerSets) => (d.queued ? ` AI is grading ${plural(d.queued, "answer")} again.` : "");

  const save = useCallback(
    (confirm: boolean) => {
      if (!current) return;
      const body = { ...(dirty ? { regions: toRegionIn(edited) } : {}), ...(confirm ? { confirm: true } : {}) };
      if (!dirty && !confirm) return;
      const next = confirm && filter === "check" ? stepSet(shown, current.set_id, 1) : current.set_id;
      return run(
        confirm ? "confirm" : "save",
        () => anyPaperApi.update(examId, current.set_id, body),
        next === current.set_id && confirm && shown.length === 1 ? null : next,
        (d) => (confirm ? `Confirmed ${setTitle(current)}.` : "Boxes saved.") + queuedText(d),
      );
    },
    [current, dirty, edited, examId, filter, run, shown],
  );

  const movePage = (pageId: number, to: string) => {
    if (!data) return;
    if (to === "new") {
      run("move", () => anyPaperApi.create(examId, [pageId]), undefined, (d) => "Page moved to a new student." + queuedText(d));
      return;
    }
    const ids = pagesAfterMove(data.sets, pageId, to);
    if (!ids) return;
    run("move", () => anyPaperApi.update(examId, to, { page_ids: ids }), undefined, (d) => "Page moved." + queuedText(d));
  };

  // ---------------------------------------------------------------- pointer dragging
  useEffect(() => {
    if (!drag) return;
    const onMove = (e: PointerEvent) => {
      if (drag.kind === "draw") {
        setDrag({ ...drag, x: (e.clientX - drag.left) / drag.w, y: (e.clientY - drag.top) / drag.h });
        return;
      }
      const dx = (e.clientX - drag.x) / drag.w;
      const dy = (e.clientY - drag.y) / drag.h;
      const box = drag.kind === "move" ? moveBox(drag.box, dx, dy) : resizeBox(drag.box, drag.kind, dx, dy);
      setEdited((rs) => updateRegion(rs, drag.id, { box }));
    };
    const onUp = () => {
      if (drag.kind === "draw") {
        const box = boxFromPoints(drag.x0, drag.y0, drag.x, drag.y);
        if (Math.abs(drag.x - drag.x0) > 0.01 && Math.abs(drag.y - drag.y0) > 0.01) {
          setEdited((rs) => {
            const next = addRegion(rs, drawFor, drag.pageId, box);
            setSelected(next[next.length - 1].id);
            return next;
          });
        }
        setDrawFor(null);
      }
      setDrag(null);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [drag, drawFor]);

  // ---------------------------------------------------------------- keyboard
  useEffect(() => {
    if (!current || !data) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.closest("input, select, textarea, [contenteditable=true]") || !rootRef.current?.contains(t) && t !== document.body))
        return;
      const a = keyAction(e);
      if (!a) return;
      if (a.type === "nudge" || a.type === "delete" || a.type === "crossed" || a.type === "assign") {
        if (!sel) return;
      }
      if ((a.type === "confirm" || a.type === "nextBox" || a.type === "prevBox") && t?.closest("button, a, summary")) return;
      if (e.key === "Tab" && !sel) return; // Tab moves between boxes only once a box is selected
      e.preventDefault();
      switch (a.type) {
        case "next":
        case "prev":
          setCurrentId(stepSet(shown.length ? shown : data.sets, current.set_id, a.type === "next" ? 1 : -1));
          break;
        case "nextBox":
        case "prevBox": {
          if (ordered.length === 0) break;
          const i = ordered.findIndex((r) => r.id === selected);
          const j = i < 0 ? 0 : (i + (a.type === "nextBox" ? 1 : -1) + ordered.length) % ordered.length;
          setSelected(ordered[j].id);
          break;
        }
        case "assign": {
          const q = questionByNumber(data.questions, a.n);
          if (q && sel) setEdited((rs) => reassign(rs, sel.id, q.question_id));
          break;
        }
        case "delete":
          if (sel) {
            setEdited((rs) => removeRegion(rs, sel.id));
            setSelected(null);
          }
          break;
        case "crossed":
          if (sel) setEdited((rs) => updateRegion(rs, sel.id, { crossed_out: !sel.crossed_out }));
          break;
        case "notAnswered":
          if (sel?.question_id) {
            setEdited((rs) => markNotAnswered(rs, sel.question_id as string));
            setSelected(null);
          }
          break;
        case "nudge": {
          if (!sel) break;
          const b = nudge(sel.box, a.key, a.shift);
          if (b) setEdited((rs) => updateRegion(rs, sel.id, { box: b }));
          break;
        }
        case "confirm":
          save(true);
          break;
        case "save":
          save(false);
          break;
        case "deselect":
          setSelected(null);
          setDrawFor(null);
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, data, ordered, save, sel, selected, shown]);

  if (!data) return error ? <p className="t-inline-error">{error}</p> : null;
  if (data.sets.length === 0 && data.loose_pages.length === 0) return null;

  const checkCount = data.needs_check;

  return (
    <section className="t-card rc" aria-labelledby="rc-h" ref={rootRef}>
      <div className="rc-head">
        <div>
          <h3 id="rc-h">Check answer regions</h3>
          <p className="t-muted sc-small">
            {plural(data.sets.length, "student")} found ·{" "}
            {checkCount ? `${plural(checkCount, "student")} need${checkCount === 1 ? "s" : ""} a check` : "nothing left to check"}. The AI
            grades exactly what is inside each box.
          </p>
        </div>
        <div className="rc-filter" role="group" aria-label="Show">
          <button type="button" className="t-btn-outline" aria-pressed={filter === "check"} onClick={() => setFilter("check")}>
            Needs check ({checkCount})
          </button>
          <button type="button" className="t-btn-outline" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>
            All ({data.sets.length})
          </button>
        </div>
      </div>
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

      <ol className="rc-students" aria-label="Students">
        {(filter === "all" ? data.sets : data.sets.filter((s) => s.needs_check || s.set_id === currentId)).map((s) => (
          <li
            key={s.set_id}
            className={`rc-student${s.set_id === currentId ? " is-current" : ""}${dragPage !== null ? " is-target" : ""}`}
            onDragOver={(e) => {
              if (dragPage !== null) e.preventDefault();
            }}
            onDrop={(e) => {
              e.preventDefault();
              if (dragPage !== null && !s.pages.some((p) => p.page_id === dragPage)) movePage(dragPage, s.set_id);
              setDragPage(null);
            }}
          >
            <button type="button" aria-current={s.set_id === currentId ? "true" : undefined} onClick={() => setCurrentId(s.set_id)}>
              <span className={`rc-dot ${s.needs_check ? "warn" : s.status === "confirmed" ? "ok" : "fine"}`} aria-hidden="true" />
              <span className="rc-student-name">{setTitle(s)}</span>
              <span className="t-muted sc-small">{plural(s.pages.length, "page")}</span>
            </button>
          </li>
        ))}
        {filter === "check" && checkCount === 0 && <li className="t-muted sc-small rc-alldone">All students are checked.</li>}
      </ol>

      {data.loose_pages.length > 0 && (
        <div className="rc-loose">
          <strong>{plural(data.loose_pages.length, "page")} without a student.</strong>{" "}
          <button
            type="button"
            className="t-btn-outline"
            disabled={busy !== null}
            onClick={() =>
              run("loose", () => anyPaperApi.create(examId, data.loose_pages.map((p) => p.page_id)), undefined, (d) =>
                "Pages collected into one student." + queuedText(d),
              )
            }
          >
            Find answers on these pages
          </button>
        </div>
      )}

      {current && (
        <div className="rc-body">
          <div className="rc-pages">
            {current.pages.map((pg, i) => (
              <PageCanvas
                key={pg.page_id}
                page={pg}
                index={i}
                regions={edited.filter((r) => r.page_id === pg.page_id)}
                qIndex={qIndex}
                selected={selected}
                drawFor={drawFor}
                drawing={drag && drag.kind === "draw" && drag.pageId === pg.page_id ? drag : null}
                others={data.sets.filter((s) => s.set_id !== current.set_id)}
                busy={busy !== null}
                onSelect={setSelected}
                onStartDrag={setDrag}
                onMove={(to) => movePage(pg.page_id, to)}
                onDragPage={setDragPage}
              />
            ))}
          </div>

          <aside className="rc-side" aria-label="Answer regions of this student">
            <StudentPicker
              set={current}
              students={data.students}
              taken={new Set(data.sets.filter((s) => s.set_id !== current.set_id && s.student_id).map((s) => s.student_id as number))}
              busy={busy !== null}
              onPick={(sid) =>
                run("student", () => anyPaperApi.update(examId, current.set_id, { student_id: sid }), current.set_id, (d) =>
                  "Student saved." + queuedText(d),
                )
              }
            />

            <h4 className="rc-sub">Questions</h4>
            <ul className="rc-questions">
              {data.questions.map((q) => {
                const st = questionState(q.question_id, edited, data.low_confidence_below);
                const first = ordered.find((r) => r.question_id === q.question_id && !r.crossed_out);
                return (
                  <li key={q.question_id} className={`rc-q is-${st.kind}`}>
                    <button
                      type="button"
                      className="rc-q-main"
                      onClick={() => (first ? setSelected(first.id) : setDrawFor(q.question_id))}
                      title={q.prompt}
                    >
                      <span className="rc-swatch" style={{ background: questionColor(q.index) }} aria-hidden="true" />
                      <span>Q{q.index + 1}</span>
                      <span className="rc-q-state">
                        {st.kind === "missing"
                          ? "Not found"
                          : `${st.kind === "check" ? "Check" : "Found"} ${st.confidence !== null ? pct(st.confidence) : ""}${
                              st.parts > 1 ? ` · ${st.parts} parts` : ""
                            }`}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="t-btn-outline rc-mini"
                      aria-pressed={drawFor === q.question_id}
                      aria-label={`Draw a box for question ${q.index + 1}`}
                      onClick={() => setDrawFor(drawFor === q.question_id ? null : q.question_id)}
                    >
                      Draw
                    </button>
                    {st.kind !== "missing" && (
                      <button
                        type="button"
                        className="t-btn-outline rc-mini"
                        aria-label={`Question ${q.index + 1} not answered`}
                        onClick={() => setEdited((rs) => markNotAnswered(rs, q.question_id))}
                      >
                        Not answered
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
            {drawFor && (
              <p className="rc-hint" role="status">
                Drag on a page to draw the box for question {(qIndex.get(drawFor) ?? 0) + 1}. Esc cancels.
              </p>
            )}

            {sel && (
              <fieldset className="rc-box-tools">
                <legend>Selected box</legend>
                <div className="t-field">
                  <label htmlFor="rc-box-q">Answer to</label>
                  <select
                    id="rc-box-q"
                    className="t-select"
                    value={sel.question_id ?? ""}
                    onChange={(e) => setEdited((rs) => reassign(rs, sel.id, e.target.value || null))}
                  >
                    <option value="">No question</option>
                    {data.questions.map((q) => (
                      <option key={q.question_id} value={q.question_id}>
                        Question {q.index + 1}
                      </option>
                    ))}
                  </select>
                </div>
                <label className="sc-check">
                  <input
                    type="checkbox"
                    checked={sel.crossed_out}
                    onChange={(e) => setEdited((rs) => updateRegion(rs, sel.id, { crossed_out: e.target.checked }))}
                  />
                  Crossed out (not graded)
                </label>
                <button
                  type="button"
                  className="t-btn-outline"
                  onClick={() => {
                    setEdited((rs) => removeRegion(rs, sel.id));
                    setSelected(null);
                  }}
                >
                  Delete box
                </button>
              </fieldset>
            )}

            {current.reasons.length > 0 && (
              <ul className="rc-reasons">
                {current.reasons.map((r) => (
                  <li key={r}>{REASON_TEXT[r] ?? r}</li>
                ))}
              </ul>
            )}

            <div className="rc-actions">
              <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={() => save(true)}>
                {busy === "confirm" ? "Saving…" : dirty ? "Save and confirm" : "Confirm"}
              </button>
              <button type="button" className="t-btn-outline" disabled={!dirty || busy !== null} onClick={() => save(false)}>
                {busy === "save" ? "Saving…" : "Save boxes"}
              </button>
              {dirty && (
                <button type="button" className="t-btn-outline" onClick={() => setEdited(current.regions)}>
                  Undo changes
                </button>
              )}
              {data.ai_enabled && (
                <button
                  type="button"
                  className="t-btn-outline"
                  disabled={busy !== null}
                  onClick={() =>
                    run("again", () => anyPaperApi.resegment(examId, current.set_id), current.set_id, (d) => "AI looked again." + queuedText(d))
                  }
                >
                  {busy === "again" ? "Looking…" : "Find again with AI"}
                </button>
              )}
            </div>
            <div className="rc-nav">
              <button type="button" className="t-btn-outline" onClick={() => setCurrentId(stepSet(shown.length ? shown : data.sets, current.set_id, -1))}>
                ← Previous
              </button>
              <span className="t-muted sc-small">{current.status === "confirmed" ? <Pill tone="green">Confirmed</Pill> : null}</span>
              <button type="button" className="t-btn-outline" onClick={() => setCurrentId(stepSet(shown.length ? shown : data.sets, current.set_id, 1))}>
                Next →
              </button>
            </div>
            <details className="rc-keys">
              <summary>Keyboard</summary>
              <dl>
                <dt>Tab / ]</dt>
                <dd>next box ([ or Shift+Tab: previous)</dd>
                <dt>1–9</dt>
                <dd>box belongs to that question</dd>
                <dt>Arrows</dt>
                <dd>move the box (Shift: resize)</dd>
                <dt>Delete</dt>
                <dd>remove the box</dd>
                <dt>x</dt>
                <dd>crossed out on / off</dd>
                <dt>n</dt>
                <dd>question not answered</dd>
                <dt>Enter</dt>
                <dd>save, confirm, next student</dd>
                <dt>s</dt>
                <dd>save boxes</dd>
                <dt>j / k</dt>
                <dd>next / previous student</dd>
              </dl>
            </details>
          </aside>
        </div>
      )}
    </section>
  );
}

function StudentPicker({
  set,
  students,
  taken,
  busy,
  onPick,
}: {
  set: AnswerSet;
  students: { id: number; username: string; name: string }[];
  taken: Set<number>;
  busy: boolean;
  onPick: (sid: number | null) => void;
}) {
  const id = `rc-st-${set.set_id}`;
  const conf = set.match.confidence;
  return (
    <div className="rc-student-pick">
      <div className="t-field">
        <label htmlFor={id}>Student</label>
        <select
          id={id}
          className="t-select"
          value={set.student_id ?? ""}
          disabled={busy}
          onChange={(e) => onPick(e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">Not chosen</option>
          {students.map((s) => (
            <option key={s.id} value={s.id} disabled={taken.has(s.id)}>
              {s.name} ({s.username}){taken.has(s.id) ? " — has pages" : ""}
            </option>
          ))}
        </select>
      </div>
      {(set.name_read || set.id_read) && (
        <p className="t-muted sc-small">
          Wrote “{[set.name_read, set.id_read].filter(Boolean).join(" · ")}”
          {set.student && !set.match.teacher ? ` · ${pct(conf)} match` : ""}
        </p>
      )}
      {!set.student && set.match.candidates.length > 0 && (
        <div className="rc-cands">
          {set.match.candidates
            .filter((c) => !taken.has(c.student_id))
            .map((c) => (
              <button key={c.student_id} type="button" className="t-btn-outline rc-mini" disabled={busy} onClick={() => onPick(c.student_id)}>
                {c.name} · {pct(c.score)}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}

function PageCanvas({
  page,
  index,
  regions,
  qIndex,
  selected,
  drawFor,
  drawing,
  others,
  busy,
  onSelect,
  onStartDrag,
  onMove,
  onDragPage,
}: {
  page: SetPage;
  index: number;
  regions: Region[];
  qIndex: Map<string, number>;
  selected: string | null;
  drawFor: string | null;
  drawing: Extract<Drag, { kind: "draw" }> | null;
  others: AnswerSet[];
  busy: boolean;
  onSelect: (id: string | null) => void;
  onStartDrag: (d: Drag) => void;
  onMove: (to: string) => void;
  onDragPage: (id: number | null) => void;
}) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const rect = () => canvasRef.current?.getBoundingClientRect();

  const startBox = (e: ReactPointerEvent, r: Region, kind: "move" | Handle) => {
    if (drawFor) return;
    e.stopPropagation();
    e.preventDefault();
    onSelect(r.id);
    const b = rect();
    if (!b || b.width === 0) return;
    onStartDrag({ kind, id: r.id, x: e.clientX, y: e.clientY, box: r.box, w: b.width, h: b.height });
  };

  return (
    <figure className="rc-page">
      <figcaption>
        <span
          className="rc-grip"
          draggable
          onDragStart={(e) => {
            e.dataTransfer.setData("text/plain", String(page.page_id));
            onDragPage(page.page_id);
          }}
          onDragEnd={() => onDragPage(null)}
          title="Drag onto a student to move this page"
        >
          ⠿ Page {index + 1}
        </span>
        <span className="t-muted sc-small">upload page {page.seq + 1}</span>
        <select
          className="t-select rc-move"
          aria-label={`Move page ${index + 1} to`}
          value=""
          disabled={busy}
          onChange={(e) => e.target.value && onMove(e.target.value)}
        >
          <option value="">Move to…</option>
          {others.map((s) => (
            <option key={s.set_id} value={s.set_id}>
              {setTitle(s)}
            </option>
          ))}
          <option value="new">A new student</option>
        </select>
      </figcaption>
      <div
        ref={canvasRef}
        className={`rc-canvas${drawFor ? " is-drawing" : ""}`}
        data-page-id={page.page_id}
        onPointerDown={(e) => {
          const b = rect();
          if (!b || b.width === 0) return;
          if (!drawFor) {
            onSelect(null);
            return;
          }
          e.preventDefault();
          const x = (e.clientX - b.left) / b.width;
          const y = (e.clientY - b.top) / b.height;
          onStartDrag({ kind: "draw", pageId: page.page_id, x0: x, y0: y, x, y, left: b.left, top: b.top, w: b.width, h: b.height });
        }}
      >
        {page.image ? <img src={page.image} alt={`Page ${index + 1}`} draggable={false} /> : <div className="rc-noimg">No picture</div>}
        {regions.map((r) => {
          const qi = r.question_id ? qIndex.get(r.question_id) : undefined;
          const color = qi !== undefined ? questionColor(qi) : "#6B7686";
          const low = r.source !== "teacher" && r.needs_check;
          const label = `${qi !== undefined ? `Q${qi + 1}` : "No question"}${r.crossed_out ? " · crossed out" : ""}${
            r.source === "teacher" ? "" : ` · ${pct(r.confidence)}`
          }`;
          return (
            <div
              key={r.id}
              className={`rc-box${r.id === selected ? " is-selected" : ""}${r.crossed_out ? " is-crossed" : ""}${low ? " is-low" : ""}`}
              style={{
                left: `${r.box[0] * 100}%`,
                top: `${r.box[1] * 100}%`,
                width: `${(r.box[2] - r.box[0]) * 100}%`,
                height: `${(r.box[3] - r.box[1]) * 100}%`,
                borderColor: color,
              }}
              role="button"
              aria-pressed={r.id === selected}
              aria-label={`Box ${label}, page ${index + 1}`}
              data-region-id={r.id}
              onPointerDown={(e) => startBox(e, r, "move")}
            >
              <span className="rc-tag" style={{ background: color }}>
                {label}
              </span>
              {r.id === selected &&
                (["nw", "ne", "sw", "se"] as Handle[]).map((h) => (
                  <span
                    key={h}
                    className={`rc-handle rc-${h}`}
                    data-handle={h}
                    style={{ borderColor: color }}
                    onPointerDown={(e) => startBox(e, r, h)}
                  />
                ))}
            </div>
          );
        })}
        {drawing && (
          <div
            className="rc-box is-new"
            style={{
              left: `${Math.min(drawing.x0, drawing.x) * 100}%`,
              top: `${Math.min(drawing.y0, drawing.y) * 100}%`,
              width: `${Math.abs(drawing.x - drawing.x0) * 100}%`,
              height: `${Math.abs(drawing.y - drawing.y0) * 100}%`,
            }}
          />
        )}
      </div>
    </figure>
  );
}
