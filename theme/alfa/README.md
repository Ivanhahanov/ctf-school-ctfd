# Alfa CTF theme

Dark CTFd theme in Alfa Digital brand styling, built for **CTFd 3.8.5**
(`core` / core-beta base, Bootstrap 5.3).

## Brand

From `alfa_digital_guidelines.pdf`:

| Token       | Value      | Use                        |
|-------------|------------|----------------------------|
| Alfa Red    | `#EF3124`  | navbar, primary buttons    |
| Cyan        | `#05FFFF`  | links, accents, focus, glow|
| Black       | `#000000`  | background                 |
| Typography  | Styrene UI | all UI text                |

## How it works (no build step)

This theme reuses the compiled Bootstrap bundle shipped with CTFd's `core`
theme and layers brand styling on top of it, so **no `npm`/Vite build is
required**:

- `static/assets/` + `static/manifest.json` — copied verbatim from `core`;
  `Assets.css("assets/scss/main.scss")` still resolves through the manifest.
- `static/css/alfa.css` — the brand overlay, linked in `templates/base.html`
  *after* the Bootstrap bundle. It overrides Bootstrap CSS variables and forces
  a dark-only scheme (`<html data-bs-theme="dark">`), so the light/dark toggle
  is hidden.
- `static/fonts/` — Styrene UI web fonts (`@font-face` declared in `alfa.css`).
- `static/img/appsec.svg`, `signs.svg` — brand artwork. `appsec.svg` is a
  cleaned copy of the source: the opaque black background `<rect>` and the red
  "COLLABORATION" lettering were removed so the artwork blends into the page
  background instead of sitting in a black box.

### Landing page (home)

The landing page is **not** baked into the theme — it is native, editable CTFd
page content, so it can be changed without touching any template:

1. Admin → Pages → open the **index** page.
2. Set **Format = HTML**.
3. Paste the contents of [`landing.html`](landing.html) into the content field
   and Save.

`landing.html` is fully self-contained (scoped styles + inline brand colors), so
it also survives a theme change. Its only external asset is the artwork, served
from the theme at `/themes/alfa/static/img/appsec.svg`; to decouple it fully,
upload `appsec.svg` under Admin → Files and swap the `<img src>`.

`templates/page.html` is the stock CTFd page template (just renders the page
content in a container).

### Rules page (Правила)

Same idea — [`rules.html`](rules.html) is self-contained page content. Admin →
Pages → New Page, Title `Правила`, Route `rules`, Format `HTML`, tick *Show in
navbar*, paste `rules.html`, Save. It then appears in the navbar automatically.

### i18n / Russian

`base.html` sets `<html lang="{{ get_locale() }}">` so client scripts can follow
the site language. Two theme-level Russian fixes:

- CTFd's `ru` catalog translates `Submit` → *Войти* (Login), which is wrong on
  the **challenge flag** button. `challenge.html` overrides it to *Отправить* in
  `ru` while the login page keeps *Войти*.
- The lab_manager plugin ships its own RU/EN strings (see the plugin).

## Deploy

The image already installs the theme (see `ctfd/Dockerfile`):

```dockerfile
COPY ./theme/alfa /opt/CTFd/CTFd/themes/alfa
```

Then activate it (once, per environment):

1. `Admin → Config → Theme → alfa`, **or** set the `ctf_theme` config to `alfa`
   (`PATCH /api/v1/configs {"ctf_theme":"alfa"}`).
2. Set the index page content to the welcome text (see above).

## Files changed vs. the `core` base

- `templates/base.html` — `data-bs-theme="dark"`, link `alfa.css`, theme-path fixes.
- `templates/components/navbar.html` — `Alfa CTF` brand fallback.
- `templates/page.html` — AppSec hero on the index route.
- `static/css/alfa.css`, `static/fonts/*`, `static/img/{appsec,signs}.svg` — new.

Everything else is unchanged from `core`, so all other pages (challenges,
scoreboard, users, teams, settings, errors) render and inherit the dark styling.
