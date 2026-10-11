// Slice 3: client-side checks for external-website exams. Mirrors the server rules in
// server/app/schemas.py (ExamIn / normalize_domain) so teachers see problems before saving.
import type { ExamMode } from "../../api/types";

export const MAX_EXTERNAL_URL = 2000;
export const MAX_ALLOWED_DOMAINS = 30;
export const MAX_FINISH_CONTAINS = 200;

/** Same pattern as the server: letters, digits, hyphens, dot-separated labels. */
const DOMAIN_RE = /^[a-z0-9-]+(\.[a-z0-9-]+)*$/;

export interface ExternalSettingsValue {
  mode: ExamMode;
  external_url: string | null;
  allowed_domains: string[];
  finish_url_contains: string | null;
}

export type ExternalSettingsErrors = Partial<Record<"external_url" | "allowed_domains" | "finish_url_contains", string>>;

export const EMPTY_EXTERNAL_SETTINGS: ExternalSettingsValue = {
  mode: "questions",
  external_url: null,
  allowed_domains: [],
  finish_url_contains: null,
};

/** Lowercase host of an http(s) URL, or null if it is not one. */
export function urlHost(url: string | null | undefined): string | null {
  const s = (url ?? "").trim();
  if (!/^https?:\/\//i.test(s)) return null;
  try {
    const u = new URL(s);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.hostname ? u.hostname.toLowerCase().replace(/^\[|\]$/g, "") : null;
  } catch {
    return null;
  }
}

/** Error text for the exam website address, or null when fine. */
export function validateExternalUrl(url: string | null | undefined): string | null {
  const s = (url ?? "").trim();
  if (!s) return "Paste the address of the exam website.";
  if (s.length > MAX_EXTERNAL_URL) return `The address is too long (max ${MAX_EXTERNAL_URL} characters).`;
  if (!/^https?:\/\//i.test(s)) return "Use the full address, starting with https://";
  if (!urlHost(s)) return "This does not look like a website address.";
  return null;
}

/**
 * Turn what a teacher typed into a bare hostname: trims, lowercases, accepts a pasted
 * address ("https://canvas.dku.edu/courses/1" -> "canvas.dku.edu"), drops a leading
 * "*." and a port or path. Does not validate.
 */
export function normalizeDomainInput(raw: string): string {
  let s = raw.trim().toLowerCase();
  if (!s) return "";
  if (/^[a-z][a-z0-9+.-]*:\/\//.test(s)) {
    const host = urlHost(s);
    if (host) return host;
    s = s.replace(/^[a-z][a-z0-9+.-]*:\/\//, "");
  }
  s = s.split(/[/?#]/)[0];
  s = s.replace(/:\d*$/, "");
  s = s.replace(/^\*\./, "");
  return s.replace(/\.+$/, "");
}

/** True when the (already normalized) entry is a valid allowed website on the server. */
export function isValidDomain(d: string): boolean {
  return d.length > 0 && d.length <= 253 && DOMAIN_RE.test(d) && (d.includes(".") || d === "localhost");
}

/** Split textarea text (one per line; commas and spaces also work) into raw entries. */
export function splitDomainText(text: string): string[] {
  return text
    .split(/[\n,;\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

/** Normalized, de-duplicated list (blank entries dropped), order kept. */
export function normalizeDomains(entries: string[]): string[] {
  const out: string[] = [];
  for (const e of entries) {
    const d = normalizeDomainInput(e);
    if (d && !out.includes(d)) out.push(d);
  }
  return out;
}

/** Error text for the allowed-websites list, or null when fine. */
export function validateDomains(entries: string[]): string | null {
  const list = normalizeDomains(entries);
  const bad = list.filter((d) => !isValidDomain(d));
  if (bad.length === 1) return `“${bad[0]}” is not a website name like canvas.dku.edu.`;
  if (bad.length > 1) return `These are not website names like canvas.dku.edu: ${bad.join(", ")}.`;
  if (list.length > MAX_ALLOWED_DOMAINS) return `Add at most ${MAX_ALLOWED_DOMAINS} websites.`;
  return null;
}

/** True when `host` is allowed by `domain` (the domain itself or any subdomain). */
export function hostAllowed(host: string, domain: string): boolean {
  const h = host.toLowerCase();
  const d = domain.toLowerCase();
  return h === d || h.endsWith("." + d);
}

/** All errors for the settings; {} when valid (and always {} for "questions" mode). */
export function validateExternalSettings(v: ExternalSettingsValue): ExternalSettingsErrors {
  if (v.mode !== "external") return {};
  const errors: ExternalSettingsErrors = {};
  const urlErr = validateExternalUrl(v.external_url);
  if (urlErr) errors.external_url = urlErr;
  const domErr = validateDomains(v.allowed_domains);
  if (domErr) errors.allowed_domains = domErr;
  if ((v.finish_url_contains ?? "").length > MAX_FINISH_CONTAINS)
    errors.finish_url_contains = `Keep this under ${MAX_FINISH_CONTAINS} characters.`;
  return errors;
}

/**
 * Fields to send in POST/PUT /api/exams (CONTRACT.md slice 3). For "questions" mode the
 * external fields are null/[] (the caller sends its questions); for "external" mode the
 * caller must send `questions: []`.
 */
export function externalExamInput(v: ExternalSettingsValue): ExternalSettingsValue {
  if (v.mode !== "external") return { ...EMPTY_EXTERNAL_SETTINGS };
  const finish = (v.finish_url_contains ?? "").trim();
  return {
    mode: "external",
    external_url: (v.external_url ?? "").trim() || null,
    allowed_domains: normalizeDomains(v.allowed_domains),
    finish_url_contains: finish || null,
  };
}

/** Settings from an Exam (or anything with the optional slice-3 fields). */
export function externalSettingsFrom(e: Partial<ExternalSettingsValue> | null | undefined): ExternalSettingsValue {
  return {
    mode: e?.mode === "external" ? "external" : "questions",
    external_url: e?.external_url ?? null,
    allowed_domains: [...(e?.allowed_domains ?? [])],
    finish_url_contains: e?.finish_url_contains ?? null,
  };
}
