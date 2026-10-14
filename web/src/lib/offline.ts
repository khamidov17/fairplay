// Slice 6 A: offline exam packages and offline start (CONTRACT.md "A. Offline exam packages").
//
// - The check-in page downloads GET /api/student/exams/{id}/package and keeps it in
//   localStorage["fairplay:pkg:<exam_id>"].
// - If the network is down when the student presses Start, the typed start code decrypts the
//   package (PBKDF2-SHA256(start_code + exam_id + package key, salt, the package's
//   "iterations": 600 000 since version 3, 100 000 before) -> AES-GCM). A wrong code fails
//   the GCM check. The exam then runs as a local attempt "offline:<exam_id>"; on reconnect the
//   exam page calls POST start with offline_started_at and syncs the answers.
// - Every exam page caches its last AttemptView in localStorage["fairplay:attempt-view:<aid>"]
//   and uses it when GET attempt fails with a network error.

import { NetworkError, slice6Api } from "../api/client";
import type { AttemptView, OfflineExam, OfflinePackage } from "../api/types";
import { ATTEMPT_PREFIX } from "./answerQueue";

/** Audit 2026-10-13: package version 3 (the count is also written in the package). */
export const PBKDF2_ITERATIONS = 600_000;
/** Packages without an "iterations" field (versions 1 and 2). */
export const LEGACY_ITERATIONS = 100_000;
const MIN_ITERATIONS = 1_000;
const MAX_ITERATIONS = 10_000_000;
export const OFFLINE_PREFIX = "offline:";
export const packageKey = (examId: string) => `fairplay:pkg:${examId}`;
export const attemptViewKey = (aid: string) => `fairplay:attempt-view:${aid}`;
export const offlineRecordKey = (examId: string) => `fairplay:offline:${examId}`;
/** Audit S-03: the live-only half of a version-2 package key. */
export const packageKeyKey = (examId: string) => `fairplay:pkgkey:${examId}`;

