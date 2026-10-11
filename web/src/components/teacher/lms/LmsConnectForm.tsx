import { useEffect, useState, type FormEvent } from "react";
import { lmsApi } from "../../../api/client";
import type { LmsConnection, LmsProvider, LmsProviderId } from "../../../api/types";
import { errorText } from "../util";
import "./lms.css";

/** Step-by-step token help per LMS (plain language). */
export function TokenHelp({ provider }: { provider: LmsProvider }) {
  return (
    <details className="lms-help">
      <summary>How to get a token</summary>
      {provider.id === "canvas" ? (
        <>
          <ol>
            <li>Sign in to Canvas.</li>
            <li>
              Open <strong>Account → Settings</strong>.
            </li>
            <li>
              Under “Approved integrations”, press <strong>+ New access token</strong>.
            </li>
            <li>Purpose: “FairPlay”. Leave the expiry date empty or pick the end of term.</li>
            <li>Copy the token and paste it here. Canvas shows it only once.</li>
          </ol>
          <p>
            Your Canvas admin can later replace personal tokens with a developer key or LTI 1.3, so teachers
            do not need to copy tokens.
          </p>
        </>
      ) : (
        <p>{provider.token_help}</p>
      )}
    </details>
  );
}

export function LmsConnectForm({
  providers,
  onConnected,
  onCancel,
}: {
  providers: LmsProvider[];
  onConnected: (c: LmsConnection) => void;
  onCancel?: () => void;
}) {
  const [providerId, setProviderId] = useState<LmsProviderId>("canvas");
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (providers.length && !providers.some((p) => p.id === providerId)) setProviderId(providers[0].id);
  }, [providers, providerId]);

  const provider = providers.find((p) => p.id === providerId);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!baseUrl.trim() || !token.trim()) {
      setError("Enter the web address and the token.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const c = await lmsApi.connect({ provider: providerId, base_url: baseUrl.trim(), token: token.trim(), label: label.trim() });
      setToken("");
      onConnected(c);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="lms-form" onSubmit={submit} aria-label="Connect an LMS">
      <div className="lms-providers" role="group" aria-label="Which LMS does your school use?">
        {providers.map((p) => (
          <button
            key={p.id}
            type="button"
            className="lms-provider"
            aria-pressed={p.id === providerId}
            onClick={() => {
              setProviderId(p.id);
              setError(null);
            }}
          >
            <span className="lms-provider-name">{p.name}</span>
            <span className={`t-pill ${p.status === "ready" ? "green" : "orange"}`}>
              {p.status === "ready" ? "Ready" : "Beta"}
            </span>
          </button>
        ))}
      </div>

      {provider && provider.status === "beta" && (
        <p className="lms-note">
          {provider.name} support is in beta: it follows the published API but has not been tested with a real{" "}
          {provider.name} site yet.
        </p>
      )}

      <div className="t-field">
        <label htmlFor="lms-url">{provider?.name ?? "LMS"} web address</label>
        <input
          id="lms-url"
          className="input"
          type="url"
          inputMode="url"
          autoComplete="off"
          placeholder={provider?.base_url_hint || "https://"}
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
        />
      </div>
      <div className="t-field">
        <label htmlFor="lms-token">Access token</label>
        <input
          id="lms-token"
          className="input"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <span className="lms-note">
          The token is kept on the FairPlay server only. Nobody can see it again, not even you.
        </span>
      </div>
      {provider && <TokenHelp provider={provider} />}
      <div className="t-field">
        <label htmlFor="lms-label">Name (optional)</label>
        <input
          id="lms-label"
          className="input"
          placeholder="e.g. DKU Canvas"
          maxLength={120}
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </div>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      <div className="lms-actions">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Checking the token…" : `Connect ${provider?.name ?? ""}`.trim()}
        </button>
        {onCancel && (
          <button type="button" className="t-btn-outline" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
