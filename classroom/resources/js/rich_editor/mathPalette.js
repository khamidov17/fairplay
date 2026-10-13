/**
 * Canvas LMS RCE Equation Editor symbol boards.
 * Ported from packages/canvas-rce/.../EquationEditorToolbar/buttons.js
 * https://github.com/instructure/canvas-lms (AGPL-3.0)
 *
 * `latex` is MathLive insert() input (Canvas `command`, including \placeholder{}).
 * `advanced` is what Canvas types into the LaTeX textarea (`advancedCommand`).
 * `icon` is KaTeX used only for the button face (Canvas `svgCommand`).
 */

import katex from 'katex';

function t(msgid) {
  return (typeof gettext === 'function') ? gettext(msgid) : msgid;
}

const BASIC = [
  { face: 'x\u2B1A', icon: 'x_{\\square}', latex: '_{\\placeholder{}}', advanced: '_{}', title: 'Subscript' },
  { face: 'x\u2B1A', icon: 'x^{\\square}', latex: '^{\\placeholder{}}', advanced: '^{}', title: 'Superscript' },
  { face: '\u2B1A/\u2B1A', icon: '\\frac{n}{m}', latex: '\\frac{\\placeholder{}}{\\placeholder{}}', advanced: '\\frac{ }{ }', title: 'Fraction' },
  { face: '\u221A', icon: '\\sqrt{\\square}', latex: '\\sqrt{\\placeholder{}}', advanced: '\\sqrt{ }', title: 'Square Root' },
  { face: '\u221An', icon: '\\sqrt[n]{\\square}', latex: '\\sqrt[\\placeholder{}]{\\placeholder{}}', advanced: '\\sqrt[ ]{ }', title: 'N-th Root' },
  { face: '\u27E8', latex: '\\langle', title: 'Left Angle Bracket' },
  { face: '\u27E9', latex: '\\rangle', title: 'Right Angle Bracket' },
  { face: 'C(n,m)', icon: '\\binom{n}{m}', latex: '\\binom{\\placeholder{}}{\\placeholder{}}', advanced: '\\binom{ }{ }', title: 'Binomial Coefficient' },
  { face: 'f', latex: 'f', title: 'F (function)' },
  { face: '\u2032', latex: '\\prime', title: 'Prime' },
  { face: '+', latex: '+', title: 'Plus' },
  { face: '\u2212', latex: '-', title: 'Minus' },
  { face: '\u00B1', latex: '\\pm', title: 'Plus/Minus' },
  { face: '\u2213', latex: '\\mp', title: 'Minus/Plus' },
  { face: '\u00B7', latex: '\\cdot', title: 'Centered Dot' },
  { face: '=', latex: '=', title: 'Equals Sign' },
  { face: '\u00D7', latex: '\\times', title: 'Multiplication Sign' },
  { face: '\u00F7', latex: '\\div', title: 'Division Sign' },
  { face: '*', latex: '\\ast', title: 'Asterisk' },
  { face: '\u2234', latex: '\\therefore', title: 'Therefore' },
  { face: '\u2235', latex: '\\because', title: 'Because' },
  { face: '\u2211', icon: '\\sum', latex: '\\sum_{\\placeholder{}}^{\\placeholder{}}', advanced: '\\sum_{ }^{ }', title: 'Sum' },
  { face: '\u220F', icon: '\\prod', latex: '\\prod_{\\placeholder{}}^{\\placeholder{}}', advanced: '\\prod_{ }^{ }', title: 'Product' },
  { face: '\u2210', icon: '\\coprod', latex: '\\coprod_{\\placeholder{}}^{\\placeholder{}}', advanced: '\\coprod_{ }^{ }', title: 'Coproduct' },
  { face: '\u222B', icon: '\\int_{a}^{b}', latex: '\\int_{\\placeholder{}}^{\\placeholder{}}', advanced: '\\int_{ }^{ }', title: 'Definite Integral' },
  { face: '\u222B', icon: '\\int', latex: '\\int', title: 'Indefinite Integral' },
  { face: '\u2115', latex: '\\mathbb{N}', title: 'Natural Numbers' },
  { face: '\u2119', latex: '\\mathbb{P}', title: 'Prime Numbers' },
  { face: '\u2124', latex: '\\mathbb{Z}', title: 'Integers' },
  { face: '\u211A', latex: '\\mathbb{Q}', title: 'Rational Numbers' },
  { face: '\u211D', latex: '\\mathbb{R}', title: 'Real Numbers' },
  { face: '\u2102', latex: '\\mathbb{C}', title: 'Complex Numbers' },
  { face: '\u210D', latex: '\\mathbb{H}', title: 'Quaternions' },
  { face: 'x\u0305', icon: '\\overline{x}', latex: '\\overline{\\placeholder{}}', advanced: '\\overline{ }', title: 'Bar' },
  { face: 'x\u0302', icon: '\\hat{x}', latex: '\\hat{\\placeholder{}}', advanced: '\\hat{ }', title: 'Hat' },
  { face: 'v\u20D7', icon: '\\vec{v}', latex: '\\vec{\\placeholder{}}', advanced: '\\vec{ }', title: 'Vector (Notation)' },
];

