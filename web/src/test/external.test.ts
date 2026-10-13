import { describe, expect, it } from "vitest";
import {
  EMPTY_EXTERNAL_SETTINGS,
  externalExamInput,
  externalSettingsFrom,
  hostAllowed,
  broadDomainWarnings,
  externalUrlWarning,
  isValidDomain,
  normalizeDomainInput,
  normalizeDomains,
  splitDomainText,
  urlHost,
  validateDomains,
  validateExternalSettings,
  validateExternalUrl,
} from "../components/teacher/externalValidation";

describe("external exam validation", () => {
  it("accepts only full http(s) addresses", () => {
    expect(validateExternalUrl("https://canvas.dku.edu/courses/1/quizzes/2")).toBeNull();
    expect(validateExternalUrl("  http://localhost:3000/form ")).toBeNull();
    expect(validateExternalUrl("")).toMatch(/Paste/);
    expect(validateExternalUrl(null)).toMatch(/Paste/);
    expect(validateExternalUrl("canvas.dku.edu/quiz")).toMatch(/https:\/\//);
    expect(validateExternalUrl("ftp://files.dku.edu")).toMatch(/https:\/\//);
    expect(validateExternalUrl("javascript:alert(1)")).toMatch(/https:\/\//);
    expect(validateExternalUrl("https://" + "a".repeat(2000) + ".com")).toMatch(/too long/);
  });

  it("extracts the host", () => {
    expect(urlHost("https://Canvas.DKU.edu:443/x?y")).toBe("canvas.dku.edu");
    expect(urlHost("nope")).toBeNull();
  });

  it("normalizes typed or pasted website names", () => {
    expect(normalizeDomainInput("  Login.MicrosoftOnline.com ")).toBe("login.microsoftonline.com");
    expect(normalizeDomainInput("https://canvas.dku.edu/courses/1")).toBe("canvas.dku.edu/courses/1"); // path kept = prefix
    expect(normalizeDomainInput("docs.google.com/forms/x")).toBe("docs.google.com/forms/x");
    expect(normalizeDomainInput("https://Docs.Google.com/forms/d/AbC/viewform?usp=1")).toBe("docs.google.com/forms/d/AbC/viewform");
    expect(normalizeDomainInput("*.DKU.edu")).toBe("*.dku.edu");
    expect(normalizeDomainInput("canvas.dku.edu/")).toBe("canvas.dku.edu");
    expect(normalizeDomainInput("localhost:8080")).toBe("localhost");
    expect(normalizeDomainInput("dku.edu.")).toBe("dku.edu");
    expect(normalizeDomains(["dku.edu", "DKU.edu", "", " x.org "])).toEqual(["dku.edu", "x.org"]);
    expect(splitDomainText("a.com\n\n b.org , c.net\n")).toEqual(["a.com", "b.org", "c.net"]);
  });

  it("matches the server's domain rules", () => {
    expect(isValidDomain("canvas.dku.edu")).toBe(true);
    expect(isValidDomain("localhost")).toBe(true);
    expect(isValidDomain("127.0.0.1")).toBe(true);
    expect(isValidDomain("canvas")).toBe(false);
    expect(isValidDomain("bad_host.com")).toBe(false);
    expect(isValidDomain("a..b.com")).toBe(false);
    expect(isValidDomain(".dku.edu")).toBe(false);
    expect(isValidDomain("*.dku.edu")).toBe(true);
    expect(isValidDomain("docs.google.com/forms/d/e/X/")).toBe(true);
    expect(isValidDomain("docs.google.com/a b")).toBe(false);
    expect(validateDomains(["dku.edu", "canvas"])).toMatch(/canvas/);
    expect(validateDomains(["a b.com"])).toMatch(/a b\.com/); // the textarea splits on spaces first
    expect(validateDomains(Array.from({ length: 31 }, (_, i) => `d${i}.edu`))).toMatch(/at most 30/);
    expect(validateDomains([])).toBeNull();
  });

  it("matches exact hosts; subdomains only with *.; paths as prefixes (audit #10)", () => {
    expect(hostAllowed("canvas.dku.edu", "dku.edu")).toBe(false);
    expect(hostAllowed("dku.edu", "dku.edu")).toBe(true);
    expect(hostAllowed("canvas.dku.edu", "*.dku.edu")).toBe(true);
    expect(hostAllowed("dku.edu", "*.dku.edu")).toBe(true);
    expect(hostAllowed("evildku.edu", "*.dku.edu")).toBe(false);
    expect(hostAllowed("docs.google.com", "docs.google.com/forms/d/e/X/", "/forms/d/e/X/viewform")).toBe(true);
    expect(hostAllowed("docs.google.com", "docs.google.com/forms/d/e/X/", "/document/d/Y/edit")).toBe(false);
  });

  it("warns about entries that open far more than the exam", () => {
    expect(broadDomainWarnings(["google.com"])[0]).toMatch(/all of google\.com/);
    expect(broadDomainWarnings(["*.google.com"])[0]).toMatch(/search and AI/);
    expect(broadDomainWarnings(["*.dku.edu"])[0]).toMatch(/sub-site/);
    expect(broadDomainWarnings(["docs.google.com/forms/d/e/X/", "canvas.dku.edu"])).toEqual([]);
    expect(externalUrlWarning("https://forms.gle/abc")).toMatch(/full form address/);
  });

  it("validates whole settings only in external mode", () => {
    expect(validateExternalSettings(EMPTY_EXTERNAL_SETTINGS)).toEqual({});
    const errs = validateExternalSettings({
      mode: "external",
      external_url: "",
      allowed_domains: ["nope"],
      finish_url_contains: "x".repeat(201),
    });
    expect(Object.keys(errs).sort()).toEqual(["allowed_domains", "external_url", "finish_url_contains"]);
    expect(
      validateExternalSettings({
        mode: "external",
        external_url: "https://forms.gle/abc",
        allowed_domains: ["docs.google.com", "accounts.google.com"],
        finish_url_contains: "formResponse",
      }),
    ).toEqual({});
  });

  it("builds the request fields", () => {
    expect(
      externalExamInput({
        mode: "external",
        external_url: " https://canvas.dku.edu/q ",
        allowed_domains: ["https://Login.microsoftonline.com/x", "dku.edu", "dku.edu"],
        finish_url_contains: "  ",
      }),
    ).toEqual({
      mode: "external",
      external_url: "https://canvas.dku.edu/q",
      allowed_domains: ["login.microsoftonline.com/x", "dku.edu"],
      finish_url_contains: null,
    });
    expect(
      externalExamInput({ mode: "questions", external_url: "https://x.org", allowed_domains: ["x.org"], finish_url_contains: "a" }),
    ).toEqual(EMPTY_EXTERNAL_SETTINGS);
    expect(externalSettingsFrom(undefined)).toEqual(EMPTY_EXTERNAL_SETTINGS);
    expect(externalSettingsFrom({ mode: "external", external_url: "https://a.org", allowed_domains: ["b.org"] })).toEqual({
      mode: "external",
      external_url: "https://a.org",
      allowed_domains: ["b.org"],
      finish_url_contains: null,
    });
  });
});
