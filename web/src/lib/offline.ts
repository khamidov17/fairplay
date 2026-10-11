// Slice 6 A + phase 1a: offline exam packages and offline start (CONTRACT.md "A. Offline exam
// packages" and "Phase 1a — offline package security").
//
// - The check-in page downloads GET /api/student/exams/{id}/package (format 2: AES-GCM with a
//   random per-exam key K) and, once the exam is live, GET .../package-key (K wrapped with
//   PBKDF2-SHA256(start_code + exam_id, salt, iterations) -> AES-GCM). Both are kept in
//   localStorage ("fairplay:pkg:<exam_id>", "fairplay:pkgkey:<exam_id>"). The package alone
//   cannot be opened with any start code; it needs the key release, which the server hands out
//   only while the exam is open.
// - If the network is down when the student presses Start, the typed start code unwraps K
//   (a wrong code fails the GCM check) and K opens the package. The exam then runs as a local
//   attempt "offline:<exam_id>"; on reconnect the exam page calls POST start with
//   offline_started_at (the server checks the start code again) and syncs the answers.
// - Every exam page caches its last AttemptView in localStorage["fairplay:attempt-view:<aid>"]
//   and uses it when GET attempt fails with a network error.

import { NetworkError, securityApi, slice6Api } from "../api/client";
import type { AttemptView, OfflineExam, OfflineKeyRelease, OfflinePackage } from "../api/types";

export const PACKAGE_VERSION = 2;
export const PBKDF2_ITERATIONS = 100_000;
export const OFFLINE_PREFIX = "offline:";
export const packageKey = (examId: string) => `fairplay:pkg:${examId}`;
export const keyReleaseKey = (examId: string) => `fairplay:pkgkey:${examId}`;
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

const utf8 = (s: string) => new TextEncoder().encode(s);

