# Quotax design system (Linux)

Quotax is a GNOME Shell top bar utility. It should feel like part of the shell: quiet in the top bar, dense and legible in the menu, never decorative for its own sake. It follows the macOS design system and adapts it to GNOME (Yaru and Adwaita shell themes, light and dark).

## Principles

- **The user chooses.** Nothing is monitored until the user links an account.
- **Glanceable first.** The top bar shows one number per linked account: the tightest account-wide window. Model-scoped limits (e.g. weekly Fable) appear only in the menu because they do not block the whole account.
- **Color means state.** Neutral when healthy, orange when running low, red when nearly exhausted. Brand color is limited to the Claude glyph in the menu.
- **Native materials.** The menu is the shell's own `PopupMenu` (`PanelMenu.Button`), with the theme's font, `.button` styles and accent color, so light/dark and the user's accent work for free.

## Color

Shared rules live in `extension/stylesheet.css`; `stylesheet-dark.css` and `stylesheet-light.css` import it and add the colors that depend on the shell style.

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `level.normal` | panel text / `#33d17a` bar | same | More than 20% remaining |
| `level.warning` | `#ffa348` text, `#ff7800` bar | `#e66100` text, `#ff7800` bar | 6–20% remaining |
| `level.critical` | `#ff7b63` text, `#e01b24` bar | `#c01c28` text, `#e01b24` bar | 5% or less remaining |
| `panel.warning`, `panel.critical` | `#ffa348`, `#f66151` | same (the top bar is dark) | Top bar values |
| `accent.claude` | `#d97757` | same | Claude glyph in the menu |
| `accent.codex`, `accent.grok`, `accent.ollama` | text color | text color | Monochrome brands |
| `text.secondary` | white at 62% | black at 60% | Subtitles, resets, suffixes, issues |
| `surface.card` | white at 6% | black at 4.5% | Account and provider cards |
| `surface.track` | text color at 13% | same | Empty part of usage bars |
| `surface.badge` | white at 10% | black at 7% | Plan badge |
| `link` | `-st-accent-color` | same | "Open the sign-in page" |

Stale data (last refresh failed but previous data is shown) renders at 55% opacity in the top bar.

## Typography

The shell theme's font only. Digits use `font-feature-settings: "tnum"` so values do not jitter.

| Role | Size | Weight |
| --- | --- | --- |
| Top bar value | theme | 500 |
| Menu title, provider name | theme | bold |
| Window label | theme | regular |
| Window value | theme | bold |
| Secondary text (subtitle, reset, issues, suffix) | 0.9em | regular, `text.secondary` |
| Plan badge | 0.8em | 600, `text.secondary`, max 110 px |

## Spacing and shape

- Menu content 360 px wide; header padding 6/6/12/12; 8 px between cards.
- Cards: 12 px padding, 14 px radius, 12 px between rows.
- Usage bar: 6 px tall capsule; non-zero values render at least as wide as the bar is tall.
- Top bar: 10 px between accounts, 4 px between glyph and value, glyph 14 px.
- The card list scrolls inside the menu when it is taller than the work area; header and "Add account" stay visible.

## Glyphs

Custom stroked shapes drawn with Cairo in `extension/glyphs.js`, line width 15% of the glyph size, round caps and joins:

- **Claude**: ten-ray burst with alternating ray length.
- **Codex**: terminal prompt `>_`.
- **Grok**: open ring with a diagonal slash.
- **Ollama Cloud**: llama head, two ears leaning outwards over a rounded head with two eye dots.
- **Gauge**: arc with a needle at one third, in the top bar when nothing is available yet.

## Components

- **Top bar label** (`QuotaIndicator._renderPanel`): glyph + percentage per linked account; the gauge glyph when nothing is available.
- **Account card**: header (glyph, provider name, account email in secondary text, plan badge, `view-more-symbolic` button revealing the source and a destructive "Unlink account" button), one window row per window, optional issue line.
- **Settings card**: shown by the gear button; "Show" with a segmented "Percentage left / Percentage used" choice.
- **Empty state**: card with title, one-line explanation and a small `.button.default` "Add account".
- **Add account page**: back chevron + title; one provider card per service with the detected CLI login ("Link" small button, or "Linked" with a checkmark) and a sign-in row ("Sign in to another account…" → spinner + "Finish signing in in your browser…" + link + "Cancel", or issue line + "Try again"); footnote explaining that sign-in happens in the browser.
- **Window row**: label, value + suffix (`left`/`used`), usage bar, reset countdown.
- **Issue line**: symbolic icon + secondary text; text between backticks renders in monospace.

## Copy

UI copy is English with an Italian catalog (`quotax/locale/it.json`, shared by the extension and the command line). Sentence case, short, no exclamation marks. Countdown format: `2h 10m`, `3d 4h` (`3g 4h` in Italian), `12m`. Relative time: `now`, `3 min ago`, `2 h ago`.
