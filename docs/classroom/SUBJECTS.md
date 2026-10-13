# DKU OJ subjects

DKU OJ started as a tool for computer science courses. This page lists the DKU subject areas DKU OJ now supports, the DKU course-code prefixes that map to each one, and the features that make sense for each. The code that implements it is in `classroom/judge/subjects/` (see `ADDING_A_SUBJECT.md`).

Researched on 2026-10-13 from DKU's public pages (sources at the end). I list a course-code prefix only when I saw it on a DKU page. Unconfirmed prefixes are listed separately at the end, for the DKU OJ team to check.

## What DKU teaches (summary of the sources)

- **Majors** (ugstudies.dukekunshan.edu.cn, [1]):
  - Applied Mathematics and Computational Sciences (tracks: Computer Science, Mathematics).
  - Arts and Media (Arts, Media).
  - Behavioral Science (Economics, Psychology, Neuroscience).
  - Computation and Design (Computer Science, Digital Media, Social Policy).
  - Cultures and Societies (Cultural Anthropology, Sociology).
  - Data Science.
  - Environmental Science (Biogeochemistry, Biology, Chemistry, Public Policy).
  - Global China Studies.
  - Global Health (Biology, Public Policy).
  - Humanities (Creative Writing and Translation, Literature, Philosophy and Religion, World History).
  - Materials Science (Chemistry, Physics).
  - Molecular Bioscience (Biogeochemistry, Biophysics, Cell and Molecular Biology, Genetics and Genomics, Neuroscience).
  - Philosophy, Politics, and Economics (Economic History, Philosophy, Political Science, Public Policy).
  - Quantitative Political Economy (Economics, Political Science, Public Policy).
- **Divisions:** Natural and Applied Sciences (DNAS) [2][3][4][5], Arts and Humanities [6], Social Sciences [7][8].
- **Common Core** (12 credits) [1]: CCORE 101 China in the World, CCORE 201 Global Challenges in Science, Technology, and Health, CCORE 202 Ethics, Citizenship and the Examined Life. The DNAS four-year plans also list these as GCHINA 101, GLOCHALL 201 and ETHLDR 201 [2][3][4][5].
- **Language requirement** (8 to 16 credits) [1]:
  - English for Academic Purposes (EAP 101A to 102B).
  - Chinese as a Second Language: the CHIN courses. They are taught by the Language and Culture Center (LCC), which also runs Written and Oral Communication (WOC) and "Additional Languages" [9][10].
  - CSL focuses on conversation and speaking. It runs from CHIN 101 Beginning Chinese through CHIN 402, with heritage-learner sections (CHIN 131/132) and specialty courses CHIN 411 to 422 (film, literature, professional) [9].
- **Integrated science:** BIOL 110, CHEM 110 and PHYS 121 are "Integrated Science", and INTGSCI 205 also appears [2][5].
- **Mainland students:** CHSC 101/102 are Ministry of Education courses for students from Mainland China, Hong Kong, Macau and Taiwan [3].

## Feature packs

A **feature pack** is a named group of capabilities. A subject is a list of packs. Every subject gets `core`.

| Pack | Status | What it switches on |
|---|---|---|
| `core` | live | Question types: choice (single and multiple), true/false, fill in the blank, short answer (AI-graded), file upload (manual grading), composite (the editor shows it, but it cannot be saved yet). Also Excel import, assignments, grading, analytics. Every subject has it. |
| `code` | live | Programming problems, the judge, the IDE and Run tests, test data, the code harness, MOSS, rankings (the user ranking list and the contest scoreboard for students), and submissions-by-language statistics. |
| `math` | placeholder | Planned: formula input, numeric answers with tolerance and units, a math keyboard. |
| `lab` | placeholder | Planned: lab reports, data tables and plots, units and significant figures. |
| `writing` | placeholder | Planned: essays with rubrics, drafts and feedback rounds. |
| `language` | placeholder | Planned: listening and speaking (audio answers), character and pinyin input, vocabulary drills. |
| `chem` | placeholder, with a course-page section | Planned: chemical formulas and equations, structure drawing, lab safety checklists. This is the worked example in `ADDING_A_SUBJECT.md`. |

A placeholder pack has no capabilities yet. Subjects can already list it, so when someone builds the feature, every subject that should get it gets it with no further change.

## Subjects

| Key | Label | DKU prefixes (verified) | Packs (core always included) | Why |
|---|---|---|---|---|
| `cs` | Computer science | COMPSCI [2][3], CS [3][4], INFOSCI [4][5], COMPDSGN [4] | code, math | Today's DKU OJ, unchanged. The migration default for existing courses. |
| `math` | Mathematics and statistics | MATH [2][3], STATS [2][3], STAT [3][5] | math | Proofs and calculations: choice, numeric fill-in, short answer and uploaded handwritten work now; formula input later. |
| `phys` | Physics | PHYS [2][5] | math, lab | Numeric problems with units; lab reports. |
| `chem` | Chemistry | CHEM [2][5] | chem, math, lab | As physics, plus chemistry-specific input (the worked example). |
| `bio` | Biology and life sciences | BIOL [2][5] | lab | Concept checks, labelled diagrams (file upload for now), lab reports. |
| `envsci` | Environmental science | ENVIR [5] | lab, math | Field and lab data, quantitative questions. |
| `econ` | Economics | ECON [2][11] | math | Quantitative problem sets, graphs. |
| `socsci` | Social and behavioral sciences | PSYCH [12], SOSC [2] | writing | Concept checks, short essays, data interpretation. |
| `humanities` | Arts and humanities | none verified yet | writing | Essays, close reading, file upload of creative work. Teachers pick it on the course form. |
| `lang` | Languages (Chinese, EAP) | CHIN [9], EAP [1][2][3] | language, writing | Vocabulary and grammar items now; audio and character input later. |
| `general` | Common Core and general education | CCORE [1], GCHINA [2][3], GLOCHALL [2][3], ETHLDR [3][4], CHSC [3] | writing | Readings, reflections and short essays. |
| `other` | Other | (none) | (core only) | Anything else. Also used when a course holds a subject key that no longer exists. |

