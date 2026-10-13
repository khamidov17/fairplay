// 2026-10-13: /student/join/<code> — the link a teacher shares from the class page.
// Signed out: remember the code, sign in, then the home page joins (JoinClass).
import { useEffect, useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { getToken } from "../../api/client";

export const PENDING_JOIN_KEY = "fairplay-pending-join";

export function rememberJoinCode(code: string): void {
  try {
    sessionStorage.setItem(PENDING_JOIN_KEY, code);
  } catch {
    /* private mode: the student types the code instead */
  }
}

export function takeJoinCode(): string | null {
  try {
    const c = sessionStorage.getItem(PENDING_JOIN_KEY);
    sessionStorage.removeItem(PENDING_JOIN_KEY);
    return c;
  } catch {
    return null;
  }
}

export default function JoinLinkPage() {
  const { code = "" } = useParams();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const clean = code.replace(/[^A-Za-z0-9]/g, "").toUpperCase().slice(0, 20);
    if (clean) rememberJoinCode(clean);
    setReady(true);
  }, [code]);
  if (!ready) return null;
  return <Navigate to={getToken("student") ? "/student" : "/student/login"} replace />;
}