const GREEK = [
  ['\u03B1', '\\alpha', 'Alpha'], ['\u03B2', '\\beta', 'Beta'], ['\u03B3', '\\gamma', 'Gamma'],
  ['\u03B4', '\\delta', 'Delta'], ['\u03B5', '\\epsilon', 'Epsilon'], ['\u03B6', '\\zeta', 'Zeta'],
  ['\u03B7', '\\eta', 'Eta'], ['\u03B8', '\\theta', 'Theta'], ['\u03B9', '\\iota', 'Iota'],
  ['\u03BA', '\\kappa', 'Kappa'], ['\u03BB', '\\lambda', 'Lambda'], ['\u03BC', '\\mu', 'Mu'],
  ['\u03BD', '\\nu', 'Nu'], ['\u03BE', '\\xi', 'Xi'], ['\u03C0', '\\pi', 'Pi'],
  ['\u03C1', '\\rho', 'Rho'], ['\u03C3', '\\sigma', 'Sigma'], ['\u03C4', '\\tau', 'Tau'],
  ['\u03C5', '\\upsilon', 'Upsilon'], ['\u03C6', '\\phi', 'Phi'], ['\u03C7', '\\chi', 'Chi'],
  ['\u03C8', '\\psi', 'Psi'], ['\u03C9', '\\omega', 'Omega'], ['\u03DD', '\\digamma', 'Digamma'],
  ['\u03B5', '\\varepsilon', 'Epsilon (Variant)'], ['\u03D1', '\\vartheta', 'Theta (Variant)'],
  ['\u03F0', '\\varkappa', 'Kappa (Variant)'], ['\u03D6', '\\varpi', 'Pi (Variant)'],
  ['\u03F1', '\\varrho', 'Rho (Variant)'], ['\u03C2', '\\varsigma', 'Sigma (Variant)'],
  ['\u03C6', '\\varphi', 'Phi (Variant)'], ['\u0393', '\\Gamma', 'Uppercase Gamma'],
  ['\u0394', '\\Delta', 'Uppercase Delta'], ['\u0398', '\\Theta', 'Uppercase Theta'],
  ['\u039B', '\\Lambda', 'Uppercase Lambda'], ['\u039E', '\\Xi', 'Uppercase Xi'],
  ['\u03A0', '\\Pi', 'Uppercase Pi'], ['\u03A3', '\\Sigma', 'Uppercase Sigma'],
  ['\u03A5', '\\Upsilon', 'Uppercase Upsilon'], ['\u03A6', '\\Phi', 'Uppercase Phi'],
  ['\u03A8', '\\Psi', 'Uppercase Psi'], ['\u03A9', '\\Omega', 'Uppercase Omega'],
].map(([face, latex, title]) => ({ face, latex, title }));

