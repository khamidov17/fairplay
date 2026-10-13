/** MathLive (MIT) loads only in real browsers; jsdom tests keep the LaTeX textarea. */

export function mathLiveSupported() {
  return typeof window !== 'undefined'
    && !!window.customElements
    && typeof navigator !== 'undefined'
    && !/jsdom/i.test(navigator.userAgent || '');
}

let loading = null;

export function loadMathLive() {
  if (!mathLiveSupported()) return Promise.resolve(null);
  if (loading) return loading;
  loading = import('mathlive').then((mod) => {
    const MFE = mod.MathfieldElement;
    if (MFE) {
      // katex.min.css already on editor pages; skip MathLive CDN/font fetches.
      MFE.fontsDirectory = null;
      MFE.soundsDirectory = null;
    }
    return MFE || null;
  }).catch(() => null);
  return loading;
}

export function hideVirtualKeyboard() {
  const kb = typeof window !== 'undefined' ? window.mathVirtualKeyboard : null;
  if (kb) {
    try {
      kb.visible = false;
      if (typeof kb.hide === 'function') kb.hide();
      kb.container = null;
    } catch (e) { /* ignore */ }
  }
  if (typeof document === 'undefined') return;
  document.querySelectorAll('math-virtual-keyboard').forEach((el) => {
    el.hidden = true;
    el.style.display = 'none';
    el.style.pointerEvents = 'none';
  });
}
