import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError } from "../../../api/client";
import { scanAiApi, type CaptureInfo } from "../../../api/scanAi";
import "../../../styles/scanAi.css";

interface Shot {
  id: number;
  file: File;
  url: string;
}

let nextId = 1;

/** /teacher/capture/:token — phone page: photograph answer pages and send them (no login). */
export default function CapturePage() {
  const { token = "" } = useParams();
  const [info, setInfo] = useState<CaptureInfo | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [shots, setShots] = useState<Shot[]>([]);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let alive = true;
    scanAiApi.captureInfo(token).then(
      (i) => alive && setInfo(i),
      (e) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 410) setFatal("This link has expired. Open a new one on the computer.");
        else if (e instanceof ApiError && e.status === 404) setFatal("This link is not valid.");
        else setFatal("Cannot reach FairPlay. Check that the phone is on the same network, then reload.");
      },
    );
    return () => {
      alive = false;
    };
  }, [token]);

  useEffect(
    () => () => {
      shots.forEach((s) => URL.revokeObjectURL?.(s.url));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  function add(files: FileList | null) {
    if (!files) return;
    const list = Array.from(files).map((file) => ({ id: nextId++, file, url: URL.createObjectURL?.(file) ?? "" }));
    setShots((s) => [...s, ...list]);
    setError(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  async function send() {
    if (shots.length === 0) return;
    setError(null);
    setProgress(0);
    try {
      const res = await scanAiApi.captureUpload(token, shots.map((s) => s.file), setProgress);
      shots.forEach((s) => URL.revokeObjectURL?.(s.url));
      setShots([]);
      setSent((n) => n + res.photos);
    } catch (e) {
      if (e instanceof ApiError && e.status === 410) setFatal("This link has expired. Open a new one on the computer.");
      else setError(e instanceof ApiError ? e.detail : "Sending failed — check the connection and try again.");
    } finally {
      setProgress(null);
    }
  }

  return (
    <main className="cap-page">
      <header>
        <p className="t-muted sc-small">FairPlay · Scan with your phone</p>
        <h1 style={{ margin: "4px 0", fontSize: 22 }}>{info ? info.exam_title : "Answer sheets"}</h1>
        {info?.course && <p className="t-muted">{info.course}</p>}
      </header>
      {fatal ? (
        <div className="alert alert-error" role="alert">
          {fatal}
        </div>
      ) : !info ? (
        <p className="t-muted">Loading…</p>
      ) : (
        <>
          <p>
            Lay each page flat on a dark surface, fill the screen with it and take the photo. Take all pages, then tap
            Send.
          </p>
          <label className="btn btn-primary cap-shoot">
            Take photo
            <input
              ref={inputRef}
              type="file"
              className="sr-only"
              accept="image/*"
              capture="environment"
              multiple
              onChange={(e) => add(e.target.files)}
              disabled={progress !== null}
            />
          </label>
          {shots.length > 0 && (
            <ul className="cap-thumbs" aria-label="Photos to send">
              {shots.map((s, i) => (
                <li key={s.id}>
                  {s.url ? <img src={s.url} alt={`Photo ${i + 1}`} /> : <span>Photo {i + 1}</span>}
                  <button
                    type="button"
                    aria-label={`Remove photo ${i + 1}`}
                    onClick={() => setShots((list) => list.filter((x) => x.id !== s.id))}
                    disabled={progress !== null}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
          {error && (
            <div className="alert alert-error" role="alert">
              {error}
            </div>
          )}
          {progress !== null && (
            <div className="sc-progress" role="progressbar" aria-label="Sending" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
              <span style={{ width: `${Math.round(progress * 100)}%` }} />
            </div>
          )}
          <button type="button" className="btn btn-primary cap-shoot" disabled={shots.length === 0 || progress !== null} onClick={send}>
            {progress !== null ? "Sending…" : shots.length ? `Send ${shots.length} photo${shots.length === 1 ? "" : "s"}` : "Send"}
          </button>
          {sent > 0 && (
            <div className="alert alert-success" role="status">
              Sent {sent} photo{sent === 1 ? "" : "s"}. They appear on the computer in a moment — take more or close this page.
            </div>
          )}
          {info.unprinted && <p className="t-muted sc-small">These sheets were not printed by FairPlay: you will map pages to students on the computer.</p>}
        </>
      )}
    </main>
  );
}
