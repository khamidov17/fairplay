import { Link } from "react-router-dom";
import { describeEntry, effectiveAllowedSites } from "../../components/teacher/externalValidation";
import type { Exam } from "../../api/types";
import { KIND_LABEL, kindOf } from "./examDraft";
import { QuestionImages, TopicChips } from "../../components/QuestionImages";

const LETTERS = "ABCDEFGH";

export function QuestionsView({ exam }: { exam: Exam }) {
  if (exam.mode === "external") {
    const sites = exam.allowed_domains ?? [];
    return (
      <section className="t-card" aria-labelledby="qv-h" style={{ gap: 14 }}>
        <h2 id="qv-h">Exam website</h2>
        <p style={{ margin: 0 }}>
          Students answer on{" "}
          <a href={exam.external_url ?? undefined} target="_blank" rel="noreferrer">
            {exam.external_url}
          </a>
          , opened inside the locked FairPlay app after they enter the start code.
        </p>
        <div>
          <strong style={{ fontSize: 14 }}>Websites students may open</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 20 }}>
            {effectiveAllowedSites(exam.external_url, sites).map((d) => (
              <li key={d}>{describeEntry(d)}</li>
            ))}
          </ul>
        </div>
        <p className="t-small t-muted" style={{ margin: 0 }}>
          {exam.finish_url_contains
            ? `The exam finishes by itself when the page address contains "${exam.finish_url_contains}". Students can also press Finish exam.`
            : "Students press Finish exam in the app when they are done."}
        </p>
      </section>
    );
  }
  return (
    <section className="t-card" aria-labelledby="qv-h" style={{ gap: 14 }}>
      <div className="t-card-head">
        <h2 id="qv-h">
          Questions{" "}
          <span className="t-muted" style={{ fontWeight: 500 }}>
            · {exam.questions.length}
          </span>
        </h2>
        {exam.status === "draft" && (
          <Link to={`/teacher/exams/${exam.id}/edit`} className="t-card-link">
            Edit questions
          </Link>
        )}
      </div>
      {exam.shuffle_questions && (
        <p className="t-small t-muted" style={{ margin: 0 }}>
          Each student sees these in a shuffled order.
        </p>
      )}
      {exam.questions.length === 0 ? (
        <div className="t-empty">This exam has no questions.</div>
      ) : (
        <ol style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 12 }}>
          {exam.questions.map((q, i) => {
            const kind = kindOf(q);
            return (
              <li key={q.id} className="t-q">
                <div className="t-q-summary">
                  <span className="t-q-num">{String(i + 1).padStart(2, "0")}</span>
                  <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
                    <QuestionImages images={q.images} compact />
                    <span className="t-q-prompt">{q.prompt}</span>
                    <TopicChips topics={q.topics} />
                    {q.type === "mcq" && kind !== "tf" && (
                      <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
                        {q.options.map((o, j) => (
                          <li key={j} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 14 }}>
                            <span className="t-opt-letter">{LETTERS[j]}</span>
                            <span>{o}</span>
                            {q.correct_index === j && <span className="t-pill green">Correct</span>}
                          </li>
                        ))}
                      </ul>
                    )}
                    {kind === "tf" && (
                      <span className="t-small">
                        Answer: <strong>{q.correct_index === 0 ? "True" : "False"}</strong>
                      </span>
                    )}
                    {q.type === "numeric" && (
                      <span className="t-small">
                        Answer <strong className="mono">{q.answer_number}</strong>
                        {q.tolerance ? (
                          <>
                            {" "}
                            (± <span className="mono">{q.tolerance}</span>)
                          </>
                        ) : (
                          " (exact)"
                        )}
                      </span>
                    )}
                    {q.type === "short" && (
                      <span className="t-small">
                        Accepted: <strong>{q.accepted_answers.join(", ")}</strong>
                      </span>
                    )}
                    {q.type === "essay" && (
                      <>
                        {q.rubric?.length > 0 && (
                          <ul className="t-small" style={{ margin: 0, paddingLeft: 18 }}>
                            {q.rubric.map((r, j) => (
                              <li key={j}>
                                {r.label} · {r.points} pts
                              </li>
                            ))}
                          </ul>
                        )}
                        {(q.min_words != null || q.max_words != null) && (
                          <span className="t-small t-muted">
                            {q.min_words != null && `At least ${q.min_words} words`}
                            {q.min_words != null && q.max_words != null && " · "}
                            {q.max_words != null && `At most ${q.max_words} words`}
                          </span>
                        )}
                      </>
                    )}
                    <span className="t-q-meta">
                      {KIND_LABEL[kind]} · {q.points} {q.points === 1 ? "pt" : "pts"}
                    </span>
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
