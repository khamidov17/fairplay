// Phase 1b: the answer queue never loses an answer.
//
// A fake server applies the real server rule (server/app/routes_student.py save_answers):
// a save is stored only if its client_seq is strictly greater than the stored one; an
// exact repeat is reported saved; anything else returns saved:false with the stored seq.
// The network between them can drop requests, drop responses, answer 5xx, or hang.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError, STUDENT_REQUEST_TIMEOUT_MS, request } from "../api/client";
import type { AnswerMap, SaveAnswersResponse } from "../api/types";
import { AnswerQueue, attemptStorageKey, readStoredAttempt } from "../lib/answerQueue";

type Fault = "ok" | "drop-request" | "drop-response" | "5xx" | "hang" | "hang-then-apply" | "409";

class FakeServer {
  answers: AnswerMap = {};
  seq = -1;
  calls: { seq: number; answers: AnswerMap; fault: Fault }[] = [];
  faults: Fault[] = []; // consumed per call; "ok" when empty
  defaultFault: Fault = "ok";

  private apply(answers: AnswerMap, seq: number): SaveAnswersResponse {
    if (seq > this.seq) {
      this.answers = { ...answers };
      this.seq = seq;
      return { saved: true, client_seq: seq, server_time: "" };
    }
    const dup = seq === this.seq && JSON.stringify(answers) === JSON.stringify(this.answers);
    return { saved: dup, client_seq: this.seq, server_time: "" };
  }

  put = (answers: AnswerMap, seq: number): Promise<SaveAnswersResponse> => {
    const fault = this.faults.length ? this.faults.shift()! : this.defaultFault;
    this.calls.push({ seq, answers: { ...answers }, fault });
    switch (fault) {
      case "ok":
        return Promise.resolve(this.apply(answers, seq));
      case "drop-request":
        return Promise.reject(new NetworkError());
      case "drop-response":
        this.apply(answers, seq);
        return Promise.reject(new NetworkError());
      case "5xx":
        return Promise.reject(new ApiError(503, "Service Unavailable"));
      case "409":
        return Promise.reject(new ApiError(409, "Time is up"));
      case "hang":
        return new Promise(() => {});
      case "hang-then-apply":
        // arrives at the server much later (after the client gave up), never answers
        setTimeout(() => this.apply(answers, seq), 60_000);
        return new Promise(() => {});
    }
  };
}

/** Small deterministic PRNG so the randomized tests are reproducible. */
function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

