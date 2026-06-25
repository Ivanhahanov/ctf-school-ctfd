# Security model: workspace authorization & anti-AI

This document describes the two security mechanisms the platform implements
around the per-team lab workspaces:

1. **Authorization** — making sure a player can only reach *their own* workspace.
2. **Anti-AI** — raising the cost of solving challenges with an AI agent, and
   detecting/attributing suspected automation (report-only; the organizer decides).

Everything below is per-session and stateless, so it scales horizontally with the
number of running labs and adds no central component on the request hot path.

---

## 1. Authorization

### Threat
Workspace hostnames are predictable (`workspace-<team>-c<challengeID>.<domain>`).
Without auth, anyone could open another team's desktop by guessing the URL. The
desktop is a full interactive environment with access to the challenge's internal
services, so this is a serious leak.

### Design — a signed, team-scoped token (not URL secrecy)

```
 player (logged into CTFd)
    │  click "Open Workspace"
    ▼
 CTFd  /lab/<id>/enter
    │  mint HS256 token { team, exp }   (HMAC, CTF_SCHOOL_SECRET)
    │  Set-Cookie lab_auth=<token>; Domain=.<domain>; HttpOnly; Secure; SameSite=Lax
    │  302 → https://workspace-<team>-c<id>.<domain>
    ▼
 Envoy Gateway (TLS) ──► workspace pod
                          ├─ guard (Go)  ── verify token: signature + team==MINE + exp
                          │                  ├ inject anti-AI watermark
                          │                  └ reverse-proxy (HTTP + WebSocket)
                          └─ desktop (noVNC)
```

- **No passwords.** The token is minted from the player's *existing* CTFd login,
  so there is nothing extra to type.
- **One cookie, correct scope.** The cookie is set on the parent domain
  (`Domain=.ctf.school.local`), so the browser sends it to *every* `*.<domain>`
  workspace — but each per-session **guard** only accepts a token whose `team`
  claim equals its own injected `GUARD_TEAM`. Team A's cookie reaches team B's
  guard and is rejected. Isolation comes from the cryptographic binding, **not**
  from the hostname being secret — so predictable hostnames are harmless.
- **Stateless.** The guard verifies an HMAC signature; there is no session store
  and no central auth service to bottleneck.
- **WebSocket included.** noVNC runs over WebSocket; the same cookie rides the
  upgrade request, so the stream is authorized too.
- **Revocation.** Tokens are short-lived (`WORKSPACE_TOKEN_TTL`, default 8h) and
  re-minted whenever the player re-opens the lab. Rotating `CTF_SCHOOL_SECRET`
  invalidates every outstanding token at once.

### Token format
A compact JWT (HS256), signed with the shared secret both CTFd and the controller
already use (`CTF_SCHOOL_SECRET`):

```
base64url(header).base64url({"team":"t1","exp":1730000000}).base64url(HMAC_SHA256(secret, h.p))
```

Verified identically in `ctfd/plugin/__init__.py` (`_make_workspace_token`) and
`workspace-guard/main.go` (`verifyToken`).

### Failure behaviour
- Missing/invalid/expired/tampered token, or wrong team → **403**.
- A top-level navigation without a cookie → **302** back to CTFd, where re-opening
  the lab re-mints the cookie (no dead end for the player).

### Transport
The cookie is `Secure`, so the whole flow requires HTTPS. The gateway terminates
TLS (`*.ctf.school.local` wildcard cert via cert-manager) and plain HTTP is
301-redirected to HTTPS. Scheme is controlled by `CTF_SCHEME` (`http` for local
bring-up, `https` for the real deployment).

---

## 2. Anti-AI

> Honest framing: you **cannot** truly prevent a screenshot of a pixel stream —
> the OS can always capture what the browser paints. So the strategy is not
> "block" but **raise cost + trace + detect + report**, in layers. Enforcement
> that matters is server-side; the client only contributes signals.

### Implemented today (in the guard)

| Layer | Mechanism | Effect |
|-------|-----------|--------|
| **Rendering** | A faint, per-session **tiled watermark** of the team label is injected into the noVNC page (`workspace-guard/guard.js`). | Every screenshot carries the team identity (traceable) and gains visual noise that degrades automated OCR / vision models. Static (no motion) so it does not distract the player. |
| **Capture hardening** | Block context-menu / text-selection / copy / drag; **veil** (blur+dim) the stream when the tab loses focus or is hidden. | Frustrates naive scrape-and-OCR bots and off-screen capture windows. |
| **Pixel-only surface** | The challenge is delivered as a noVNC canvas, not a DOM. | There is no text/markup to scrape; an agent must OCR pixels and synthesize input — much higher cost than reading a page. |
| **Internal-only services** | Challenge services are reachable **only** from inside the workspace (no external route). | An agent cannot hit the challenge API directly; it must drive the desktop. |

### Why this lives in the guard
The guard is already per-session and on the request path. Putting the watermark
(and, next, the detection beacon) there means: no central bottleneck, the desktop
image is unchanged, and the per-session identity needed for traceability is right
there (`GUARD_SID`, `GUARD_TEAM`).

### Roadmap (designed, not yet built)
- **Behavioural telemetry (report-only).** A guard-hosted beacon collects weak
  signals — `navigator.webdriver`/headless flags, mouse-curvature and
  keystroke-timing statistics, solve-velocity — and ships them async to a
  collector, off the stream path. The organizer sees an anomaly score per team;
  **nothing is auto-blocked.**
- **Solve-dynamics anomaly detection** server-side (superhuman speed, perfectly
  linear pointer paths, no idle time).
- **Challenge-design gates** for steps vision models are weak at (timing/drag
  puzzles, multi-window spatial reasoning, CAPTCHA-gated flag reveal).

### What is explicitly *not* claimed
- Not a guarantee against a determined human-in-the-loop using AI assistance.
- Not screenshot-proof. The watermark makes captures **attributable**, not
  impossible.
- Client-side checks are bypassable on their own; they exist to feed the
  server-side report, which is the durable layer.

---

## Configuration reference

| Setting | Where | Meaning |
|---------|-------|---------|
| `CTF_SCHOOL_SECRET` | CTFd + controller (+ guard via controller) | HMAC secret for flags **and** workspace tokens. Rotate to invalidate all tokens/flags. |
| `CTF_SCHEME` | CTFd + controller | `http` / `https`; drives cookie `Secure` and workspace URLs. |
| `CTF_DOMAIN` | CTFd + controller | Base domain for workspace hosts and the cookie domain. |
| `WORKSPACE_TOKEN_TTL` | CTFd plugin | Token lifetime (default 8h). |
| `MAX_CONCURRENT_LABS` | CTFd plugin | Per-team running-lab quota. |
| `GUARD_TEAM` / `GUARD_SID` / `GUARD_SECRET` / `GUARD_LOGIN_URL` | guard (set by controller) | Per-session guard config. |
