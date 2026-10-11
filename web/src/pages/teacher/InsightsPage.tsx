// Slice 6 F: class insights — topic × exam heatmap, what to reteach, hardest questions,
// common mistakes. Data: GET /api/insights?course=&exam_ids=
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { insightsApi } from "../../api/client";
import type { Insights, InsightsExam, MistakeCluster, TopicInsight } from "../../api/types";
import { Loading } from "../../components/Loading";
import { errorText, pct, plural } from "../../components/teacher/util";
import {
  HeatLegend,
  TrendText,
  heatStep,
  practiceForTopic,
  profilePath,
  shortDate,
} from "../../components/teacher/insights/viz";

const RETEACH_COUNT = 5;
const HARDEST_SHOWN = 6;

/** Lowest tagged topics with at least 2 answers — "What to reteach". */
export function reteachTopics(topics: TopicInsight[], n = RETEACH_COUNT): TopicInsight[] {
  return topics
    .filter((t) => !t.untagged && t.n_answers >= 2)
    .slice()
    .sort((a, b) => a.avg_percent - b.avg_percent || a.topic.localeCompare(b.topic))
    .slice(0, n);
}

const SOURCE_LABEL = { tag: "mistake tag", teacher: "your comment", ai: "AI draft" } as const;

export default function InsightsPage() {
  const [course, setCourse] = useState("");
  const [examIds, setExamIds] = useState<string[]>([]);
  const [data, setData] = useState<Insights | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showUntagged, setShowUntagged] = useState(false);
  const [tableView, setTableView] = useState(false);
  const [allHardest, setAllHardest] = useState(false);

  const idsKey = examIds.join(",");
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    insightsApi.insights(course, idsKey ? idsKey.split(",") : []).then(
      (d) => {
        if (!alive) return;
        setData(d);
        setLoading(false);
      },
      (e) => {
        if (!alive) return;
        setError(errorText(e));
        setLoading(false);
      },
    );
    return () => {
      alive = false;
    };
  }, [course, idsKey]);

  const topics = useMemo(
    () => (data?.topics ?? []).filter((t) => showUntagged || !t.untagged),
    [data, showUntagged],
  );
  const hasUntagged = (data?.topics ?? []).some((t) => t.untagged);
  const exams = data?.exams ?? [];
  const reteach = reteachTopics(data?.topics ?? []);

  function toggleExam(id: string) {
    setExamIds((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));
  }

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Class insights{course ? ` · ${course}` : ""}</h1>
          <p>
            {data
              ? `${plural(exams.length, "exam")} · ${plural(data.attempt_count, "submission")} · ungraded answers are left out`
              : "Which topics your class finds hard, and the mistakes behind it."}
          </p>
        </div>
        <div className="t-head-actions ins-filters">
          <label className="t-gb-filter" htmlFor="ins-course">
            Course
            <select
              id="ins-course"
              className="t-select"
              value={course}
              onChange={(e) => {
                setCourse(e.target.value);
                setExamIds([]);
              }}
            >
              <option value="">All courses</option>
              {(data?.courses ?? []).map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <details className="ins-exam-picker">
            <summary className="t-btn-outline">
              {examIds.length ? `${plural(examIds.length, "exam")} selected` : "All exams"}
            </summary>
            <fieldset>
              <legend className="sr-only">Exams to include</legend>
              {(data?.all_exams ?? []).length === 0 && <p className="t-muted t-small">No exams yet.</p>}
              {(data?.all_exams ?? []).map((e) => (
                <label key={e.exam_id} className="ins-check">
                  <input type="checkbox" checked={examIds.includes(e.exam_id)} onChange={() => toggleExam(e.exam_id)} />
                  <span>
                    {e.title}
                    <span className="t-muted t-small"> · {[e.course, shortDate(e.date)].filter(Boolean).join(" · ")}</span>
                  </span>
                </label>
              ))}
              {examIds.length > 0 && (
                <button type="button" className="t-link-btn" onClick={() => setExamIds([])}>
                  Use all exams
                </button>
              )}
            </fieldset>
          </details>
        </div>
      </header>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      {loading && !data ? (
        <Loading label="Loading insights…" />
      ) : !data ? null : data.topics.length === 0 ? (
        <div className="t-card">
          <div className="t-empty">
            <strong>Nothing to show yet</strong>
            Insights appear once students have submitted exams and answers are graded. Give every question a topic
            so results can be grouped.
          </div>
        </div>
      ) : (
        <div className="ins-stack" aria-busy={loading}>
          <section className="t-card" aria-labelledby="ins-heat-h">
            <div className="ins-card-head">
              <h2 id="ins-heat-h">Topics by exam</h2>
              <div className="ins-toggles">
                {hasUntagged && (
                  <label className="ins-check">
                    <input type="checkbox" checked={showUntagged} onChange={(e) => setShowUntagged(e.target.checked)} />
                    Show questions without a topic
                  </label>
                )}
                <label className="ins-check">
                  <input type="checkbox" checked={tableView} onChange={(e) => setTableView(e.target.checked)} />
                  Table view
                </label>
              </div>
            </div>
            {!tableView && <HeatLegend />}
            {tableView ? <TopicTable topics={topics} /> : <Heatmap topics={topics} exams={exams} />}
          </section>

          <div className="ins-two">
            <section className="t-card" aria-labelledby="ins-reteach-h">
              <h2 id="ins-reteach-h">What to reteach</h2>
              {reteach.length === 0 ? (
                <p className="t-muted">Not enough graded answers per topic yet.</p>
              ) : (
                <ol className="ins-reteach">
                  {reteach.map((t) => (
                    <li key={t.topic}>
                      <div>
                        <strong>{t.topic}</strong>
                        <span className="t-muted t-small">
                          {" "}
                          · {pct(t.avg_percent)} average · {plural(t.n_students, "student")}
                        </span>
                        <div className="t-small">
                          Trend: <TrendText value={t.trend} />
                        </div>
                      </div>
                      <Link className="t-btn-outline ins-small-btn" to={practiceForTopic(t.topic)}>
                        Make practice
                      </Link>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <section className="t-card" aria-labelledby="ins-hard-h">
              <h2 id="ins-hard-h">Hardest questions</h2>
              {data.hardest.length === 0 ? (
                <p className="t-muted">No question lost points yet.</p>
              ) : (
                <div className="t-table-scroll">
                  <table className="t-table">
                    <thead>
                      <tr>
                        <th scope="col">Question</th>
                        <th scope="col" className="num">
                          Average
                        </th>
                        <th scope="col" className="num">
                          Points lost
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {(allHardest ? data.hardest : data.hardest.slice(0, HARDEST_SHOWN)).map((h) => (
                        <tr key={`${h.exam_id}:${h.question_id}`}>
                          <td>
                            <Link to={`/teacher/exams/${encodeURIComponent(h.exam_id)}?tab=results`} className="t-row-link">
                              {h.exam_title} · Q{h.question_index + 1}
                            </Link>
                            <div className="ins-prompt">{h.prompt}</div>
                            {h.topics.length > 0 && (
                              <div className="ins-chips">
                                {h.topics.map((t) => (
                                  <span key={t} className="ins-chip">
                                    {t}
                                  </span>
                                ))}
                              </div>
                            )}
                          </td>
                          <td className="num">{pct(h.avg_percent)}</td>
                          <td className="num">{h.lost_points}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {data.hardest.length > HARDEST_SHOWN && (
                    <button type="button" className="t-link-btn" onClick={() => setAllHardest((v) => !v)}>
                      {allHardest ? "Show fewer" : `Show all ${data.hardest.length}`}
                    </button>
                  )}
                </div>
              )}
            </section>
          </div>

          <section aria-labelledby="ins-mist-h" className="ins-stack">
            <h2 id="ins-mist-h" className="ins-section-title">
              Common mistakes
            </h2>
            {data.mistakes.length === 0 ? (
              <div className="t-card">
                <p className="t-muted" style={{ margin: 0 }}>
                  No mistakes recorded yet. They come from your grading comments and the AI&apos;s mistake tags on
                  paper exams.
                </p>
              </div>
            ) : (
              <div className="ins-mistakes">
                {data.mistakes.map((m) => (
                  <MistakeCard key={m.label} m={m} />
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}

function Heatmap({ topics, exams }: { topics: TopicInsight[]; exams: InsightsExam[] }) {
  return (
    <div className="ins-heat-wrap">
      <table className="ins-heat">
        <caption className="sr-only">Average score per topic and exam. Empty cells: topic not asked.</caption>
        <thead>
          <tr>
            <th scope="col">Topic</th>
            {exams.map((e) => (
              <th scope="col" key={e.exam_id}>
                <span className="ins-heat-exam">{e.title}</span>
                <span className="ins-heat-date">{shortDate(e.date)}</span>
              </th>
            ))}
            <th scope="col">Overall</th>
            <th scope="col">Trend</th>
          </tr>
        </thead>
        <tbody>
          {topics.map((t) => {
            const cells = new Map(t.by_exam.map((c) => [c.exam_id, c]));
            return (
              <tr key={t.topic}>
                <th scope="row">{t.untagged ? <em>No topic</em> : t.topic}</th>
                {exams.map((e) => {
                  const c = cells.get(e.exam_id);
                  const step = heatStep(c?.avg_percent);
                  return (
                    <td
                      key={e.exam_id}
                      className={`ins-heat-cell ins-heat-${step ?? "none"}`}
                      title={c ? `${t.topic} · ${e.title}: ${pct(c.avg_percent)} over ${plural(c.n_answers, "answer")}` : "Not asked"}
                    >
                      {c ? pct(c.avg_percent) : <span className="sr-only">Not asked</span>}
                    </td>
                  );
                })}
                <td className={`ins-heat-cell ins-heat-${heatStep(t.avg_percent) ?? "none"} ins-heat-total`}>
                  {pct(t.avg_percent)}
                </td>
                <td className="ins-heat-trend">
                  <TrendText value={t.trend} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TopicTable({ topics }: { topics: TopicInsight[] }) {
  return (
    <div className="t-table-scroll">
      <table className="t-table">
        <thead>
          <tr>
            <th scope="col">Topic</th>
            <th scope="col" className="num">
              Average
            </th>
            <th scope="col" className="num">
              Answers
            </th>
            <th scope="col" className="num">
              Students
            </th>
            <th scope="col">Trend (last exam vs earlier)</th>
          </tr>
        </thead>
        <tbody>
          {topics.map((t) => (
            <tr key={t.topic}>
              <td className="strong">{t.untagged ? "No topic" : t.topic}</td>
              <td className="num">{pct(t.avg_percent)}</td>
              <td className="num">{t.n_answers}</td>
              <td className="num">{t.n_students}</td>
              <td>
                <TrendText value={t.trend} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MistakeCard({ m }: { m: MistakeCluster }) {
  const topic = m.topics[0];
  return (
    <article className="t-card ins-mistake" aria-label={m.label}>
      <div className="ins-mistake-head">
        <h3>{m.label}</h3>
        <span className="t-pill blue">{plural(m.count, "answer")}</span>
      </div>
      {m.topics.length > 0 && (
        <div className="ins-chips">
          {m.topics.map((t) => (
            <span key={t} className="ins-chip">
              {t}
            </span>
          ))}
        </div>
      )}
      <ul className="ins-examples">
        {m.examples.map((ex) => (
          <li key={`${ex.attempt_id}:${ex.question_id}`}>
            <q>{ex.text}</q>
            <div className="t-small t-muted">
              <Link to={profilePath(ex.username)}>{ex.name || ex.username}</Link>
              {" · "}
              <Link to={`/teacher/exams/${encodeURIComponent(ex.exam_id)}/submissions/${encodeURIComponent(ex.attempt_id)}`}>
                {ex.exam_title} · Q{ex.question_index + 1}
              </Link>
              {` · ${SOURCE_LABEL[ex.source] ?? ex.source}`}
            </div>
          </li>
        ))}
      </ul>
      {topic && (
        <Link className="t-btn-outline ins-small-btn" to={practiceForTopic(topic)}>
          Make practice
        </Link>
      )}
    </article>
  );
}
