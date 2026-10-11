import { useState } from "react";
import { saveBlob, scanAiApi } from "../../../api/scanAi";
import { errorText, plural } from "../../../components/teacher/util";

/** "Export CSV" / "Export Excel": approved and automatic grades only; warns about blanks. */
export function ExportButtons({ examId }: { examId: string }) {
  const [busy, setBusy] = useState<"csv" | "xlsx" | null>(null);
  const [msg, setMsg] = useState<{ tone: "warn" | "error"; text: string } | null>(null);

  async function run(fmt: "csv" | "xlsx") {
    setBusy(fmt);
    setMsg(null);
    try {
      const out = await scanAiApi.exportGrades(examId, fmt);
      saveBlob(out.blob, out.filename);
      if (out.ungraded > 0)
        setMsg({ tone: "warn", text: `${plural(out.ungraded, "answer is", "answers are")} not graded yet — left blank in the file.` });
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <span className="ex-buttons">
      <button type="button" className="t-btn-outline" disabled={busy !== null} onClick={() => run("csv")}>
        {busy === "csv" ? "Exporting…" : "Export CSV"}
      </button>
      <button type="button" className="t-btn-outline" disabled={busy !== null} onClick={() => run("xlsx")}>
        {busy === "xlsx" ? "Exporting…" : "Export Excel"}
      </button>
      {msg && (
        <span className={msg.tone === "error" ? "t-inline-error" : "sc-low"} role={msg.tone === "error" ? "alert" : "status"}>
          {msg.text}
        </span>
      )}
    </span>
  );
}
