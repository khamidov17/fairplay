"""Shared LLM-grading helpers for the AI-graded question types.

Short-answer, fill-in-the-blank and (supplementary) code AI grading all need the same plumbing:
send a prompt with retry on transient errors, strip markdown fences, parse the JSON response and
calibrate each sub-score.  This module keeps that logic in one place so the per-type code only
provides the prompt and the sub-question table.
"""
import json
import logging
import re
import time

from django.utils.translation import gettext_lazy as _

from judge.utils.calibration import calibrate

__all__ = ['call_llm', 'feedback_leaks', 'parse_calibrated_scores', 'sub_score_cases', 'WITHHELD_FEEDBACK']

logger = logging.getLogger(__name__)

# Transient status codes that warrant a retry (429 rate-limit, 5xx server errors).
_TRANSIENT_CODES = (429, 500, 502, 503, 504)
_RETRY_DELAYS = (1, 5, 25)


def call_llm(llm, messages, *, max_tokens=2048):
    """Send *messages* with retry on transient errors, returning the stripped text.

    Exponential back-off 1s -> 5s -> 25s (max 3 retries).  Markdown code fences are stripped so a
    model that wraps its JSON in ``` blocks still parses.  Returns ``None`` when every attempt
    fails or the error is not transient.
    """
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            result = llm.chat(
                messages, profile='grading',
                max_tokens=max_tokens, temperature=0,
                response_format={'type': 'json_object'},
            )
            text = result.text.strip()
            if text.startswith('```'):
                lines = text.split('\n')
                text = '\n'.join(lines[1:]) if len(lines) > 1 else text
                if text.endswith('```'):
                    text = text.rsplit('```', 1)[0]
            return text.strip()
        except Exception as exc:
            logger.warning('LLM attempt %d failed', attempt + 1, exc_info=True)
            if attempt < len(_RETRY_DELAYS):
                msg = str(exc).lower()
                if any(str(code) in msg for code in _TRANSIENT_CODES):
                    time.sleep(_RETRY_DELAYS[attempt])
                    continue
            return None
    return None


def _norm_name(name):
    return ' '.join(str(name or '').split()).casefold()[:30]


def _match_sub_scores(sub_scores, sub_questions, log_prefix):
    """Map the model's ``sub_scores`` onto configured rubric rows.

    Returns ``{row_index: item}``. The configured table is the authority:

    * When every returned item names a configured row, items are matched **by name**
      (whitespace/case-insensitive); a second item for an already matched row is a
      duplicate and is dropped.
    * Otherwise items are matched **by position**. Items past the end of the table are
      ignored (never added to the total).

    Rows the model did not return are simply absent from the map; the caller scores
    them 0.
    """
    items = [item for item in sub_scores if isinstance(item, dict)]
    by_name = {}
    for idx, sq in enumerate(sub_questions):
        by_name.setdefault(_norm_name(sq.get('name')), []).append(idx)

    matched = {}
    names = [_norm_name(item.get('name')) for item in items]
    if items and all(n in by_name for n in names):
        free = {n: list(idxs) for n, idxs in by_name.items()}
        for item, n in zip(items, names):
            if free[n]:
                matched[free[n].pop(0)] = item
            else:
                logger.warning('%sLLM returned a duplicate sub_score for "%s"; ignored.', log_prefix, n)
        return matched

    for idx, item in enumerate(items):
        if idx < len(sub_questions):
            matched[idx] = item
        else:
            logger.warning(
                '%sLLM returned more sub_scores (%d) than rubric rows (%d); extra item ignored.',
                log_prefix, len(items), len(sub_questions))
            break
    return matched


# A CJK character is one token (those scripts have no spaces); other words are runs of letters/digits.
_CJK = '぀-ヿ㐀-䶿一-鿿豈-﫿가-힯'
_TOKEN_RE = re.compile(f'[{_CJK}]|[^\\W_{_CJK}]+')
_TAG_RE = re.compile(r'<[^>]+>')
# A verbatim run this long from a long secret counts as quoting it.
_LEAK_RUN = 5

WITHHELD_FEEDBACK = _('Feedback withheld: it quoted confidential grading material.')


def _tokens(text):
    return _TOKEN_RE.findall(_TAG_RE.sub(' ', str(text or '')).casefold())


def _joined(tokens):
    return ' ' + ' '.join(tokens) + ' '


