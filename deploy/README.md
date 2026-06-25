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
    kind/   sources.yaml GitRepository for the controller & challenges repos
            sync.yaml    Flux Kustomizations: infra → data/cache → ctfd → gateway
                         → monitoring → controller → challenges (dependsOn-ordered)
    cloud/  sync.yaml    same bases + per-env ${VAR} substitution
```

## Install (local = prod)

```bash
# local:
cd ctfd && make all        # cluster + cilium + images + flux bootstrap + hosts

# prod: the SAME, minus the kind/images/hosts bits —
flux bootstrap github --owner=<you> --repository=ctfd \
  --branch=main --path=deploy/clusters/cloud
```

`make flux` runs `flux bootstrap` against your repo and waits for reconciliation.

## ⚠️ Set these before going live (currently local placeholders)

- **`clusters/kind/sources.yaml`** — the `controller` and `challenges`
  `GitRepository` URLs are `your-org/...` placeholders. **Change them** to your
  real repos.
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