const OPERATORS = [
  ['\u2227', '\\wedge', 'And'], ['\u2228', '\\vee', 'Or'], ['\u222A', '\\cup', 'Union'],
  ['\u2229', '\\cap', 'Intersection'], ['\u22C4', '\\diamond', 'Diamond'],
  ['\u25B3', '\\bigtriangleup', 'Upward Pointing Triangle'],
  ['\u2296', '\\ominus', 'Encircled Minus'], ['\u228E', '\\uplus', 'Disjoint Union'],
  ['\u2297', '\\otimes', 'Encircled Times'], ['\u2295', '\\oplus', 'Encircled Plus'],
  ['\u25BD', '\\bigtriangledown', 'Downward Pointing Triangle'],
  ['\u2293', '\\sqcap', 'Square Cap'], ['\u25C1', '\\triangleleft', 'Leftward Pointing Triangle'],
  ['\u2294', '\\sqcup', 'Square Cup'], ['\u25B7', '\\triangleright', 'Rightward Pointing Triangle'],
  ['\u2299', '\\odot', 'Encircled Dot'], ['\u25EF', '\\bigcirc', 'Big Circle'],
  ['\u2020', '\\dagger', 'Dagger'], ['\u2021', '\\ddagger', 'Double Dagger'],
  ['\u2240', '\\wr', 'Wreath Product'], ['\u2A3F', '\\amalg', 'Amalg (Coproduct)'],
].map(([face, latex, title]) => ({ face, latex, title }));

const RELATIONSHIPS = [
  ['<', '<', 'Less Than'], ['>', '>', 'Greater Than'],
  ['\u2261', '\\equiv', 'Equivalent (Identity)'], ['\u2245', '\\cong', 'Congruent'],
  ['\u223C', '\\sim', 'Equivalence Class'], ['\u2209', '\\notin', 'Not In (Not An Element Of)'],
  ['\u2260', '\\ne', 'Not Equal'], ['\u221D', '\\propto', 'Proportional'],
  ['\u2248', '\\approx', 'Approximately'], ['\u2264', '\\le', 'Less Than Or Equal'],
  ['\u2265', '\\ge', 'Greater Than Or Equal'], ['\u2208', '\\in', 'In (Element Of)'],
  ['\u220B', '\\ni', 'Contains'], ['\u2282', '\\subset', 'Subset (Strict)'],
  ['\u2283', '\\supset', 'Superset (Strict)'], ['\u2284', '\\not\\subset', 'Not Subset (Strict)'],
  ['\u2285', '\\not\\supset', 'Not Superset (Strict)'], ['\u2286', '\\subseteq', 'Subset'],
  ['\u2287', '\\supseteq', 'Superset'], ['\u2288', '\\not\\subseteq', 'Not Subset'],
  ['\u2289', '\\not\\supseteq', 'Not Superset'], ['\u22A8', '\\models', 'Inference'],
  ['\u227A', '\\prec', 'Precedes'], ['\u227B', '\\succ', 'Succeeds'],
  ['\u227C', '\\preceq', 'Precedes Equal'], ['\u227D', '\\succeq', 'Succeeds Equal'],
  ['\u2243', '\\simeq', 'Group Isomorphism'], ['|', '\\mid', 'Vertical Bar (Set Builder Notation)'],
  ['\u226A', '\\ll', 'Nested Less Than'], ['\u226B', '\\gg', 'Nested Greater Than'],
  ['\u2225', '\\parallel', 'Parallel'], ['\u22C8', '\\bowtie', 'Bowtie'],
  ['\u228F', '\\sqsubset', 'Square Subset (Strict)'], ['\u2290', '\\sqsupset', 'Square Superset (Strict)'],
  ['\u2323', '\\smile', 'Cup Product'], ['\u2291', '\\sqsubseteq', 'Square Subset'],
  ['\u2292', '\\sqsupseteq', 'Square Superset'], ['\u2250', '\\doteq', 'Approaches the Limit'],
  ['\u2322', '\\frown', 'Cap Product'], ['\u22A2', '\\vdash', 'Turnstile (Yields)'],
  ['\u22A3', '\\dashv', 'Reverse Turnstile (Does Not Yield)'],
  ['\u2203', '\\exists', 'Exists'], ['\u2205', '\\varnothing', 'Empty Set'],
].map(([face, latex, title]) => ({ face, latex, title }));

