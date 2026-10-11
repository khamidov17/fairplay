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
    expect(post).toHaveBeenCalledWith({ action: "lock" });
    send({ type: "locked" });
    await expect(p).resolves.toEqual({ ok: true });
    expect(bridge.isLocked()).toBe(true);
    bridge.unlock();
    expect(post).toHaveBeenCalledWith({ action: "unlock" });
    send({ type: "unlocked" });
    expect(bridge.isLocked()).toBe(false);
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
    };
    expect(bridge.openExternal(params)).toBe(false);
    const post = native();
    expect(bridge.openExternal(params)).toBe(true);
    // phase 1a: exit_check (invigilator exit verifier) is always sent, null when unknown
    expect(post).toHaveBeenCalledWith({ action: "openExternal", ...params, exit_check: null });
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
