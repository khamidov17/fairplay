# FairPlay name and look (2026-10-16)

The classroom site now presents itself as **FairPlay** and uses the FairPlay visual style (CONTRACT.md "Visual style"): Plus Jakarta Sans, light surfaces, primary blue `#2F4BD8`, ink `#1B2533`, rounded controls.

## Name
- `SITE_NAME = 'FairPlay'`, `SITE_LONG_NAME = 'FairPlay Classroom'`, admin title "FairPlay Admin". Page titles read "… - FairPlay Classroom".
- Top bar: FairPlay mark + name (`templates/site-logo-fragment.html`); e-mail logo `resources/icons/logo.svg`.
- Favicons, app icons, `manifest.json`, `browserconfig.xml`: regenerated from the FairPlay mark (`resources/icons/*.png` are now tracked).
- Footer: "FairPlay · built on the open-source DMOJ" with the link to the DMOJ source. DMOJ is AGPL-3.0, so keep this credit.
- Migration 0226 renames the Site from the DMOJ default to "FairPlay Classroom" (a name an admin chose is kept). The demo fixture, seed data, the Canvas export column ("FairPlay Score") and the CS subject description no longer say DMOJ / DKU OJ.

## Look
- `resources/fairplay.scss` is loaded last in `style.scss` and restyles the old pages without editing each one: white top bar with a blue active pill, underline tabs, flat blue buttons, hairline inputs with a blue focus ring, light table and section headers, soft verdict colours, sign-in card.
- Palette in `vars-common.scss` / `vars-default.scss`. New token `--oj-heading-bg` is the fill of header bands; `--oj-heading` stays a text colour.
- Fonts are self-hosted in `resources/fonts` (SIL OFL), never a CDN: Plus Jakarta Sans for the interface, Source Serif 4 for question text.
- Admin site: `resources/fairplay-admin.css` (white menu and top bar, FairPlay colours), loaded from `templates/admin/base_site.html`.

## Light by default
- FairPlay is light. The dark theme is used only when a user picks "Dark" in their profile; "Default (light)" (was "Follow system default") no longer follows the computer's dark mode. Before this, pages mixed light and dark parts when the computer was in dark mode.
- Templates use `DARK_MODE` / `DARK_MEDIA`; SCSS uses `html.theme-dark`; the code editors follow the same setting. Migration 0227 updates the choice label.

## Checks
- `judge/views/tests/test_fairplay_brand.py` (12 tests): name, footer credit, icons, fonts, admin theme, light default, no `prefers-color-scheme` left.
- Full suite: 1909 tests OK; flake8 clean; no missing migrations.
- Crawl of 716 pages as anonymous, student, TA, instructor and admin: no 5xx, no JavaScript errors.