const ARROWS = [
  ['\u27F5', '\\longleftarrow', 'Left Arrow'], ['\u27F6', '\\longrightarrow', 'Right Arrow'],
  ['\u27F8', '\\Longleftarrow', 'Thick Left Arrow'], ['\u27F9', '\\Longrightarrow', 'Thick Right Arrow'],
  ['\u27F7', '\\longleftrightarrow', 'Logical Equivalence'],
  ['\u2195', '\\updownarrow', 'Upward And Downward Pointing Arrow'],
  ['\u27FA', '\\Longleftrightarrow', 'Logical Equivalence (Thick)'],
  ['\u21D5', '\\Updownarrow', 'Upward And Downward Pointing Arrow (Thick)'],
  ['\u21A6', '\\mapsto', 'Maps To'], ['\u2197', '\\nearrow', 'Up And Right Diagonal Arrow'],
  ['\u21A9', '\\hookleftarrow', 'Left Arrow With Hook'],
  ['\u21AA', '\\hookrightarrow', 'Right Arrow With Hook'],
  ['\u2198', '\\searrow', 'Down And Right Diagonal Arrow'],
  ['\u21BC', '\\leftharpoonup', 'Left Upward Harpoon Arrow'],
  ['\u21C0', '\\rightharpoonup', 'Right Upward Harpoon Arrow'],
  ['\u2199', '\\swarrow', 'Down And Left Diagonal Arrow'],
  ['\u21BD', '\\leftharpoondown', 'Left Downard Harpoon Arrow'],
  ['\u21C1', '\\rightharpoondown', 'Right Downward Harpoon Arrow'],
  ['\u2196', '\\nwarrow', 'Up And Left Diagonal Arrow'],
  ['\u2193', '\\downarrow', 'Downward Arrow'], ['\u21D3', '\\Downarrow', 'Thick Downward Arrow'],
  ['\u2191', '\\uparrow', 'Upward Arrow'], ['\u21D1', '\\Uparrow', 'Thick Upward Arrow'],
  ['\u2192', '\\rightarrow', 'Rightward Arrow'], ['\u21D2', '\\Rightarrow', 'Thick Rightward Arrow'],
  ['\u2190', '\\leftarrow', 'Leftward Arrow'], ['\u21D0', '\\Leftarrow', 'Thick Leftward Arrow'],
  ['\u2194', '\\leftrightarrow', 'Logical Equivalence (Short)'],
  ['\u21D4', '\\Leftrightarrow', 'Logical Equivalence (Short And Thick)'],
  ['\u21CC', '\\rightleftharpoons', 'Equilibrium'],
].map(([face, latex, title]) => ({ face, latex, title }));

const DELIMITERS = [
  ['\u230A', '\\lfloor', 'Left Floor'], ['\u230B', '\\rfloor', 'Right Floor'],
  ['\u2308', '\\lceil', 'Left Ceiling'], ['\u2309', '\\rceil', 'Right Ceiling'],
  ['/', '/', 'Forward Slash'],
  ['{', '\\lbrace', 'Left Curly Brace'], ['}', '\\rbrace', 'Right Curly Brace'],
].map(([face, latex, title]) => ({ face, latex, title }));

