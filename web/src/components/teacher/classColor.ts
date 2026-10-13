// Class card colours (white text on each passes WCAG AA for large text).
const COLORS = ["#3b5bdb", "#2b8a3e", "#1864ab", "#5f3dc4", "#a61e4d", "#c2410c", "#0b7285", "#495057"];

export function classColor(id: number): string {
  return COLORS[Math.abs(id) % COLORS.length] ?? COLORS[0]!;
}

/** The link a student opens to join (signed in or not). */
export function joinLink(code: string): string {
  const clean = code.replace(/[^A-Za-z0-9]/g, "");
  return `${window.location.origin}/student/join/${encodeURIComponent(clean)}`;
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
