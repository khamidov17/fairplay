import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  NetworkError,
  auth,
  getToken,
  setToken,
  setUnauthorizedHandler,
  studentApi,
  teacherApi,
  tokenKey,
} from "../api/client";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async () =>
    new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

const call = (fn: ReturnType<typeof mockFetch>, i = 0) => {
  const [url, init] = fn.mock.calls[i] as unknown as [string, RequestInit];
  return { url, init, headers: init.headers as Record<string, string>, body: init.body ? JSON.parse(String(init.body)) : undefined };
};

describe("api client", () => {
  let unauthorized: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    unauthorized = vi.fn();
    setUnauthorizedHandler(unauthorized);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("stores one token per role", () => {
    setToken("teacher", "T");
    setToken("student", "S");
    expect(localStorage.getItem("proctora:token:teacher")).toBe("T");
    expect(localStorage.getItem("proctora:token:student")).toBe("S");
    expect(tokenKey("student")).toBe("proctora:token:student");
  });

  it("login posts credentials without a token", async () => {
    setToken("teacher", "old");
    const f = mockFetch(200, { token: "abc", role: "teacher", name: "Demo Teacher" });
    const res = await auth.login("teacher", "teacher123");
    expect(res.token).toBe("abc");
    const c = call(f);
    expect(c.url).toBe("/api/auth/login");
    expect(c.init.method).toBe("POST");
    expect(c.body).toEqual({ username: "teacher", password: "teacher123" });
    expect(c.headers.Authorization).toBeUndefined();
  });

  it("wrong login is an ApiError 401 without redirect", async () => {
    mockFetch(401, { detail: "Invalid username or password" });
    await expect(auth.login("x", "y")).rejects.toMatchObject({ status: 401, detail: "Invalid username or password" });
    expect(unauthorized).not.toHaveBeenCalled();
  });

  it("sends the role's bearer token", async () => {
    setToken("teacher", "TT");
    setToken("student", "SS");
    const f = mockFetch(200, []);
    await teacherApi.listExams();
    expect(call(f).headers.Authorization).toBe("Bearer TT");
    await studentApi.listExams();
    expect(call(f, 1).headers.Authorization).toBe("Bearer SS");
    expect(call(f, 1).url).toBe("/api/student/exams");
  });

  it("401 clears the token and goes to that role's login", async () => {
    setToken("student", "SS");
    mockFetch(401, { detail: "Not authenticated" });
    await expect(studentApi.listExams()).rejects.toBeInstanceOf(ApiError);
    expect(getToken("student")).toBeNull();
    expect(unauthorized).toHaveBeenCalledWith("student");
  });

  it("maps 422 validation lists to readable text", async () => {
    setToken("teacher", "T");
    mockFetch(422, { detail: [{ msg: "mcq needs 2-8 options" }, { msg: "title must not be empty" }] });
    await expect(teacherApi.createExam({ title: "", duration_min: 1, questions: [] })).rejects.toMatchObject({
      status: 422,
      detail: "mcq needs 2-8 options; title must not be empty",
    });
  });

  it("network failure throws NetworkError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    await expect(teacherApi.listExams()).rejects.toBeInstanceOf(NetworkError);
  });

  it("uses the contract's paths, methods and bodies", async () => {
    setToken("teacher", "T");
    setToken("student", "S");
    const f = mockFetch(200, {});
    await teacherApi.getExam("e_1");
    await teacherApi.updateExam("e_1", { title: "t", duration_min: 5, questions: [] });
    await teacherApi.openExam("e_1");
    await teacherApi.closeExam("e_1");
    await teacherApi.live("e_1");
    await teacherApi.results("e_1");
    await studentApi.start("e_1", "4729");
    await studentApi.getAttempt("a_1");
    await studentApi.saveAnswers("a_1", { q_1: 2 }, 7);
    await studentApi.heartbeat("a_1", 3, true);
    await studentApi.events("a_1", [{ type: "blocked_shortcut", detail: "Cmd-key48", client_time: "2026-01-01T00:00:00Z" }]);
    await studentApi.submit("a_1", { q_1: 2 }, 8);
    const got = f.mock.calls.map((_, i) => {
      const c = call(f, i);
      return [c.init.method, c.url, c.body];
    });
    expect(got).toEqual([
      ["GET", "/api/exams/e_1", undefined],
      ["PUT", "/api/exams/e_1", { title: "t", duration_min: 5, questions: [] }],
      ["POST", "/api/exams/e_1/open", undefined],
      ["POST", "/api/exams/e_1/close", undefined],
      ["GET", "/api/exams/e_1/live", undefined],
      ["GET", "/api/exams/e_1/results", undefined],
      ["POST", "/api/student/exams/e_1/start", { start_code: "4729" }],
      ["GET", "/api/student/attempts/a_1", undefined],
      ["PUT", "/api/student/attempts/a_1/answers", { answers: { q_1: 2 }, client_seq: 7 }],
      ["POST", "/api/student/attempts/a_1/heartbeat", { current_question: 3, locked: true }],
      [
        "POST",
        "/api/student/attempts/a_1/events",
        { events: [{ type: "blocked_shortcut", detail: "Cmd-key48", client_time: "2026-01-01T00:00:00Z" }] },
      ],
      ["POST", "/api/student/attempts/a_1/submit", { answers: { q_1: 2 }, client_seq: 8 }],
    ]);
  });

  it("handles 204 on delete", async () => {
    setToken("teacher", "T");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    await expect(teacherApi.deleteExam("e_1")).resolves.toBeNull();
  });
});
