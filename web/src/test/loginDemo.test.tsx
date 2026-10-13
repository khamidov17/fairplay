import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import LoginPage from "../pages/LoginPage";

/** Audit S-01 / W-04: demo passwords only when the server says it is a demo server. */
describe("login page demo hint", () => {
  afterEach(() => vi.unstubAllGlobals());

  function renderWith(health: unknown) {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(health), { status: 200 })));
    render(
      <MemoryRouter>
        <LoginPage role="teacher" />
      </MemoryRouter>,
    );
  }

  it("is hidden on a real server", async () => {
    renderWith({ ok: true, demo_logins: false });
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/health"));
    expect(screen.queryByText(/teacher123/)).toBeNull();
  });

  it("is shown on a dev or demo server", async () => {
    renderWith({ ok: true, demo_logins: true });
    expect(await screen.findByText(/teacher \/ teacher123/)).toBeTruthy();
  });

  it("is hidden when the health check fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    render(
      <MemoryRouter>
        <LoginPage role="student" />
      </MemoryRouter>,
    );
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByText(/alice123/)).toBeNull();
  });
});
