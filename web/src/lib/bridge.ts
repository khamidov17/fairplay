// Native bridge to the FairPlay Mac app (CONTRACT.md "Native bridge").
// In a normal browser every call is a no-op and isNative() is false.

import type { ExitCheck, IntegrityEvent } from "../api/types";

export type NativeMessage =
  | { type: "state"; locked: boolean; trusted: boolean; version: string }
  | { type: "locked" }
  | { type: "lockFailed"; reason: string }
  | { type: "unlocked" }
  | { type: "event"; event: IntegrityEvent };

/** Slice 3: open an external-website exam inside the lockdown (CONTRACT.md "Native bridge (added)"). */
export interface OpenExternalParams {
  attempt_id: string;
  url: string;
  allowed_domains: string[];
  finish_url_contains: string | null;
  deadline_at: string;
  server_time: string;
  title: string;
  /** Student bearer token: the app sends heartbeats/events itself while the web page is gone. */
  token: string;
  /** Phase 1a: offline verifier of the invigilator exit code. */
  exit_check?: ExitCheck | null;
}

/**
 * Phase 1a: what the Mac app needs to check the invigilator exit code (Ctrl-Opt-Cmd-E).
 * It asks the server first (POST /api/student/attempts/{attempt_id}/exit-code with the token)
 * and falls back to exit_check only when the server cannot be reached.
 * attempt_id is null while an offline-started attempt has not reached the server yet.
 */
export interface ExamSession {
  attempt_id: string | null;
  token: string | null;
  exit_check: ExitCheck | null;
}

type OutMessage =
  | { action: "lock" }
  | ({ action: "examSession" } & ExamSession)
  | { action: "unlock" }
  | { action: "hello" }
  | ({ action: "openExternal" } & OpenExternalParams);

interface ProctoraHandler {
  postMessage(msg: OutMessage): void;
}

declare global {
  interface Window {
    webkit?: { messageHandlers?: { proctora?: ProctoraHandler } };
  }
}

function handler(): ProctoraHandler | undefined {
  return typeof window !== "undefined" ? window.webkit?.messageHandlers?.proctora : undefined;
}

export function isNative(): boolean {
  return !!handler();
}

function post(msg: OutMessage): void {
  const h = handler();
  if (!h) return;
  try {
    h.postMessage(msg);
  } catch {
    /* ignore */
  }
}

// Last known lock state, kept up to date from native messages.
let locked = false;
// Last "state" message from native (null until the Mac app answers "hello").
let nativeState: { locked: boolean; trusted: boolean; version: string } | null = null;
const listeners = new Set<(msg: NativeMessage) => void>();

function onWindowEvent(e: Event) {
  const detail = (e as CustomEvent).detail as NativeMessage | undefined;
  if (!detail || typeof detail !== "object" || typeof detail.type !== "string") return;
  if (detail.type === "state") {
    locked = !!detail.locked;
    nativeState = { locked: !!detail.locked, trusted: !!detail.trusted, version: String(detail.version ?? "") };
  }
  else if (detail.type === "locked") locked = true;
  else if (detail.type === "unlocked" || detail.type === "lockFailed") locked = false;
  listeners.forEach((l) => l(detail));
}

let installed = false;
function install() {
  if (installed || typeof window === "undefined") return;
  installed = true;
  window.addEventListener("proctora-native", onWindowEvent);
}

/** Subscribe to native messages. Returns an unsubscribe function. */
export function onNative(listener: (msg: NativeMessage) => void): () => void {
  install();
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function isLocked(): boolean {
  return isNative() && locked;
}

/** Last state the Mac app reported (null outside the app or before it answered). */
export function getNativeState(): { locked: boolean; trusted: boolean; version: string } | null {
  return isNative() ? nativeState : null;
}

export function hello(): void {
  install();
  post({ action: "hello" });
}

export function unlock(): void {
  post({ action: "unlock" });
}

/**
 * Ask the Mac app to open an external-website exam (it must already be locked; the app
 * locks first if needed). Returns false outside the Mac app (nothing is sent).
 */
export function openExternal(params: OpenExternalParams): boolean {
  if (!isNative()) return false;
  post({
    action: "openExternal",
    attempt_id: params.attempt_id,
    url: params.url,
    allowed_domains: [...params.allowed_domains],
    finish_url_contains: params.finish_url_contains ?? null,
    deadline_at: params.deadline_at,
    server_time: params.server_time,
    title: params.title,
    token: params.token,
    exit_check: params.exit_check ?? null,
  });
  return true;
}

/** Tell the Mac app which attempt is being written (before locking, and again when it changes). */
export function setExamSession(session: ExamSession): void {
  post({
    action: "examSession",
    attempt_id: session.attempt_id,
    token: session.token,
    exit_check: session.exit_check ?? null,
  });
}

export type LockResult = { ok: true } | { ok: false; reason: string };

/**
 * Ask the Mac app to lock the computer and wait for "locked" or "lockFailed".
 * Outside the Mac app resolves immediately with ok (nothing to lock).
 */
export function requestLock(timeoutMs = 10000): Promise<LockResult> {
  install();
  if (!isNative()) return Promise.resolve({ ok: true });
  return new Promise((resolve) => {
    let done = false;
    const finish = (r: LockResult) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      off();
      resolve(r);
    };
    const off = onNative((msg) => {
      if (msg.type === "locked") finish({ ok: true });
      else if (msg.type === "lockFailed") finish({ ok: false, reason: msg.reason || "unknown" });
      else if (msg.type === "state" && msg.locked) finish({ ok: true });
    });
    const timer = setTimeout(() => finish({ ok: false, reason: "timeout" }), timeoutMs);
    post({ action: "lock" });
  });
}

/** Test helper: reset module state. */
export function _resetBridgeForTests(): void {
  locked = false;
  nativeState = null;
  listeners.clear();
}
