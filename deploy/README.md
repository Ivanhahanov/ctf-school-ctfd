# deploy/ — IaC via Flux GitOps (OCI artifacts)

The **entire platform is reconciled by Flux** from **OCI artifacts** — not a Git
clone. `make` (local) and CI (prod) publish the working tree of each repo as an OCI
artifact to **Docker Hub**; Flux pulls and reconciles it. **Local and prod run the
identical procedure, manifests, AND registry** (`docker.io/$DOCKERHUB_USER`) — they
differ only in the cluster (kind vs cloud), how dev container images are delivered
(kind-load vs pushed), the artifact tag, and `/etc/hosts`.

One deploy/update procedure, everywhere:

```
1. publish   flux push artifact oci://$REGISTRY/<repo>:$TAG   (deploy / controller / challenges)
2. secret    kubectl create secret sops-age --from-file=age.agekey=$KEY   (once)
3. seed      apply the OCI sources + root Kustomization (envsubst $REGISTRY/$TAG)
   → Flux decrypts the SOPS secret and reconciles everything.
Update = re-publish + `flux reconcile`  (locally: `make deploy`).
```

```
deploy/
  infrastructure/   operators as HelmReleases + HelmRepositories (cert-manager,
                    envoy-gateway, mariadb-operator, victoria-metrics).
  apps/             platform manifests: mariadb, keydb, ctfd, gateway, monitoring.
  clusters/
    base/sync.yaml         the SHARED Flux Kustomizations (infra → data/cache → ctfd
                           → gateway → monitoring → controller → challenges), ${VAR}
                           + postBuild.substituteFrom the cluster-config ConfigMap.
                           sourceRef = OCIRepository (flux-system / controller / challenges).
    kind/  flux-system.yaml   OCI sources + root Kustomization (${REGISTRY}/${TAG},
                              applied imperatively by `make` — the bootstrap seed)
           cluster-config.yaml   local values
           secrets.yaml + secrets/   SOPS-encrypted ctf-school-secret (decrypted by Flux)
    cloud/ flux-system.yaml / cluster-config.yaml / secrets.yaml + secrets/  (same, prod)
```

Both clusters run the **identical** `clusters/base`. The only per-cluster divergence
is the `cluster-config` ConfigMap (domain, scheme, registry, tag) and the SOPS secret
values. Debugging on kind exercises the exact prod mechanism (OCI + postBuild + SOPS).

## Install — three separated flows

Registry defaults to the Docker Hub org `explabs`; override `DOCKERHUB_USER` if yours
differs. `docker login` before any push.

```bash
# ── DEV — local kind, build here ──────────────────────────────────────────────
# Builds + KIND-LOADS images (NOT pushed); publishes the tiny manifest artifacts.
cd ctfd && make dev
flux get kustomizations --watch          # Flux reconciles async (MariaDB ~5 min)
make hosts                               # once the Gateway has an IP (needs cloud-provider-kind)
make dev-update                          # day-2: rebuild+reload images, re-publish, reconcile

# ── CI — build + PUSH everything (no cluster), at $(TAG) + :latest ────────────
make ci                                  # docker push images + flux push artifacts (:latest)

# ── DEMO — prod-style on kind: PULL pre-built :latest (run `make ci` first) ───
make demo                                # kind + Flux pulls explabs/ctf-school-* :latest

# ── PROD — Flux only (assumes `make ci` already pushed :latest) ──────────────
make prod TAG=latest                     # flux install + sops-age + seed (clusters/cloud)
# Cloud tracks the moving :latest (cluster-config IMAGE_TAG=latest), so this one flow
# ships prod. Re-push then force the rollout: kubectl -n ctfd rollout restart deploy/ctfd.
# For an immutable release instead: `make ci/prod TAG=v0.1.1` + set IMAGE_TAG=v0.1.1.
```

The split is deliberate:
- **`ci`** pushes container images (the heavy step) + artifacts, at `$(TAG)` **and** `:latest`.
- **`dev`** never pushes images — it kind-loads them (fast local iteration) and only
  publishes the small OCI manifest artifacts Flux needs.
- **`demo`** builds nothing: it prepares a kind cluster and lets Flux **pull** the
  pre-built `:latest` images + artifacts — the exact prod path, on a throwaway cluster.
- **`prod`** builds/pushes nothing; points Flux at the versioned artifacts `ci` produced.

No `flux bootstrap`, no git PAT: Flux only **pulls** two OCI artifacts from Docker Hub —
`docker.io/explabs/ctf-school-deploy` (this repo) and
`docker.io/explabs/ctf-school-controller-config` (the controller). Docker Hub repos are
flat, so `ctf-school` is a repo-name PREFIX under `explabs`, not a nested path.
Challenges are NOT Flux-managed — load them with the llm-ctf-2026 script (`make crds`).
Private repos → `flux create secret oci`. Pin `TAG` in prod.

## Secrets — SOPS everywhere (one procedure)

Both clusters keep the `ctf-school-secret` **SOPS-encrypted in git**
(`clusters/<env>/secrets/secrets.enc.yaml`) and Flux decrypts it in-cluster. The ONLY
imperative secret step — identical local and prod — is loading the **age private key**
as the `sops-age` Secret (`make secret`, or the `kubectl create secret` above). The key
is never committed (`.sops-age-key.txt` / `.sops.yaml` recipient is the public half).
Split keys: `flag_secret` (flag HMAC), `jwt_secret` (workspace tokens) — see the top-level
`SECURITY-REVIEW.md`.

## ⚠️ Before going live

- **`clusters/cloud/flux-system.yaml` + `cluster-config.yaml`** — set `REGISTRY` to your
  registry, pin `TAG` to an immutable version/digest, keep `OCI_INSECURE=false`, and set
  `CTF_DOMAIN` / image repos. Consider `spec.verify` (cosign) on the OCIRepositories.
- **age key** — generate a prod age key, load it as `sops-age`, and
  `sops updatekeys` the cloud secret to that recipient. Guard the private key.
- **Images** — prod pushes `controller` / `workspace-guard` / `ctfd` / `desktop` /
  challenge images to the registry (pinned tags); locally they're `kind load`ed by
  `make images`.

## CNI exception

Cilium is **not** Flux-managed — it's the CNI everything depends on, installed at
bootstrap (`make cilium`, before Flux; `kind.yaml` sets `disableDefaultCNI: true`). It
enforces the per-session lab `NetworkPolicy` (team/namespace isolation).

## Versions

Operator chart versions in `infrastructure/operators.yaml` are major-bounded semver
ranges, so Flux pulls the latest stable within the major. For stricter prod control,
pin exact versions + Renovate, and pin the OCI artifact `TAG` per release.
