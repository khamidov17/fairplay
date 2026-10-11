// Slice 6 A/B/C: offline packages (WebCrypto), offline start + reconnect, attempt-view cache,
// topic input, image checks, delivery cards, picture viewer.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { AttemptView, OfflineKeyRelease, OfflinePackage, StudentQuestion } from "../api/types";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    studentApi: {
      listExams: vi.fn(),
      start: vi.fn(),
      getAttempt: vi.fn(),
      saveAnswers: vi.fn(),
      heartbeat: vi.fn(),
      events: vi.fn(),
      submit: vi.fn(),
    },
    slice6Api: {
      topics: vi.fn(),
      examPackage: vi.fn(),
      startAfterOffline: vi.fn(),
      uploadImage: vi.fn(),
    },
    securityApi: {
      packageKey: vi.fn(),
      timeline: vi.fn(),
    },
  };
});

import { ApiError, NetworkError, securityApi, slice6Api, studentApi } from "../api/client";
import { QuestionImages } from "../components/QuestionImages";
import { ExternalExamSettings } from "../components/teacher/ExternalExamSettings";
import { imageFileProblem } from "../components/teacher/QuestionImagesEditor";
import { TopicInput, addTopic } from "../components/teacher/TopicInput";
import { EMPTY_EXTERNAL_SETTINGS } from "../components/teacher/externalValidation";
import { StartCodeForm } from "../components/student/StartCodeForm";
import {
  WrongCodeError,
  attemptViewKey,
  cacheAttemptView,
  cachedAttemptView,
  canStartOffline,
  decryptPackage,
  downloadPackage,
  encryptPackage,
  keyReleaseKey,
  offlineRecordKey,
  packageKey,
  readOfflineRecord,
} from "../lib/offline";
import ExamTakingPage from "../pages/student/ExamTakingPage";
import { draftFromExam, toExamInput, validateDraft, newQuestion } from "../pages/teacher/examDraft";

