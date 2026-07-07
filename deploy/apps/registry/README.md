# Task-image registry

A private in-cluster container registry (CNCF **Distribution**, `registry:2`) for
**challenge / task images**. Challenge images bake challenge source, so — unlike the
platform desktop images, which live on public Docker Hub (`explabs/ctf-school-desktop-*`)
— they must never leave the cluster. This registry keeps them private, TLS-fronted, and
network-isolated.

```
registry.${CTF_DOMAIN}  ──HTTPS(wildcard cert)──▶  Envoy Gateway  ──▶  registry Service :5000
                                                       │
        anything else in the cluster ──────────────────┘  ✗ blocked by NetworkPolicy
```

## Properties

| Requirement | How |
|---|---|
| Isolated from apps/tasks | own `registry` namespace + `NetworkPolicy` — **only `envoy-gateway-system` may reach :5000**; egress limited to DNS |
| HTTPRoute + HTTPS wildcard cert | `httproute.yaml` attaches to the ctfd Gateway's **`https-wildcard`** listener (terminates TLS with `ctfd-tls`); HTTP :80 deliberately not attached |
| Secure storage | basic auth (`htpasswd`, bcrypt) + persistent PVC; restricted PSA; non-root, drop-ALL, RO rootfs |
| Large images | `BackendTrafficPolicy` raises the request/idle timeouts for multi-GB layers |

## Bootstrap the auth secret (per cluster)

The registry Deployment waits for a `registry-auth` Secret (keys `htpasswd`, `http_secret`)
in the `registry` namespace. Supply it via the same SOPS flow as `ctf-school-secret`:

```bash
# 1) pick credentials and generate the pieces
USER=ctf-ci; PASS=$(openssl rand -base64 18)
HTPASSWD=$(docker run --rm --entrypoint htpasswd httpd:2 -Bbn "$USER" "$PASS")
HTTP_SECRET=$(openssl rand -hex 32)

# 2) render the Secret for the target cluster (kind | cloud) and SOPS-encrypt it
#    (.sops.yaml auto-encrypts *.enc.yaml under clusters/*/secrets/)
cd deploy/clusters/cloud/secrets      # or clusters/kind/secrets
cat > registry-auth.enc.yaml <<EOF
apiVersion: v1
kind: Secret
metadata: { name: registry-auth, namespace: registry }
type: Opaque
stringData:
  htpasswd: |
    $HTPASSWD
  http_secret: $HTTP_SECRET
EOF
sops -e -i registry-auth.enc.yaml

# 3) add it to that cluster's secrets kustomization
yq -i '.resources += ["registry-auth.enc.yaml"]' kustomization.yaml

# 4) keep $USER/$PASS in your password manager — the CI/Makefile logs in with them
```

Flux's `secrets` Kustomization decrypts and applies it after `infrastructure` creates the
`registry` namespace.

## Pushing challenge images

The `llm-ctf-2026` Makefile pushes challenge images here:

```bash
make -C llm-ctf-2026 deploy-prod \
    REGISTRY=registry.${CTF_DOMAIN} TAG=latest \
    REGISTRY_USER=ctf-ci REGISTRY_PASSWORD='…' \
    CTFD_URL=https://ctf.school CTFD_TOKEN=ctfd_…
```

## Pulling into lab pods (prod)

For nodes to pull private challenge images, the controller stamps a `dockerconfigjson`
Secret (`task-registry-pull`) into every session namespace and sets `imagePullSecrets` on
the workspace + challenge pods. Give it the registry credentials via
`CTF_REGISTRY_PULLSECRET` — a `~/.docker/config.json` blob, wired from the controller
secret (`clusters/base/sync.yaml`, key `registry_dockerconfigjson`, `optional: true`):

```bash
# build the docker config for the task-registry and add it to ctf-school-secret
AUTH=$(printf '%s:%s' "$USER" "$PASS" | base64)
DOCKERCFG=$(printf '{"auths":{"registry.%s":{"username":"%s","password":"%s","auth":"%s"}}}' \
  "$CTF_DOMAIN" "$USER" "$PASS" "$AUTH")
# then, in the cluster's SOPS secret (clusters/<cluster>/secrets/secrets.enc.yaml under
# stringData), add:   registry_dockerconfigjson: <DOCKERCFG>   and `sops updatekeys`.
```

When `CTF_REGISTRY_PULLSECRET` is unset the feature is **off** — no secret, no
`imagePullSecrets`. That's the kind/dev default (images are `kind load`ed straight onto
the nodes) and is also fine when every challenge image is public.