/** Key-encryption key from the start code (the same derivation as the server). */
export async function deriveKek(
  startCode: string,
  examId: string,
  salt: Uint8Array,
  subtle: SubtleCrypto,
  iterations = PBKDF2_ITERATIONS,
): Promise<CryptoKey> {
  const base = await subtle.importKey("raw", utf8(startCode.trim() + examId), "PBKDF2", false, ["deriveKey"]);
  return subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

/** Unwrap the package key K with the typed start code. Throws WrongCodeError if the code is wrong. */
export async function unwrapKey(release: OfflineKeyRelease, startCode: string, subtle: SubtleCrypto): Promise<CryptoKey> {
  const kek = await deriveKek(startCode, release.exam_id, b64ToBytes(release.salt), subtle, release.iterations || PBKDF2_ITERATIONS);
  let raw: ArrayBuffer;
  try {
    raw = await subtle.decrypt(
      { name: "AES-GCM", iv: b64ToBytes(release.iv), additionalData: utf8(release.exam_id) },
      kek,
      b64ToBytes(release.wrapped_key),
    );
  } catch {
    throw new WrongCodeError();
  }
  return subtle.importKey("raw", raw, "AES-GCM", false, ["decrypt"]);
}

/**
 * Open a package with its key release and the typed start code.
 * Throws WrongCodeError if the code is wrong (or package and key do not belong together).
 */
export async function decryptPackage(
  pkg: OfflinePackage,
  release: OfflineKeyRelease,
  startCode: string,
  subtle: SubtleCrypto | null = defaultSubtle(),
): Promise<OfflineExam> {
  if (!subtle) throw new Error("This browser cannot open offline exams");
  if (release.exam_id !== pkg.exam_id) throw new WrongCodeError();
  const key = await unwrapKey(release, startCode, subtle);
  let plain: ArrayBuffer;
  try {
    plain = await subtle.decrypt(
      { name: "AES-GCM", iv: b64ToBytes(pkg.iv), additionalData: utf8(pkg.exam_id) },
      key,
      b64ToBytes(pkg.ciphertext),
    );
  } catch {
    throw new WrongCodeError(); // a key from another opening of the exam
  }
  return JSON.parse(new TextDecoder().decode(plain)) as OfflineExam;
}

/** Same format as the server: a fresh key K, the package and its key release (tests and tools). */
export async function encryptPackage(
  plain: unknown,
  startCode: string,
  examId: string,
  subtle: SubtleCrypto,
  iterations = PBKDF2_ITERATIONS,
  random: (a: Uint8Array) => Uint8Array = (a) => (globalThis.crypto.getRandomValues(a), a),
): Promise<{ pkg: OfflinePackage; release: OfflineKeyRelease }> {
  const k = random(new Uint8Array(32));
  const iv = random(new Uint8Array(12));
  const contentKey = await subtle.importKey("raw", k, "AES-GCM", false, ["encrypt"]);
  const ct = await subtle.encrypt({ name: "AES-GCM", iv, additionalData: utf8(examId) }, contentKey, utf8(JSON.stringify(plain)));
  const salt = random(new Uint8Array(16));
  const wiv = random(new Uint8Array(12));
  const kek = await deriveKek(startCode, examId, salt, subtle, iterations);
  const wrapped = await subtle.encrypt({ name: "AES-GCM", iv: wiv, additionalData: utf8(examId) }, kek, k);
  return {
    pkg: { exam_id: examId, version: PACKAGE_VERSION, alg: "AES-GCM", iv: bytesToB64(iv), ciphertext: bytesToB64(new Uint8Array(ct)) },
    release: {
      exam_id: examId,
      version: PACKAGE_VERSION,
      alg: "PBKDF2-SHA256/AES-GCM",
      iterations,
      salt: bytesToB64(salt),
      iv: bytesToB64(wiv),
      wrapped_key: bytesToB64(new Uint8Array(wrapped)),
    },
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
  // format 1 packages (start code only) are no longer accepted
  return p && p.exam_id === examId && p.version === PACKAGE_VERSION && typeof p.ciphertext === "string" ? p : null;
}

export function storedKeyRelease(examId: string): OfflineKeyRelease | null {
  const r = readJson<OfflineKeyRelease>(keyReleaseKey(examId));
  return r && r.exam_id === examId && typeof r.wrapped_key === "string" ? r : null;
}

/** Both halves are on this computer: an offline start is possible. */
export function canStartOffline(examId: string): boolean {
  return !!storedPackage(examId) && !!storedKeyRelease(examId);
}

/** Fetch the key release (served only while the exam is open). Never throws. */
export async function downloadKeyRelease(examId: string): Promise<boolean> {
  try {
    const rel = await securityApi.packageKey(examId);
    return writeJson(keyReleaseKey(examId), rel);
  } catch {
    return false;
  }
}

/**
 * Download the package for later (check-in page), then its key release when the exam is open.
 * Never throws; returns true when the package is stored.
 */
export async function downloadPackage(examId: string): Promise<boolean> {
  let stored: boolean;
  try {
    const pkg = await slice6Api.examPackage(examId);
    stored = writeJson(packageKey(examId), pkg);
  } catch {
    return false;
  }
  if (stored) await downloadKeyRelease(examId);
  return stored;
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
export function offlineAttemptView(exam: OfflineExam, now = Date.now(), release?: OfflineKeyRelease | null): AttemptView {
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
    exit_check: release?.exit_check ?? null,
  };
}

/**
 * Network is down at start: open the stored package with the typed code and create the local
 * attempt. Resumes an existing local attempt for the same exam. Throws WrongCodeError, or
 * NetworkError when the package or its key release is not on this computer.
 */
export async function startOffline(
  examId: string,
  startCode: string,
  subtle: SubtleCrypto | null = defaultSubtle(),
): Promise<AttemptView> {
  const pkg = storedPackage(examId);
  const release = storedKeyRelease(examId);
  if (!pkg || !release) throw new NetworkError();
  const exam = await decryptPackage(pkg, release, startCode, subtle);
  const existing = readOfflineRecord(examId);
  if (existing && existing.start_code === startCode.trim()) return existing.view;
  const view = offlineAttemptView(exam, Date.now(), release);
  saveOfflineRecord({ exam_id: examId, start_code: startCode.trim(), started_at: view.started_at, view });
  return view;
}
