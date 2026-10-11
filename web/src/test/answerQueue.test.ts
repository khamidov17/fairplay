import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError } from "../api/client";
import type { AnswerMap, SaveAnswersResponse } from "../api/types";
import { AnswerQueue, attemptStorageKey, readStoredAttempt } from "../lib/answerQueue";

const ok = (seq: number): SaveAnswersResponse => ({ saved: true, client_seq: seq, server_time: "2026-10-11T10:00:00Z" });

describe("AnswerQueue (offline save)", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it("increments client_seq and writes localStorage on every change", () => {
    const put = vi.fn(async (_a: AnswerMap, seq: number) => ok(seq));
    const q = new AnswerQueue("a1", put);
    q.init({});
    q.set("q1", 2);
    q.set("q2", "paris");
    q.set("q1", undefined);
    expect(q.getClientSeq()).toBe(3);
    expect(readStoredAttempt("a1")).toEqual({ answers: { q2: "paris" }, client_seq: 3 });
    expect(JSON.parse(localStorage.getItem(attemptStorageKey("a1"))!).client_seq).toBe(3);
    expect(q.status()).toBe("pending");
  });

  it("debounces saves by 1 s and becomes synced", async () => {
    const put = vi.fn(async (_a: AnswerMap, seq: number) => ok(seq));
    const q = new AnswerQueue("a1", put);
    const statuses: string[] = [];
    q.subscribe((s) => statuses.push(s));
    q.init({});
    q.set("q1", 1);
    await vi.advanceTimersByTimeAsync(500);
    q.set("q1", 2);
    await vi.advanceTimersByTimeAsync(999);
    expect(put).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(put).toHaveBeenCalledTimes(1);
    expect(put).toHaveBeenCalledWith({ q1: 2 }, 2);
    expect(q.status()).toBe("synced");
    expect(statuses).toContain("pending");
    expect(statuses[statuses.length - 1]).toBe("synced");
  });

  it("retries after 3 s while offline, backing off, then syncs", async () => {
    let fail = 2;
    const put = vi.fn(async (_a: AnswerMap, seq: number) => {
      if (fail-- > 0) throw new NetworkError();
      return ok(seq);
    });
    const q = new AnswerQueue("a1", put, { jitter: 0 });
    q.init({});
    q.set("q1", 5);
    await vi.advanceTimersByTimeAsync(1000);
    expect(put).toHaveBeenCalledTimes(1);
    expect(q.status()).toBe("pending");
    await vi.advanceTimersByTimeAsync(2999);
    expect(put).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(put).toHaveBeenCalledTimes(2);
    expect(q.status()).toBe("pending");
    await vi.advanceTimersByTimeAsync(5999); // second failure: 6 s
    expect(put).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(put).toHaveBeenCalledTimes(3);
    expect(q.status()).toBe("synced");
  });

  it("resumes from localStorage after reload and re-syncs unsynced answers", async () => {
    localStorage.setItem(attemptStorageKey("a1"), JSON.stringify({ answers: { q1: 3 }, client_seq: 4 }));
    const put = vi.fn(async (_a: AnswerMap, seq: number) => ok(seq));
    const q = new AnswerQueue("a1", put);
    q.init({ q1: 1 });
    expect(q.getAnswers()).toEqual({ q1: 3 });
    expect(q.status()).toBe("pending");
    await vi.advanceTimersByTimeAsync(0);
    expect(put).toHaveBeenCalledWith({ q1: 3 }, 4);
    expect(q.status()).toBe("synced");
    q.set("q2", 1);
    expect(q.getClientSeq()).toBe(5);
  });

  it("is synced right away when local copy matches the server", () => {
    localStorage.setItem(attemptStorageKey("a1"), JSON.stringify({ answers: { q1: 3 }, client_seq: 4 }));
    const q = new AnswerQueue("a1", vi.fn());
    q.init({ q1: 3 });
    expect(q.status()).toBe("synced");
  });

  it("starts from the server answers when nothing is stored locally", () => {
    const q = new AnswerQueue("a1", vi.fn());
    q.init({ q1: "x" });
    expect(q.getAnswers()).toEqual({ q1: "x" });
    expect(q.getClientSeq()).toBe(0);
    expect(q.status()).toBe("synced");
  });

  it("jumps past a newer server seq when the save is ignored", async () => {
    const put = vi
      .fn<(a: AnswerMap, seq: number) => Promise<SaveAnswersResponse>>()
      .mockResolvedValueOnce({ saved: false, client_seq: 10, server_time: "" })
      .mockImplementation(async (_a, seq) => ok(seq));
    const q = new AnswerQueue("a1", put);
    q.init({});
    q.set("q1", 1);
    await vi.advanceTimersByTimeAsync(1000);
    // The immediate resend is a setTimeout(0) created mid-tick; fake timers treat that as 1 ms.
    await vi.advanceTimersByTimeAsync(10);
    expect(put).toHaveBeenLastCalledWith({ q1: 1 }, 11);
    expect(q.status()).toBe("synced");
  });

  it("stops and reports closed on 409", async () => {
    const onClosed = vi.fn();
    const put = vi.fn(async () => {
      throw new ApiError(409, "Time is up");
    });
    const q = new AnswerQueue("a1", put, { onClosed });
    q.init({});
    q.set("q1", 1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(onClosed).toHaveBeenCalledTimes(1);
    expect(q.status()).toBe("closed");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(put).toHaveBeenCalledTimes(1);
  });
});
