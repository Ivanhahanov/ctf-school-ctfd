# Platform e2e tests

End-to-end checks for the live stack. Two groups:

- **security / cluster** — workspace authorization (token isolation), anti-AI
  watermark injection, and flag generation↔validation parity. Need only
  `kubectl` + the shared secret.
- **CTFd integration** — registration, challenge creation, lab-flag config.
  Skip automatically unless `CTFD_TOKEN` is set.

## Prerequisites

- `kubectl` pointed at the cluster.
- The `joy` challenge loaded: `python3 tools/load_challenge.py challenges/joy --insecure`.
- Python deps: `pip install -r ctfd/e2e/requirements.txt`.

## Run

```bash
# security/cluster only (no CTFd token needed)
CTF_SCHOOL_SECRET=ctf-school-secret-key pytest ctfd/e2e -v

# everything, incl. CTFd integration
export CTFD_URL=https://ctf.school.local
export CTFD_TOKEN=ctfd_xxxxxxxx      # CTFd → Settings → Access Tokens (admin)
export CTFD_INSECURE=1               # dev self-signed TLS
export CTF_SCHOOL_SECRET=ctf-school-secret-key
pytest ctfd/e2e -v
```

The security tests spin up a throwaway `LabSession` (team `t-e2e`) and tear it
down afterwards. `joy`'s `LabSpace`/`LabService` must already be applied (the
loader does that).

## What each test asserts

| Test | Asserts |
|------|---------|
| `TestWorkspaceAuthorization` | no token → 403, own-team token → 200, wrong-team → 403, tampered → 403 |
| `TestAntiAI` | watermark + per-session label injected into the noVNC page |
| `TestFlags` | controller-injected `$FLAG` == CTFd's derived flag; flags differ per team |
| `TestCTFdIntegration` | a new user can register; the `Joy` challenge exists and is a `lab` type with dynamic-flag config |
