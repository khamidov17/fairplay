# Agent C: AI features end to end (no real provider)

Branch `worktree-agent-a58d1283380198ac9` (reset to `fairplay-classroom` @ fafbc74). No migrations.

## How the site calls the AI

- `judge/llm/` is the only client. `LLM_PROVIDER='litellm'` (default) uses the `openai` SDK against
  `LITELLM_API_BASE` + `/v1/chat/completions`, with `LITELLM_API_KEY`, `LITELLM_DEFAULT_MODEL`,
  `LITELLM_TIMEOUT` and named `LITELLM_PROFILES` (`grading`, `decompose`, `question_import`, `hint`).
  Azure is the other backend. The base URL was already a setting, so nothing had to be made
  configurable in code.
- Callers: short-answer / AI fill-blank / code-rubric grading (`question_types/ai_grading.call_llm`,
  JSON `sub_scores`), AI hints (`ai_hints.get_hint`), AI difficulty, AI tags, AI decompose
  (`views/problem_create_v2.py`), Excel import formatting (`views/problem_import.py`).
- Entry points: `POST /submission/<id>/ai-grade` (student or teacher), `POST /submission/<id>/ai-hint`,
  `POST /contest/<key>/grade/ai` (Grade page "Batch grading", Celery `batch_ai_grade`), the rejudge
  widget (Celery `ai_rejudge_submission`). "Auto grade after end" (`auto_grade_contest`) never calls
  the LLM (objective types only).

## The stub

`classroom/scripts/ai_stub.py`: a standard-library HTTP server that speaks the OpenAI chat
completions API, recognises the feature from the system prompt, and returns deterministic replies
(each rubric row 75% of its max, a fixed hint, `medium`, a two-row decomposition, ...). Modes, switched
at runtime with `POST /__mode?mode=...`: `ok`, `error` (HTTP 500 whose body carries a fake provider
key and internal URL), `ratelimit` (429), `timeout`, `garbage` (prose instead of JSON), `badjson`
(HTTP 200 HTML body), `empty`, `leak` (feedback quotes the reference / accepted answers), `partial`
(one rubric row at full marks), `full`. `GET /__log` shows what the site sent.

Dev wiring (env only): `dmoj/local_settings.example.py` now reads `CLASSROOM_LLM_API_BASE`,
`CLASSROOM_LLM_API_KEY`, `CLASSROOM_LLM_MODEL`, `CLASSROOM_LLM_TIMEOUT` (never applied to test runs),
and `CLASSROOM_CELERY_BROKER_URL` so parallel dev Celery workers do not take each other's tasks.
Copy the example to `local_settings.py` again to get them.

## What was tested in the browser (Playwright, dev server :8103, Celery worker, stub)

| Flow | Result |
|---|---|
| Teacher, question editor for `cs_short_mutable`: AI Feedback on, AI difficulty (Medium), Auto-generate grading table (2 rows), rubric, save | pass; config stored |
| Authoring helpers with stub error / garbage / badjson / empty | 503 "Could not reach the AI service", no 500, no provider detail |
| Teacher, assignment editor: Allow AI Hint, Allow AI Feedback, auto grade delay | saved (after renaming seed keys, see below) |
| Student: submit short answer, "View AI Feedback": per-row feedback, counter 5 -> 4 | pass |
| Student, stub error / garbage / badjson / empty / ratelimit / timeout | **was** 200 "ok", page reloads with no message, rate slot used up (5 failures = locked out for 5 min). **Now** 503 "The AI grader is unavailable right now. Your answer is saved; ...", slot refunded, answer left for manual grading, no provider text |
| Student, stub `leak` | **was** reference answer shown as feedback. **Now** "Feedback withheld: it quoted confidential grading material." |
| Student, stub `partial` | 5/10, not 10/10 (AI-1 fix holds over HTTP) |
| Teacher, Grade page "Batch grading" ok, then outage | ok: graded=3; outage: **was** reported like success and wiped earlier AI scores to 0. **Now** scores kept, `GET grade/ai` gives `failed`, the page shows "Last AI run: N submission(s) not graded" |
| Teacher save + Release All on an AI-graded submission | pass |
| Workspace AI hint (homework allows hints): hint shown, counter 5 -> 4, outage message | pass; counter said "remaining today", now "remaining this window" |
| Exam with hidden results, hints and AI feedback allowed | no hint tab, hint POST 403, AI-grade POST 403 with a correct reason |
| Auto grade after end (delay 0, contest ending in 2 min) | ran at end time in Celery, MCQ regraded, short answer untouched, zero LLM calls |

