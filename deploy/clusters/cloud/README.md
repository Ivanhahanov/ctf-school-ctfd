# Cloud cluster — bootstrap

Same procedure AND registry as `kind` (shared `clusters/base`, OCI artifacts on Docker
Hub, SOPS) — prod just pins an immutable `TAG` and sets its own `cluster-config` values.
The `ctf-school-secret` ships **SOPS-encrypted** in `./secrets/secrets.enc.yaml`.

## Order of operations

```
seed (flux-system) → OCIRepositories + root Kustomization — no secret-in-missing-ns
infrastructure     → creates namespaces (ctfd, controller-system, monitoring, …)
  ├─ secrets   (dependsOn infrastructure) → SOPS-decrypts ctf-school-secret → both ns
  └─ controller(dependsOn infrastructure) → Deployment mounts the secret (secretKeyRef)
```

The `secrets` Kustomization has `decryption.provider: sops`, `secretRef: sops-age`.
That `sops-age` Secret holds the **age private key** and must exist in `flux-system`
before `secrets` reconciles. Supplied out-of-band (below) — never committed.

## Bootstrap

```sh
# 1. CI (anywhere with docker login) — build + push images + artifacts for a version:
make -C ctfd ci TAG=v0.1.0

# 2. On the cloud cluster — load the age key ONCE, then deploy with Flux only:
kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=$AGE_KEY_FILE
make -C ctfd prod TAG=v0.1.0            # flux install + secret + seed (clusters/cloud)
```

`make prod` runs `flux install` + `secret` + `seed` against `clusters/cloud`; `secrets`
waits on `infrastructure`, decrypts, and creates `ctf-school-secret` in both namespaces.
Give Flux pull creds with `flux create secret oci` if the Docker Hub repos are private.
Day-2 update = `make ci TAG=<newver>` then `make prod TAG=<newver>`.

## TLS + DNS

Per-session workspaces are subdomains (`workspace-<id>.<domain>`), so the cert is a
**wildcard** `*.<domain>`. The cert source is `clusters/cloud/tls/` — **pick one option**
(this is a short-lived event platform, < 60 days, so either works). kind always
self-signs and never touches Let's Encrypt.

**DNS records — add these once at your provider (both options):**
- `A  <domain>            → <gateway LoadBalancer IP>`
- `A  *.<domain>          → <gateway LoadBalancer IP>`   (every session subdomain)
- (`A  grafana.<domain>   → <gateway LB IP>` if you expose Grafana)

### Option A — Let's Encrypt (Cloudflare DNS-01), auto-issue + renew  *(default)*
Wildcard requires DNS-01; cert-manager writes the `_acme-challenge` TXT **automatically**
via the provider API (you never touch TXT):
1. Cloudflare: scoped token (Zone → DNS → Edit). Other provider → swap the `dns01` solver
   in `tls/acme-issuers.yaml` + the Secret. No provider API? see the note below.
2. `sops secrets/secrets.enc.yaml` → set `cloudflare-api-token` / `api-token`; set
   `ACME_EMAIL` in `cluster-config.yaml`.
3. **Rate-limit-safe:** ships `TLS_ISSUER: letsencrypt-staging` — validate the whole flow
   here first (untrusted, browser-warns — expected). Once `kubectl -n ctfd get certificate
   ctfd-tls` is Ready, flip `TLS_ISSUER: letsencrypt-prod`. One cert (SANs `<domain>` +
   `*.<domain>`) = a single order, well within LE limits.
   > No DNS API? Delegate just the challenge with a one-time static
   > `_acme-challenge.<domain> CNAME <api-capable-zone>` (Cloudflare/**acme-dns**), or move
   > the domain's nameservers to Cloudflare (free) — registrar stays put.

### Option B — Manual cert (no cert-manager, no renewal)
Because the event is shorter than a cert's lifetime, you can issue a wildcard **once** and
just hand it in — no auto-renewal needed:
1. Get a wildcard cert, e.g. `certbot certonly --manual --preferred-challenges dns -d
   '<domain>' -d '*.<domain>'` (adds one TXT by hand), or a purchased cert.
2. In `tls/kustomization.yaml`: comment out `acme-issuers.yaml` + `certificate.yaml`,
   uncomment `ctfd-tls.enc.yaml`, and `sops tls/ctfd-tls.enc.yaml` → paste `tls.crt`
   (fullchain) + `tls.key`.
   *(Or skip the repo entirely: `kubectl -n ctfd create secret tls ctfd-tls
   --cert=fullchain.pem --key=privkey.pem`.)*

## Day-2

- **Edit the secret:** `sops deploy/clusters/cloud/secrets/secrets.enc.yaml`
  (needs the private key in `~/.config/sops/age/keys.txt` or `SOPS_AGE_KEY_FILE`).
- **Rotate recipients:** edit the `age:` list in the repo-root `.sops.yaml`, then
  `sops updatekeys deploy/clusters/cloud/secrets/secrets.enc.yaml`.
- **Rotate the secret value:** open with `sops`, change `secret`/`metrics_token`,
  save — Flux re-applies. Restart the CTFd/controller pods to pick up the new env.

> The public recipient in `.sops.yaml` is committed (safe). The private key is not.
> Anyone with the private key can decrypt every cloud secret — guard it accordingly.