### Matching rules

- **Detecting the subject.** When a course is created with "Detect from course code" selected, DKU OJ reads the letters at the start of the course code (`Organization.short_name`, then the name) and looks them up in the table above. A prefix it does not recognise gets `cs`, today's behaviour. The teacher can change the subject on the course form, and an administrator can change it in the admin.
- **Existing courses** become `cs` through the migration, so nothing changes for them.

## Who sees what

- **Course pages.** On a course page, a class, an assignment, or a problem attached to courses, a user sees the features of those courses. A teacher of `COMPSCI 201` and `BIOL 110` sees the IDE and programming tools in the CS course and not in the biology course.
- **Site-wide pages.** On pages outside a course (the navigation bar, the problem list, the IDE page), a user sees the union of the features of all their courses. Their courses are the ones where they are a member, class member, course admin, course instructor or class admin.
- **Users with no course.** Anonymous visitors and new accounts see CS features, so the public site is unchanged.
- **Site-wide course editors** (superusers and holders of `judge.edit_all_organization`) see everything. Site teachers are given `is_staff`, so `is_staff` on its own does not count.
- **The programming-problem rule applies to everyone.** Even a site editor cannot attach a programming problem to a course whose subject has no code pack. The rule protects the course.

What a non-code course does not show, and what is refused on the server:

| Feature | Hidden in | Refused on the server |
|---|---|---|
| Programming question type | create v2 type picker; problem list "Add" menu and format filter; problem workspace format filter | create v2 save (403), Excel import create (400) |
| IDE | navigation entry (`/ide`), course and teacher-home links | `/ide/` (404), IDE run (403) |
| Judge status, runtimes, language statistics | navigation entries (`/status`, `/runtimes`, `/stats/language`) | not refused |
| MOSS | contest tab | MOSS page (404) |
| Rankings | contest tab for students (teachers keep it), `/users` navigation entry | not refused (the scoreboard is still reachable by URL) |
| "Hide IDE / Run Test output", "Run pretests only" | assignment editor | not needed (unchecked switches send `false`) |

## Prefixes to confirm with the DKU OJ team

I believe these prefixes exist, but I could not find them on a DKU page. They are not mapped, so courses using them get `cs` until someone picks a subject or adds the prefix to `definitions.py`:

- POLSCI, SOCIOL, ANTHRO, PUBPOL (social sciences)
- GLHLTH (global health)
- NEUROSCI
- MATSCI (materials science)
- ARTS, MEDIA, HIST, LIT, PHIL, RELIG (arts and humanities)
- WOC (written and oral communication)
- INTGSCI (integrated science, interdisciplinary), DKU 101, CAPSTONE

## Sources

1. DKU Undergraduate Studies, Majors: https://ugstudies.dukekunshan.edu.cn/academics/majors
2. DNAS, Data Science four-year plan: https://dnas.dukekunshan.edu.cn/?p=4182
3. DNAS, Applied Mathematics and Computational Sciences (CS track) four-year plan: https://dnas.dukekunshan.edu.cn/?p=4185
4. DNAS, Computation and Design four-year plan: https://dnas.dukekunshan.edu.cn/?p=4196
5. DNAS, Environmental Science (Biogeochemistry) four-year plan (PDF): https://dnas.dukekunshan.edu.cn/wp-content/uploads/2024/04/4-Year-Planning-ENV-SCI-BIOGEOCHEM-Even-year-by-Chuanhui-Gu-1.pdf
6. Division of Arts and Humanities, Majors: https://ah.dukekunshan.edu.cn/majors/
7. Division of Social Sciences, About: https://social-science.dukekunshan.edu.cn/about/
8. Division of Social Sciences, Majors: https://social-science.dukekunshan.edu.cn//majors/
9. Language and Culture Center, Chinese as a Second Language: https://lcc.dukekunshan.edu.cn/?p=1313
10. Language and Culture Center, home: https://lcc.dukekunshan.edu.cn/
11. Duke Global Education course database, DKU ECON 314 International Trade: https://courses.globaled.duke.edu/course/econ-314-international-trade-0
12. Duke Global Education course database, DKU PSYCH 101 Introductory Psychology, PSYCH 202 Cognitive Psychology, PSYCH 204 Social Psychology: https://courses.globaled.duke.edu/node/17744, https://courses.globaled.duke.edu/node/17745, https://courses.globaled.duke.edu/node/17751
13. DKU News, the new Behavioral Science major: https://news.dukekunshan.edu.cn/campus-news/new-behavioral-science-major-attract-students-global-cross-cultural-mindset/
