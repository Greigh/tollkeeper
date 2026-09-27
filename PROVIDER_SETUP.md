# Provider setup

Most adapters probe automatically from credentials the tool already stores on
your machine — nothing to configure. A few providers expose no local token the
router can use and need one value pasted in manually. Everything below is
read-only: the router stores your paste in the OS keychain and never sends it
anywhere except that provider's own API.

## The `router cred` command

```
router cred backend                      # which keychain backend is in use
router cred set <service> <account>      # prompts securely (or reads stdin)
router cred get <service> <account>      # masked; --show for raw value
router cred delete <service> <account>
router cred env <service> <account>      # prints the ROUTER_CRED_* override
```

Values are never echoed or logged. Prefer `router cred set` over putting
secrets in `config.toml`; env vars (`ROUTER_CRED_*` or the provider-specific
vars listed below) work too and take precedence.

## Zero-config providers

These need nothing beyond having used the tool while signed in:

| Provider | Where the credential comes from |
|---|---|
| Claude | macOS Keychain (`Claude Code-credentials`) or `~/.claude/.credentials.json` |
| Cursor | Cursor IDE `state.vscdb` (`cursorAuth/accessToken`) |
| Codex | `~/.codex/auth.json` |
| Gemini | `~/.gemini/oauth_creds.json`, or the Antigravity jetski token / IDE state |
| OpenRouter / DeepSeek / xAI / MiniMax / Kimi / Zcode / Perplexity API | API key env var |

If Gemini shows "OAuth token expired", open Antigravity once — it refreshes
its token on launch and the next probe works.

## Perplexity — needs a session cookie

Perplexity has no API that returns subscription rate limits; only the web
session exposes them (`/rest/rate-limit/all`).

1. Log into `perplexity.ai` in a browser.
2. DevTools → **Application** (Chrome/Edge) or **Storage** (Firefox) →
   Cookies → `https://www.perplexity.ai`.
3. Copy the value of **`__Secure-next-auth.session-token`**, then:

   ```
   router cred set perplexity session_cookie
   ```

   You can paste either the bare token, a `name=value` pair, or the whole
   `Cookie` request header — the adapter normalizes it.

4. **Optional**: some accounts also carry a second session cookie named
   `__Secure-pplx.session.<uuid>` (the name is unique to you). If probes keep
   reporting "session expired", copy that cookie as a full `name=value` pair:

   ```
   router cred set perplexity pplx_session
   ```

   Paste it as `__Secure-pplx.session.<your-uuid>=<value>`.

Env fallbacks: `PERPLEXITY_SESSION_COOKIE`, `PERPLEXITY_PPLX_SESSION`.

Session cookies expire when you log out or after ~30 days — the card will say
"session expired" when it needs a fresh paste.

## Zed — dashboard cookie or editor token

Two options; the dashboard cookie shows more.

- **Dashboard cookie** (full quota, token spend, edit predictions):
  1. Log into `zed.dev` in a browser.
  2. DevTools → Cookies → copy the `zed.session` cookie's **value**.
  3. `router cred set zed session_cookie`

- **Editor token** (plan + edit predictions only — the billing endpoints
  reject it): on macOS this is read automatically from the keychain entry
  Zed writes when you sign in in the editor. If the card says "token
  rejected", open Zed and sign in again to refresh it.

Env fallbacks: `ZED_SESSION_COOKIE`, `ZED_EDITOR_TOKEN`.

## Amp — session cookie (last resort)

Amp's CLI usually reports quota itself. If it doesn't, paste a session cookie
from `ampcode.com` (DevTools → Cookies → copy the session cookie):

```
router cred set amp session_cookie
```

Env fallback: `AMP_SESSION_COOKIE`.
