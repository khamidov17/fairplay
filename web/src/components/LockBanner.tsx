import { isNative } from "../lib/bridge";

/** Slim yellow strip shown when the student UI runs in a plain browser (not the Mac app). */
export function NotLockedBanner() {
  if (isNative()) return null;
  return (
    <div className="banner-yellow" role="status">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="5" y="11" width="14" height="10" rx="2" />
        <path d="M8 11V7a4 4 0 0 1 7.5-2" />
      </svg>
      <span>Not in locked mode. Use the FairPlay Mac app for real exams.</span>
    </div>
  );
}