const MISC = [
  ['\u2200', '\\forall', 'For All'], ['\u2026', '\\ldots', 'Low Horizontal Dots'],
  ['\u22EF', '\\cdots', 'Centered Horizontal Dots'], ['\u22EE', '\\vdots', 'Vertical Dots'],
  ['\u22F1', '\\ddots', 'Diagonal Dots'], ['\u221A', '\\surd', 'Square Root Symbol'],
  ['\u25B3', '\\triangle', 'Triangle'], ['\u2113', '\\ell', 'Script L'],
  ['\u22A4', '\\top', 'Top'], ['\u266D', '\\flat', 'Flat (Music)'],
  ['\u266E', '\\natural', 'Natural (Music)'], ['\u266F', '\\sharp', 'Sharp (Music)'],
  ['\u2118', '\\wp', 'Power Set'], ['\u22A5', '\\bot', 'Bottom'],
  ['\u2663', '\\clubsuit', 'Clubs (Suit)'], ['\u2662', '\\diamondsuit', 'Diamonds (Suit)'],
  ['\u2661', '\\heartsuit', 'Hearts (Suit)'], ['\u2660', '\\spadesuit', 'Spades (Suit)'],
  ['\\', '\\backslash', 'Backslash'], ['|', '\\vert', 'Vertical Bar (Set Builder Notation)'],
  ['\u27C2', '\\perp', 'Perpendicular'], ['\u2207', '\\nabla', 'Nabla'],
  ['\u210F', '\\hbar', 'H Bar'], ['\u2218', '\\circ', 'Open Circle'],
  ['\u2022', '\\bullet', 'Solid Circle'], ['\u2216', '\\setminus', 'Set Minus'],
  ['\u00AC', '\\neg', 'Not (Negation)'], ['Re', '\\Re', 'Real Portion (of Complex Number)'],
  ['Im', '\\Im', 'Imaginary Portion (of Complex Number)'],
  ['\u2202', '\\partial', 'Partial (Derivative)'], ['\u221E', '\\infty', 'Infinity'],
  ['\u2135', '\\aleph', 'Aleph'], ['\u00B0', '^\\circ', 'Degree Symbol'],
  ['\u2220', '\\angle', 'Angle'],
].map(([face, latex, title]) => ({ face, latex, title }));

export const MATH_PALETTE = [
  { id: 'basic', label: 'Basic', keys: BASIC },
  { id: 'greek', label: 'Greek', keys: GREEK },
  { id: 'operators', label: 'Operators', keys: OPERATORS },
  { id: 'relationships', label: 'Relationships', keys: RELATIONSHIPS },
  { id: 'arrows', label: 'Arrows', keys: ARROWS },
  { id: 'delimiters', label: 'Delimiters', keys: DELIMITERS },
  { id: 'misc', label: 'Misc', keys: MISC },
];

export function renderMathPalette() {
  const tabs = MATH_PALETTE.map((g, i) => (
    `<button type="button" class="rte-math-tab${i === 0 ? ' is-active' : ''}" data-math-tab="${g.id}">${t(g.label)}</button>`
  )).join('');
  const panels = MATH_PALETTE.map((g, i) => {
    const keys = g.keys.map((k) => {
      const adv = k.advanced ? ` data-math-advanced="${escapeAttr(k.advanced)}"` : '';
      const face = renderFace(k);
      const icon = face.includes('katex') ? ' rte-math-key--icon' : '';
      return `<button type="button" class="rte-math-key${icon}" data-math-tex="${escapeAttr(k.latex)}"${adv} title="${escapeAttr(k.title || k.face)}">${face}</button>`;
    }).join('');
    return `<div class="rte-math-keys" data-math-panel="${g.id}"${i === 0 ? '' : ' hidden'}>${keys}</div>`;
  }).join('');
  return `<div class="rte-math-palette"><div class="rte-math-tabs">${tabs}</div>${panels}</div>`;
}

function iconTex(k) {
  if (k.icon) return k.icon;
  if (k.latex && !k.latex.includes('placeholder')) return k.latex;
  return '';
}

function renderFace(k) {
  const tex = iconTex(k);
  if (tex) {
    try {
      const html = katex.renderToString(tex, { throwOnError: false, displayMode: false });
      if (html && !html.includes('katex-error')) return html;
    } catch (e) { /* fall back to the unicode face */ }
  }
  return escapeHtml(k.face || '');
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
}

function escapeAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}
