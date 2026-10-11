import { describe, expect, it } from "vitest";
import { ago, clockOffset, formatCountdown, remainingMs } from "../lib/timer";

describe("timer math", () => {
  it("computes the server clock offset", () => {
    const client = Date.parse("2026-10-11T10:00:00Z");
    expect(clockOffset("2026-10-11T10:00:05Z", client)).toBe(5000);
    expect(clockOffset("2026-10-11T09:59:58Z", client)).toBe(-2000);
    expect(clockOffset("garbage", client)).toBe(0);
  });

  it("remaining time uses the offset and never goes negative", () => {
    const client = Date.parse("2026-10-11T10:00:00Z");
    const deadline = "2026-10-11T10:30:00Z";
    expect(remainingMs(deadline, 0, client)).toBe(30 * 60 * 1000);
    // server is 1 min ahead -> 1 min less left
    expect(remainingMs(deadline, 60_000, client)).toBe(29 * 60 * 1000);
    expect(remainingMs(deadline, 0, Date.parse("2026-10-11T11:00:00Z"))).toBe(0);
  });

  it("formats the countdown", () => {
    expect(formatCountdown(0)).toBe("0:00");
    expect(formatCountdown(999)).toBe("0:01");
    expect(formatCountdown(65_000)).toBe("1:05");
    expect(formatCountdown(30 * 60_000)).toBe("30:00");
    expect(formatCountdown(3_725_000)).toBe("1:02:05");
    expect(formatCountdown(-5)).toBe("0:00");
  });

  it("formats heartbeat age", () => {
    const now = Date.parse("2026-10-11T10:00:00Z");
    expect(ago(null, now)).toBe("never");
    expect(ago("2026-10-11T09:59:57Z", now)).toBe("3 s ago");
    expect(ago("2026-10-11T09:55:00Z", now)).toBe("5 min ago");
  });
});