describe("AnswerQueue reliability", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  // ------------------------------------------------------------ backoff
  it("backs off exponentially up to the cap", () => {
    const q = new AnswerQueue("a1", vi.fn(), { jitter: 0 });
    expect([1, 2, 3, 4, 5, 6, 20].map((n) => q.retryDelay(n))).toEqual([
      3000, 6000, 12000, 24000, 30000, 30000, 30000,
    ]);
  });

  it("adds +-20 % jitter so a room does not retry in lockstep", () => {
    const lo = new AnswerQueue("a1", vi.fn(), { random: () => 0 });
    const hi = new AnswerQueue("a1", vi.fn(), { random: () => 0.999999 });
    expect(lo.retryDelay(1)).toBe(2400);
    expect(hi.retryDelay(1)).toBe(3600);
    expect(lo.retryDelay(9)).toBe(24000);
    expect(hi.retryDelay(9)).toBe(36000);
    const r = rng(1);
    const delays = new Set(Array.from({ length: 50 }, () => new AnswerQueue("x", vi.fn(), { random: r }).retryDelay(1)));
    expect(delays.size).toBeGreaterThan(30);
  });

  it("retries 5xx with backoff, resets after a success, and loses nothing", async () => {
    const srv = new FakeServer();
    srv.faults = ["5xx", "5xx", "5xx"];
    const q = new AnswerQueue("a1", srv.put, { jitter: 0 });
    q.init({});
    q.set("q1", 1);
    await vi.advanceTimersByTimeAsync(1000); // send 1 fails
    await vi.advanceTimersByTimeAsync(3000); // retry 1 fails
    await vi.advanceTimersByTimeAsync(6000); // retry 2 fails
    expect(srv.calls).toHaveLength(3);
    expect(q.status()).toBe("pending");
    expect(readStoredAttempt("a1")).toEqual({ answers: { q1: 1 }, client_seq: 1 });
    await vi.advanceTimersByTimeAsync(12000); // retry 3 succeeds
    expect(srv.calls).toHaveLength(4);
    expect(q.status()).toBe("synced");
    expect(srv.answers).toEqual({ q1: 1 });
    // after a success the next failure starts again at 3 s
    srv.faults = ["5xx"];
    q.set("q2", "x");
    await vi.advanceTimersByTimeAsync(1000);
    expect(srv.calls).toHaveLength(5);
    await vi.advanceTimersByTimeAsync(3000);
    expect(srv.calls).toHaveLength(6);
    expect(srv.answers).toEqual({ q1: 1, q2: "x" });
  });

  it("nudge() sends at once when the network is back instead of waiting the backoff", async () => {
    const srv = new FakeServer();
    srv.defaultFault = "drop-request";
    const q = new AnswerQueue("a1", srv.put, { jitter: 0 });
    q.init({});
    q.set("q1", 1);
    await vi.advanceTimersByTimeAsync(1000 + 3000 + 6000 + 12000); // 4 failures, next in 24 s
    expect(srv.calls).toHaveLength(4);
    srv.defaultFault = "ok";
    q.nudge();
    await vi.advanceTimersByTimeAsync(0);
    expect(srv.calls).toHaveLength(5);
    expect(q.status()).toBe("synced");
    // nudge with nothing pending does nothing
    q.nudge();
    await vi.advanceTimersByTimeAsync(0);
    expect(srv.calls).toHaveLength(5);
  });

  it("nudge() does not cut the 1 s debounce short (no failure yet)", async () => {
    const srv = new FakeServer();
    const q = new AnswerQueue("a1", srv.put);
    q.init({});
    q.set("q1", 1);
    q.nudge();
    await vi.advanceTimersByTimeAsync(500);
    expect(srv.calls).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(500);
    expect(srv.calls).toHaveLength(1);
  });

  // ------------------------------------------------------------ timeouts
  it("a save that hangs times out and is retried (sync is never blocked)", async () => {
    const srv = new FakeServer();
    srv.faults = ["hang"];
    const q = new AnswerQueue("a1", srv.put, { jitter: 0, timeoutMs: 20_000 });
    q.init({});
    q.set("q1", 1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(srv.calls).toHaveLength(1);
    q.set("q2", 2); // typing continues while the save hangs
    await vi.advanceTimersByTimeAsync(19_999);
    expect(srv.calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1); // timeout -> retry in 3 s
    await vi.advanceTimersByTimeAsync(3000);
    expect(srv.calls).toHaveLength(2);
    expect(srv.calls[1]).toMatchObject({ seq: 2, answers: { q1: 1, q2: 2 } });
    expect(q.status()).toBe("synced");
    expect(srv.answers).toEqual({ q1: 1, q2: 2 });
  });

  it("a timed-out save that reaches the server late cannot overwrite newer answers", async () => {
    const srv = new FakeServer();
    srv.faults = ["hang-then-apply"];
    const q = new AnswerQueue("a1", srv.put, { jitter: 0, timeoutMs: 20_000 });
    q.init({});
    q.set("q1", "old");
    await vi.advanceTimersByTimeAsync(1000); // seq 1 hangs, will land at +60 s
    q.set("q1", "new");
    await vi.advanceTimersByTimeAsync(20_000 + 3000); // timeout + retry: seq 2 saved
    expect(srv.answers).toEqual({ q1: "new" });
    await vi.advanceTimersByTimeAsync(60_000); // the stale seq-1 request finally arrives
    expect(srv.answers).toEqual({ q1: "new" });
    expect(srv.seq).toBe(2);
    expect(q.status()).toBe("synced");
  });

  it("a lost response is retried and recognized as already saved", async () => {
    const srv = new FakeServer();
    srv.faults = ["drop-response"];
    const q = new AnswerQueue("a1", srv.put, { jitter: 0 });
    q.init({});
    q.set("q1", 1);
    await vi.advanceTimersByTimeAsync(1000 + 3000);
    expect(srv.calls.map((c) => c.seq)).toEqual([1, 1]);
    expect(q.status()).toBe("synced");
    expect(q.getClientSeq()).toBe(1);
  });

  // ------------------------------------------------------------ reload / persistence
  it("answers typed offline survive a reload and are sent after it", async () => {
    const srv = new FakeServer();
    srv.defaultFault = "drop-request";
    const q1 = new AnswerQueue("a1", srv.put);
    q1.init({});
    q1.set("q1", 2);
    q1.set("q2", "typed offline");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(srv.answers).toEqual({});
    q1.stop(); // page unload
    // reload (still offline: GET attempt falls back to the cached view = old server state)
    const q2 = new AnswerQueue("a1", srv.put);
    q2.init({}, -1);
    expect(q2.getAnswers()).toEqual({ q1: 2, q2: "typed offline" });
    expect(q2.status()).toBe("pending");
    q2.set("q3", 3.5);
    srv.defaultFault = "ok";
    await vi.advanceTimersByTimeAsync(40_000);
    expect(srv.answers).toEqual({ q1: 2, q2: "typed offline", q3: 3.5 });
    expect(q2.status()).toBe("synced");
    expect(readStoredAttempt("a1")).toEqual({ answers: srv.answers, client_seq: srv.seq });
  });

  it("stop() ignores the answer of a save still in flight", async () => {
    let resolve!: (v: SaveAnswersResponse) => void;
    const put = vi.fn(() => new Promise<SaveAnswersResponse>((r) => (resolve = r)));
    const q = new AnswerQueue("a1", put);
    const seen: string[] = [];
    q.init({});
    q.set("q1", 1);
    await vi.advanceTimersByTimeAsync(1000);
    q.subscribe((s) => seen.push(s));
    q.stop();
    resolve({ saved: true, client_seq: 1, server_time: "" });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(seen).toEqual([]);
    expect(put).toHaveBeenCalledTimes(1);
  });

  // ------------------------------------------------------------ another computer / tab
  it("without a local copy, continues from the server's client_seq (spare laptop)", async () => {
    const srv = new FakeServer();
    srv.answers = { q1: 0, q2: "from laptop A" };
    srv.seq = 41;
    const q = new AnswerQueue("a1", srv.put);
    q.init({ ...srv.answers }, 41);
    expect(q.status()).toBe("synced");
    q.set("q2", "from laptop B");
    expect(q.getClientSeq()).toBe(42); // a submit right now would carry 42 > 41
    await vi.advanceTimersByTimeAsync(1000);
    expect(srv.calls).toEqual([{ seq: 42, answers: { q1: 0, q2: "from laptop B" }, fault: "ok" }]);
    expect(srv.answers).toEqual({ q1: 0, q2: "from laptop B" });
  });

  it("an old laptop with a stale local copy adopts the server's newer answers on load", async () => {
    localStorage.setItem(attemptStorageKey("a1"), JSON.stringify({ answers: { q1: "stale A" }, client_seq: 5 }));
    const srv = new FakeServer();
    srv.answers = { q1: "newer B", q2: "B" };
    srv.seq = 30;
    const q = new AnswerQueue("a1", srv.put);
    q.init({ ...srv.answers }, 30);
    expect(q.getAnswers()).toEqual({ q1: "newer B", q2: "B" });
    expect(q.status()).toBe("synced");
    expect(readStoredAttempt("a1")).toEqual({ answers: { q1: "newer B", q2: "B" }, client_seq: 30 });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(srv.calls).toHaveLength(0);
  });

  it("same seq with different answers (two tabs) is not taken as saved: it bumps and resends", async () => {
    const srv = new FakeServer();
    srv.answers = { q1: "tab A" };
    srv.seq = 3;
    const q = new AnswerQueue("a1", srv.put);
    localStorage.setItem(attemptStorageKey("a1"), JSON.stringify({ answers: { q1: "x" }, client_seq: 2 }));
    q.init({ q1: "x" }, 2); // tab B loaded before tab A's save
    q.set("q1", "tab B"); // seq 3, same as tab A's
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(10);
    expect(srv.calls.map((c) => c.seq)).toEqual([3, 4]);
    expect(srv.answers).toEqual({ q1: "tab B" });
    expect(q.status()).toBe("synced");
  });

  // ------------------------------------------------------------ order + no loss (randomized)
  for (const seed of [1, 2, 3, 4, 5]) {
    it(`randomized faults (seed ${seed}): seqs only grow and the server ends with the last answers`, async () => {
      const r = rng(seed);
      const srv = new FakeServer();
      const faults: Fault[] = ["ok", "ok", "drop-request", "drop-response", "5xx", "hang", "hang-then-apply"];
      srv.put = ((orig) => (a: AnswerMap, s: number) => {
        srv.faults = [faults[Math.floor(r() * faults.length)]];
        return orig(a, s);
      })(srv.put);
      const q = new AnswerQueue("a1", srv.put, { random: r });
      q.init({});
      const qids = ["q1", "q2", "q3", "q4", "q5"];
      for (let i = 0; i < 120; i++) {
        const qid = qids[Math.floor(r() * qids.length)];
        const v = r() < 0.1 ? undefined : r() < 0.5 ? Math.floor(r() * 4) : `t${i}-ünï-数学`;
        q.set(qid, v);
        await vi.advanceTimersByTimeAsync(Math.floor(r() * 4000));
      }
      // network healthy again
      const healthy = (a: AnswerMap, s: number) => {
        srv.calls.push({ seq: s, answers: { ...a }, fault: "ok" });
        if (s > srv.seq) {
          srv.answers = { ...a };
          srv.seq = s;
          return Promise.resolve({ saved: true, client_seq: s, server_time: "" });
        }
        const dup = s === srv.seq && JSON.stringify(a) === JSON.stringify(srv.answers);
        return Promise.resolve({ saved: dup, client_seq: srv.seq, server_time: "" });
      };
      // swap the transport the queue uses (it holds the function it was given)
      (q as unknown as { put: typeof healthy }).put = healthy;
      await vi.advanceTimersByTimeAsync(200_000); // backoff cap 30 s + jitter, late arrivals land
      q.nudge();
      await vi.advanceTimersByTimeAsync(1000);
      expect(q.status()).toBe("synced");
      expect(srv.answers).toEqual(q.getAnswers());
      expect(srv.seq).toBe(q.getClientSeq());
      // the queue never sent a smaller seq after a bigger one
      const sent = srv.calls.map((c) => c.seq);
      for (let i = 1; i < sent.length; i++) expect(sent[i]).toBeGreaterThanOrEqual(sent[i - 1]);
      expect(readStoredAttempt("a1")).toEqual({ answers: q.getAnswers(), client_seq: q.getClientSeq() });
    });
  }

  it("409 (time up) stops the queue but keeps the local copy for the submit", async () => {
    const srv = new FakeServer();
    srv.faults = ["409"];
    const onClosed = vi.fn();
    const q = new AnswerQueue("a1", srv.put, { onClosed });
    q.init({});
    q.set("q1", "last words");
    await vi.advanceTimersByTimeAsync(1000);
    expect(onClosed).toHaveBeenCalledTimes(1);
    expect(q.getAnswers()).toEqual({ q1: "last words" });
    expect(readStoredAttempt("a1")).toEqual({ answers: { q1: "last words" }, client_seq: 1 });
  });
});

