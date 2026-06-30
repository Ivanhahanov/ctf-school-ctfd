# Writing a challenge

Challenges are **defined as code** in a directory under `challenges/<name>/` and
loaded with one command. A challenge is three pieces:

| Piece | Kind | Purpose |
|-------|------|---------|
| `labspace.yaml`   | `LabSpace` (cluster-scoped CRD)  | The environment blueprint: which services, what workspace, network & TTL. |
| `labservice.yaml` | `LabService` (cluster-scoped CRD) | One container of the challenge (the vulnerable app, a DB, …). Repeat for multiple. |
| `challenge.yml`   | CTFd definition | Name, scoring, description, and the wiring to the CRDs. |

Copy `challenges/joy/` as a starting template.

```
challenges/
  joy/
    labspace.yaml
    labservice.yaml
    challenge.yml
    README.md
```

## How it fits together

```
challenge.yml ──lab_space_ref──► LabSpace ──serviceSelector──► LabService(s)
     │                                                              │
     └── flag_service ──────────────────────────────────────────► $FLAG (HMAC)
```

- `challenge.yml.lab_space_ref` must equal `LabSpace.metadata.name`.
- `LabSpace.spec.serviceSelector` selects `LabService`s by label
  (convention: `ctf.school/challenge: <name>`).
- `challenge.yml.flag_service` names the `LabService` whose `$FLAG` env var is the
  flag for this challenge.

## Flags (dynamic, per-team, never stored)

You do **not** write a static flag. Add a dynamic env var to the LabService:

```yaml
env:
  - name: FLAG
    dynamicValue:
      type: hmac          # that's all — no format, no template
```

The flag **format** is not your concern: it's platform-wide, set once by the
operator via the `CTF_FLAG_FORMAT` env (shared by the controller and CTFd, just
like `CTF_SCHOOL_SECRET`). You only declare the variable.

At runtime the controller injects
`FLAG = CTF_FLAG_FORMAT % base64url(HMAC-SHA256(CTF_SCHOOL_SECRET, "<service-name>" + "<team>"))[:24]`,
and CTFd validates a submission by deriving the exact same value. Consequences:

- Every team gets a **unique** flag — sharing it is useless.
- Nothing static is stored, so it can't leak from a config or image.
- Your app just has to expose `$FLAG` to the player somehow (print it, serve it,
  hide it behind the vulnerability, etc.).

Other dynamic types: `type: random` (with `length:`) for non-flag secrets
(DB passwords, tokens) that should differ per session.

## Workspace (what the player gets)

```yaml
workspace:
  type: VNC                    # VNC (browser desktop) | Terminal
  image: vpc/ctf-desktop:latest
  port: 6080                   # the port your workspace image serves its web UI on
```

- The workspace is the player's **single entry point**. It is fronted by the
  per-session **guard**, which authorizes access (team-scoped token) and injects
  the anti-AI watermark — you get this for free, no image changes.
- `type: Terminal` gives a ttyd shell (default port 7681); `type: VNC` gives a
  noVNC desktop (default 6901; the sample desktop uses 6080 — set `port`).

## Networking

```yaml
network:
  allowInternet: false   # no egress to the public internet
  internalOnly: true     # services reachable ONLY from inside the workspace
```

Keep `internalOnly: true` unless a challenge genuinely needs a public endpoint.
Internal services are reachable from the workspace by their `LabService` name as
hostname (e.g. `http://joy:8000`).

## LabService fields you can use

```yaml
spec:
  image: <image>
  imagePullPolicy: IfNotPresent          # optional
  command: ["/bin/sh","-c"]              # optional, overrides entrypoint
  args: ["..."]                          # optional
  ports:
    - { name: http, containerPort: 8000, protocol: TCP }
  exposure: { type: HTTP, targetPort: http }   # how the workspace/gateway reaches it
  env:
    - { name: KEY, value: "static" }
    - { name: FLAG, dynamicValue: { type: hmac } }   # format is platform-wide
  resources:                              # requests/limits (please set these)
    requests: { cpu: 25m, memory: 32Mi }
    limits:   { cpu: 100m, memory: 64Mi }
  liveness:  { ... }                      # standard k8s probe (optional)
  readiness: { ... }                      # standard k8s probe (optional)
```

## LabSpace fields

```yaml
spec:
  serviceSelector: { matchLabels: { ctf.school/challenge: <name> } }
  network:   { allowInternet: false, internalOnly: true }
  resources: { limits: { cpu: "1", memory: 512Mi } }   # quota for the whole session ns
  workspace: { type: VNC, image: ..., port: 6080 }
  defaultTTL: "1h"                                       # auto-reaped after this
```

## Deploy / update

```bash
export CTFD_URL=https://ctf.school.local
export CTFD_TOKEN=ctfd_xxxxxxxx        # CTFd → Settings → Access Tokens (admin)
python3 tools/load_challenge.py <name> --insecure
```

The loader `kubectl apply`s the CRDs and create-or-updates the CTFd challenge —
idempotent, so re-run it after any edit. To only refresh the CTFd side:
`--skip-crds`.

## Checklist for a new challenge

- [ ] `LabService.metadata.labels."ctf.school/challenge"` matches the LabSpace
      `serviceSelector`.
- [ ] `challenge.yml.lab_space_ref` == `LabSpace.metadata.name`.
- [ ] `challenge.yml.flag_service` == the `LabService.metadata.name` carrying `$FLAG`.
- [ ] `resources` set on every LabService.
- [ ] `network.internalOnly: true` unless a public endpoint is required.
- [ ] Loaded with `tools/load_challenge.py` and smoke-tested (start lab → open
      workspace → retrieve & submit flag).

## Conventions / good practice

- Keep the challenge self-contained in its directory; no manual `kubectl` steps.
- Prefer small public base images with an inline `command` over building a custom
  image (faster to load, easier to review) — see `challenges/joy`.
- Set tight `resources`; many teams run many labs at once.
- Don't hardcode secrets — use `dynamicValue`.
