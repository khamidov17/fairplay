// Slice 6 A: offline exam packages and offline start (CONTRACT.md "A. Offline exam packages").
//
// - The check-in page downloads GET /api/student/exams/{id}/package and keeps it in
//   localStorage["fairplay:pkg:<exam_id>"].
// - If the network is down when the student presses Start, the typed start code decrypts the
//   package (PBKDF2-SHA256(start_code + exam_id, salt, 100 000) -> AES-GCM). A wrong code fails
//   the GCM check. The exam then runs as a local attempt "offline:<exam_id>"; on reconnect the
//   exam page calls POST start with offline_started_at and syncs the answers.
// - Every exam page caches its last AttemptView in localStorage["fairplay:attempt-view:<aid>"]
//   and uses it when GET attempt fails with a network error.

import { NetworkError, slice6Api } from "../api/client";
import type { AttemptView, OfflineExam, OfflinePackage } from "../api/types";

export const PBKDF2_ITERATIONS = 100_000;
export const OFFLINE_PREFIX = "offline:";
export const packageKey = (examId: string) => `fairplay:pkg:${examId}`;
export const attemptViewKey = (aid: string) => `fairplay:attempt-view:${aid}`;
export const offlineRecordKey = (examId: string) => `fairplay:offline:${examId}`;

export class WrongCodeError extends Error {
  constructor() {
    super("Wrong start code");
    this.name = "WrongCodeError";
  }
}

export const offlineAttemptId = (examId: string) => `${OFFLINE_PREFIX}${examId}`;
export const isOfflineAttemptId = (aid: string) => aid.startsWith(OFFLINE_PREFIX);
export const examIdOfOffline = (aid: string) => aid.slice(OFFLINE_PREFIX.length);

// ---------- base64 / crypto ----------

export function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function bytesToB64(bytes: Uint8Array): string {
  let s = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) s += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  return btoa(s);
}

export function defaultSubtle(): SubtleCrypto | null {
  const c = (globalThis as { crypto?: Crypto }).crypto;
  return c?.subtle ?? null;
}

export async function deriveKey(
  startCode: string,
  examId: string,
  salt: Uint8Array,
  subtle: SubtleCrypto,
  iterations = PBKDF2_ITERATIONS,
): Promise<CryptoKey> {
  const base = await subtle.importKey("raw", new TextEncoder().encode(startCode + examId), "PBKDF2", false, ["deriveKey"]);
  return subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

/** Decrypt a package with the typed start code. Throws WrongCodeError if the code is wrong. */
export async function decryptPackage(
  pkg: OfflinePackage,
  startCode: string,
  subtle: SubtleCrypto | null = defaultSubtle(),
  iterations = PBKDF2_ITERATIONS,
): Promise<OfflineExam> {
  if (!subtle) throw new Error("This browser cannot open offline exams");
  const key = await deriveKey(startCode.trim(), pkg.exam_id, b64ToBytes(pkg.salt), subtle, iterations);
  let plain: ArrayBuffer;
  try {
    plain = await subtle.decrypt({ name: "AES-GCM", iv: b64ToBytes(pkg.iv) }, key, b64ToBytes(pkg.ciphertext));
  } catch {
    throw new WrongCodeError();
  }
  return JSON.parse(new TextDecoder().decode(plain)) as OfflineExam;
}

/** Same format as the server (used by tests and tools). */
export async function encryptPackage(
  plain: unknown,
  startCode: string,
  examId: string,
  subtle: SubtleCrypto,
  iterations = PBKDF2_ITERATIONS,
  random: (a: Uint8Array) => Uint8Array = (a) => (globalThis.crypto.getRandomValues(a), a),
): Promise<OfflinePackage> {
  const salt = random(new Uint8Array(16));
  const iv = random(new Uint8Array(12));
  const key = await deriveKey(startCode, examId, salt, subtle, iterations);
  const ct = await subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(JSON.stringify(plain)));
  return {
    exam_id: examId,
    version: 1,
    alg: "AES-GCM",
    salt: bytesToB64(salt),
    iv: bytesToB64(iv),
    ciphertext: bytesToB64(new Uint8Array(ct)),
  };
}

