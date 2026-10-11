import { useEffect, useState } from "react";
import * as bridge from "../../lib/bridge";
import { AlertIcon, CheckIcon, DotsIcon } from "./icons";

export interface Readiness {
  /** Running inside the FairPlay Mac app. */
  native: boolean;
  /** Accessibility (lock) permission: true / false, or null while unknown. */
  trusted: boolean | null;
}

/** What we can truly know about this computer: Mac app present + lock permission. */
export function useReadiness(): Readiness {
  const [state, setState] = useState(() => bridge.getNativeState());
  useEffect(() => {
    const off = bridge.onNative((msg) => {
      if (msg.type === "state") setState(bridge.getNativeState());
    });
    // Ask again: the answer to the app-level hello may have arrived before we listened.
    bridge.hello();
    return off;
  }, []);
  const native = bridge.isNative();
  return { native, trusted: native && state ? state.trusted : null };
}

type Tone = "ok" | "warn" | "wait";

function CheckRow({ tone, title, detail }: { tone: Tone; title: string; detail?: string }) {
  return (
    <li className={`stu-check${tone === "ok" ? " ok" : tone === "warn" ? " warn" : ""}`}>
      {tone === "ok" ? <CheckIcon /> : tone === "warn" ? <AlertIcon /> : <DotsIcon />}
      <span>
        <span className="sr-only">{tone === "ok" ? "Ready: " : tone === "warn" ? "Needs attention: " : "Checking: "}</span>
        {title}
        {detail && <span className="stu-check-detail">{detail}</span>}
      </span>
    </li>
  );
}

/** The checks list. Only shows checks we can actually verify from the web page. */
export function ReadinessChecks({ readiness }: { readiness: Readiness }) {
  const { native, trusted } = readiness;
  return (
    <ul className="stu-checks" aria-label="Laptop checks">
      {native ? (
        <CheckRow tone="ok" title="Running in the FairPlay Mac app" />
      ) : (
        <CheckRow
          tone="warn"
          title="Not running in the FairPlay Mac app"
          detail="You are in a normal browser. Use the FairPlay Mac app for real exams."
        />
      )}
      {native &&
        (trusted === true ? (
          <CheckRow tone="ok" title="Lock permission granted" />
        ) : trusted === false ? (
          <CheckRow
            tone="warn"
            title="Lock permission is off"
            detail="Open System Settings → Privacy & Security → Accessibility and turn on FairPlay."
          />
        ) : (
          <CheckRow tone="wait" title="Checking lock permission…" />
        ))}
    </ul>
  );
}

export function readinessOk(r: Readiness): boolean {
  return r.native && r.trusted === true;
}
