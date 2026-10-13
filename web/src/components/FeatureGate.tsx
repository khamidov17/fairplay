// 2026-10-13: renders an optional page only when the server switched the feature on.
import { useEffect, useState, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { loadFeatures, type Feature } from "../lib/features";
import { Loading } from "./Loading";

export function FeatureGate({ feature, fallback, children }: { feature: Feature; fallback: string; children: ReactNode }) {
  const [on, setOn] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    void loadFeatures().then((f) => alive && setOn(f.has(feature)));
    return () => {
      alive = false;
    };
  }, [feature]);
  if (on === null) return <Loading label="Loading…" />;
  return on ? <>{children}</> : <Navigate to={fallback} replace />;
}
