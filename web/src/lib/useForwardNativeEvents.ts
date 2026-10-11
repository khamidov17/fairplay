// Sends integrity events from the Mac app (lock started, blocked shortcuts, ...) to the
// server for pages that are not the exam page itself (website exams, their finish page).
import { useEffect } from "react";
import { studentApi } from "../api/client";
import type { IntegrityEvent } from "../api/types";
import * as bridge from "./bridge";

const RETRY_MS = 3000;

export function useForwardNativeEvents(attemptId: string): void {
  useEffect(() => {
    if (!attemptId) return;
    let alive = true;
    let queue: IntegrityEvent[] = [];
    let sending = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const flush = async () => {
      if (!alive || sending || queue.length === 0) return;
      sending = true;
      const batch = queue;
      queue = [];
      try {
        await studentApi.events(attemptId, batch);
      } catch {
        queue = [...batch, ...queue];
        if (alive) timer = setTimeout(() => void flush(), RETRY_MS);
      } finally {
        sending = false;
      }
    };

    const off = bridge.onNative((msg) => {
      if (msg.type !== "event" || !msg.event) return;
      queue.push({
        type: String(msg.event.type),
        detail: String(msg.event.detail ?? ""),
        client_time: msg.event.client_time ?? new Date().toISOString(),
      });
      void flush();
    });
    return () => {
      alive = false;
      off();
      if (timer) clearTimeout(timer);
      void (queue.length ? studentApi.events(attemptId, queue).catch(() => undefined) : undefined);
    };
  }, [attemptId]);
}
