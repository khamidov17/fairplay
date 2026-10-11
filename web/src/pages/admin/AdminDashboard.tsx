import { useCallback, useEffect, useState } from "react";
import { adminApi, ApiError } from "../../api/client";
import type { AccountStatus, AdminStats, AdminTeacher } from "../../api/types";
import { Loading } from "../../components/Loading";

type Action = "approve" | "disable" | "enable";

const STATUS: Record<AccountStatus, { label: string; tone: string }> = {
  active: { label: "Active", tone: "green" },
  pending: { label: "Pending approval", tone: "orange" },
  disabled: { label: "Disabled", tone: "red" },
};

const ACTIONS: Record<AccountStatus, { action: Action; label: string; className: string }[]> = {
  pending: [
    { action: "approve", label: "Approve", className: "btn btn-primary" },
    { action: "disable", label: "Disable", className: "btn btn-secondary" },
  ],
  active: [{ action: "disable", label: "Disable", className: "btn btn-secondary" }],
  disabled: [{ action: "enable", label: "Enable", className: "btn btn-secondary" }],
};

export function fmtDate(iso: string | null): string {
  if (!iso) return "Never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

function Tile({ label, value, sub, tone }: { label: string; value: number; sub?: string; tone?: string }) {
  return (
    <div className={`t-stat${tone === "orange" && value > 0 ? " warn" : ""}`} data-testid={`tile-${label}`}>
      <span className={`t-stat-value${tone ? ` ${tone}` : ""}`}>
        {value.toLocaleString()}
        {sub && <small> {sub}</small>}
      </span>
      <span className="t-stat-label">{label}</span>
    </div>
  );
}

export default function AdminDashboard() {
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [teachers, setTeachers] = useState<AdminTeacher[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([adminApi.stats(), adminApi.teachers()]);
      setStats(s);
      setTeachers(t);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Cannot reach the server. Try again.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(t: AdminTeacher, action: Action) {
    setBusyId(t.id);
    setError(null);
    try {
      const updated = await adminApi.setTeacherStatus(t.id, action);
      setTeachers((list) => list?.map((x) => (x.id === updated.id ? updated : x)) ?? null);
      setStats(await adminApi.stats());
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Cannot reach the server. Try again.");
    } finally {
      setBusyId(null);
    }
  }

  if (!stats || !teachers) {
    return error ? (
      <div className="alert alert-error" role="alert">
        {error}
      </div>
    ) : (
      <Loading label="Loading dashboard…" />
    );
  }

  return (
    <>
      <div className="t-page-head">
        <div className="t-head-text">
          <h1>Overview</h1>
          <p>Teachers, students and exams across FairPlay.</p>
        </div>
      </div>

      <section className="t-stats" aria-label="Platform totals">
        <Tile label="Active teachers" value={stats.teachers.active} tone="blue" />
        <Tile label="Pending teachers" value={stats.teachers.pending} tone="orange" />
        <Tile label="Students" value={stats.students} />
        <Tile label="Exams created" value={stats.exams_created} />
        <Tile label="Exams held" value={stats.exams_held} />
        <Tile label="Attempts submitted" value={stats.attempts_submitted} />
        <Tile label="Teachers active, 7 days" value={stats.active_teachers_7d} tone="green" />
        <Tile label="Teachers active, 30 days" value={stats.active_teachers_30d} tone="green" />
      </section>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <section className="t-card" aria-labelledby="teachers-h">
        <div className="t-card-head">
          <h2 id="teachers-h">Teachers</h2>
          <span className="t-muted">{teachers.length} accounts</span>
        </div>
        {teachers.length === 0 ? (
          <p className="t-muted" style={{ margin: 0 }}>
            No teachers yet.
          </p>
        ) : (
          <div className="t-table-scroll">
            <table className="t-table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Email / username</th>
                  <th scope="col">Signed up</th>
                  <th scope="col">Last login</th>
                  <th scope="col" className="num">
                    Exams
                  </th>
                  <th scope="col" className="num">
                    Students reached
                  </th>
                  <th scope="col">Status</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {teachers.map((t) => (
                  <tr key={t.id} data-testid={`teacher-row-${t.username}`}>
                    <td className="strong">{t.name}</td>
                    <td>
                      <div className="a-ident">
                        {t.email && <span>{t.email}</span>}
                        <span className="t-muted">{t.username}</span>
                      </div>
                    </td>
                    <td>{fmtDate(t.created_at)}</td>
                    <td>{fmtDate(t.last_login_at)}</td>
                    <td className="num">{t.exams}</td>
                    <td className="num">{t.students_reached}</td>
                    <td>
                      <span className={`t-pill ${STATUS[t.status].tone}`}>{STATUS[t.status].label}</span>
                    </td>
                    <td>
                      <div className="a-actions">
                        {ACTIONS[t.status].map((a) => (
                          <button
                            key={a.action}
                            type="button"
                            className={a.className}
                            disabled={busyId === t.id}
                            aria-label={`${a.label} ${t.name}`}
                            onClick={() => void act(t, a.action)}
                          >
                            {a.label}
                          </button>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
