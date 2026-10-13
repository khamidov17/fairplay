import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { getToken } from "../api/client";
import type { Role } from "../api/types";

export function RequireAuth({ role, children }: { role: Role; children: ReactNode }) {
  if (!getToken(role)) return <Navigate to={`/${role}/login`} replace />;
  return <>{children}</>;
}
