# Cloud cluster — Flux bootstrap

Same GitOps mechanism as `kind` (shared `clusters/base`); only `cluster-config.yaml`
and the secret delivery differ. On cloud the `ctf-school-secret` HMAC secret ships
**SOPS-encrypted** in `./secrets/secrets.enc.yaml` and is decrypted in-cluster.

## Order of operations

```
root (flux-system)  → applies only CRs (Kustomizations) — no secret-in-missing-ns
infrastructure      → creates namespaces (ctfd, controller-system, monitoring, …)
  ├─ secrets   (dependsOn infrastructure) → SOPS-decrypts ctf-school-secret → both ns
  └─ controller(dependsOn infrastructure) → Deployment mounts the secret (secretKeyRef)
```

The `secrets` Kustomization has `decryption.provider: sops`, `secretRef: sops-age`.
That `sops-age` Secret holds the **age private key** and must exist in `flux-system`
before `secrets` reconciles. It is supplied out-of-band (below) — never committed.

## One-time bootstrap

1. **Install Flux controllers** on the cluster (kustomize-controller must be built
   with SOPS support — the stock images are):
   ```sh
   flux install
   ```

2. **Load the age private key** as the decryption Secret. The key is on the dev
   machine at `ctfd/.sops-age-key.txt` (gitignored) and in the password manager:
   ```sh
   kubectl create namespace flux-system --dry-run=client -o yaml | kubectl apply -f -
   cat ctfd/.sops-age-key.txt | kubectl -n flux-system create secret generic sops-age \
     --from-file=age.agekey=/dev/stdin
   ```
   Flux looks for keys under the `*.agekey` data key — the filename matters.

3. **Point Flux at this repo** (adjust the source in `flux-system.yaml`; private
   repo needs a read-only deploy key), then let it reconcile. `secrets` will wait
   on `infrastructure`, decrypt, and create `ctf-school-secret` in both namespaces.

## Day-2

- **Edit the secret:** `sops deploy/clusters/cloud/secrets/secrets.enc.yaml`
  (needs the private key in `~/.config/sops/age/keys.txt` or `SOPS_AGE_KEY_FILE`).
- **Rotate recipients:** edit the `age:` list in the repo-root `.sops.yaml`, then
  `sops updatekeys deploy/clusters/cloud/secrets/secrets.enc.yaml`.
- **Rotate the secret value:** open with `sops`, change `secret`/`metrics_token`,
  save — Flux re-applies. Restart the CTFd/controller pods to pick up the new env.

> The public recipient in `.sops.yaml` is committed (safe). The private key is not.
> Anyone with the private key can decrypt every cloud secret — guard it accordingly.
