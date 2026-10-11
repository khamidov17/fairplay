import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, auth, homePath, setToken } from "../api/client";
import type { Role } from "../api/types";
import { Icon, LogoMark } from "../components/ui/Icon";
import { validateRegister, type RegisterErrors, type RegisterForm } from "./registerValidation";
import "../styles/teacher.css";
import "../styles/admin.css";

const ROLES: { id: Role; label: string; sub: string }[] = [
  { id: "teacher", label: "Teacher", sub: "Create and run exams" },
  { id: "student", label: "Student", sub: "Write exams, see grades" },
];

type Field = Exclude<keyof RegisterForm, "role">;
const FIELDS: { id: Field; label: string; type: string; autoComplete: string; hint?: string }[] = [
  { id: "name", label: "Full name", type: "text", autoComplete: "name" },
  { id: "email", label: "Email", type: "email", autoComplete: "email" },
  { id: "username", label: "Username", type: "text", autoComplete: "username", hint: "You can sign in with this or your email." },
  { id: "password", label: "Password", type: "password", autoComplete: "new-password", hint: "At least 8 characters, with a digit or symbol." },
  { id: "confirm", label: "Confirm password", type: "password", autoComplete: "new-password" },
];

export default function RegisterPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const initialRole = params.get("role");
  const [form, setForm] = useState<RegisterForm>({
    name: "",
    email: "",
    username: "",
    password: "",
    confirm: "",
    role: initialRole === "teacher" || initialRole === "student" ? initialRole : "",
  });
  const [errors, setErrors] = useState<RegisterErrors>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const set = (k: keyof RegisterForm, v: string) => {
    setForm((f) => ({ ...f, [k]: v }));
    if (errors[k]) setErrors((e) => ({ ...e, [k]: undefined }));
  };

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setServerError(null);
    const errs = validateRegister(form);
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    try {
      const res = await auth.register({
        name: form.name.trim(),
        email: form.email.trim(),
        username: form.username.trim(),
        password: form.password,
        role: form.role as Role,
      });
      if (res.status === "pending" || !res.token) {
        setPending(res.detail ?? "Your teacher account is waiting for approval by an administrator.");
        return;
      }
      setToken(res.role, res.token);
      navigate(homePath(res.role), { replace: true });
    } catch (err) {
      if (err instanceof ApiError) setServerError(err.detail);
      else setServerError("Cannot reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  const loginRole: Role = form.role === "student" ? "student" : "teacher";

  return (
    <div className="t-login">
      <aside className="t-login-side" aria-label="About FairPlay">
        <span className="t-brand">
          <LogoMark />
          <span>FairPlay</span>
        </span>
        <h2>Fair exams start with your own account.</h2>
        <ul className="t-login-points">
          <li>
            <Icon name="check" size={18} />
            Teachers create exams and grade in one place
          </li>
          <li>
            <Icon name="check" size={18} />
            Students join their classes with a code
          </li>
          <li>
            <Icon name="check" size={18} />
            Sign in later with your username or email
          </li>
        </ul>
      </aside>
      <main className="t-login-main">
        <div className="t-login-card a-register-card">
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <h1>Create account</h1>
            <p className="t-muted" style={{ margin: 0, fontSize: 15 }}>
              Choose your role, then fill in your details.
            </p>
          </div>
          {pending ? (
            <div className="a-done" role="status">
              <div className="alert alert-info">{pending}</div>
              <p className="t-muted" style={{ margin: 0 }}>
                You can sign in once an administrator approves your account.
              </p>
              <Link className="btn btn-secondary btn-block" to="/teacher/login">
                Back to sign in
              </Link>
            </div>
          ) : (
            <form onSubmit={onSubmit} noValidate>
              <fieldset className="a-roles" aria-describedby={errors.role ? "role-error" : undefined}>
                <legend>I am a</legend>
                {ROLES.map((r) => (
                  <label key={r.id} className={`a-role${form.role === r.id ? " selected" : ""}`}>
                    <input
                      type="radio"
                      name="role"
                      value={r.id}
                      checked={form.role === r.id}
                      onChange={() => set("role", r.id)}
                    />
                    <span className="a-role-label">{r.label}</span>
                    <span className="a-role-sub">{r.sub}</span>
                  </label>
                ))}
                {errors.role && (
                  <span id="role-error" className="a-field-error">
                    {errors.role}
                  </span>
                )}
              </fieldset>
              {FIELDS.map((f) => (
                <div className="t-field" key={f.id}>
                  <label htmlFor={`reg-${f.id}`}>{f.label}</label>
                  <input
                    id={`reg-${f.id}`}
                    className="input"
                    type={f.type}
                    autoComplete={f.autoComplete}
                    autoCapitalize={f.id === "name" ? undefined : "none"}
                    spellCheck={f.id === "name"}
                    value={form[f.id]}
                    onChange={(e) => set(f.id, e.target.value)}
                    aria-invalid={errors[f.id] ? true : undefined}
                    aria-describedby={errors[f.id] ? `reg-${f.id}-error` : f.hint ? `reg-${f.id}-hint` : undefined}
                  />
                  {errors[f.id] ? (
                    <span id={`reg-${f.id}-error`} className="a-field-error">
                      {errors[f.id]}
                    </span>
                  ) : (
                    f.hint && (
                      <span id={`reg-${f.id}-hint`} className="a-help">
                        {f.hint}
                      </span>
                    )
                  )}
                </div>
              ))}
              {serverError && (
                <div className="alert alert-error" role="alert">
                  {serverError}
                </div>
              )}
              <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={busy}>
                {busy ? "Creating account…" : "Create account"}
              </button>
            </form>
          )}
          <p className="t-login-switch" style={{ margin: 0 }}>
            Already have an account? <Link to={`/${loginRole}/login`}>Sign in</Link>
          </p>
        </div>
      </main>
    </div>
  );
}
