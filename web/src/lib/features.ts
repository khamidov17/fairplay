// 2026-10-13: optional parts of FairPlay (Scan & Grade, LMS sync, Practice) are off unless
// the server lists them in /api/health "features" (FAIRPLAY_FEATURES on the server).
// Insights is hidden in the UI for now (its student-profile API stays on).
import { useEffect, useState } from "react";

export type Feature = "scan" | "lms" | "practice" | "insights";

let cached: Promise<Set<Feature>> | null = null;

export function loadFeatures(): Promise<Set<Feature>> {
  if (!cached) {
    cached = fetch("/api/health", { headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null))
      .then((j: { features?: unknown } | null) => {
        const list = Array.isArray(j?.features) ? (j?.features as unknown[]) : [];
        return new Set(list.filter((f): f is Feature => typeof f === "string") as Feature[]);
      })
      .catch(() => new Set<Feature>());
  }
  return cached;
}

/** Test helper. */
export function _resetFeaturesForTests(): void {
  cached = null;
}

/** The switched-on optional features (empty until the server answers, or on error). */
export function useFeatures(): Set<Feature> {
  const [features, setFeatures] = useState<Set<Feature>>(() => new Set());
  useEffect(() => {
    let alive = true;
    void loadFeatures().then((f) => alive && setFeatures(f));
    return () => {
      alive = false;
    };
  }, []);
  return features;
}