// ---------- storage ----------

function readJson<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeJson(key: string, v: unknown): boolean {
  try {
    localStorage.setItem(key, JSON.stringify(v));
    return true;
  } catch {
    return false; // full (big images) or unavailable
  }
}

export function storedPackage(examId: string): OfflinePackage | null {
  const p = readJson<OfflinePackage>(packageKey(examId));
  return p && p.exam_id === examId && typeof p.ciphertext === "string" ? p : null;
}

/** Download the package for later (check-in page). Never throws; returns true when stored. */
export async function downloadPackage(examId: string): Promise<boolean> {
  try {
    const pkg = await slice6Api.examPackage(examId);
    return writeJson(packageKey(examId), pkg);
  } catch {
    return false;
  }
}

export interface CachedView {
  view: AttemptView;
  /** server clock offset (ms) measured when the view was cached */
  offset_ms: number;
}

export function cacheAttemptView(view: AttemptView, offsetMs: number): void {
  writeJson(attemptViewKey(view.attempt_id), { view, offset_ms: offsetMs } satisfies CachedView);
}

/** Cached view with server_time re-based to "now" so the countdown stays right. */
export function cachedAttemptView(aid: string, now = Date.now()): AttemptView | null {
  const c = readJson<CachedView>(attemptViewKey(aid));
  if (!c || !c.view || c.view.attempt_id !== aid) return null;
  return { ...c.view, server_time: new Date(now + (Number(c.offset_ms) || 0)).toISOString() };
}

export function clearAttemptView(aid: string): void {
  try {
    localStorage.removeItem(attemptViewKey(aid));
  } catch {
    /* ignore */
  }
}

// ---------- offline attempts ----------

export interface OfflineRecord {
  exam_id: string;
  start_code: string;
  started_at: string;
  view: AttemptView;
  /** set once the server accepted the attempt */
  real_attempt_id?: string | null;
}

export function readOfflineRecord(examId: string): OfflineRecord | null {
  const r = readJson<OfflineRecord>(offlineRecordKey(examId));
  return r && r.exam_id === examId && r.view ? r : null;
}

export function saveOfflineRecord(r: OfflineRecord): void {
  writeJson(offlineRecordKey(r.exam_id), r);
}

export function clearOfflineRecord(examId: string): void {
  try {
    localStorage.removeItem(offlineRecordKey(examId));
  } catch {
    /* ignore */
  }
}

/** Build the local attempt from a decrypted package. */
export function offlineAttemptView(exam: OfflineExam, now = Date.now()): AttemptView {
  return {
    attempt_id: offlineAttemptId(exam.exam_id),
    exam_id: exam.exam_id,
    title: exam.title,
    course: exam.course,
    duration_min: exam.duration_min,
    started_at: new Date(now).toISOString(),
    deadline_at: new Date(now + exam.duration_min * 60_000).toISOString(),
    server_time: new Date(now).toISOString(),
    questions: exam.questions,
    answers: {},
    status: "in_progress",
    mode: "questions",
    external_url: null,
    allowed_domains: [],
    finish_url_contains: null,
  };
}

/**
 * Network is down at start: open the stored package with the typed code and create the local
 * attempt. Resumes an existing local attempt for the same exam. Throws WrongCodeError, or
 * NetworkError when there is no package on this computer.
 */
export async function startOffline(
  examId: string,
  startCode: string,
  subtle: SubtleCrypto | null = defaultSubtle(),
  iterations = PBKDF2_ITERATIONS,
): Promise<AttemptView> {
  const pkg = storedPackage(examId);
  if (!pkg) throw new NetworkError();
  const exam = await decryptPackage(pkg, startCode, subtle, iterations);
  const existing = readOfflineRecord(examId);
  if (existing && existing.start_code === startCode.trim()) return existing.view;
  const view = offlineAttemptView(exam);
  saveOfflineRecord({ exam_id: examId, start_code: startCode.trim(), started_at: view.started_at, view });
  return view;
}