const api = studentApi as unknown as Record<string, ReturnType<typeof vi.fn>>;
const s6 = slice6Api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const sec = securityApi as unknown as Record<string, ReturnType<typeof vi.fn>>;
// Node's WebCrypto (Node >= 19 exposes it as globalThis.crypto; jsdom keeps it). Loaded via a
// variable specifier so the browser build's type check needs no @types/node.
let subtle: SubtleCrypto;
beforeAll(async () => {
  if (!globalThis.crypto?.subtle) {
    const mod = "node:crypto";
    const { webcrypto } = (await import(/* @vite-ignore */ mod)) as { webcrypto: Crypto };
    vi.stubGlobal("crypto", webcrypto);
  }
  subtle = globalThis.crypto.subtle;
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

const QUESTIONS: StudentQuestion[] = [
  { id: "q1", type: "mcq", prompt: "2 + 4 = ?", points: 1, options: ["5", "6"], images: [{ url: "data:image/png;base64,iVBORw0KGgo=", alt: "A diagram" }] },
  { id: "q2", type: "short", prompt: "Capital of France?", points: 1, options: [] },
];

// ---------------------------------------------------------------- crypto
describe("offline package crypto", () => {
  it("round-trips with WebCrypto and rejects a wrong code", async () => {
    const plain = { exam_id: "e_1", title: "Quiz", questions: QUESTIONS };
    const { pkg, release } = await encryptPackage(plain, "4821", "e_1", subtle, 1000);
    expect(pkg).toMatchObject({ exam_id: "e_1", alg: "AES-GCM", version: 2 });
    expect(pkg).not.toHaveProperty("salt"); // nothing in the package depends on the start code
    expect(release).toMatchObject({ exam_id: "e_1", iterations: 1000 });
    expect(await decryptPackage(pkg, release, "4821", subtle)).toEqual(plain);
    await expect(decryptPackage(pkg, release, "4822", subtle)).rejects.toBeInstanceOf(WrongCodeError);
    // a key release from another opening of the exam does not open this package
    const other = await encryptPackage(plain, "4821", "e_1", subtle, 1000);
    await expect(decryptPackage(pkg, other.release, "4821", subtle)).rejects.toBeInstanceOf(WrongCodeError);
  });

  it("opens a package made by the server (Python cryptography, 100 000 iterations)", async () => {
    const pkg: OfflinePackage = {
      exam_id: "e_py",
      version: 2,
      alg: "AES-GCM",
      iv: "SKPvQJc7imEN7D1G",
      ciphertext:
        "0cM9BjcqnAnvUJNUUgy62ukMHx1ML9D5biSCBsqjUcIuLArQKfOtZQNFDwe7CHA9xWzaJe0aa4wAH/JxLSK5sm30Due+uieHd6G37HmZmnVCBjpKINfS/liMNJL5JXAKbxt12JSmuvQ=",
    };
    const release: OfflineKeyRelease = {
      exam_id: "e_py",
      version: 2,
      alg: "PBKDF2-SHA256/AES-GCM",
      iterations: 100000,
      salt: "Cw0PX2GJD2TAMde9/RzMlw==",
      iv: "wu9kMAbJjZyR3VTU",
      wrapped_key: "sbvneWHulXLW2g51E5I04XuRWRAQJXKdFAtFo2LifKxCrMUgb67cPwZoDJpCHlWX",
    };
    const plain = await decryptPackage(pkg, release, "4821", subtle);
    expect(plain).toEqual({ exam_id: "e_py", title: "Python made", questions: [{ id: "q_1", prompt: "Ünï ✓" }] });
    await expect(decryptPackage(pkg, release, "4820", subtle)).rejects.toBeInstanceOf(WrongCodeError);
  });

  it("downloads and stores the package and its key release; failures are silent", async () => {
    s6.examPackage.mockResolvedValueOnce({ exam_id: "e_1", version: 2, ciphertext: "x" });
    sec.packageKey.mockResolvedValueOnce({ exam_id: "e_1", wrapped_key: "k" });
    expect(await downloadPackage("e_1")).toBe(true);
    expect(JSON.parse(localStorage.getItem(packageKey("e_1"))!)).toMatchObject({ exam_id: "e_1" });
    expect(JSON.parse(localStorage.getItem(keyReleaseKey("e_1"))!)).toMatchObject({ wrapped_key: "k" });
    expect(canStartOffline("e_1")).toBe(true);
    // exam not open yet: the package is kept, the key is not there, no offline start
    s6.examPackage.mockResolvedValueOnce({ exam_id: "e_3", version: 2, ciphertext: "x" });
    sec.packageKey.mockRejectedValueOnce(new ApiError(409, "Exam is not open"));
    expect(await downloadPackage("e_3")).toBe(true);
    expect(canStartOffline("e_3")).toBe(false);
    s6.examPackage.mockRejectedValueOnce(new NetworkError());
    expect(await downloadPackage("e_2")).toBe(false);
    // an old format-1 package is ignored
    localStorage.setItem(packageKey("e_4"), JSON.stringify({ exam_id: "e_4", version: 1, ciphertext: "x" }));
    localStorage.setItem(keyReleaseKey("e_4"), JSON.stringify({ exam_id: "e_4", wrapped_key: "k" }));
    expect(canStartOffline("e_4")).toBe(false);
  });

  it("caches the attempt view with the clock offset re-based to now", () => {
    const view = attempt({ attempt_id: "a_9", server_time: "2026-01-01T00:00:00.000Z" });
    cacheAttemptView(view, 5000);
    const got = cachedAttemptView("a_9", Date.parse("2026-05-01T00:00:00Z"))!;
    expect(got.questions).toHaveLength(2);
    expect(got.server_time).toBe("2026-05-01T00:00:05.000Z");
    expect(cachedAttemptView("nope")).toBeNull();
  });
});

function attempt(over: Partial<AttemptView> = {}): AttemptView {
  const now = Date.now();
  return {
    attempt_id: "a_1",
    exam_id: "e_1",
    title: "Quiz",
    course: "MATH 201",
    duration_min: 30,
    started_at: new Date(now).toISOString(),
    deadline_at: new Date(now + 30 * 60_000).toISOString(),
    server_time: new Date(now).toISOString(),
    questions: QUESTIONS,
    answers: {},
    status: "in_progress",
    mode: "questions",
    external_url: null,
    allowed_domains: [],
    finish_url_contains: null,
    ...over,
  };
}

// ---------------------------------------------------------------- offline start flow
describe("offline start", () => {
  beforeEach(() => {
    api.heartbeat.mockResolvedValue({ ok: true, status: "in_progress", server_time: new Date().toISOString(), deadline_at: attempt().deadline_at });
    api.saveAnswers.mockImplementation(async (_a: string, _ans: unknown, seq: number) => ({ saved: true, client_seq: seq, server_time: new Date().toISOString() }));
    api.events.mockResolvedValue({ ok: true, stored: 0 });
    api.submit.mockResolvedValue({ status: "submitted", submitted_at: new Date().toISOString(), receipt: "K7Q2-M9XD" });
  });

  async function storePackage(code = "4821") {
    const plain = { exam_id: "e_1", title: "Quiz", course: "MATH 201", duration_min: 30, mode: "questions", shuffle_questions: false, questions: QUESTIONS, generated_at: new Date().toISOString() };
    // default iterations: the app decrypts with 100 000
    const { pkg, release } = await encryptPackage(plain, code, "e_1", subtle);
    localStorage.setItem(packageKey("e_1"), JSON.stringify(pkg));
    localStorage.setItem(keyReleaseKey("e_1"), JSON.stringify(release));
  }

  function renderStart() {
    return render(
      <MemoryRouter initialEntries={["/student/checkin"]}>
        <Routes>
          <Route path="/student/checkin" element={<StartCodeForm examId="e_1" />} />
          <Route path="/student/exam/:attemptId" element={<ExamTakingPage />} />
          <Route path="/student/done/:attemptId" element={<p>done page</p>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  async function typeCode(user: ReturnType<typeof userEvent.setup>, code: string) {
    await user.click(screen.getByRole("textbox", { name: "Digit 1 of 4" }));
    await user.keyboard(code);
    await user.click(screen.getByRole("button", { name: "Start exam" }));
  }

  it("decrypts the stored package when the network is down, then reconnects, syncs and submits", { timeout: 20000 }, async () => {
    await storePackage();
    const user = userEvent.setup();
    api.start.mockRejectedValue(new NetworkError());
    // still offline at first, then the server accepts the attempt
    s6.startAfterOffline.mockRejectedValueOnce(new NetworkError());
    renderStart();
    await typeCode(user, "4821");

    expect((await screen.findAllByText(/Question 1 of 2/, {}, { timeout: 5000 }))[0]).toBeInTheDocument();
    expect(screen.getByTestId("offline-banner")).toHaveTextContent(/Working offline/);
    expect(screen.getByRole("img", { name: "A diagram" })).toBeInTheDocument();
    const rec = readOfflineRecord("e_1")!;
    expect(rec.start_code).toBe("4821");
    expect(rec.view.attempt_id).toBe("offline:e_1");

    // answer while offline: kept on this computer
    await user.click(screen.getByText("6"));
    expect(screen.getByText("Saved on this computer")).toBeInTheDocument();

    // connection is back: the next save/heartbeat starts the real attempt with offline_started_at
    s6.startAfterOffline.mockResolvedValue(attempt({ attempt_id: "a_real" }));
    await waitFor(() => expect(api.saveAnswers).toHaveBeenCalledWith("a_real", { q1: 1 }, expect.any(Number)), { timeout: 8000 });
    expect(s6.startAfterOffline).toHaveBeenCalledWith("e_1", "4821", rec.started_at);
    await waitFor(() => expect(screen.queryByTestId("offline-banner")).toBeNull());
    expect(readOfflineRecord("e_1")!.real_attempt_id).toBe("a_real");

    await user.click(screen.getByRole("button", { name: /^Next/ }));
    await user.click(screen.getAllByRole("button", { name: /Review/i })[0]);
    const submits = screen.getAllByRole("button", { name: /^Submit/i });
    await user.click(submits[submits.length - 1]);
    const confirm = screen.getAllByRole("button", { name: /^(Submit|Yes)/i });
    await user.click(confirm[confirm.length - 1]);
    expect(await screen.findByText("done page")).toBeInTheDocument();
    expect(api.submit).toHaveBeenCalledWith("a_real", { q1: 1 }, expect.any(Number));
    expect(localStorage.getItem(offlineRecordKey("e_1"))).toBeNull();
  });

  it("a wrong code cannot open the package", { timeout: 15000 }, async () => {
    await storePackage("4821");
    const user = userEvent.setup();
    api.start.mockRejectedValue(new NetworkError());
    renderStart();
    await typeCode(user, "1111");
    expect(await screen.findByRole("alert", {}, { timeout: 5000 })).toHaveTextContent(/Wrong start code/);
    expect(readOfflineRecord("e_1")).toBeNull();
  });

  it("a package without its key release cannot start offline", async () => {
    await storePackage("4821");
    localStorage.removeItem(keyReleaseKey("e_1"));
    const user = userEvent.setup();
    api.start.mockRejectedValue(new NetworkError());
    renderStart();
    await typeCode(user, "4821");
    expect(await screen.findByRole("alert")).toHaveTextContent(/Cannot reach the server/);
    expect(readOfflineRecord("e_1")).toBeNull();
  });

  it("without a package the network error is shown as before", async () => {
    const user = userEvent.setup();
    api.start.mockRejectedValue(new NetworkError());
    renderStart();
    await typeCode(user, "4821");
    expect(await screen.findByRole("alert")).toHaveTextContent(/Cannot reach the server/);
  });

  it("resumes from the cached attempt view when GET attempt fails with a network error", async () => {
    cacheAttemptView(attempt(), 0);
    api.getAttempt.mockRejectedValue(new NetworkError());
    api.heartbeat.mockRejectedValue(new NetworkError());
    render(
      <MemoryRouter initialEntries={["/student/exam/a_1"]}>
        <Routes>
          <Route path="/student/exam/:attemptId" element={<ExamTakingPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect((await screen.findAllByText(/Question 1 of 2/))[0]).toBeInTheDocument();
    expect(localStorage.getItem(attemptViewKey("a_1"))).not.toBeNull();
  });

  it("shows the error when there is no cached view", async () => {
    api.getAttempt.mockRejectedValue(new NetworkError());
    render(
      <MemoryRouter initialEntries={["/student/exam/a_1"]}>
        <Routes>
          <Route path="/student/exam/:attemptId" element={<ExamTakingPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("Cannot open the exam")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- topics
function TopicHarness({ initial = [] as string[] }) {
  const [v, setV] = useState<string[]>(initial);
  return (
    <>
      <label htmlFor="q1-topics">Topics</label>
      <TopicInput id="q1-topics" value={v} onChange={setV} suggestions={[{ topic: "algebra", question_count: 3 }, { topic: "geometry", question_count: 1 }]} />
      <output data-testid="topics">{v.join("|")}</output>
    </>
  );
}

describe("topic input", () => {
  it("Enter and comma add, Backspace removes, suggestions autocomplete", async () => {
    const user = userEvent.setup();
    render(<TopicHarness />);
    const input = screen.getByLabelText("Topics");
    await user.type(input, "  Linear   Equations {Enter}");
    expect(screen.getByTestId("topics").textContent).toBe("linear equations");
    await user.type(input, "fractions,");
    expect(screen.getByTestId("topics").textContent).toBe("linear equations|fractions");
    await user.type(input, "{Backspace}");
    expect(screen.getByTestId("topics").textContent).toBe("linear equations");
    await user.type(input, "geo");
    expect(screen.getByRole("option", { name: /geometry/ })).toBeInTheDocument();
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByTestId("topics").textContent).toBe("linear equations|geometry");
    await user.click(screen.getByRole("button", { name: "Remove topic geometry" }));
    expect(screen.getByTestId("topics").textContent).toBe("linear equations");
  });

  it("dedupes and caps at five", () => {
    let l: string[] = [];
    for (const t of ["A", "a", "b", "c", "d", "e", "f"]) l = addTopic(l, t);
    expect(l).toEqual(["a", "b", "c", "d", "e"]);
    expect(addTopic(["a"], "   ")).toEqual(["a"]);
  });

  it("the draft requires a topic and sends topics, images and delivery", () => {
    const q = { ...newQuestion("short"), prompt: "Capital?", accepted: "Paris" };
    const d = { ...draftFromExam(), title: "Quiz", questions: [q] };
    expect(validateDraft(d).byKey[q.key]?.topics).toMatch(/topic/);
    const ok = { ...d, delivery: "paper" as const, questions: [{ ...q, topics: ["geography"], images: [{ url: "/api/uploads/abcdefabcdefabcdef", alt: " map " }] }] };
    expect(validateDraft(ok).byKey[q.key]).toBeUndefined();
    const input = toExamInput(ok);
    expect(input.delivery).toBe("paper");
    expect(input.questions[0]).toMatchObject({ topics: ["geography"], images: [{ url: "/api/uploads/abcdefabcdefabcdef", alt: "map" }] });
  });
});

// ---------------------------------------------------------------- images + delivery
describe("question images", () => {
  it("accepts only png/jpeg/gif/webp up to 5 MB", () => {
    expect(imageFileProblem({ name: "a.png", type: "image/png", size: 1000 })).toBeNull();
    expect(imageFileProblem({ name: "a.webp", type: "image/webp", size: 1000 })).toBeNull();
    expect(imageFileProblem({ name: "a.svg", type: "image/svg+xml", size: 10 })).toMatch(/PNG, JPEG, GIF or WebP/);
    expect(imageFileProblem({ name: "a.png", type: "image/png", size: 6 * 1024 * 1024 })).toMatch(/5 MB/);
  });

  it("opens a picture large and closes it with Escape", async () => {
    const user = userEvent.setup();
    render(<QuestionImages images={[{ url: "/api/uploads/x", alt: "Triangle ABC" }]} />);
    await user.click(screen.getByRole("button", { name: /Enlarge picture 1: Triangle ABC/ }));
    expect(screen.getByRole("dialog", { name: "Triangle ABC" })).toBeInTheDocument();
    act(() => {
      fireEvent.keyDown(screen.getByRole("button", { name: "Close" }), { key: "Escape" });
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("delivery cards", () => {
  it("shows three choices and keeps mode in step", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onDelivery = vi.fn();
    render(
      <ExternalExamSettings value={EMPTY_EXTERNAL_SETTINGS} onChange={onChange} delivery="computer" onDeliveryChange={onDelivery} />,
    );
    expect(screen.getByRole("radio", { name: /On computers \(FairPlay app, locked\)/ })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: /On paper/ }));
    expect(onDelivery).toHaveBeenLastCalledWith("paper");
    expect(onChange).not.toHaveBeenCalled(); // still mode "questions"
    await user.click(screen.getByText("On another website"));
    expect(onDelivery).toHaveBeenLastCalledWith("website");
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "external" }));
  });
});
