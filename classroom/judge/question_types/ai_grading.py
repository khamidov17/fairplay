"""Shared LLM-grading helpers for the AI-graded question types.

Short-answer, fill-in-the-blank and (supplementary) code AI grading all need the same plumbing:
send a prompt with retry on transient errors, strip markdown fences, parse the JSON response and
calibrate each sub-score.  This module keeps that logic in one place so the per-type code only
provides the prompt and the sub-question table.
"""
import json
import logging
import time

from judge.utils.calibration import calibrate

__all__ = ['call_llm', 'parse_calibrated_scores', 'sub_score_cases']

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


def parse_calibrated_scores(raw, sub_questions, *, log_prefix=''):
    """Parse an LLM JSON reply and calibrate each sub-score.

    *sub_questions* is the ordered list of dicts ``{name, max_points, strictness}`` sent to the
    model; the reply's ``sub_scores`` are matched to them **by index** (not name, which the model
    may rephrase).  Returns a list of ``{name, max_points, score, feedback}`` dicts, or ``None``
    when the reply is missing/empty (caller decides whether that means "fall back" or "error").
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('%sLLM returned non-JSON: %.200s', log_prefix, (raw or '')[:200])
        return None

    sub_scores = parsed.get('sub_scores', [])
    if not isinstance(sub_scores, list) or not sub_scores:
        return None

    calibrated = []
    for i, item in enumerate(sub_scores):
        try:
            raw_score = float(item.get('score', 0))
        except (ValueError, TypeError):
            logger.warning('%snon-numeric score from LLM: %r', log_prefix, item.get('score'))
            raw_score = 0.0
        name = str(item.get('name', ''))[:30]
        if i < len(sub_questions):
            max_pts = sub_questions[i]['max_points']
            strictness = sub_questions[i]['strictness']
        else:
            logger.warning(
                '%sLLM returned more sub_scores (%d) than sub_questions (%d); '
                'extra item "%s" defaulting max_pts=1.0.',
                log_prefix, len(sub_scores), len(sub_questions), name)
            max_pts = 1.0
            strictness = 'normal'

        cal_score = calibrate(raw_score, max_pts, strictness)
        calibrated.append({
            'name': name,
            'max_points': max_pts,
            'score': cal_score,
            'feedback': str(item.get('feedback', ''))[:2000],
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
