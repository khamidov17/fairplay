import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, auth, homePath, setToken } from "../api/client";
import type { Role, SessionRole } from "../api/types";
import { Icon, LogoMark } from "../components/ui/Icon";
import "../styles/teacher.css";
import "../styles/admin.css";

const COPY: Record<SessionRole, { title: string; sub: string; hint: string; pitch: string; points: string[] }> = {
  teacher: {
    title: "Teacher sign in",
    sub: "Create exams, run them live and grade in one place.",
    hint: "teacher / teacher123",
    pitch: "Calm, fair exams on every student laptop.",
    points: ["Full-screen lockdown on every laptop", "Start codes you read aloud in the room", "Grade essays with rubrics, release when ready"],
  },
  student: {
    title: "Student sign in",
    sub: "Sign in to write your exam and see your grades.",
    hint: "alice / alice123",
    pitch: "Your answers are saved as you type.",
    points: ["Answers sync even if the Wi-Fi drops", "Your teacher reads the start code aloud", "See feedback once grades are released"],
  },
  superadmin: {
    title: "Administrator sign in",
    sub: "Approve teachers and see how FairPlay is used.",
    hint: "admin / admin123",
    pitch: "One place to look after every teacher account.",
    points: ["Approve new teacher sign-ups", "See exams created and held", "Disable accounts that should not sign in"],
  },
};

export default function LoginPage({ role }: { role: SessionRole }) {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [forgot, setForgot] = useState(false);
  const copy = COPY[role];
  const other: Role = role === "teacher" ? "student" : "teacher";
  const canRegister = role !== "superadmin";

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!username.trim() || !password) {
      setError("Enter your username or email and your password.");
      return;
    }
    setBusy(true);
    try {
      const res = await auth.login(username.trim(), password);
      // Any sign-in page works for any account: go to that role's home.
      setToken(res.role, res.token);
      navigate(homePath(res.role), { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setError("Invalid username, email or password.");
      else if (err instanceof ApiError) setError(err.detail);
      else setError("Cannot reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="t-login">
      <aside className="t-login-side" aria-label="About FairPlay">
        <span className="t-brand">
          <LogoMark />
          <span>FairPlay</span>
        </span>
        <h2>{copy.pitch}</h2>
        <ul className="t-login-points">
          {copy.points.map((p) => (
            <li key={p}>
              <Icon name="check" size={18} />
              {p}
            </li>
          ))}
        </ul>
      </aside>
      <main className="t-login-main">
        <div className="t-login-card">
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <h1>{copy.title}</h1>
            <p className="t-muted" style={{ margin: 0, fontSize: 15 }}>
              {copy.sub}
            </p>
          </div>
          <form onSubmit={onSubmit} noValidate>
            <div className="t-field">
              <label htmlFor="username">Username or email</label>
              <input
                id="username"
                className="input"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
              />
            </div>
            <div className="t-field">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                className="input"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <button
                type="button"
                className="a-link-btn"
                aria-expanded={forgot}
                onClick={() => setForgot((f) => !f)}
              >
                Forgot password?
              </button>
              {forgot && (
                <p className="a-help" role="note">
                  Contact your administrator to reset your password.
                </p>
              )}
            </div>
            {error && (
              <div className="alert alert-error" role="alert">
                {error}
              </div>
            )}
            <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
          <p className="t-demo" style={{ margin: 0 }}>
            Demo account: <span className="mono">{copy.hint}</span>
          </p>
          {canRegister && (
            <>
              <p className="t-login-switch" style={{ margin: 0 }}>
                New to FairPlay? <Link to={`/register?role=${role}`}>Create account</Link>
              </p>
              <p className="t-login-switch" style={{ margin: 0 }}>
                {role === "teacher" ? "Writing an exam? " : "Teaching? "}
                <Link to={`/${other}/login`}>{other === "teacher" ? "Teacher sign in" : "Student sign in"}</Link>
              </p>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
