// Audit 2026-10-12: app proof on start + X-FairPlay-Session on every attempt request.
import { afterEach, describe, expect, it, vi } from "vitest";
import { setToken, studentApi } from "../api/client";
import { attemptIdOfPath, getAttemptSession } from "../lib/appSession";
import * as bridge from "../lib/bridge";

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as { webkit?: unknown }).webkit;
  bridge._resetBridgeForTests();
  localStorage.clear();
});

describe("app session", () => {
  it("parses the attempt id of student paths", () => {
    expect(attemptIdOfPath("/api/student/attempts/a_1/answers")).toBe("a_1");
    expect(attemptIdOfPath("/api/student/attempts/a%201")).toBe("a 1");
    expect(attemptIdOfPath("/api/student/exams/e_1/start")).toBeNull();
  });

  it("in the app: challenge -> signed proof -> start, then the session goes on every request", async () => {
    setToken("student", "tok");
    const postMessage = vi.fn((msg: { action: string; nonce?: string }) => {
      if (msg.action === "attest")
        setTimeout(() =>
          window.dispatchEvent(new CustomEvent("proctora-native", {
            detail: { type: "attestation", nonce: msg.nonce, device_id: "dev-1", signature: "f".repeat(64), version: "0.5", build: "release" },
          })),
        );
    });
    (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { proctora: { postMessage } } };
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/challenge")) return json({ nonce: "n-1", expires_in: 120 });
      if (url.endsWith("/start")) {
        const body = JSON.parse(String(init?.body));
        expect(body.client).toEqual({ device_id: "dev-1", nonce: "n-1", signature: "f".repeat(64), version: "0.5", build: "release" });
        return json({ attempt_id: "a_1", session: "sess-1", questions: [], answers: {} });
      }
      return json({ saved: true, client_seq: 1, server_time: "x" });
    });
    vi.stubGlobal("fetch", fetchMock);

    const view = await studentApi.start("e_1", "123456");
    expect(view.attempt_id).toBe("a_1");
    expect(getAttemptSession("a_1")).toBe("sess-1");
    expect(postMessage).toHaveBeenCalledWith({ action: "session", attempt_id: "a_1", token: "tok", session: "sess-1" });

    await studentApi.saveAnswers("a_1", {}, 1);
    const [, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
    expect((init?.headers as Record<string, string>)["X-FairPlay-Session"]).toBe("sess-1");
  });

  it("in a browser: no proof is sent (the server decides)", async () => {
    setToken("student", "tok");
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      expect(url).not.toContain("/challenge");
      expect(JSON.parse(String(init?.body))).toEqual({ start_code: "123456" });
      return json({ detail: "Open this exam in the FairPlay app." }, 403);
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(studentApi.start("e_1", "123456")).rejects.toMatchObject({ status: 403 });
  });
});
