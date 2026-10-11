import type { ReactNode } from "react";
import type { ExamStatus } from "../api/types";

const LABEL: Record<ExamStatus, string> = { draft: "Draft", live: "Live", closed: "Closed" };

export function StatusChip({ status }: { status: ExamStatus }) {
  return <span className={`chip chip-${status}`}>{LABEL[status]}</span>;
}

export function Chip({ tone, children }: { tone: "success" | "warning" | "error" | "info" | "neutral"; children: ReactNode }) {
  return <span className={`chip chip-${tone}`}>{children}</span>;
}
