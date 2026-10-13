// Native bridge to the FairPlay Mac app (CONTRACT.md "Native bridge").
// In a normal browser every call is a no-op and isNative() is false.

import type { IntegrityEvent } from "../api/types";

export type NativeMessage =
  | { type: "state"; locked: boolean; trusted: boolean; version: string }
  | { type: "locked" }
  | { type: "lockFailed"; reason: string }
  | { type: "unlocked" }
  /** Audit 2026-10-12: the app unlocks only once the server confirms the hand-in. */
  | { type: "unlockPending"; reason: string }
  | { type: "event"; event: IntegrityEvent }
  | { type: "attestation"; nonce: string; device_id: string; signature: string; version: string; build: string }
  | { type: "attestationFailed"; nonce: string; reason: string }
  | { type: "preflight"; problems: string[] };

/** The attempt the app is locking for: it sends its own signed beats with these. */
export interface ExamSessionParams {
  attempt_id: string;
  token: string;
  session: string;
}

/** Signed proof for POST start (CONTRACT.md "App session"). */
export interface ClientProof {
  device_id: string;
  nonce: string;
  signature: string;
  version: string;
  build: string;
}

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
  /** App session of the attempt (X-FairPlay-Session); "" for an attempt still offline. */
  session: string;
}

type OutMessage =
  | ({ action: "lock"; offline?: boolean } & Partial<ExamSessionParams>)
  | ({ action: "session" } & ExamSessionParams)
  | { action: "attest"; nonce: string }
  | { action: "unlock" }
  | { action: "hello" }
  | { action: "preflight" }
  | ({ action: "openExternal" } & OpenExternalParams);

interface ProctoraHandler {
  postMessage(msg: OutMessage): void;
}

/** WebView2 (Windows app) transport: window.chrome.webview.postMessage(json). */
interface WebView2 {
  postMessage(msg: unknown): void;
  addEventListener(type: "message", listener: (e: unknown) => void): void;
}

declare global {
  interface Window {
    // macOS app (WKWebView).
    webkit?: { messageHandlers?: { proctora?: ProctoraHandler } };
    // Windows app (WebView2). Named chrome.webview by the runtime.
    chrome?: { webview?: WebView2 };
  }
}

function macHandler(): ProctoraHandler | undefined {
  return typeof window !== "undefined" ? window.webkit?.messageHandlers?.proctora : undefined;
}

function winHandler(): WebView2 | undefined {
  return typeof window !== "undefined" ? window.chrome?.webview : undefined;
}

export function isNative(): boolean {
  return !!macHandler() || !!winHandler();
}

/** Which native host we're inside, for platform-specific copy/download links. */
export function nativePlatform(): "mac" | "windows" | null {
  if (macHandler()) return "mac";
  if (winHandler()) return "windows";
  return null;
}

function post(msg: OutMessage): void {
  // Both hosts receive the same message shape; only the transport differs. The Windows
  // shell dispatches replies through the identical 'proctora-native' CustomEvent, so the
  // receive side below is shared.
  const mac = macHandler();
  if (mac) {
    try {
      mac.postMessage(msg);
    } catch {
      /* ignore */
    }
    return;
  }
  const win = winHandler();
  if (win) {
    try {
      win.postMessage(msg);
    } catch {
      /* ignore */
    }
  }
}

// Audit W-10: the native host may put a per-launch nonce on the page before any page
// script runs (window.__fairplayNonce) and stamp every 'proctora-native' event with it as `bridge_nonce` (not `nonce`: attestation
// messages already carry the server challenge in `nonce`).
// Taken once here and removed from window, so later page scripts can't read it; when a host
// set one, events without it (dispatched by page JS) are ignored. Hosts that don't set one
// (older builds) work as before.
let expectedNonce: string | null = null;
if (typeof window !== "undefined") {
  const w = window as unknown as { __fairplayNonce?: unknown };
  if (typeof w.__fairplayNonce === "string" && w.__fairplayNonce.length >= 16) expectedNonce = w.__fairplayNonce;
  try {
    delete w.__fairplayNonce;
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
  const detail = (e as CustomEvent).detail as (NativeMessage & { bridge_nonce?: unknown }) | undefined;
  if (!detail || typeof detail !== "object" || typeof detail.type !== "string") return;
  if (expectedNonce !== null && detail.bridge_nonce !== expectedNonce) return;   // audit W-10
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
    session: params.session,
  });
  return true;
}

/**
 * Tell the app which attempt it is locked for, so it sends its own signed beats and
 * checks with the server before unlocking. Call again when the session changes.
 */
export function setExamSession(params: ExamSessionParams): void {
  if (!params.session) return;
  post({ action: "session", attempt_id: params.attempt_id, token: params.token, session: params.session });
}

/**
 * Ask the app to sign a start challenge. Resolves null outside the app or when the app
 * cannot sign (old build): the server then refuses the start.
 */
export function attest(nonce: string, timeoutMs = 5000): Promise<ClientProof | null> {
  install();
  if (!isNative()) return Promise.resolve(null);
  return new Promise((resolve) => {
    let done = false;
    const finish = (r: ClientProof | null) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      off();
      resolve(r);
    };
    const off = onNative((msg) => {
      if (msg.type === "attestation" && msg.nonce === nonce)
        finish({
          device_id: String(msg.device_id),
          nonce,
          signature: String(msg.signature),
          version: String(msg.version ?? ""),
          build: String(msg.build ?? ""),
        });
      else if (msg.type === "attestationFailed" && msg.nonce === nonce) finish(null);
    });
    const timer = setTimeout(() => finish(null), timeoutMs);
    post({ action: "attest", nonce });
  });
}

export type LockResult = { ok: true } | { ok: false; reason: string };

/**
 * Ask the Mac app to lock the computer and wait for "locked" or "lockFailed".
 * Outside the Mac app resolves immediately with ok: there is nothing to lock, and the
 * server only lets a browser get this far when FAIRPLAY_REQUIRE_APP=0 (demo mode); real
 * exams refuse browser starts and only trust the app's own signed beats (audit #1, #2).
 */
export function requestLock(timeoutMs = 10000, exam?: Partial<ExamSessionParams>): Promise<LockResult> {
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
    // Audit M-06: the app locks only for a valid exam session, or when the page says
    // this is an offline start (no server session yet; the app then can't unlock by itself
    // until the attempt reaches the server).
    post(exam && exam.session ? { action: "lock", ...exam } : { action: "lock", offline: true });
  });
}

/**
 * Audit M-11: ask the app which problems must be fixed BEFORE locking (Remote Login,
 * extra display, VM, Accessibility off...). [] = ready. Outside the app, or an app build
 * that doesn't know "preflight", resolves [] after the timeout (the lock's own checks
 * still run).
 */
export function preflight(timeoutMs = 3000): Promise<string[]> {
  install();
  if (!isNative()) return Promise.resolve([]);
  return new Promise((resolve) => {
    let done = false;
    const finish = (p: string[]) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      off();
      resolve(p);
    };
    const off = onNative((msg) => {
      if (msg.type === "preflight") finish(Array.isArray(msg.problems) ? msg.problems.map(String) : []);
    });
    const timer = setTimeout(() => finish([]), timeoutMs);
    post({ action: "preflight" });
  });
}

/** Test helper: pretend the host set this nonce at load (null = none). */
export function _setNonceForTests(nonce: string | null): void {
  expectedNonce = nonce;
}

/** Test helper: reset module state. */
export function _resetBridgeForTests(): void {
  locked = false;
  nativeState = null;
  listeners.clear();
}
