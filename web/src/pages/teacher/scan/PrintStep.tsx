import { useEffect, useMemo, useState } from "react";
import { scanApi, type PrintInfo, type Roster } from "../../../api/scan";
import { Loading } from "../../../components/Loading";
import { errorText, formatWhen, plural } from "../../../components/teacher/util";

const SOURCE_TEXT: Record<Roster["source"], string> = {
  picked: "Students you picked",
  course: "Everyone in the linked LMS course",
  class: "Everyone in the exam's class",
  all: "Nobody picked yet (pick students, or put the exam in a class)",
};

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function PrintStep({ examId, title }: { examId: string; title: string }) {
  const [roster, setRoster] = useState<Roster | null>(null);
  const [info, setInfo] = useState<PrintInfo | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [lines, setLines] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([scanApi.roster(examId), scanApi.printInfo(examId)]).then(
      ([r, i]) => {
        if (!alive) return;
        setRoster(r);
        setPicked(new Set(r.students.map((s) => s.username)));
        setInfo(i);
        setLines(Object.fromEntries(i.questions.map((q) => [q.question_id, q.answer_lines ? String(q.answer_lines) : ""])));
      },
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [examId]);

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (roster?.all_students ?? []).filter((s) => !f || s.name.toLowerCase().includes(f) || s.username.toLowerCase().includes(f));
  }, [roster, filter]);

  if (!roster || !info) return error ? <div className="alert alert-error" role="alert">{error}</div> : <Loading label="Loading…" />;

  const rosterChanged =
    picked.size !== roster.students.length || roster.students.some((s) => !picked.has(s.username));

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

  const saveRoster = () =>
    act("roster", async () => {
      const r = await scanApi.saveRoster(examId, [...picked]);
      setRoster(r);
      setPicked(new Set(r.students.map((s) => s.username)));
      setInfo(await scanApi.printInfo(examId));
      setNotice(`Booklets will be printed for ${plural(r.students.length, "student")}.`);
    });

  const saveLines = () => {
    const body: Record<string, number | null> = {};
    for (const q of info.questions) {
      if (q.type === "mcq") continue;
      const v = (lines[q.question_id] ?? "").trim();
      const n = v === "" ? null : Number(v);
      if (n !== null && (!Number.isInteger(n) || n < 1 || n > 40)) {
        setError(`Question ${q.index + 1}: answer space must be 1 to 40 lines.`);
        return;
      }
      body[q.question_id] = n;
    }
    return act("lines", async () => {
      setInfo(await scanApi.saveAnswerLines(examId, body));
      setNotice("Answer space saved.");
    });
  };

  const parts = info.booklet_parts ?? 1;
  const download = (blank: boolean, part = 1) =>
    act(blank ? "blank" : `pdf${part}`, async () => {
      const blob = await scanApi.printPdf(examId, blank, part);
      const safe = title.replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "") || "exam";
      const suffix = blank ? "blank" : parts > 1 ? `booklets-part${part}-of-${parts}` : "booklets";
      saveBlob(blob, `${safe}-${suffix}.pdf`);
      setInfo(await scanApi.printInfo(examId));
    });

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
      {info.layout_changed && (
        <div className="alert alert-warning" role="alert">
          The questions or answer space changed after you printed. Print again — papers printed before cannot be read
          correctly.
        </div>
      )}

      <section className="t-card" aria-labelledby="sc-roster-h">
        <div className="sc-card-head">
          <div>
            <h3 id="sc-roster-h">Who gets a booklet</h3>
            <p className="t-muted">
              {SOURCE_TEXT[roster.source]} · {plural(roster.students.length, "student")}
            </p>
          </div>
          <div className="sc-inline">
            <button type="button" className="t-btn-outline" onClick={() => setPicked(new Set(roster.all_students.map((s) => s.username)))}>
              Select all
            </button>
            <button type="button" className="t-btn-outline" onClick={() => setPicked(new Set())}>
              Clear
            </button>
          </div>
        </div>
        <div className="t-field">
          <label htmlFor="sc-filter">Find a student</label>
          <input id="sc-filter" className="input" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Name or username" />
        </div>
        <ul className="sc-roster" aria-label="Students">
          {shown.map((s) => (
            <li key={s.id}>
              <label className="sc-check">
                <input
                  type="checkbox"
                  checked={picked.has(s.username)}
                  onChange={(e) => {
                    const next = new Set(picked);
                    if (e.target.checked) next.add(s.username);
                    else next.delete(s.username);
                    setPicked(next);
                  }}
                />
                <span>{s.name}</span>
                <span className="t-muted">{s.username}</span>
              </label>
            </li>
          ))}
        </ul>
        <div className="sc-inline">
          <button type="button" className="btn btn-secondary" onClick={saveRoster} disabled={!rosterChanged || busy !== null}>
            {busy === "roster" ? "Saving…" : picked.size === 0 ? "Use the default list" : `Save list (${picked.size})`}
          </button>
          {picked.size === 0 && <span className="t-muted">With nobody picked, the linked course (or everyone) is used.</span>}
        </div>
      </section>

      <section className="t-card" aria-labelledby="sc-space-h">
        <h3 id="sc-space-h">Answer space</h3>
        <p className="t-muted">Lines of writing space per question. Leave empty for the default.</p>
        <ol className="sc-lines">
          {info.questions.map((q) => (
            <li key={q.question_id}>
              <span className="sc-lines-prompt">
                <strong>{q.index + 1}.</strong> {q.prompt.length > 90 ? `${q.prompt.slice(0, 90)}…` : q.prompt}
              </span>
              {q.type === "mcq" ? (
                <span className="t-muted">Bubbles to fill</span>
              ) : (
                <label className="sc-lines-input">
                  <span className="sr-only">Lines for question {q.index + 1}</span>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={40}
                    inputMode="numeric"
                    placeholder={String(q.default_lines ?? "")}
                    value={lines[q.question_id] ?? ""}
                    onChange={(e) => setLines({ ...lines, [q.question_id]: e.target.value })}
                  />
                  <span className="t-muted">lines</span>
                </label>
              )}
            </li>
          ))}
        </ol>
        <div className="sc-inline">
          <button type="button" className="btn btn-secondary" onClick={saveLines} disabled={busy !== null}>
            {busy === "lines" ? "Saving…" : "Save answer space"}
          </button>
        </div>
      </section>

      <section className="t-card sc-print-card" aria-labelledby="sc-print-h">
        <h3 id="sc-print-h">Print</h3>
        <p>
          {plural(info.pages_per_student, "page")} per student · A4 · every page has two FairPlay codes (top-right and
          bottom-left). Print single-sided at 100% size.
        </p>
        <p className="t-muted">
          {info.printed_at ? `Last printed ${formatWhen(info.printed_at)}.` : "Not printed yet."}
        </p>
        <div className="sc-inline">
          {parts <= 1 ? (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => download(false)}
              disabled={busy !== null || info.student_count === 0 || info.needs_roster}
            >
              {busy === "pdf1" ? "Preparing PDF…" : `Download booklets (${plural(info.student_count, "student")})`}
            </button>
          ) : (
            Array.from({ length: parts }, (_, i) => i + 1).map((n) => (
              <button
                key={n}
                type="button"
                className="btn btn-primary"
                onClick={() => download(false, n)}
                disabled={busy !== null || info.needs_roster}
              >
                {busy === `pdf${n}` ? "Preparing PDF…" : `Booklets part ${n} of ${parts}`}
              </button>
            ))
          )}
          <button type="button" className="t-btn-outline" onClick={() => download(true)} disabled={busy !== null}>
            {busy === "blank" ? "Preparing…" : "Download a blank copy"}
          </button>
        </div>
        {info.needs_roster && (
          <p className="sc-small" role="note">
            Pick the students who get a booklet above (or put this exam in a class) before printing named booklets.
          </p>
        )}
        {parts > 1 && (
          <p className="t-muted sc-small">
            {plural(info.student_count, "student")}: booklets come in {parts} PDFs of up to {info.booklets_per_part} students each.
          </p>
        )}
        <p className="t-muted sc-small">
          A blank copy has a Name box instead of a printed name. You pick the student when its scan comes in.
        </p>
      </section>
    </div>
  );
}