/** A version-2 package without its key: the student was never online after the exam opened. */
export class PackageLockedError extends Error {
  constructor() {
    super("This exam's offline copy is still locked on this computer");
    this.name = "PackageLockedError";
  }
}

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
  packageKey = "",
): Promise<CryptoKey> {
  const base = await subtle.importKey(
    "raw",
    new TextEncoder().encode(startCode + examId + packageKey),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
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
  iterations?: number,
  packageKey = "",
): Promise<OfflineExam> {
  if (!subtle) throw new Error("This browser cannot open offline exams");
  if ((pkg.version ?? 1) >= 2 && !packageKey) throw new PackageLockedError();
  // The count comes from the package unless the caller says otherwise (tests).
  iterations ??= typeof pkg.iterations === "number" ? pkg.iterations : LEGACY_ITERATIONS;
  if (!Number.isInteger(iterations) || iterations < MIN_ITERATIONS || iterations > MAX_ITERATIONS) {
    throw new WrongCodeError();   // a damaged copy: never hang the page on a huge count
  }
  const key = await deriveKey(startCode.trim(), pkg.exam_id, b64ToBytes(pkg.salt), subtle, iterations, packageKey);
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
  packageKey = "",
): Promise<OfflinePackage> {
  const salt = random(new Uint8Array(16));
  const iv = random(new Uint8Array(12));
  const key = await deriveKey(startCode, examId, salt, subtle, iterations, packageKey);
  const ct = await subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(JSON.stringify(plain)));
  return {
    exam_id: examId,
    version: packageKey ? 3 : 1,
    alg: "AES-GCM",
    iterations,
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

export function storedPackage(examId: string): (OfflinePackage & { saved_at?: number }) | null {
  const p = readJson<OfflinePackage & { saved_at?: number }>(packageKey(examId));
  return p && p.exam_id === examId && typeof p.ciphertext === "string" ? p : null;
}

/** Download the package for later (check-in page). Never throws; returns true when stored.
 *  Also tries to get the package key (only given once the exam is live). */
export async function downloadPackage(examId: string): Promise<boolean> {
  try {
    const pkg = await slice6Api.examPackage(examId);
    const ok = writeJson(packageKey(examId), { ...pkg, saved_at: Date.now() });
    if (ok) await fetchPackageKey(examId);
    return ok;
  } catch {
    return false;
  }
}

export function storedPackageKey(examId: string): string {
  try {
    return localStorage.getItem(packageKeyKey(examId)) ?? "";
  } catch {
    return "";
  }
}

/** Audit S-03: fetch and keep the live-only package key. Never throws. */
export async function fetchPackageKey(examId: string): Promise<boolean> {
  try {
    const r = await slice6Api.packageKey(examId);
    if (!/^[0-9a-f]{64}$/.test(r.key)) return false;
    localStorage.setItem(packageKeyKey(examId), r.key);
    return true;
  } catch {
    return false;
  }
}

/** While online: for every live exam whose package is on this computer without its key,
 *  get the key now, so a network drop at start still allows an offline start. */
export async function unlockStoredPackages(liveExamIds: string[]): Promise<void> {
  for (const id of liveExamIds) {
    const pkg = storedPackage(id);
    if (pkg && (pkg.version ?? 1) >= 2 && !storedPackageKey(id)) await fetchPackageKey(id);
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
  /** Audit W-03: never written to storage any more (older records may still have it). */
  start_code?: string;
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
  const { start_code: _never, ...rest } = r;      // audit W-03: the code stays in memory only
  void _never;
  writeJson(offlineRecordKey(r.exam_id), rest);
}

// ---------- audit W-03: start codes live in memory only ----------

const rememberedCodes = new Map<string, string>();

export function rememberStartCode(examId: string, code: string): void {
  rememberedCodes.set(examId, code.trim());
}

/** The code typed on this page load, or (old records only) the stored one. */
export function rememberedStartCode(examId: string): string | null {
  return rememberedCodes.get(examId) ?? readOfflineRecord(examId)?.start_code ?? null;
}

export function forgetStartCode(examId: string): void {
  rememberedCodes.delete(examId);
}

/** No code in memory (the page was reloaded after an offline start): ask again. */
export class NeedStartCodeError extends Error {
  constructor(public readonly reason: "reload" | "changed") {
    super(reason === "changed" ? "The start code changed" : "Type the start code again");
    this.name = "NeedStartCodeError";
  }
}

// ---------- audit W-03: what exam data stays on this computer ----------

const EXAM_DATA_PREFIXES = [
  "fairplay:pkg:",
  "fairplay:pkgkey:",
  "fairplay:offline:",
  "fairplay:attempt-view:",
  "fairplay:session:",
  "proctora:marks:",
];
/** Packages fetched for an exam that never showed up: dropped after this long. */
export const PACKAGE_MAX_AGE_MS = 24 * 3600 * 1000;

function storageKeys(): string[] {
  try {
    const out: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k) out.push(k);
    }
    return out;
  } catch {
    return [];
  }
}

function remove(k: string): void {
  try {
    localStorage.removeItem(k);
  } catch {
    /* ignore */
  }
}

/** A local answer copy the server already has in full (audit 2026-10-13). */
function answerCopyIsSynced(k: string): boolean {
  try {
    const v = JSON.parse(localStorage.getItem(k) ?? "null") as { client_seq?: unknown; synced?: unknown } | null;
    return !!v && typeof v.client_seq === "number" && typeof v.synced === "number" && v.synced >= v.client_seq;
  } catch {
    return false;
  }
}

/** Sign-out: exam content, packages, keys and sessions go. Answer copies
 *  ("proctora:attempt:...") go too when the server already has them; unsent ones stay
 *  (the student's own work, under their account: answerQueue.ts), except old copies of
 *  offline attempts without an owner, which the next student could otherwise pick up
 *  (audit 2026-10-13). */
export function clearExamDataOnSignOut(): void {
  for (const k of storageKeys()) {
    if (EXAM_DATA_PREFIXES.some((p) => k.startsWith(p))) remove(k);
    else if (k.startsWith(`${ATTEMPT_PREFIX}offline:`)) remove(k);
    else if (k.startsWith(ATTEMPT_PREFIX) && answerCopyIsSynced(k)) remove(k);
  }
  rememberedCodes.clear();
}

/** After each exam list from the server: drop data of handed-in attempts and of packages
 *  that are past their use (closed exams, or older than PACKAGE_MAX_AGE_MS). */
export function pruneExamData(
  list: { id: string; status: string; attempt?: { attempt_id: string; status: string } | null }[],
  now = Date.now(),
): void {
  const byExam = new Map(list.map((e) => [e.id, e]));
  const doneAttempts = new Set(list.filter((e) => e.attempt?.status === "submitted").map((e) => e.attempt!.attempt_id));
  for (const k of storageKeys()) {
    const pkg = /^fairplay:(pkg|pkgkey|offline):(.+)$/.exec(k);
    if (pkg && pkg[1] && pkg[2]) {
      const examId = pkg[2];
      const e = byExam.get(examId);
      const handedIn = e?.attempt?.status === "submitted";
      const closed = e ? e.status !== "live" && e.attempt?.status !== "in_progress" : false;
      const saved = pkg[1] === "pkg" ? storedPackage(examId)?.saved_at : undefined;
      const stale = !e && pkg[1] !== "offline" && (saved === undefined || now - saved > PACKAGE_MAX_AGE_MS);
      if (handedIn || closed || stale) remove(k);
      continue;
    }
    const att = /^(?:fairplay:attempt-view:|fairplay:session:|proctora:marks:|proctora:attempt:(?:u\d+:)?)(.+)$/.exec(k);
    if (att?.[1] && doneAttempts.has(att[1])) remove(k);
  }
}

/** Audit W-02: the server's package id changed (new start code): fetch it again. */
export async function refreshPackageIfChanged(examId: string, packageId: string | null | undefined): Promise<void> {
  if (!packageId) return;
  const pkg = storedPackage(examId);
  if (pkg && pkg.package_id !== packageId) await downloadPackage(examId);
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
    allowed_apps: exam.allowed_apps ?? [],
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
  iterations?: number,
): Promise<AttemptView> {
  const pkg = storedPackage(examId);
  if (!pkg) throw new NetworkError();
  const exam = await decryptPackage(pkg, startCode, subtle, iterations, storedPackageKey(examId));
  // The code opened the package, so it is right: resume the local attempt if there is one.
  rememberStartCode(examId, startCode);
  const existing = readOfflineRecord(examId);
  if (existing) return existing.view;
  const view = offlineAttemptView(exam);
  saveOfflineRecord({ exam_id: examId, started_at: view.started_at, view });
  return view;
}