def feedback_leaks(feedback, secrets, public_text=''):
    """Whether model *feedback* quotes any of *secrets* (reference answer, accepted blank answers,
    rubric) verbatim.

    Matching is on word tokens (case, punctuation and markup ignored). A short secret (up to
    ``_LEAK_RUN`` tokens) leaks when it appears whole; a longer one when any ``_LEAK_RUN``-token run
    of it appears. Runs that also occur in *public_text* (the question and the student's own
    answer) are not secret and never count.
    """
    fb = _joined(_tokens(feedback))
    if not fb.strip():
        return False
    public = _joined(_tokens(public_text))
    for secret in secrets:
        sec = _tokens(secret)
        if not sec:
            continue
        if len(sec) <= _LEAK_RUN:
            runs = [sec]
        else:
            runs = [sec[i:i + _LEAK_RUN] for i in range(len(sec) - _LEAK_RUN + 1)]
        for run in runs:
            needle = _joined(run)
            if needle in fb and needle not in public:
                return True
    return False


def parse_calibrated_scores(raw, sub_questions, *, log_prefix='', secrets=(), public_text=''):
    """Parse an LLM JSON reply and calibrate each sub-score.

    *sub_questions* is the ordered list of dicts ``{name, max_points, strictness}`` sent to the
    model. The result always has **exactly one item per configured row**, in table order, so the
    caller's total (``sum(max_points)``) is the configured rubric total: a row the model left out
    scores 0, unknown/extra rows are ignored and duplicates are collapsed (see
    ``_match_sub_scores``). Returns a list of ``{name, max_points, score, feedback}`` dicts, or
    ``None`` when the reply is missing/empty or matches no row (caller decides whether that means
    "fall back" or "error").

    Feedback that quotes any of *secrets* (see ``feedback_leaks``) is replaced with
    ``WITHHELD_FEEDBACK``; the score is kept. The prompt forbids quoting them, but the student sees
    this text, so it is checked here rather than trusted.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('%sLLM returned non-JSON: %.200s', log_prefix, (raw or '')[:200])
        return None

    if not isinstance(parsed, dict):
        return None
    sub_scores = parsed.get('sub_scores', [])
    if not isinstance(sub_scores, list) or not sub_scores or not sub_questions:
        return None

    matched = _match_sub_scores(sub_scores, sub_questions, log_prefix)
    if not matched:
        return None
    if len(matched) < len(sub_questions):
        logger.warning('%sLLM scored %d of %d rubric rows; missing rows score 0.',
                       log_prefix, len(matched), len(sub_questions))

    calibrated = []
    for idx, sq in enumerate(sub_questions):
        max_pts = sq['max_points']
        item = matched.get(idx)
        if item is None:
            calibrated.append({
                'name': str(sq.get('name', ''))[:30],
                'max_points': max_pts,
                'score': 0.0,
                'feedback': '',
            })
            continue
        try:
            raw_score = float(item.get('score', 0))
        except (ValueError, TypeError):
            logger.warning('%snon-numeric score from LLM: %r', log_prefix, item.get('score'))
            raw_score = 0.0
        if raw_score != raw_score or raw_score in (float('inf'), float('-inf')):
            raw_score = 0.0
        name = str(item.get('name', '') or sq.get('name', ''))[:30]
        cal_score = calibrate(raw_score, max_pts, sq.get('strictness', 'normal'))
        feedback = str(item.get('feedback', ''))[:2000]
        if secrets and feedback_leaks(feedback, secrets, public_text):
            logger.warning('%sLLM feedback for row %d quoted grading material; withheld.', log_prefix, idx + 1)
            feedback = str(WITHHELD_FEEDBACK)
        calibrated.append({
            'name': name,
            'max_points': max_pts,
            'score': cal_score,
            'feedback': feedback,
        })
    return calibrated


def sub_score_cases(calibrated):
    """Convert calibrated ``{name, max_points, score, feedback}`` items into SubmissionTestCase
    dicts (one per sub-question) so the status page renders a grading table."""
    cases = []
    for i, item in enumerate(calibrated):
        cases.append({
            'case': i + 1,
            'status': 'AC' if item['score'] >= item['max_points'] else 'WA',
            'points': item['score'],
            'total': item['max_points'],
            'feedback': item['name'],
            'extended_feedback': item.get('feedback', ''),
        })
    return cases
