// Audit 2026-10-12 tier 3 web: W-09 allowed-site wording, W-10 bridge nonce, W-14 announcements.
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OFFLINE_ANNOUNCE_MS, useAnnouncements } from "../components/student/LockedBar";
import { describeEntry, effectiveAllowedSites, startUrlEntry } from "../components/teacher/externalValidation";
import * as bridge from "../lib/bridge";

describe("W-09 allowed sites as the app applies them", () => {
  it("opens only the exam's folder on shared hosts, the host elsewhere", () => {
    expect(startUrlEntry("https://docs.google.com/forms/d/e/ABC/viewform")).toBe("docs.google.com/forms/d/e/ABC/");
    expect(startUrlEntry("https://quiz.dku.edu/exam/1")).toBe("quiz.dku.edu");
    expect(effectiveAllowedSites("https://docs.google.com/forms/d/e/ABC/viewform", ["accounts.google.com"])).toEqual([
      "docs.google.com/forms/d/e/ABC/",
      "accounts.google.com",
    ]);
  });
  it("says in words what each entry opens", () => {
    expect(describeEntry("dku.edu")).toMatch(/this site only, not its sub-sites/);
    expect(describeEntry("*.dku.edu")).toMatch(/every site under it/);
    expect(describeEntry("docs.google.com/forms/d/e/ABC/")).toMatch(/only pages under this folder/);
  });
});

describe("W-10 native events need the host's nonce", () => {
  afterEach(() => {
    bridge._setNonceForTests(null);
    bridge._resetBridgeForTests();
  });
  const send = (detail: unknown) => window.dispatchEvent(new CustomEvent("proctora-native", { detail }));

  it("ignores events without the nonce once the host set one", () => {
    const seen = vi.fn();
    bridge.onNative(seen);
    bridge._setNonceForTests("n".repeat(32));
    send({ type: "locked" });                                   // forged by page JS
    expect(seen).not.toHaveBeenCalled();
    send({ type: "locked", nonce: "n".repeat(32) });            // wrong field: not the bridge nonce
    expect(seen).not.toHaveBeenCalled();
    send({ type: "locked", bridge_nonce: "n".repeat(32) });     // from the app
    expect(seen).toHaveBeenCalledTimes(1);
  });

  // Regression 2026-10-13: the app stamped the bridge nonce into `nonce`, which attestation
  // replies already use for the server challenge, so every start inside the app timed out
  // and the server answered "Open this exam in the FairPlay app".
  it("still accepts the app's attestation (challenge in `nonce`, bridge nonce beside it)", async () => {
    const w = window as unknown as { webkit?: unknown };
    const postMessage = vi.fn((m: { action?: string; nonce?: string }) => {
      if (m.action === "attest")
        setTimeout(() =>
          send({
            type: "attestation", nonce: m.nonce, device_id: "dev1", signature: "sig",
            version: "0.5", build: "dev", bridge_nonce: "n".repeat(32),
          }),
        );
    });
    w.webkit = { messageHandlers: { proctora: { postMessage } } };
    try {
      bridge._setNonceForTests("n".repeat(32));
      const proof = await bridge.attest("challenge-123", 1000);
      expect(proof).toEqual({ device_id: "dev1", nonce: "challenge-123", signature: "sig", version: "0.5", build: "dev" });
    } finally {
      delete w.webkit;
    }
  });

  it("accepts events from hosts that set no nonce (older builds)", () => {
    const seen = vi.fn();
    bridge.onNative(seen);
    send({ type: "state", locked: false, trusted: true, version: "0.5" });
    expect(seen).toHaveBeenCalledTimes(1);
  });
});

describe("W-14 screen-reader announcements", () => {
  afterEach(() => vi.useRealTimers());
  it("speaks only lasting offline states and the time cues, not every save", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ sync, remaining }) => useAnnouncements(sync, remaining), {
      initialProps: { sync: "pending" as const as "pending" | "synced", remaining: 20 * 60_000 },
    });
    rerender({ sync: "synced", remaining: 20 * 60_000 });            // quick save: silent
    expect(result.current).toBe("");
    rerender({ sync: "pending", remaining: 20 * 60_000 });
    act(() => vi.advanceTimersByTime(OFFLINE_ANNOUNCE_MS + 10));
    expect(result.current).toMatch(/kept on this computer/);
    rerender({ sync: "synced", remaining: 20 * 60_000 });
    expect(result.current).toBe("All answers saved.");
    rerender({ sync: "synced", remaining: 5 * 60_000 - 1000 });
    expect(result.current).toBe("5 minutes left.");
    rerender({ sync: "synced", remaining: 59_000 });
    expect(result.current).toBe("1 minute left.");
  });
});

describe("M-11 preflight before locking", () => {
  afterEach(() => {
    delete (window as unknown as { webkit?: unknown }).webkit;
    bridge._resetBridgeForTests();
  });
  it("is empty outside the app and returns the app's problems inside it", async () => {
    await expect(bridge.preflight()).resolves.toEqual([]);
    const postMessage = vi.fn();
    (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { proctora: { postMessage } } };
    const p = bridge.preflight(1000);
    expect(postMessage).toHaveBeenCalledWith({ action: "preflight" });
    window.dispatchEvent(new CustomEvent("proctora-native", { detail: { type: "preflight", problems: ["Turn off Remote Login"] } }));
    await expect(p).resolves.toEqual(["Turn off Remote Login"]);
  });
  it("an app build without preflight just times out to [] (the lock still checks)", async () => {
    vi.useFakeTimers();
    (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { proctora: { postMessage: vi.fn() } } };
    const p = bridge.preflight(500);
    vi.advanceTimersByTime(600);
    await expect(p).resolves.toEqual([]);
    vi.useRealTimers();
  });
});
