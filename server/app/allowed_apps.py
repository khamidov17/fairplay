"""2026-10-14: apps a teacher may allow during an exam.

Every other windowed app is closed when the computer locks (VPN clients are always kept by
the apps themselves). The teacher ticks entries from this short, curated list in the exam
editor; free-form ids are refused so a typo can't open a hole. Each entry names the app's
macOS bundle ids and Windows process names (lowercase, without .exe).

"risky" marks apps that can reach files or the internet on their own (open documents,
web lookups, add-ins). The editor shows a warning next to them.
"""
from __future__ import annotations

CATALOG: dict[str, dict] = {
    "calculator": {"name": "Calculator", "mac": ["com.apple.calculator"], "windows": ["calculatorapp", "calc"],
                   "risky": False},
    "grapher": {"name": "Grapher (Mac)", "mac": ["com.apple.grapher"], "windows": [], "risky": False},
    "textedit": {"name": "TextEdit / Notepad", "mac": ["com.apple.TextEdit"], "windows": ["notepad"],
                 "risky": False},
    "preview": {"name": "Preview (PDF viewer)", "mac": ["com.apple.Preview"], "windows": [], "risky": True},
    "numbers": {"name": "Numbers", "mac": ["com.apple.iWork.Numbers"], "windows": [], "risky": True},
    "pages": {"name": "Pages", "mac": ["com.apple.iWork.Pages"], "windows": [], "risky": True},
    "excel": {"name": "Microsoft Excel", "mac": ["com.microsoft.Excel"], "windows": ["excel"], "risky": True},
    "word": {"name": "Microsoft Word", "mac": ["com.microsoft.Word"], "windows": ["winword"], "risky": True},
    "matlab": {"name": "MATLAB", "mac": ["com.mathworks.matlab"], "windows": ["matlab"], "risky": True},
}

MAX_ALLOWED_APPS = len(CATALOG)


def catalog_out() -> list[dict]:
    """For the exam editor: key, name, risky."""
    return [{"key": k, "name": v["name"], "risky": bool(v["risky"]),
             "mac": bool(v["mac"]), "windows": bool(v["windows"])} for k, v in CATALOG.items()]


def normalize(keys: list[str]) -> list[str]:
    """Known keys only, in catalog order, no duplicates."""
    wanted = {str(k).strip().lower() for k in keys or []}
    unknown = wanted - set(CATALOG)
    if unknown:
        raise ValueError(f"unknown app(s): {', '.join(sorted(unknown))}")
    return [k for k in CATALOG if k in wanted]


def for_student(keys: list[str] | None) -> list[dict]:
    """What the exam page hands to the app: name + the ids each platform matches."""
    out = []
    for k in keys or []:
        v = CATALOG.get(k)
        if v:
            out.append({"key": k, "name": v["name"], "mac": list(v["mac"]), "windows": list(v["windows"])})
    return out