## Fixes (all with tests)

1. **AI failure on demand counted as success.** `run_ai_for_submission` now returns `ai-unavailable`
   when the grader asked the model and got nothing usable (short answer and AI fill-blank set
   `submission._ai_unavailable`). The view answers 503 with a plain message and refunds the global quota
   and the per-problem window slot (`ai_grading_quota.refund_request`). Teachers get the same message.
2. **An outage wiped a stored AI score.** `_keeps_stored_ai_score` keeps the stored AC/WA AI result when
   the AI run failed (re-run button, batch re-run).
3. **Batch AI grading hid outages.** `batch_ai_grade` counts graded / failed (skips are neither),
   stores them (`record_batch_ai_outcome`), `GET /contest/<key>/grade/ai` returns them and the Grade
   page shows a warning.
4. **Feedback could leak the answer key.** `parse_calibrated_scores(..., secrets=, public_text=)`
   withholds row feedback that quotes the reference answer, accepted blank answers or the rubric
   (whole match for short secrets, any 5-token run for long ones, CJK per character; text that is in
   the question or the student's own answer does not count). Score is kept. Used by short answer, fill
   blank and code rubric.
5. **AI grading offered while results are hidden.** `student_can_trigger_ai` refuses when
   `results_visible_to` is false (same rule as AI-4 for hints), so the model is not called and the button
   is not shown.
6. **Non-JSON 200 from the gateway** made the backend raise `AttributeError` instead of `LLMError`
   (litellm and azure). Fixed.
7. Hint counter wording after a hint ("remaining today") in `status.html` and `workspace.js`.
8. The AI-grade 403 always said "only your latest submission"; it now gives the real reason
   (question type, hidden results, not latest, not graded yet, not enabled).

## Tests

New `judge/views/tests/test_ai_stub_e2e.py` (21 tests) runs the real `judge.llm` backend against
`ai_stub.py` on a free port: every failure mode for AI grade, hint, difficulty, decompose and the bare
backend; refunds; score kept on failed re-run; leak withheld (short answer and fill blank); hidden exam;
batch counts and page warning; hint wording; leak detector unit tests.

- Before the fix (fix files restored to HEAD, tests kept): 13 failures, 7 errors out of 21. The 6 that
  pass on both versions are guards (ok path, partial, hint ok/failures, authoring helpers).
- After: `test_ai_stub_e2e test_ai_tag_and_grading test_contest_ai_grading test_ai_grading_quota
  test_ai_grading_dual_role test_ai_hints test_contest_grade_variants judge.llm.tests.test_llm
  judge.models.tests.test_question`: 306 tests OK. `test_assignment_draft test_exam_contest judge.tests
  test_click_through_fixes test_contest_grade_fixes test_fill_blank test_mcq test_p1_gap_fixes
  test_problem_create_v2 test_problem_workspace test_problem_import`: 407 tests OK (two `judge.tests`
  failures from my first fill-blank change were fixed). `test_deploy_security`: OK. flake8 clean.

## Left / uncertain

- **Seed data:** `seed_dev_data` keys (`cs101_hw1`, ...) fail the model validator `^[a-z0-9]+$`, so
  saving any seeded assignment in the editor returns 400 "Contest id must be ^[a-z0-9]+$". I renamed
  the keys in my own DB only; the seed should use valid keys (other agents' scripts use the old ones).
- **Timeouts block a web worker:** `LITELLM_TIMEOUT` defaults to 120 s and the SDK retries once, so a
  hung provider holds a student's "View AI Feedback" or hint request for up to ~4 minutes (measured 11 s
  with a 5 s timeout). Lower the timeout or move these calls to Celery (P3).
- `call_llm`'s 429/5xx retry never fires: `LLMError` carries only the exception class name, not the
  status code. Left as is (enabling it would add up to 31 s on the request path).
- AI difficulty: an unusable reply silently falls back to the length heuristic and is shown as
  "AI suggest". Minor.
- Students see AI feedback text but not the AI number until release (by design in the code). The
  workspace hint tab needs a selected submission (`?submission=`) or a fresh submit; on a plain reopen
  it says "Submit a solution first".
- AI fill-blank on outage still scores by exact match (by design); the student now also gets the
  "unavailable" message and no slot is used.
- I flushed the shared Redis cache DB 0 once while resetting rate limits (cache only, no data).
- Browser scripts are in my scratchpad (`lib.js`, `t1_editor.js` ... `t6_auto.js`), not committed.