describe("request() timeout", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
    vi.useRealTimers();
  });

  it("aborts a hanging request and throws NetworkError", async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    globalThis.fetch = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      signal = init?.signal ?? undefined;
      return new Promise<Response>((_res, rej) => {
        signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
      });
    }) as typeof fetch;
    const p = request("student", "PUT", "/api/x", { a: 1 }, { timeoutMs: 5000 });
    const caught = p.catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(4999);
    expect(signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const err = await caught;
    expect(err).toBeInstanceOf(NetworkError);
    expect((err as Error).message).toMatch(/did not answer in time/);
  });

  it("does not abort a request that answers in time, and student calls use a timeout", async () => {
    let init: RequestInit | undefined;
    globalThis.fetch = vi.fn(async (_u: RequestInfo | URL, i?: RequestInit) => {
      init = i;
      return new Response(JSON.stringify({ saved: true, client_seq: 1, server_time: "" }), { status: 200 });
    }) as typeof fetch;
    const { studentApi } = await import("../api/client");
    const res = await studentApi.saveAnswers("a1", { q1: 1 }, 1);
    expect(res.saved).toBe(true);
    expect(init?.signal).toBeDefined();
    expect(init?.signal?.aborted).toBe(false);
    expect(STUDENT_REQUEST_TIMEOUT_MS).toBeGreaterThanOrEqual(10_000);
  });
});
