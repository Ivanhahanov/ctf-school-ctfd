# deploy/ — GitOps (Flux)

The **entire platform is deployed by Flux** from this directory. Local (kind) and
production (cloud) run the **same** flow and the **same** manifests — they differ
only in three things: creating the cluster, building/loading dev images, and
`/etc/hosts`. There is no `helm install` or `kubectl apply` of platform
components anymore; Flux reconciles everything from Git.

```
deploy/
  infrastructure/        operators as HelmReleases (cert-manager, envoy-gateway,
                         mariadb-operator, victoria-metrics) + HelmRepositories.
                         Versions are semver ranges → Flux tracks latest stable.
  apps/                  the platform's Kubernetes manifests
    mariadb/  keydb/     stateful layer
    ctfd/                CTFd Deployment/Service/RBAC/secret
    gateway/             GatewayClass / Gateway / HTTPRoutes / TLS / policies
    monitoring/          values.yaml (Helm) + scrape/route/dashboards (kustomize)
  clusters/
    base/   sources.yaml GitRepository for the controller & challenges repos
            sync.yaml    Flux Kustomizations: infra → data/cache → ctfd → gateway
                         → monitoring → controller → challenges (dependsOn-ordered),
                         parameterised with ${VAR} + postBuild.substituteFrom.
                         SHARED by every cluster — the single deployment mechanism.
    kind/   cluster-config.yaml  local values (domain, local images, latest)
            secrets.yaml         dev-only ctf-school-secret (prod: external)
            kustomization.yaml   = ../base + the two files above
    cloud/  cluster-config.yaml  prod values (domain, registry images, tag)
            kustomization.yaml   = ../base + cluster-config (secret supplied externally)
```

Both clusters run the **identical** `clusters/base`. The ONLY thing that differs
is each cluster's `cluster-config` ConfigMap — Flux substitutes its values into
the `${VAR}` placeholders at reconcile time. Debugging on kind therefore exercises
the exact prod mechanism (ConfigMap + postBuild), not a different code path.

## Install (local = prod)

```bash
# local:
cd ctfd && make all        # cluster + cilium + images + flux + hosts

# prod: the SAME, minus kind/images/hosts —
flux install
kubectl apply -f deploy/clusters/cloud/flux-system.yaml
```

`make flux` does **`flux install` + `kubectl apply` of the read-only source** —
**not** `flux bootstrap`. Flux only **reads** the repo, never writes to it, and
holds **no broad PAT**. The repo URL lives in `clusters/<env>/flux-system.yaml`.

### Public vs private repos
- **Public** → nothing else to do; Flux clones over HTTPS, no credentials.
- **Private** → give Flux a **read-only deploy key** per repo (least privilege,
  no write, no account-wide token):

  ```bash
  # generates a keypair + prints the public key to add as a READ-ONLY deploy key
  flux create secret git flux-system \
    --url=ssh://git@github.com/Ivanhahanov/ctf-school-ctfd \
    --namespace=flux-system
  ```
  Add the printed key under the repo's **Settings → Deploy keys** (leave “Allow
  write access” **unchecked**), switch the GitRepository `url` to the `ssh://…`
  form, and add `secretRef: { name: flux-system }`. Repeat per repo
  (controller, challenges) — deploy keys are per-repo. For many private repos a
  GitHub App or a fine-grained, read-only, repo-scoped PAT is the alternative.

## ⚠️ Set these before going live (currently local placeholders)

- **`clusters/base/sources.yaml`** — the `controller` and `challenges`
  `GitRepository` URLs. Confirm they point at your real repos (shared by both
  clusters).
- **`clusters/cloud/cluster-config.yaml`** — `CTF_DOMAIN`, `CONTROLLER_IMAGE`,
  `CTFD_IMAGE`, `IMAGE_TAG` are `your-org/...`/`ctf.example.com` placeholders.
  This ConfigMap is the one and only place prod values are set.
  - *Recommended:* host the three repos (ctfd, ctf-school-controller,
    ctf-school-challenges) on GitHub; bootstrap Flux against `ctfd`.
- **Images** — `controller`, `workspace-guard`, `ctfd-lab`, `ctf-desktop` are
  built and `kind load`ed locally (`make images`, sibling paths in the Makefile).
  **For prod, push them to a registry** (GHCR or your cloud's registry) and set
  the image refs in the manifests / cluster overlay.
  - *Recommended:* GHCR (`ghcr.io/<org>/...`) with image tags pinned per release;
    use Flux's image-automation to bump them, or set `IMAGE_TAG` in the cloud
    `cluster-config`.

## CNI exception

Cilium is **not** Flux-managed: it is the CNI everything depends on, so it is
installed at bootstrap (`make cilium`, before Flux) — see `kind.yaml`
(`disableDefaultCNI: true`). It enforces the per-session lab `NetworkPolicy`
the controller creates (team/namespace isolation).

## Versions

Operator chart versions in `infrastructure/operators.yaml` are major-bounded
semver ranges (`>=x.y.0 <X+1.0.0`), so Flux pulls the **latest stable** within
the major automatically. For stricter prod control, pin exact versions + Renovate.
