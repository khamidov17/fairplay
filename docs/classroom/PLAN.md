# DKU OJ improvements — build plan (agent team)

Agreed 2026-10-13: we build on top of DKU OJ's code and give the result to DKU for their own use, in collaboration with the DKU OJ team (the professor agreed to collaborate). We do not sell it and we do not rebrand it. The work lives in `classroom/` in this repository for now and goes back to the DKU OJ team as reviewable changes.

Goals: fix every bug from the review; make it subject-aware so that CS teachers keep the full feature set and teachers of other DKU subjects (math, chemistry, biology, languages, economics, ...) get a dashboard with only what fits their subject, based on the DKU curriculum; adding a new subject must be a small, documented step; make it fast enough for large exams.

Branding: keep DKU OJ's name, branding, LICENSE (AGPL-3.0) and history as they are. FairPlay is the name of our separate locked exam app only.

**Later, not now:** the Connector API between the FairPlay exam app and DKU OJ. Keep a clean service boundary.

## Architecture

```
fairplay/
  classroom/   FairPlay Classroom = our fork of DKU OJ (Django 4.2, Jinja2, MySQL/MariaDB, Redis, Celery, DMOJ judge). AGPL-3.0.
  server/      FairPlay exam server (FastAPI). Lockdown sessions, live room, integrity events. Our own code, separate program.
  web/         React UI of the exam server (student exam UI inside the Mac/Windows app, live room).
  mac/ windows/  Locked student apps.
```

- **Two programs, one product.** Classroom is the teacher dashboard (courses, classes, rosters, question bank, assignments, judge, grading, analytics, Canvas export). The exam server runs locked exams. They talk only over the **FairPlay Connector API** (HTTP + scoped tokens). Keep it that way: no shared database, no importing each other's code. This keeps the AGPL code (classroom/) separate from our own code (server/, web/, mac/, windows/).
- **The same Connector API is what we ask DKU OJ to add.** Our classroom/ is the reference implementation; the patch we send DKU is the same code.
- **License.** classroom/ keeps DKU OJ's LICENSE (AGPL-3.0) and a NOTICE crediting DMOJ and DKU OJ. Do not remove copyright headers. Anyone using the hosted Classroom must be able to get its source (add a "Source code" link in the footer).

## Later: the Connector (design notes only, NOT this round)

1. The teacher creates an assignment in Classroom and ticks **"Run in FairPlay"** (`Contest.fairplay_required`).
2. For such an assignment, Classroom hides it and its problems from students on every Classroom page and API (access check, problem access, exports, submission lists, result filters, AC rates, test-case/tester downloads, practice submits, PDF/Word export). Students never sit the exam on Classroom.
3. The FairPlay exam server pulls an **exam package** (questions, options, points, window, roster; no answer keys, no hidden tests) with a scoped service token, caches it before the start.
4. Students take the exam in the locked app against the exam server. Classroom being slow or down does not affect the exam.
5. At hand-in (and as a queue with retries), the exam server **pushes each student's answers** to Classroom as submissions on behalf of that student (marked `source=fairplay`). Classroom grades them (judge, MCQ, AI), and teachers grade/release/export as usual.
6. The exam server reads **release status and released scores** to show students their results only after release. Integrity report: a link from Classroom analytics to the exam server report.

Connector endpoints (Classroom side, all under `/api/fairplay/v1/`, service token with scopes):
- `GET courses`, `GET courses/<id>/classes`, `GET classes/<id>/roster`, `GET classes/<id>/assignments`
- `GET assignments/<key>/package` (fairplay_required only)
- `POST assignments/<key>/submissions` (idempotent by client submission id)
- `GET assignments/<key>/status` and `GET assignments/<key>/scores` (release-aware)
- Token model: `FairPlayServiceToken` (hashed, scopes, course restriction, expiry, last used, audit log).

## Subjects

- `Organization` (course) gets `subject` (cs, math, chem, bio, lang, econ, other). Default cs for existing data.
- A **feature pack registry** decides which menus, question types and tools a course shows: CS = everything DKU OJ has (judge, IDE, programming problems, MOSS, rankings, test data, harness). Non-CS = core types (choice, true/false, fill-blank, short answer, file upload) + course tools, without IDE/judge/rankings/MOSS; math/chem later get the math pack, etc. (see design canvas A1/T3).
- The teacher's home and course pages render per subject (same templates, sections switched by the pack).

## Phases (each ends with tests green and a commit)

- **P0 Import and boot.** Done. See `BASELINE.md`.
- **P1 Fix every review finding.** Done (1826 tests). Independent verification pending.
- **P2 Subjects.** `subject` on courses + a feature-pack registry in ONE place (`judge/subjects/`), so a new subject = one registry entry + optional templates; CS = everything; other subjects = reduced; based on DKU's curriculum; documented in `docs/classroom/ADDING_A_SUBJECT.md` with a worked example.
- **P3 Performance**: web server config (workers, DB driver vs gevent), AI and PDF calls off the request path (Celery), analytics caching, a load test of 300 students joining and submitting.
- **P4 Independent verification** of every phase by a reviewer who did not write the code.

## Rules for every agent

- Work only in your assigned files/area; read `docs/classroom/PLAN.md`, `docs/classroom/REVIEW.md` and `docs/classroom/BASELINE.md` (after P0) first.
- Every fix ships with a test that fails before and passes after. Run the relevant tests; never claim a pass you did not see.
- Keep DMOJ/DKU behaviour unless the change is a bug fix or in the plan.
- No secrets in code. No network calls to CDNs in templates.
- Write a short report at `docs/classroom/reports/<phase>-<area>.md`: what changed, tests run with results, anything left undone or uncertain.
