// Slice 5 — LMS sync: connect an LMS, link courses (roster import), see linked courses.
import { useCallback, useEffect, useState } from "react";
import { lmsApi } from "../../api/client";
import type { LmsConnection, LmsCourse, LmsCourseLink, LmsProvider, LmsSyncResult } from "../../api/types";
import { Loading } from "../../components/Loading";
import { LmsConnectForm } from "../../components/teacher/lms/LmsConnectForm";
import { errorText, formatWhen, plural } from "../../components/teacher/util";
import { Icon } from "../../components/ui/Icon";
import "../../components/teacher/lms/lms.css";

export function syncSummary(s: LmsSyncResult): string {
  const parts = [`${s.added} added`];
  if (s.updated) parts.push(`${s.updated} renamed`);
  if (s.removed) parts.push(`${s.removed} removed`);
  return `${plural(s.total, "student")} in the LMS course · ${parts.join(", ")}`;
}

export default function LmsPage() {
  const [providers, setProviders] = useState<LmsProvider[]>([]);
  const [connections, setConnections] = useState<LmsConnection[]>([]);
  const [links, setLinks] = useState<LmsCourseLink[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const [openConn, setOpenConn] = useState<number | null>(null);
  const [courses, setCourses] = useState<LmsCourse[] | null>(null);
  const [coursesBusy, setCoursesBusy] = useState(false);
  const [coursesError, setCoursesError] = useState<string | null>(null);

  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const [c, l] = await Promise.all([lmsApi.connections(), lmsApi.linkedCourses()]);
    setConnections(c);
    setLinks(l);
    return c;
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([lmsApi.providers(), lmsApi.connections(), lmsApi.linkedCourses()]).then(
      ([p, c, l]) => {
        if (!alive) return;
        setProviders(p);
        setConnections(c);
        setLinks(l);
        setShowForm(c.length === 0);
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
  }, []);

  async function loadCourses(connId: number) {
    setOpenConn(connId);
    setCourses(null);
    setCoursesError(null);
    setCoursesBusy(true);
    try {
      setCourses(await lmsApi.courses(connId));
    } catch (e) {
      setCoursesError(errorText(e));
      reload().catch(() => undefined); // picks up last_error on the connection
    } finally {
      setCoursesBusy(false);
    }
  }

  async function onConnected(c: LmsConnection) {
    setShowForm(false);
    setNotice(`Connected to ${c.provider_name}${c.lms_user_name ? ` as ${c.lms_user_name}` : ""}.`);
    await reload().catch(() => undefined);
    await loadCourses(c.id);
  }

  async function run(key: string, fn: () => Promise<void>) {
    setBusyKey(key);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setActionError(errorText(e));
      reload().catch(() => undefined);
    } finally {
      setBusyKey(null);
    }
  }

  const link = (connId: number, course: LmsCourse) =>
    run(`link:${course.lms_course_id}`, async () => {
      const r = await lmsApi.linkCourse(connId, course.lms_course_id);
      setNotice(`Linked ${r.name}. ${syncSummary(r.sync)}.`);
      setCourses((cs) => cs?.map((c) => (c.lms_course_id === course.lms_course_id ? { ...c, link_id: r.id } : c)) ?? cs);
      await reload();
    });

  const resync = (linkId: number) =>
    run(`sync:${linkId}`, async () => {
      const r = await lmsApi.resync(linkId);
      setNotice(`Roster of ${r.name} updated. ${syncSummary(r.sync)}.`);
      await reload();
    });

  const disconnect = (c: LmsConnection) =>
    run(`del:${c.id}`, async () => {
      const ok = window.confirm(
        `Remove the ${c.provider_name} connection? Linked courses are unlinked from your exams. ` +
          "Students already imported keep their FairPlay accounts.",
      );
      if (!ok) return;
      await lmsApi.disconnect(c.id);
      if (openConn === c.id) {
        setOpenConn(null);
        setCourses(null);
      }
      const left = await reload();
      if (left.length === 0) setShowForm(true);
      setNotice(`${c.provider_name} connection removed.`);
    });

  if (loading) return <Loading />;

  const openConnection = connections.find((c) => c.id === openConn) ?? null;

  return (
    <>
      <header className="t-page-head">
        <div className="t-head-text">
          <h1>LMS sync</h1>
          <p>Bring your class list from Canvas (or another LMS) and send released grades back.</p>
        </div>
        {!showForm && connections.length > 0 && (
          <div className="t-head-actions">
            <button type="button" className="t-btn-outline" onClick={() => setShowForm(true)}>
              <Icon name="plus" size={18} />
              Connect another LMS
            </button>
          </div>
        )}
      </header>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {actionError && (
        <div className="alert alert-error" role="alert">
          {actionError}
        </div>
      )}
      {notice && (
        <div className="alert alert-success" role="status">
          {notice}
        </div>
      )}

      {showForm && (
        <section className="t-card" aria-labelledby="lms-connect-h">
          <div className="t-card-head">
            <h2 id="lms-connect-h">Connect your LMS</h2>
          </div>
          <LmsConnectForm
            providers={providers}
            onConnected={onConnected}
            onCancel={connections.length ? () => setShowForm(false) : undefined}
          />
        </section>
      )}

      {connections.length > 0 && (
        <section className="t-card" aria-labelledby="lms-conn-h">
          <div className="t-card-head">
            <h2 id="lms-conn-h">Connections</h2>
          </div>
          <div>
            {connections.map((c) => (
              <div key={c.id} className="lms-conn">
                <div className="lms-conn-main">
                  <span className="lms-conn-title">
                    {c.label || c.provider_name}
                    {c.status === "beta" && <span className="t-pill orange">Beta</span>}
                  </span>
                  <span className="lms-conn-sub">
                    {c.provider_name} · {c.base_url}
                    {c.lms_user_name ? ` · signed in as ${c.lms_user_name}` : ""}
                  </span>
                  <span className="lms-conn-sub">
                    {c.last_sync_at ? `Last sync ${formatWhen(c.last_sync_at)}` : "Not synced yet"} ·{" "}
                    {plural(c.course_count, "linked course")}
                  </span>
                  {c.last_error && <span className="t-inline-error">{c.last_error}</span>}
                </div>
                <div className="lms-actions">
                  <button
                    type="button"
                    className="t-btn-outline"
                    onClick={() => loadCourses(c.id)}
                    disabled={coursesBusy && openConn === c.id}
                  >
                    {coursesBusy && openConn === c.id ? "Loading courses…" : "Show courses"}
                  </button>
                  <button
                    type="button"
                    className="t-btn-outline"
                    onClick={() => disconnect(c)}
                    disabled={busyKey === `del:${c.id}`}
                    aria-label={`Remove ${c.label || c.provider_name}`}
                  >
                    <Icon name="trash" size={16} />
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {openConnection && (
        <section className="t-card" aria-labelledby="lms-courses-h">
          <div className="t-card-head">
            <h2 id="lms-courses-h">Your courses in {openConnection.provider_name}</h2>
          </div>
          <p className="lms-note">
            Linking a course imports its students into FairPlay. Exams linked to the course can only be started by
            those students.
          </p>
          {coursesBusy && <Loading />}
          {coursesError && (
            <div className="alert alert-error" role="alert">
              {coursesError}
            </div>
          )}
          {courses && courses.length === 0 && (
            <div className="t-empty">
              <strong>No courses found</strong>
              You are not listed as a teacher in any course on this LMS.
            </div>
          )}
          {courses && courses.length > 0 && (
            <div className="t-table-scroll">
              <table className="t-table">
                <thead>
                  <tr>
                    <th>Course</th>
                    <th>Code</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {courses.map((c) => (
                    <tr key={c.lms_course_id}>
                      <td className="strong">{c.name}</td>
                      <td>{c.code || "—"}</td>
                      <td className="num">
                        {c.link_id ? (
                          <button
                            type="button"
                            className="t-btn-outline"
                            onClick={() => resync(c.link_id as number)}
                            disabled={busyKey === `sync:${c.link_id}`}
                          >
                            <Icon name="sync" size={16} />
                            {busyKey === `sync:${c.link_id}` ? "Syncing…" : "Re-sync roster"}
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn btn-primary"
                            onClick={() => link(openConnection.id, c)}
                            disabled={busyKey === `link:${c.lms_course_id}`}
                            aria-label={`Link ${c.name}`}
                          >
                            {busyKey === `link:${c.lms_course_id}` ? "Linking…" : "Link"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <section className="t-card" aria-labelledby="lms-linked-h">
        <div className="t-card-head">
          <h2 id="lms-linked-h">Linked courses</h2>
        </div>
        {links.length === 0 ? (
          <div className="t-empty">
            <strong>No linked courses yet</strong>
            {connections.length ? "Press “Show courses” and link one." : "Connect your LMS first."}
          </div>
        ) : (
          <div className="t-table-scroll">
            <table className="t-table">
              <thead>
                <tr>
                  <th>Course</th>
                  <th>LMS</th>
                  <th className="num">Students</th>
                  <th className="num">Exams</th>
                  <th>Last sync</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {links.map((l) => (
                  <tr key={l.id}>
                    <td>
                      <span className="strong">{l.name}</span>
                      {l.code && <span className="t-muted"> · {l.code}</span>}
                      {l.last_error && <div className="t-inline-error">{l.last_error}</div>}
                    </td>
                    <td>{l.provider_name}</td>
                    <td className="num">{l.member_count}</td>
                    <td className="num">{l.exam_count}</td>
                    <td>{formatWhen(l.last_sync_at)}</td>
                    <td className="num">
                      <button
                        type="button"
                        className="t-btn-outline"
                        onClick={() => resync(l.id)}
                        disabled={busyKey === `sync:${l.id}`}
                        aria-label={`Re-sync roster of ${l.name}`}
                      >
                        <Icon name="sync" size={16} />
                        {busyKey === `sync:${l.id}` ? "Syncing…" : "Re-sync roster"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="lms-note">
          To use a linked course, pick it under “Course in your LMS” when you set up an exam. After you release grades,
          press “Send grades” on the exam’s Results tab.
        </p>
      </section>
    </>
  );
}
