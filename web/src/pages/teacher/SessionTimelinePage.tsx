// Phase 1a: one student's exam session, minute by minute — sign-ins, start, saves
// (coalesced), connection gaps, lockdown events, submit, grade changes — merged by the server
// (GET /api/exams/{id}/attempts/{aid}/timeline) and shown as one ordered list.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, securityApi } from "../../api/client";
import type { SessionTimeline, TimelineEvent } from "../../api/types";
import { Loading } from "../../components/Loading";
import { Pill } from "../../components/teacher/TeacherLayout";
import { errorText, formatWhen } from "../../components/teacher/util";
import { formatClock } from "../../lib/timer";
import "../../styles/timeline.css";

type Filter = "all" | "look";

const ACTOR_LABEL: Record<string, string> = { student: "Student", teacher: "Teacher", system: "FairPlay" };
const SOURCE_LABEL: Record<string, string> = {
  audit: "Server log",
  integrity: "Lockdown app",
  heartbeat: "Connection",
};

function dayOf(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

export function timelineCounts(events: TimelineEvent[]) {
  return {
    look: events.filter((e) => e.tone === "warn").length,
    lockdown: events.filter((e) => e.source === "integrity" && e.tone === "warn").length,
    gaps: events.filter((e) => e.action === "connection_lost").length,
  };
}

export default function SessionTimelinePage() {
  const { id: examId = "", attemptId = "" } = useParams();
  const [data, setData] = useState<SessionTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const load = useCallback(async () => {
    try {
      setData(await securityApi.timeline(examId, attemptId));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404 ? "This exam session was not found." : errorText(e));
    }
  }, [examId, attemptId]);

  useEffect(() => {
    void load();
  }, [load]);

  const back = `/teacher/exams/${encodeURIComponent(examId)}/submissions/${encodeURIComponent(attemptId)}`;
  const shown = useMemo(
    () => (data ? (filter === "look" ? data.events.filter((e) => e.tone === "warn") : data.events) : []),
    [data, filter],
  );

  if (error)
    return (
      <div className="t-page">
        <Link to={back} className="t-back">
          ← Submission
        </Link>
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      </div>
    );
  if (!data) return <Loading label="Loading session timeline…" />;

  const counts = timelineCounts(data.events);
  let lastDay = "";

  return (
    <div className="t-page t-review">
      <div className="t-review-top">
        <Link to={back} className="t-back">
          ← {data.student.name}'s submission
        </Link>
        <button type="button" className="t-btn-outline" onClick={() => void load()}>
          Refresh
        </button>
      </div>

      <header className="t-review-head">
        <div>
          <h1>Session timeline</h1>
          <p className="t-muted" style={{ margin: 0 }}>
            {data.student.name} · {data.student.username} · {data.exam_title}
          </p>
        </div>
        <div className="tl-pills" aria-label="Session status">
          {data.status === "submitted" ? <Pill tone="green">Submitted</Pill> : <Pill tone="blue" live>Still writing</Pill>}
          {data.late && <Pill tone="orange">After the deadline</Pill>}
          {data.auto_submitted && <Pill>Submitted when the exam closed</Pill>}
        </div>
      </header>

      <section className="t-card tl-facts" aria-label="Session facts">
        <div>
          <span className="tl-fact-label">Started</span>
          <span className="tl-fact-value">{formatWhen(data.started_at)}</span>
        </div>
        <div>
          <span className="tl-fact-label">Deadline</span>
          <span className="tl-fact-value">{formatWhen(data.deadline_at)}</span>
        </div>
        <div>
          <span className="tl-fact-label">Submitted</span>
          <span className="tl-fact-value">{data.submitted_at ? formatWhen(data.submitted_at) : "—"}</span>
        </div>
        <div>
          <span className="tl-fact-label">Receipt</span>
          <span className="tl-fact-value">{data.receipt ?? "—"}</span>
        </div>
        <div>
          <span className="tl-fact-label">Connection drops</span>
          <span className="tl-fact-value">{counts.gaps}</span>
        </div>
        <div>
          <span className="tl-fact-label">Lockdown flags</span>
          <span className="tl-fact-value">{counts.lockdown}</span>
        </div>
      </section>

      <section className="t-card" aria-labelledby="tl-h">
        <div className="t-card-head">
          <h2 id="tl-h">What happened</h2>
          <div className="t-seg" role="group" aria-label="Show">
            <button type="button" className={filter === "all" ? "on" : ""} aria-pressed={filter === "all"} onClick={() => setFilter("all")}>
              Everything ({data.events.length})
            </button>
            <button type="button" className={filter === "look" ? "on" : ""} aria-pressed={filter === "look"} onClick={() => setFilter("look")}>
              Worth a look ({counts.look})
            </button>
          </div>
        </div>
        {shown.length === 0 ? (
          <p className="t-muted" style={{ margin: 0 }}>
            {filter === "look" ? "Nothing unusual in this session." : "Nothing recorded yet."}
          </p>
        ) : (
          <ol className="tl-list">
            {shown.map((ev, i) => {
              const day = dayOf(ev.at);
              const showDay = day !== lastDay;
              lastDay = day;
              return (
                <li key={`${ev.at}-${i}`} className={`tl-item ${ev.tone}`}>
                  {showDay && <div className="tl-day">{day}</div>}
                  <div className="tl-row">
                    <time className="tl-time" dateTime={ev.at}>
                      {formatClock(ev.at)}
                    </time>
                    <span className="tl-dot" aria-hidden="true" />
                    <div className="tl-body">
                      <span className="tl-summary">{ev.summary}</span>
                      <span className="tl-meta">
                        {ACTOR_LABEL[ev.actor] ?? ev.actor} · {SOURCE_LABEL[ev.source] ?? ev.source}
                        {ev.ip ? ` · ${ev.ip}` : ""}
                      </span>
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </section>
    </div>
  );
}
