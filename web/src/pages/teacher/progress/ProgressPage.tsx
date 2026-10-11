// /teacher/progress[?class=<id>] — where each student struggles across assignments (homework,
// paper and computer exams): a students x topics heatmap, this week's misconceptions, who is
// slipping or improving, and re-teach / practice actions for the struggling group.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { classesApi, type ClassSummary } from "../../../api/classes";
import { progressApi, type ClassProgress, type ProgressAction } from "../../../api/homework";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, plural } from "../../../components/teacher/util";
import "../../../styles/homework.css";
import { LEVEL_LABEL, cellClass, changeText, practiceLink, sortStudents, trendText, type StudentSort } from "./heatmap";

export default function ProgressPage() {
  const [params, setParams] = useSearchParams();
  const [classes, setClasses] = useState<ClassSummary[] | null>(null);
  const [data, setData] = useState<ClassProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<StudentSort>("name");
  const classId = Number(params.get("class") || 0) || classes?.[0]?.id || 0;

  useEffect(() => {
    classesApi.list().then(setClasses, (e) => setError(errorText(e)));
  }, []);
  useEffect(() => {
    if (!classId) return;
    let alive = true;
    setData(null);
    setError(null);
    progressApi.classProgress(classId).then(
      (d) => alive && setData(d),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [classId]);

  const rows = useMemo(() => (data ? sortStudents(data.students, sort) : []), [data, sort]);

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Progress</h1>
          <p>Where each student struggles across all graded work — recent work counts more. Adjust next week’s teaching here.</p>
        </div>
        <div className="t-head-actions">
          {classes && classes.length > 0 && (
            <select aria-label="Class" className="input" value={classId} onChange={(e) => setParams({ class: e.target.value })}>
              {classes.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          )}
        </div>
      </header>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {classes && classes.length === 0 ? (
        <div className="t-card">
          <div className="t-empty">
            <strong>No classes yet</strong>
            <span>
              Progress is shown per class. <Link to="/teacher/classes">Create a class</Link> and give it homework.
            </span>
          </div>
        </div>
      ) : !data && !error ? (
        <Loading label="Loading progress…" />
      ) : data ? (
        <>
          {data.topics.length === 0 ? (
            <div className="t-card">
              <div className="t-empty">
                <strong>No graded work yet</strong>
                Once you review homework or exams for {data.class.name}, each student’s topics show up here.
              </div>
            </div>
          ) : (
            <>
              <section className="hw-dash-top">
                <TrendCard title="Slipping" tone="down" rows={data.slipping} empty="Nobody is trending down." />
                <TrendCard title="Improved" tone="up" rows={data.improving} empty="No big jumps yet." />
                <section className="t-card" aria-labelledby="mis-h">
                  <h2 id="mis-h">Common mistakes this week</h2>
                  {data.misconceptions.length === 0 ? (
                    <p className="t-muted">No mistakes tagged in the last two weeks.</p>
                  ) : (
                    <table className="t-table hw-mis">
                      <thead>
                        <tr>
                          <th>Mistake</th>
                          <th className="num">This week</th>
                          <th className="num">Last week</th>
                          <th className="num">Change</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.misconceptions.slice(0, 8).map((m) => (
                          <tr key={m.label}>
                            <td>
                              <strong>{m.label}</strong>
                              {m.topics.length > 0 && <div className="t-muted t-small">{m.topics.join(", ")}</div>}
                            </td>
                            <td className="num">{m.this_week}</td>
                            <td className="num">{m.last_week}</td>
                            <td className={`num ${m.change > 0 ? "hw-up-bad" : m.change < 0 ? "hw-down-good" : ""}`}>{changeText(m.change)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </section>
              </section>

              <Heatmap data={data} rows={rows} sort={sort} onSort={setSort} />
              <Actions actions={data.actions} />
            </>
          )}
        </>
      ) : null}
    </>
  );
}

function TrendCard({ title, tone, rows, empty }: { title: string; tone: "up" | "down"; rows: ClassProgress["slipping"]; empty: string }) {
  return (
    <section className="t-card" aria-label={title}>
      <h2>{title}</h2>
      {rows.length === 0 ? (
        <p className="t-muted">{empty}</p>
      ) : (
        <ul className="hw-trend-list">
          {rows.slice(0, 6).map((r) => (
            <li key={r.username}>
              <Link to={`/teacher/progress/students/${encodeURIComponent(r.username)}`}>{r.name}</Link>
              <span className={tone === "down" ? "hw-down" : "hw-up"}>{trendText(r.trend)} pts</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function Heatmap({
  data,
  rows,
  sort,
  onSort,
}: {
  data: ClassProgress;
  rows: ClassProgress["students"];
  sort: StudentSort;
  onSort: (s: StudentSort) => void;
}) {
  return (
    <section className="t-card" aria-labelledby="hm-h">
      <div className="t-card-head">
        <h2 id="hm-h">Topics by student</h2>
        <div className="t-seg" role="group" aria-label="Sort students">
          {(["name", "mastery", "trend"] as StudentSort[]).map((s) => (
            <button key={s} type="button" className={sort === s ? "on" : ""} aria-pressed={sort === s} onClick={() => onSort(s)}>
              {s === "name" ? "Name" : s === "mastery" ? "Weakest first" : "Slipping first"}
            </button>
          ))}
        </div>
      </div>
      <div className="hm-legend" aria-hidden="true">
        <span className="hm-cell hm-strong">80+</span>
        <span className="hm-cell hm-developing">60–79</span>
        <span className="hm-cell hm-struggling">&lt;60</span>
        <span className="hm-cell hm-none">no work</span>
      </div>
      <div className="t-table-scroll">
        <table className="hm-table">
          <caption className="hw-sr">Mastery per student and topic, percent</caption>
          <thead>
            <tr>
              <th scope="col">Student</th>
              {data.topics.map((t) => (
                <th key={t.topic} scope="col" className="hm-topic">
                  <span>{t.topic}</span>
                </th>
              ))}
              <th scope="col">Trend</th>
            </tr>
            <tr className="hm-class-row">
              <th scope="row">Class</th>
              {data.topics.map((t) => (
                <td key={t.topic} className={cellClass(t.mastery)} title={t.level ? LEVEL_LABEL[t.level] : undefined}>
                  {t.mastery != null ? Math.round(t.mastery) : "—"}
                </td>
              ))}
              <td />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.student_id}>
                <th scope="row">
                  <Link to={`/teacher/progress/students/${encodeURIComponent(r.username)}`}>{r.name}</Link>
                </th>
                {data.topics.map((t) => {
                  const c = r.cells[t.topic];
                  return (
                    <td
                      key={t.topic}
                      className={cellClass(c?.mastery)}
                      aria-label={`${r.name}, ${t.topic}: ${c?.mastery != null ? `${c.mastery}%` : "no work"}`}
                      title={c ? `${c.mastery}% over ${plural(c.n, "answer")}` : "No graded work"}
                    >
                      {c?.mastery != null ? Math.round(c.mastery) : ""}
                    </td>
                  );
                })}
                <td className={`hm-trend ${r.trend_label}`}>{trendText(r.trend, r.trend_label)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Actions({ actions }: { actions: ProgressAction[] }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  async function assign(a: ProgressAction) {
    setBusy(a.topic);
    setMsg(null);
    try {
      const r = await progressApi.practiceDrafts(a.students, [a.topic]);
      if (r.sets.length === 0) {
        setMsg(r.skipped[0]?.reason ?? "No practice could be made for this topic.");
        return;
      }
      navigate("/teacher/practice");
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="t-card" aria-labelledby="act-h">
      <h2 id="act-h">Suggested for next week</h2>
      {actions.length === 0 ? (
        <p className="t-muted">No topic needs extra attention right now.</p>
      ) : (
        <ul className="hw-actions-list">
          {actions.map((a) => (
            <li key={a.topic}>
              <Pill tone={a.kind === "reteach" ? "red" : "orange"}>{a.kind === "reteach" ? "Re-teach" : "Practice"}</Pill>
              <span className="hw-action-text">
                {a.text} <span className="t-muted t-small">(class {Math.round(a.mastery)}%)</span>
              </span>
              {a.students.length > 0 && (
                <span className="hw-action-btns">
                  <button type="button" className="btn btn-secondary" disabled={busy !== null} onClick={() => assign(a)}>
                    {busy === a.topic ? "Making practice…" : `Assign practice to ${plural(a.students.length, "student")}`}
                  </button>
                  <Link className="t-link-btn" to={practiceLink(a.students, a.topic)}>
                    Choose in Practice
                  </Link>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {msg && <p className="t-muted" role="status">{msg}</p>}
    </section>
  );
}
