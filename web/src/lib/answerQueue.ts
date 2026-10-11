// Offline-first answer store (CONTRACT.md "Student UI behaviour").
//
// - Every change is written to localStorage["proctora:attempt:<aid>"] = {answers, client_seq}
//   with client_seq incremented.
// - Changes are synced with PUT /answers, debounced 1 s.
// - While unsynced, a failed save is retried every 3 s.
// - A 409 (time up / already submitted) stops syncing and reports "closed".

import { ApiError } from "../api/client";
import type { AnswerMap, AnswerValue, SaveAnswersResponse } from "../api/types";

export type SyncStatus = "synced" | "pending" | "closed";

export interface StoredAttempt {
  answers: AnswerMap;
  client_seq: number;
}

export type PutAnswers = (answers: AnswerMap, clientSeq: number) => Promise<SaveAnswersResponse>;

export interface AnswerQueueOptions {
  debounceMs?: number;
  retryMs?: number;
  storage?: Storage;
  onClosed?: (err: ApiError) => void;
}

export const attemptStorageKey = (aid: string) => `proctora:attempt:${aid}`;

export function readStoredAttempt(aid: string, storage: Storage = localStorage): StoredAttempt | null {
  try {
    const raw = storage.getItem(attemptStorageKey(aid));
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<StoredAttempt>;
    if (!v || typeof v !== "object" || typeof v.client_seq !== "number" || typeof v.answers !== "object" || !v.answers) {
      return null;
    }
    return { answers: v.answers as AnswerMap, client_seq: v.client_seq };
  } catch {
    return null;
  }
}

function sameAnswers(a: AnswerMap, b: AnswerMap): boolean {
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && a[k] === b[k]);
}

export class AnswerQueue {
  readonly aid: string;
  private put: PutAnswers;
  private debounceMs: number;
  private retryMs: number;
  private storage: Storage;
  private onClosed?: (err: ApiError) => void;

  private answers: AnswerMap = {};
  private clientSeq = 0;
  private syncedSeq = 0;
  private closed = false;
  private inFlight = false;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<(s: SyncStatus) => void>();
  private lastStatus: SyncStatus = "synced";

  constructor(aid: string, put: PutAnswers, opts: AnswerQueueOptions = {}) {
    this.aid = aid;
    this.put = put;
    this.debounceMs = opts.debounceMs ?? 1000;
    this.retryMs = opts.retryMs ?? 3000;
    this.storage = opts.storage ?? localStorage;
    this.onClosed = opts.onClosed;
  }

  /**
   * Load state: local copy wins (it is at least as new as anything this computer sent),
   * otherwise start from the server's answers.
   */
  init(serverAnswers: AnswerMap): void {
    const local = readStoredAttempt(this.aid, this.storage);
    if (local) {
      this.answers = { ...local.answers };
      this.clientSeq = local.client_seq;
      if (sameAnswers(local.answers, serverAnswers)) {
        this.syncedSeq = this.clientSeq;
      } else {
        this.syncedSeq = 0;
        this.scheduleSend(0);
      }
    } else {
      this.answers = { ...serverAnswers };
      this.clientSeq = 0;
      this.syncedSeq = 0;
    }
    this.emit();
  }

  getAnswers(): AnswerMap {
    return { ...this.answers };
  }

  getClientSeq(): number {
    return this.clientSeq;
  }

  status(): SyncStatus {
    if (this.closed) return "closed";
    return this.syncedSeq >= this.clientSeq ? "synced" : "pending";
  }

  subscribe(fn: (s: SyncStatus) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Set (or clear with undefined) one answer. Persists immediately, syncs debounced. */
  set(questionId: string, value: AnswerValue | undefined): void {
    if (this.closed) return;
    if (value === undefined || value === "") delete this.answers[questionId];
    else this.answers[questionId] = value;
    this.clientSeq += 1;
    this.persist();
    this.emit();
    this.scheduleSend(this.debounceMs);
  }

  /** Send now (e.g. before submit). Resolves when this attempt finishes, success or not. */
  async flush(): Promise<void> {
    this.clearTimers();
    await this.send();
  }

  /** Stop all timers (on unmount or after submit). */
  stop(): void {
    this.clearTimers();
    this.closed = true;
  }

  /** Remove the local copy (after a successful submit). */
  clearStorage(): void {
    try {
      this.storage.removeItem(attemptStorageKey(this.aid));
    } catch {
      /* ignore */
    }
  }

  // ---- internals ----

  private persist(): void {
    try {
      const v: StoredAttempt = { answers: this.answers, client_seq: this.clientSeq };
      this.storage.setItem(attemptStorageKey(this.aid), JSON.stringify(v));
    } catch {
      /* storage full or unavailable: server sync still runs */
    }
  }

  private emit(): void {
    const s = this.status();
    this.lastStatus = s;
    this.listeners.forEach((l) => l(s));
  }

  private clearTimers(): void {
    if (this.debounceTimer) clearTimeout(this.debounceTimer);
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.debounceTimer = null;
    this.retryTimer = null;
  }

  private scheduleSend(ms: number): void {
    if (this.closed) return;
    if (this.debounceTimer) clearTimeout(this.debounceTimer);
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null;
      void this.send();
    }, ms);
  }

  private scheduleRetry(): void {
    if (this.closed || this.retryTimer) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.send();
    }, this.retryMs);
  }

  private async send(): Promise<void> {
    if (this.closed || this.inFlight) return;
    if (this.syncedSeq >= this.clientSeq) {
      this.emit();
      return;
    }
    const seq = this.clientSeq;
    const snapshot = { ...this.answers };
    this.inFlight = true;
    let retry = false;
    let bumped = false;
    try {
      const res = await this.put(snapshot, seq);
      if (res.saved || res.client_seq === seq) {
        // saved, or an earlier copy of this exact save already landed
        this.syncedSeq = Math.max(this.syncedSeq, seq);
      } else if (res.client_seq > seq) {
        // Server holds a newer seq (e.g. another computer). Jump past it and resend.
        if (this.clientSeq <= res.client_seq) {
          this.clientSeq = res.client_seq + 1;
          this.persist();
        }
        bumped = true;
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        this.closed = true;
        this.clearTimers();
        this.inFlight = false;
        this.emit();
        this.onClosed?.(e);
        return;
      }
      if (e instanceof ApiError && e.status === 401) {
        // redirect to login is handled by the client
        this.inFlight = false;
        return;
      }
      retry = true; // network error or server error: retry loop
    }
    this.inFlight = false;
    this.emit();
    if (this.syncedSeq < this.clientSeq) {
      if (bumped) this.scheduleSend(0); // resend at once with the new seq
      else if (retry) this.scheduleRetry();
      else if (!this.debounceTimer) this.scheduleRetry(); // changed while in flight, debounce not pending
    }
  }

  /** For tests / debugging. */
  get lastEmitted(): SyncStatus {
    return this.lastStatus;
  }
}
