# Quotax: agent instructions

GNOME Shell extension (GJS, ESM) plus a standard-library Python backend that shows remaining subscription usage for the Claude, Codex, Grok and Ollama Cloud accounts the user links. Linux port of the macOS app (github.com/turinglabsorg/quota): keep behavior, data sources and copy in sync with it.

## Layout

- `quotax/`: the backend, a Python package with no third-party dependencies. Models, parsers, fetchers, account linking, process helpers and the `quotax` command (`cli.py`). Parsers must stay unit-testable with fixture JSON.
- `quotax/locale/it.json`: the Italian catalog, shared by the backend and the extension.
- `extension/`: the GNOME Shell extension (`quotax@turinglabs.org`). `extension.js` wires `backend.js` (runs `python3 -m quotax … --json` and streams its JSON lines), `store.js` (accounts, usage, linking), `indicator.js` (top bar and menu), `glyphs.js` (Cairo glyphs and usage bar), `model.js` (window math and formatting), `i18n.js`.
- `tests/`: unittest suites.
- `scripts/`: `install.sh`, `test.sh`, `preview.sh` and the preview helper extension.
- `DESIGN.md`: design system. Read it before any UI change and update it when you add patterns.

## Commands

- Test: `scripts/test.sh`.
- Install or update: `scripts/install.sh` (then log out and back in: GNOME Shell on Wayland only loads new or updated extension code at login). `scripts/install.sh --uninstall` removes it.
- End-to-end check without UI: `quotax` (linked accounts, or detected CLI logins when none are linked). `QUOTAX_DEBUG=1` logs failed HTTP responses, `QUOTAX_DEBUG=verbose` every usage response body (never tokens).
- Render the UI: `scripts/preview.sh [dir]` with sample data (`QUOTAX_SAMPLE=1`), `scripts/preview.sh --live [dir]` with the real accounts. It runs a headless GNOME Shell with a private D-Bus and temporary XDG dirs; screenshots and `shell-*.log` land in the output dir. Regenerate `docs/screenshots` with `PREVIEW_LANG=en_US.UTF-8` after visible UI changes.
- Check a translation: `LANGUAGE=it quotax`.

## GNOME Shell gotchas

- Never block the shell: all I/O goes through async Gio calls or the backend subprocess.
- `PanelMenu.Button` already connects `destroy` to `_onDestroy`: override it and call `super._onDestroy()`, never connect your own.
- A scrolled `St.Viewport` sizes its content from the layout's minimum height, and an `St.Button` with a label has a minimum smaller than its natural height. Keep scrolled content inside `NaturalHeightList` (see `indicator.js`) or it gets squeezed.
- Only one stylesheet is loaded: `stylesheet-dark.css` or `stylesheet-light.css`, each importing `stylesheet.css`.

## Accounts

The user decides which accounts are monitored; nothing is linked automatically. Accounts are stored (without secrets) in `~/.config/quotax/accounts.json`, written atomically under `accounts.lock`; the extension watches the file, so changes made from a terminal show up immediately.

- **Shared login** (`source: "cli"`): reuses the CLI's own session (`~/.claude/.credentials.json`, `~/.codex` via the Codex CLI, `~/.grok/auth.json`, the `~/.ollama/id_ed25519` device key that `ollama signin` linked). Quotax never refreshes or writes these tokens itself: refresh tokens rotate, so doing it would sign the CLI out. When the shared Claude token has expired, Quotax briefly starts `claude` in a pseudo-terminal so the CLI renews its own token, then retries (one attempt at a time across processes via a lock in `~/.local/state/quotax`, 10-minute cooldown after a failure; see `SharedClaudeLogin`).
- **Linked by Quotax** (`source: "managed"`): isolated home at `~/.local/share/quotax/accounts/<provider>/<uuid>` (mode 700), signed in through the official CLI in the browser:
  - Codex: `CODEX_HOME=<home> codex login`; usage via `codex app-server` with the same `CODEX_HOME`, so Codex refreshes its own token.
  - Grok: `GROK_HOME=<home> grok login --oauth` in a pseudo-terminal (Grok expects a TTY); expired tokens are refreshed by running `grok models` with the same home.
  - Ollama Cloud (no CLI): Quotax creates an Ed25519 key in `<home>/.ollama/id_ed25519` (OpenSSH format, mode 600), opens `https://ollama.com/connect?name=<hostname>&key=<base64url(authorized key)>` like `ollama signin` does, and polls `POST /api/me` until ollama.com reports the linked user (an unlinked key gets an empty user, not a 401).
  - Claude: `CLAUDE_CONFIG_DIR=<home> claude auth login --claudeai`; credentials land in `<home>/.credentials.json`. `~/.claude/.credentials.json` is snapshotted before login and restored afterwards so Claude Code keeps its account. Quotax refreshes managed Claude tokens via `https://platform.claude.com/v1/oauth/token`.
- Unlinking a managed account runs the CLI logout (Codex, Grok) or `DELETE /api/user/keys/<key>` (Ollama Cloud) and removes its home.
- Child CLIs run without inherited `CLAUDE*`/`ANTHROPIC_*` variables, so a parent agent's credentials never stand in for the stored login.

## Data sources

Mirrors the macOS app and Orca (github.com/stablyai/orca, `src/main/rate-limits`, `src/main/*-accounts`):

- Claude: `GET https://api.anthropic.com/api/oauth/usage` with `anthropic-beta: oauth-2025-04-20`.
- Codex: JSON-RPC `account/read` + `account/rateLimits/read` on `codex app-server`; fallback `GET https://chatgpt.com/backend-api/wham/usage` for the shared login when the CLI is missing.
- Grok: `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` with `X-XAI-Token-Auth: xai-grok-cli`, falling back to `/v1/billing` for monthly budgets. The API omits zero-valued fields: a missing `creditUsagePercent` means 0% only when the weekly `currentPeriod` matches `billingPeriodStart/End` and no field shows explicit zeros or spend (same rule as Orca).
- Ollama Cloud: `GET https://ollama.com/api/usage` (`limits.monthly.usage`, or legacy `limits.session`/`limits.weekly`, as fractions 0–1; no reset times) and `POST https://ollama.com/api/me` for email and plan. Requests are signed as in ollama/ollama `api/client.go`: `Authorization: <base64 public key blob>:<base64 Ed25519 signature of "<METHOD>,<path>?ts=<unix>">` with the same `ts` in the query. ollama.com answers with Go field names (`Email`, `Plan`), so parse keys case-insensitively. Ed25519 lives in `quotax/ed25519.py` (stdlib only, RFC 8032 vectors in the tests).

## Rules

- Never log, print or persist tokens.
- Never type or handle user passwords: sign-in always happens in the browser on the provider's page.
- Poll no more often than every 5 minutes; the usage endpoints rate-limit aggressive clients.
- Backend: Python standard library only.
- Code, comments and docs in English.

## Localization

- English is the development language: user-facing strings are written in English in code, wrapped in `_()` (Python and JS), with `{name}` placeholders filled through keyword arguments (`_("Updated {time}", time=…)` / `_('Updated {time}', {time})`).
- Every user-facing string needs an entry in `quotax/locale/it.json` with the same placeholders; `CatalogTests` fails otherwise. Keep `_()` arguments as plain string literals so the test can find them.
