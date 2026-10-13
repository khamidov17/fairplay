import { afterEach, describe, expect, it, vi } from "vitest";
import * as bridge from "../lib/bridge";

function native() {
  const postMessage = vi.fn();
  (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { proctora: { postMessage } } };
  return postMessage;
}
const send = (detail: unknown) => window.dispatchEvent(new CustomEvent("proctora-native", { detail }));

describe("native bridge", () => {
  afterEach(() => {
    delete (window as unknown as { webkit?: unknown }).webkit;
    bridge._resetBridgeForTests();
  });

  it("is a no-op in a normal browser", async () => {
    expect(bridge.isNative()).toBe(false);
    bridge.hello();
    bridge.unlock();
    await expect(bridge.requestLock()).resolves.toEqual({ ok: true });
    expect(bridge.isLocked()).toBe(false);
  });

  it("sends hello, lock and unlock to the Mac app", async () => {
    const post = native();
    bridge.hello();
    expect(post).toHaveBeenCalledWith({ action: "hello" });
    const p = bridge.requestLock();
    expect(post).toHaveBeenCalledWith({ action: "lock", offline: true }); // audit M-06: no session = offline start
    send({ type: "locked" });
    await expect(p).resolves.toEqual({ ok: true });
    expect(bridge.isLocked()).toBe(true);
    bridge.unlock();
    expect(post).toHaveBeenCalledWith({ action: "unlock" });
    send({ type: "unlocked" });
    expect(bridge.isLocked()).toBe(false);
  });

  it("locks for the exam session when there is one (audit M-06)", async () => {
    const post = native();
    const p = bridge.requestLock(1000, { attempt_id: "a_1", token: "t", session: "s_12345678" });
    expect(post).toHaveBeenCalledWith({ action: "lock", attempt_id: "a_1", token: "t", session: "s_12345678" });
    send({ type: "lockFailed", reason: "bad_session" });
    await expect(p).resolves.toEqual({ ok: false, reason: "bad_session" });
  });

  it("reports lockFailed with the reason", async () => {
    native();
    const p = bridge.requestLock();
    send({ type: "lockFailed", reason: "accessibility" });
    await expect(p).resolves.toEqual({ ok: false, reason: "accessibility" });
  });

  it("openExternal posts the contract message only inside the Mac app", () => {
    const params = {
      attempt_id: "a_1",
      url: "https://canvas.dku.edu/quiz/3",
      allowed_domains: ["canvas.dku.edu", "login.microsoftonline.com"],
      finish_url_contains: null,
      deadline_at: "2026-10-11T10:30:00Z",
      server_time: "2026-10-11T10:00:00Z",
      title: "MATH 201 Midterm",
      token: "tok",
      session: "sess",
    };
    expect(bridge.openExternal(params)).toBe(false);
    const post = native();
    expect(bridge.openExternal(params)).toBe(true);
    expect(post).toHaveBeenCalledWith({ action: "openExternal", ...params });
  });

  it("requestLock passes the exam session to the app", async () => {
    const post = native();
    const p = bridge.requestLock(1000, { attempt_id: "a_1", token: "tok", session: "s1" });
    expect(post).toHaveBeenCalledWith({ action: "lock", attempt_id: "a_1", token: "tok", session: "s1" });
    send({ type: "locked" });
    await expect(p).resolves.toEqual({ ok: true });
  });

  it("attest resolves the app's signed proof, null outside the app", async () => {
    await expect(bridge.attest("n1")).resolves.toBeNull();
    const post = native();
    const p = bridge.attest("n1");
    expect(post).toHaveBeenCalledWith({ action: "attest", nonce: "n1" });
    send({ type: "attestation", nonce: "other", device_id: "x", signature: "y", version: "0.5", build: "release" });
    send({ type: "attestation", nonce: "n1", device_id: "dev-1", signature: "ab", version: "0.5", build: "release" });
    await expect(p).resolves.toEqual({ device_id: "dev-1", nonce: "n1", signature: "ab", version: "0.5", build: "release" });
  });

  it("setExamSession posts only with a session", () => {
    const post = native();
    bridge.setExamSession({ attempt_id: "a_1", token: "t", session: "" });
    expect(post).not.toHaveBeenCalled();
    bridge.setExamSession({ attempt_id: "a_1", token: "t", session: "s" });
    expect(post).toHaveBeenCalledWith({ action: "session", attempt_id: "a_1", token: "t", session: "s" });
  });

  it("forwards native events to listeners", () => {
    native();
    const got = vi.fn();
    const off = bridge.onNative(got);
    send({ type: "event", event: { type: "blocked_shortcut", detail: "Cmd-key48", client_time: "x" } });
    expect(got).toHaveBeenCalledWith({ type: "event", event: { type: "blocked_shortcut", detail: "Cmd-key48", client_time: "x" } });
    off();
  });
});
