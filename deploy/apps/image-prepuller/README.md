# Image pre-puller

Warms every node's containerd image cache **before** a player ever needs it, so
a `LabSession` scheduled onto a fresh autoscaled node doesn't sit through a
multi-GB cold pull (some workspace images run 4-7GB — confirmed by pulling the
actual published images, not estimating). Two `DaemonSet`s:

| DaemonSet | Images | Pull secret | Maintained |
|---|---|---|---|
| `image-prepuller-platform` | shared desktop variants (`explabs/ctf-school-desktop-*`) + the guard sidecar + `python:3.11-alpine` (joy) — all public Docker Hub | none needed | hand-edited (changes rarely — a `vpc/` release) |
| `image-prepuller-challenges` | every per-challenge service + workspace image actually hosted on `registry.${CTF_DOMAIN}` | `registry-pull` (see below) | **generated** — see `generate-challenge-images.sh` |

## Why a DaemonSet

Standard Kubernetes answer to "new node joins an autoscaled node group with an
empty cache": the DaemonSet controller schedules a copy the moment a node goes
`Ready`, independent of and ahead of whatever real workload the autoscaler
added the node for. Each container's only job is to hold its image resident —
`command: ["sleep", "infinity"]` overrides the image's real entrypoint (which
would otherwise try to boot a full desktop or service) so kubelet pulls the
image, then the container just idles on ~5m CPU / 16Mi memory.

The **guard** image is the one exception: it's a distroless static binary with
no shell (`sleep`/`sh` both fail — confirmed empirically, not assumed), so it
can't use the override trick. It fails closed on a missing `GUARD_SECRET` (real
behavior — see `workspace_guard.go`), so it's handed an unused dummy value to
start cleanly and idle on `:8080` instead of crash-looping. Nothing ever routes
traffic to this copy.

## Regenerating the challenge-images list

Run this after `make crds` (in `llm-ctf-2026/`) whenever a challenge is added,
removed, or its `workspace/` is rebuilt (workspace images are tagged by content
SHA — the tag changes on every rebuild):

```bash
./generate-challenge-images.sh > daemonset-challenges.yaml
git diff daemonset-challenges.yaml   # review before committing
```

It reads the cluster's own `LabSpace`/`LabService` objects (`kubectl get
labservices.infra.ctf.school`, `labspaces.infra.ctf.school`) — the same CRDs
`make crds` applies — so the list always matches what's actually deployed, and
filters to only images hosted on `registry.${CTF_DOMAIN}` (public
`explabs/ctf-school-desktop-*` workspace images are already covered by
`image-prepuller-platform`; including them again here would just duplicate
that pull and wrongly imply they need a pull secret they don't).

## Bootstrap the `registry-pull` secret (per cluster)

`daemonset-challenges.yaml` needs a real `kubernetes.io/dockerconfigjson`
Secret — `registry.${CTF_DOMAIN}` requires htpasswd auth even for pulls (see
`../registry/deployment.yaml`, `REGISTRY_AUTH`). A plain key inside a generic
`Opaque` secret (like `ctf-school-secret`'s `registry_dockerconfigjson`) does
**not** work for `imagePullSecrets` — that field needs an actual
`dockerconfigjson`-typed Secret object, which is exactly why the controller
reshapes the same credential into a dedicated Secret per session namespace
(`task-registry-pull`) instead of referencing `ctf-school-secret` directly.
Same reshaping is needed here, once, for the `image-prepuller` namespace.

Reuse the **same** registry credentials already provisioned for
`CTF_REGISTRY_PULLSECRET` (see `../registry/README.md`, "Pulling into lab
pods") — supply them via the same SOPS flow as `registry-auth`:

```bash
# Same $USER/$PASS as CTF_REGISTRY_PULLSECRET / registry-auth
AUTH=$(printf '%s:%s' "$USER" "$PASS" | base64)
DOCKERCFG=$(printf '{"auths":{"registry.%s":{"username":"%s","password":"%s","auth":"%s"}}}' \
  "$CTF_DOMAIN" "$USER" "$PASS" "$AUTH")

cd deploy/clusters/cloud/secrets      # or clusters/kind/secrets
cat > registry-pull.enc.yaml <<EOF
apiVersion: v1
kind: Secret
metadata: { name: registry-pull, namespace: image-prepuller }
type: kubernetes.io/dockerconfigjson
stringData:
  .dockerconfigjson: $DOCKERCFG
EOF
sops -e -i registry-pull.enc.yaml
yq -i '.resources += ["registry-pull.enc.yaml"]' kustomization.yaml
```

## Node disk sizing

Pre-pulling everything means every node needs disk headroom for the FULL
image set, not just whatever one node happens to run. Docker's layer sharing
softens this a lot in practice — `image-prepuller-challenges`'s workspace
images (e.g. `image_detectors-workspace`, `puzzle-workspace`) are `FROM` the
same `coding`/`dl` bases `image-prepuller-platform` already pulls, so only the
incremental layers on top actually cost extra disk, not each image's full
reported size again. Still worth watching node disk pressure as more
challenges/workspaces are added — this trades node disk for player wait time,
it doesn't eliminate the cost.

## Verified

Applied for real against the live cluster (not just `kustomize build`/dry-run):
`image-prepuller-platform` reached `6/6 Running` on the one existing node,
including the guard container under the full hardened `securityContext`
(read-only rootfs, all capabilities dropped, non-root, `RuntimeDefault`
seccomp) — then torn down again, since rolling this out for real is a Flux/IaC
decision, not something to leave applied ad-hoc.
