// Paper (scanned) answers arrive as {"image": url, "mcq": idx|null, ...} instead of a value.
export function paperImage(v: unknown): string | null {
  if (v && typeof v === "object" && "image" in (v as Record<string, unknown>)) {
    const url = (v as Record<string, unknown>).image;
    return typeof url === "string" ? url : null;
  }
  return null;
}

/** The value part of a paper answer (the bubble read for multiple choice), else the answer itself. */
export function paperValue<T>(v: T): T | number | null {
  if (v && typeof v === "object" && "image" in (v as Record<string, unknown>)) {
    const m = (v as Record<string, unknown>).mcq;
    return typeof m === "number" ? m : null;
  }
  return v;
}
