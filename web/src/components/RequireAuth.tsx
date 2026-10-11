import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { getToken, loginPath } from "../api/client";
import type { SessionRole } from "../api/types";

export function RequireAuth({ role, children }: { role: SessionRole; children: ReactNode }) {
  if (!getToken(role)) return <Navigate to={loginPath(role)} replace />;
  return <>{children}</>;
}
