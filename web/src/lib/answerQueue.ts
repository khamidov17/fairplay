// Offline-first answer store (CONTRACT.md "Student UI behaviour").
//
// - Every change is written to localStorage["proctora:attempt:<aid>"] = {answers, client_seq}
//   with client_seq incremented.
// - Changes are synced with PUT /answers, debounced 1 s.
// - While unsynced, a failed save is retried after 3 s, then with exponential backoff
//   (x2 per consecutive failure, capped at 30 s) and +-20 % jitter, so a room of laptops
//   that lost Wi-Fi together does not retry in lockstep. nudge() (browser "online" event,
//   a heartbeat that got through) retries at once, so the backoff never delays a reconnect.
// - A save that gets no answer within timeoutMs counts as a network failure: a half-open
//   connection after a Wi-Fi switch can hang forever and would otherwise block syncing.
// - A 409 (time up / already submitted) stops syncing and reports "closed".

import { ApiError, NetworkError } from "../api/client";
import type { AnswerMap, AnswerValue, SaveAnswersResponse } from "../api/types";

export type SyncStatus = "synced" | "pending" | "closed";

export interface StoredAttempt {
  answers: AnswerMap;
  client_seq: number;
}

export type PutAnswers = (answers: AnswerMap, clientSeq: number) => Promise<SaveAnswersResponse>;

export interface AnswerQueueOptions {
  debounceMs?: number;
  /** first retry delay; doubles per consecutive failure up to maxRetryMs */
  retryMs?: number;
  maxRetryMs?: number;
  /** jitter fraction (0.2 = +-20 %); 0 disables */
  jitter?: number;
  /** a save without an answer after this long counts as a network failure; 0 disables */
  timeoutMs?: number;
  /** injectable for tests; returns [0, 1) */
  random?: () => number;
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
  private maxRetryMs: number;
  private jitter: number;
  private timeoutMs: number;
  private random: () => number;
  private storage: Storage;
  private onClosed?: (err: ApiError) => void;

  private answers: AnswerMap = {};
  private clientSeq = 0;
  private syncedSeq = 0;
  private closed = false;
  private inFlight = false;
  private failures = 0;
  private sendGen = 0;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<(s: SyncStatus) => void>();
  private lastStatus: SyncStatus = "synced";

  constructor(aid: string, put: PutAnswers, opts: AnswerQueueOptions = {}) {
    this.aid = aid;
    this.put = put;
    this.debounceMs = opts.debounceMs ?? 1000;
    this.retryMs = opts.retryMs ?? 3000;
    this.maxRetryMs = opts.maxRetryMs ?? 30_000;
    this.jitter = opts.jitter ?? 0.2;
    this.timeoutMs = opts.timeoutMs ?? 20_000;
    this.random = opts.random ?? Math.random;
    this.storage = opts.storage ?? localStorage;
    this.onClosed = opts.onClosed;
  }

  /**
   * Load state. The local copy wins (it is at least as new as anything this computer
   * sent) unless the server holds a newer client_seq: then another computer or tab saved
   * after it (the student moved to a spare laptop and this is the old one coming back),
   * and the server's answers win. Without a local copy, start from the server's answers
   * AND its client_seq, so the next save and the submit are newer than what the server
   * holds (starting from 0 would get them silently ignored).
   */
  init(serverAnswers: AnswerMap, serverSeq = -1): void {
    const local = readStoredAttempt(this.aid, this.storage);
    if (local && local.client_seq < serverSeq) {
      this.answers = { ...serverAnswers };
      this.clientSeq = serverSeq;
      this.syncedSeq = serverSeq;
      this.persist();
    } else if (local) {
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
      this.clientSeq = Math.max(0, serverSeq);
      this.syncedSeq = this.clientSeq;
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

  /**
   * The network is probably back (browser "online" event, a heartbeat got through): if
   * answers are waiting for a retry, send them now instead of after the backoff.
   */
  nudge(): void {
    if (this.closed || this.inFlight || this.syncedSeq >= this.clientSeq) return;
    if (!this.retryTimer) return; // nothing failed: the debounce timer is in charge
    this.clearTimers();
    void this.send();
  }

  /** Delay before the next retry after `failures` (>= 1) consecutive failures. */
  retryDelay(failures: number): number {
    const base = Math.min(this.maxRetryMs, this.retryMs * 2 ** Math.max(0, failures - 1));
    if (!this.jitter) return base;
    return Math.round(base * (1 - this.jitter + 2 * this.jitter * this.random()));
  }

  /** Stop all timers (on unmount or after submit). */
  stop(): void {
    this.clearTimers();
    this.closed = true;
    this.sendGen += 1; // ignore the answer of a save still in flight
    this.inFlight = false;
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
    }, this.retryDelay(Math.max(1, this.failures)));
  }

  private withTimeout(p: Promise<SaveAnswersResponse>): Promise<SaveAnswersResponse> {
    if (!this.timeoutMs) return p;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new NetworkError("Saving timed out")), this.timeoutMs);
      p.then(
        (v) => {
          clearTimeout(t);
          resolve(v);
        },
        (e: unknown) => {
          clearTimeout(t);
          reject(e);
        },
      );
    });
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
    const gen = ++this.sendGen;
    let retry = false;
    let bumped = false;
    try {
      const res = await this.withTimeout(this.put(snapshot, seq));
      if (gen !== this.sendGen) return; // stopped meanwhile
      this.failures = 0;
      if (res.saved) {
        // saved (an exact repeat of a save that already landed is also reported saved)
        this.syncedSeq = Math.max(this.syncedSeq, seq);
      } else if (res.client_seq >= seq) {
        // The server holds this seq with DIFFERENT answers, or a newer seq (another tab
        // or computer). Jump past it and resend this computer's answers.
        if (this.clientSeq <= res.client_seq) {
          this.clientSeq = res.client_seq + 1;
          this.persist();
        }
        bumped = true;
      }
    } catch (e) {
      if (gen !== this.sendGen) return;
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
      retry = true; // network error, timeout or server error: retry with backoff
      this.failures += 1;
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
