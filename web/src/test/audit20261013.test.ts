// Security audit 2026-10-13 (student side): local answer copies are per account,
// package v3 carries its PBKDF2 count, sign-out drops copies the server already has.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { tokenUserId } from "../api/client";
import type { AnswerMap, SaveAnswersResponse } from "../api/types";
import { AnswerQueue, attemptStorageKey, readStoredAttempt } from "../lib/answerQueue";
import { LEGACY_ITERATIONS, WrongCodeError, clearExamDataOnSignOut, decryptPackage, encryptPackage, pruneExamData } from "../lib/offline";

const ok = (seq: number): SaveAnswersResponse => ({ saved: true, client_seq: seq, server_time: "2026-10-13T10:00:00Z" });

let subtle: SubtleCrypto;
beforeAll(async () => {
  if (!globalThis.crypto?.subtle) {
    const mod = "node:crypto";
    const { webcrypto } = (await import(/* @vite-ignore */ mod)) as { webcrypto: Crypto };
    vi.stubGlobal("crypto", webcrypto);
  }
  subtle = globalThis.crypto.subtle;
});

const b64url = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

describe("token account id", () => {
  it("reads the uid from the token payload", () => {
    expect(tokenUserId(`${b64url("42:student:9999999999:0:abcd")}.sig`)).toBe("42");
    expect(tokenUserId(null)).toBeNull();
    expect(tokenUserId("not-a-token")).toBeNull();
    expect(tokenUserId(`${b64url("x:y")}.s`)).toBeNull();
  });
});

describe("offline answer copy belongs to one account (confirmed finding)", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it("student B never loads or sends student A's offline answers", async () => {
    // A answers offline; nothing reaches the server
    const putA = vi.fn(async () => {
      throw new Error("offline");
    });
    const a = new AnswerQueue("offline:e1", putA, { owner: "1" });
    a.init({});
    a.set("q1", "A-answer-q1");
    a.set("q2", "A-answer-q2");
    a.stop();
    clearExamDataOnSignOut();
    // B signs in on the same computer and starts the same exam offline
    const putB = vi.fn(async (_x: AnswerMap, seq: number) => ok(seq));
    const b = new AnswerQueue("offline:e1", putB, { owner: "2" });
    b.init({});
    expect(b.getAnswers()).toEqual({});
    expect(b.getClientSeq()).toBe(0);
    await b.flush();
    expect(putB).not.toHaveBeenCalled();
    // A's unsent work is still there for A
    expect(readStoredAttempt("offline:e1", localStorage, "1")?.answers).toEqual({ q1: "A-answer-q1", q2: "A-answer-q2" });
    expect(readStoredAttempt("offline:e1", localStorage, "2")).toBeNull();
  });

  it("an old unowned offline copy is not picked up; an old online copy still is", () => {
    localStorage.setItem(attemptStorageKey("offline:e2"), JSON.stringify({ answers: { q1: 1 }, client_seq: 2 }));
    localStorage.setItem(attemptStorageKey("a_9"), JSON.stringify({ answers: { q1: 3 }, client_seq: 4 }));
    expect(readStoredAttempt("offline:e2", localStorage, "7")).toBeNull();
    expect(readStoredAttempt("a_9", localStorage, "7")?.answers).toEqual({ q1: 3 });
    // a copy that says it is someone else's is refused even under the legacy key
    localStorage.setItem(attemptStorageKey("a_8"), JSON.stringify({ answers: { q1: 3 }, client_seq: 4, owner: "5" }));
    expect(readStoredAttempt("a_8", localStorage, "7")).toBeNull();
  });

  it("sign-out drops synced copies and old unowned offline copies, keeps unsent work", async () => {
    const put = vi.fn(async (_x: AnswerMap, seq: number) => ok(seq));
    const q = new AnswerQueue("a_1", put, { owner: "3" });
    q.init({});
    q.set("q1", 1);
    await q.flush();
    expect(q.status()).toBe("synced");
    localStorage.setItem(attemptStorageKey("a_2", "3"), JSON.stringify({ answers: { q1: 1 }, client_seq: 3, synced: 1, owner: "3" }));
    localStorage.setItem(attemptStorageKey("offline:e3"), JSON.stringify({ answers: { q1: 1 }, client_seq: 1 }));
    clearExamDataOnSignOut();
    expect(localStorage.getItem(attemptStorageKey("a_1", "3"))).toBeNull();          // server has it
    expect(localStorage.getItem(attemptStorageKey("a_2", "3"))).not.toBeNull();      // unsent
    expect(localStorage.getItem(attemptStorageKey("offline:e3"))).toBeNull();        // unowned offline
  });

  it("handed-in attempts lose their scoped copy on the next exam list", () => {
    localStorage.setItem(attemptStorageKey("a_done", "3"), JSON.stringify({ answers: {}, client_seq: 1, owner: "3" }));
    pruneExamData([{ id: "e", status: "closed", attempt: { attempt_id: "a_done", status: "submitted" } }]);
    expect(localStorage.getItem(attemptStorageKey("a_done", "3"))).toBeNull();
  });
});

describe("offline package version 3", () => {
  it("uses the iteration count written in the package", async () => {
    const pkg = await encryptPackage({ exam_id: "e_v3" }, "482193", "e_v3", subtle, 1500, undefined, "ab".repeat(32));
    expect(pkg).toMatchObject({ version: 3, iterations: 1500 });
    expect(await decryptPackage(pkg, "482193", subtle, undefined, "ab".repeat(32))).toEqual({ exam_id: "e_v3" });
  });

  it("refuses a damaged count instead of hanging", async () => {
    const pkg = await encryptPackage({ exam_id: "e_v3" }, "482193", "e_v3", subtle, 1500, undefined, "ab".repeat(32));
    await expect(decryptPackage({ ...pkg, iterations: 1e12 }, "482193", subtle, undefined, "ab".repeat(32))).rejects.toBeInstanceOf(
      WrongCodeError,
    );
    expect(LEGACY_ITERATIONS).toBe(100_000);
  });
});
