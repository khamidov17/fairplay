import type { ReactNode } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { clearToken } from "../../api/client";
import { clearExamDataOnSignOut } from "../../lib/offline";
import { useFeatures } from "../../lib/features";
import { useMe } from "../../lib/useMe";
import { ShieldIcon } from "./icons";
import "../../styles/student.css";

export function StudentLogo({ to = "/student" }: { to?: string }) {
  return (
    <Link to={to} className="stu-logo" aria-label="FairPlay home">
      <span className="stu-logo-mark">
        <ShieldIcon />
      </span>
      <span>FairPlay</span>
    </Link>
  );
}

/** Top-nav layout for every student page except the exam itself. */
export function StudentLayout({ children, white }: { children: ReactNode; white?: boolean }) {
  const navigate = useNavigate();
  const me = useMe("student");
  const features = useFeatures();
  const initial = me?.name?.trim().charAt(0).toUpperCase() || "?";
  const signOut = () => {
    clearExamDataOnSignOut();   // audit W-03: exam copies, packages and sessions leave this computer
    clearToken("student");
    navigate("/student/login");
  };
  return (
    <div className={`stu${white ? " stu-white" : ""}`}>
      <header className="stu-topnav">
        <StudentLogo />
        <nav aria-label="Main" className="stu-nav">
          <NavLink to="/student" end>
            Home
          </NavLink>
          <NavLink to="/student/exams">Exams</NavLink>
          <NavLink to="/student/grades">Grades</NavLink>
          {features.has("practice") && <NavLink to="/student/practice">Practice</NavLink>}
        </nav>
        <div className="stu-user">
          <span className="stu-avatar" title={me?.name} aria-label={me ? `Signed in as ${me.name}` : "Signed in"} role="img">
            {initial}
          </span>
          <button type="button" className="stu-btn quiet" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>
      {children}
    </div>
  );
}

export function StuLoading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="stu-loading" role="status" aria-live="polite">
      <span className="stu-spin" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}
