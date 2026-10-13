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
 * Normalize one allowed-website entry (audit 2026-10-12 #10, same rules as the server):
 * an EXACT host ("canvas.dku.edu"), "*.host" for the host and every subdomain, optionally
 * with a path prefix ("docs.google.com/forms/d/e/1FAIpQL/"). Accepts a pasted address
 * (scheme, query and port are dropped); the host is lowercased, the path keeps its case.
 * Does not validate.
 */
export function normalizeDomainInput(raw: string): string {
  let s = raw.trim();
  if (!s) return "";
  s = s.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
  s = s.split(/[?#]/)[0] ?? "";
  const slash = s.indexOf("/");
  let host = slash === -1 ? s : s.slice(0, slash);
  let path = slash === -1 ? "" : s.slice(slash);
  const wildcard = host.startsWith("*.");
  if (wildcard) host = host.slice(2);
  host = host.toLowerCase().replace(/:\d*$/, "").replace(/\.+$/, "");
  if (path === "/") path = "";
  return (wildcard ? "*." : "") + host + path;
}

/** Parts of a normalized entry. */
export function parseEntry(entry: string): { wildcard: boolean; host: string; path: string } {
  const wildcard = entry.startsWith("*.");
  const rest = wildcard ? entry.slice(2) : entry;
  const slash = rest.indexOf("/");
  return { wildcard, host: slash === -1 ? rest : rest.slice(0, slash), path: slash === -1 ? "" : rest.slice(slash) };
}

const PATH_RE = /^\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$/;

/** True when the (already normalized) entry is a valid allowed website on the server. */
export function isValidDomain(entry: string): boolean {
  const { host, path } = parseEntry(entry);
  const hostOk = host.length > 0 && host.length <= 253 && DOMAIN_RE.test(host) && (host.includes(".") || host === "localhost");
  return hostOk && (!path || (path.length <= 500 && PATH_RE.test(path)));
}

/**
 * Hosts that serve many unrelated things: allowing the whole host (or a wildcard over a
 * big company) lets students reach search, AI assistants or any shared document.
 */
const BROAD = [
  "google.com", "docs.google.com", "drive.google.com", "sites.google.com", "googleusercontent.com",
  "microsoft.com", "office.com", "live.com", "sharepoint.com", "bing.com", "github.com",
  "notion.so", "dropbox.com", "wikipedia.org", "youtube.com", "openai.com", "chatgpt.com",
  "baidu.com", "qq.com", "yandex.ru", "apple.com", "icloud.com", "amazon.com",
];

/** Warning texts for entries that open far more than the exam (shown, not blocking). */
export function broadDomainWarnings(entries: string[]): string[] {
  const out: string[] = [];
  for (const e of normalizeDomains(entries)) {
    const { wildcard, host, path } = parseEntry(e);
    const broad = BROAD.some((b) => host === b || host.endsWith("." + b));
    if (wildcard && broad) out.push(`${e} opens every ${host} site, including search and AI tools.`);
    else if (broad && !path)
      out.push(`${e} opens all of ${host}. Add the exact path instead, e.g. ${host}/forms/d/e/…/`);
    else if (wildcard) out.push(`${e} also opens every sub-site of ${host}.`);
  }
  return out;
}

/** Warning for an exam address that will not work as the only allowed page. */
export function externalUrlWarning(url: string | null | undefined): string | null {
  const host = urlHost(url);
  if (host === "forms.gle") return "Short forms.gle links redirect to docs.google.com: paste the full form address.";
  return null;
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
  if (bad.length === 1) return `“${bad[0]}” is not a website like canvas.dku.edu or *.dku.edu.`;
  if (bad.length > 1) return `These are not websites like canvas.dku.edu or *.dku.edu: ${bad.join(", ")}.`;
  if (list.length > MAX_ALLOWED_DOMAINS) return `Add at most ${MAX_ALLOWED_DOMAINS} websites.`;
  return null;
}

/**
 * True when the URL parts are allowed by one entry (the same rule the Mac app applies):
 * exact host, or any subdomain for "*.host"; with a path, the URL path must start with it.
 */
export function hostAllowed(host: string, entry: string, urlPath = "/"): boolean {
  const { wildcard, host: d, path } = parseEntry(entry);
  const h = host.toLowerCase();
  const dd = d.toLowerCase();
  if (!(h === dd || (wildcard && h.endsWith("." + dd)))) return false;
  return !path || (urlPath || "/").startsWith(path);
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


// ---------- audit W-09: describe allowed sites exactly as the server and app apply them ----------

/** Hosts where the exam address opens only its own folder (server services.BROAD_HOSTS). */
export const SERVER_BROAD_HOSTS = new Set([
  "docs.google.com", "drive.google.com", "sites.google.com", "www.google.com", "google.com",
  "forms.office.com", "forms.microsoft.com", "onedrive.live.com", "www.notion.so", "notion.so",
  "github.com", "gist.github.com", "www.dropbox.com", "dropbox.com", "sharepoint.com",
  "www.bing.com", "bing.com",
]);

/** The entry the exam address always gets (server services.start_url_entry). */
export function startUrlEntry(url: string | null | undefined): string | null {
  if (!url) return null;
  let u: URL;
  try {
    u = new URL(url.trim());
  } catch {
    return null;
  }
  const host = u.hostname.toLowerCase();
  if (!host) return null;
  if (!SERVER_BROAD_HOSTS.has(host) && !host.endsWith(".sharepoint.com")) return host;
  const path = u.pathname || "/";
  const folder = path.slice(0, path.lastIndexOf("/") + 1) || "/";
  return folder === "/" ? host : host + folder;
}

/** What the students' app will allow: the exam address entry first, then the teacher's list. */
export function effectiveAllowedSites(url: string | null | undefined, entries: string[]): string[] {
  const start = startUrlEntry(url);
  const list = normalizeDomains(entries);
  return start && !list.includes(start) ? [start, ...list] : list;
}

/** One line in plain words for an entry, per parseEntry. */
export function describeEntry(entry: string): string {
  const { wildcard, host, path } = parseEntry(entry);
  if (path) return `${host}${path} — only pages under this folder`;
  if (wildcard) return `${host} and every site under it (*.${host})`;
  return `${host} — this site only, not its sub-sites`;
}
