# CTF School — deploy. IaC via Flux GitOps (OCI artifacts + SOPS). FOUR flows:
#
#   make ci      CI: build + PUSH all images AND OCI artifacts to the registry
#                ($(TAG) + :latest). No cluster. Run on release / in CI.
#   make dev     DEV: local kind — build + KIND-LOAD images (NOT pushed), publish the
#                (tiny) manifest artifacts, install Flux, seed. Then `make hosts`.
#   make demo    DEMO: prepare a kind cluster, then deploy like PROD — Flux PULLS the
#                pre-built :latest images + artifacts (needs `make ci` first). No build.
#   make prod    PROD: Flux only (install + secret + seed) against clusters/cloud —
#                assumes `make ci TAG=<ver>` pushed the images + artifacts.
#
# All share the same manifests, registry (Docker Hub org $(DOCKERHUB_USER)), and SOPS
# mechanism — only what runs locally differs. `docker login` before pushing.
#   dev = build here (kind-load) · demo/prod = pull pre-built · ci = build + push

# ── config ──────────────────────────────────────────────────────────────────────
CLUSTER    := ctfd
NS         := ctfd
DOMAIN     := ctf.school.local
CILIUM_VER ?= 1.19.0

# Registry = Docker Hub org (flat, so `ctf-school-` is a repo-name prefix). Override
# DOCKERHUB_USER for another namespace. Images/artifacts: docker.io/$USER/ctf-school-*.
DOCKERHUB_USER ?= explabs
REGISTRY       ?= docker.io/$(DOCKERHUB_USER)
OCI_INSECURE   ?= false
# Moving dev tag; CI/prod pass an immutable version. (No inline comment — it would
# leave trailing spaces in the value and break `:$(TAG)` / path concatenation.)
TAG            ?= dev
AGE_KEY_FILE   ?= .sops-age-key.txt
# `make prod` overrides FLUX_PATH to deploy/clusters/cloud.
FLUX_PATH      ?= deploy/clusters/kind
# CI builds MULTI-ARCH so prod (amd64) and the local demo/Mac (arm64) both run natively.
# Override e.g. PLATFORMS=linux/amd64 for an amd64-only (faster) CI build.
PLATFORMS      ?= linux/amd64,linux/arm64

# Sibling repos (checked out alongside). Challenges are script-deployed (llm-ctf-2026).
CONTROLLER_DIR ?= ../ctf-school-controller
GUARD_DIR      ?= ../workspace-guard
VPC_DIR        ?= ../vpc
# Desktop image versions (independent; match vpc/versions.mk + challenge infra.yaml).
BASE_VER    ?= 0.1.0
CODING_VER  ?= 0.1.0
DATASCI_VER ?= 0.1.0

BOLD  := $(shell tput bold 2>/dev/null)
GREEN := $(shell tput setaf 2 2>/dev/null)
NC    := $(shell tput sgr0 2>/dev/null)
step  = @printf '\n$(BOLD)$(GREEN)▶ $(1)$(NC)\n'
require-user = @test -n "$(DOCKERHUB_USER)" || { echo "set DOCKERHUB_USER=<your docker hub org>"; exit 1; }

.PHONY: dev dev-update reload-ctfd ci prod demo cluster cilium flux secret seed hosts \
        load-images push-images buildx-ensure publish reconcile destroy ip logs

# ═══════════════════════════ DEV — local kind ════════════════════════════════════
## Full local bring-up. Images are kind-loaded (not pushed); only manifest artifacts
## publish. Flux reconciles asynchronously — `hosts` is separate (Gateway IP isn't
## ready at bootstrap). cloud-provider-kind must run for the LoadBalancer IP.
dev: cluster cilium flux secret load-images publish seed
	@printf '\n$(BOLD)$(GREEN)✓ Dev bootstrapped.$(NC) Flux reconciling oci://$(REGISTRY)/ctf-school-deploy:$(TAG)\n'
	@printf '   Watch:  flux get kustomizations --watch\n'
	@printf '   Then:   make hosts    (once the Gateway has an IP — needs cloud-provider-kind)\n'

## Day-2 local iteration: rebuild+reload images, re-publish artifacts, reconcile.
dev-update: load-images publish reconcile

## Fastest inner loop: just rebuild the CTFd image + restart (Flux manages the rest).
reload-ctfd:
	docker build -t $(REGISTRY)/ctf-school-ctfd:latest . && kind load docker-image $(REGISTRY)/ctf-school-ctfd:latest --name $(CLUSTER)
	kubectl -n $(NS) rollout restart deployment/ctfd

# ═══════════════════════════ CI — build + push ═══════════════════════════════════
## Build and PUSH everything to the registry: container images + OCI artifacts, at
## $(TAG) AND :latest (so `make demo` / prod-latest can pull). No cluster touched.
## Run on release with an immutable TAG (e.g. make ci TAG=v0.1.0).
ci: push-images
	$(MAKE) publish TAG=$(TAG)
	@[ "$(TAG)" = "latest" ] || $(MAKE) publish TAG=latest
	@printf '\n$(BOLD)$(GREEN)✓ Pushed$(NC) images + artifacts to $(REGISTRY) (tags $(TAG) + latest).\n'

# ═══════════════════════════ PROD — Flux only ════════════════════════════════════
## Deploy/update prod (clusters/cloud). Only Flux: install + secret + seed. Assumes
## `make ci TAG=<ver>` already pushed the images and artifacts. Run: make prod TAG=<ver>
prod:
	$(MAKE) flux secret seed FLUX_PATH=deploy/clusters/cloud OCI_INSECURE=false TAG=$(TAG)
	@printf '\n$(BOLD)$(GREEN)✓ Prod seeded$(NC) from oci://$(REGISTRY)/ctf-school-deploy:$(TAG). Watch: flux get kustomizations\n'

# ═══════════════════════════ DEMO — prod-style on kind ═══════════════════════════
## Deploy like prod (Flux PULLS pre-built images + artifacts from the registry — no
## local build, no kind-load, no publish), but prepare a kind cluster first. Uses the
## :latest images/artifacts that `make ci` pushed. Run `make ci` first, then `make demo`.
demo:
	$(MAKE) cluster cilium flux secret seed TAG=latest
	@printf '\n$(BOLD)$(GREEN)✓ Demo bootstrapped.$(NC) Flux PULLS $(REGISTRY)/ctf-school-* :latest from the registry.\n'
	@printf '   Watch:  flux get kustomizations --watch\n'
	@printf '   Then:   make hosts    (once the Gateway has an IP — needs cloud-provider-kind)\n'

# ── shared building blocks ────────────────────────────────────────────────────────
cluster:
	$(call step,Ensuring kind cluster '$(CLUSTER)')
	@kind get clusters 2>/dev/null | grep -qx '$(CLUSTER)' \
	  && echo "  exists, skipping" || kind create cluster --config kind.yaml
	@printf '\n$(BOLD)NOTE:$(NC) for LoadBalancer IPs run in a separate terminal:\n'
	@echo '  sudo cloud-provider-kind --gateway-channel=disabled'

cilium:
	$(call step,Installing Cilium $(CILIUM_VER) (CNI + NetworkPolicy))
	helm repo add cilium https://helm.cilium.io/ 2>/dev/null || true
	helm repo update cilium >/dev/null
	helm upgrade --install cilium cilium/cilium --namespace kube-system \
	  --version $(CILIUM_VER) --set image.pullPolicy=IfNotPresent \
	  --set ipam.mode=kubernetes --wait --timeout 180s
	kubectl -n kube-system rollout status ds/cilium --timeout=180s

flux:
	$(call step,Installing Flux controllers)
	flux check --pre
	flux install

secret:
	$(call step,Loading the SOPS age key as 'sops-age' (flux-system))
	@test -f "$(AGE_KEY_FILE)" || { echo "  age key not found: $(AGE_KEY_FILE)"; exit 1; }
	@kubectl create namespace flux-system --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@kubectl -n flux-system create secret generic sops-age \
	  --from-file=age.agekey="$(AGE_KEY_FILE)" --dry-run=client -o yaml | kubectl apply -f -

# DEV image delivery: build + kind-load (no registry push). Names match cluster-config.
load-images:
	$(call step,Building + kind-loading dev images ($(REGISTRY)/ctf-school-*))
	@docker build -t $(REGISTRY)/ctf-school-ctfd:latest . && kind load docker-image $(REGISTRY)/ctf-school-ctfd:latest --name $(CLUSTER)
	@if [ -f "$(CONTROLLER_DIR)/Dockerfile" ]; then \
	  docker build -t $(REGISTRY)/ctf-school-controller:latest "$(CONTROLLER_DIR)" && \
	  kind load docker-image $(REGISTRY)/ctf-school-controller:latest --name $(CLUSTER); fi
	@if [ -f "$(GUARD_DIR)/Dockerfile" ]; then \
	  docker build -t $(REGISTRY)/ctf-school-guard:latest "$(GUARD_DIR)" && \
	  kind load docker-image $(REGISTRY)/ctf-school-guard:latest --name $(CLUSTER); fi
	@if [ -f "$(VPC_DIR)/Makefile" ]; then \
	  $(MAKE) -C "$(VPC_DIR)" base coding datasci REGISTRY=$(REGISTRY) && \
	  kind load docker-image $(REGISTRY)/ctf-school-desktop-base:$(BASE_VER)    --name $(CLUSTER) && \
	  kind load docker-image $(REGISTRY)/ctf-school-desktop-coding:$(CODING_VER)  --name $(CLUSTER) && \
	  kind load docker-image $(REGISTRY)/ctf-school-desktop-datasci:$(DATASCI_VER) --name $(CLUSTER); fi

# Ensure a buildx builder that supports multi-platform (the default `docker` driver
# does not). The docker-container driver bundles QEMU for cross-arch emulation.
buildx-ensure:
	@docker buildx inspect ctf-builder >/dev/null 2>&1 || docker buildx create --name ctf-builder --driver docker-container >/dev/null
	@docker buildx use ctf-builder

# CI image delivery: build MULTI-ARCH ($(PLATFORMS)) + PUSH, tagged $(TAG) AND :latest
# (so `make demo`/prod-latest pull the right arch). buildx builds + pushes in one step.
# Needs `docker login`. Emulated builds (the desktop under amd64 on an arm64 host) are slow.
push-images: buildx-ensure
	$(require-user)
	$(call step,Building + PUSHING multi-arch ($(PLATFORMS)) to $(REGISTRY) — tags $(TAG) + latest)
	docker buildx build --platform $(PLATFORMS) \
	  -t $(REGISTRY)/ctf-school-ctfd:$(TAG) -t $(REGISTRY)/ctf-school-ctfd:latest --push .
	@if [ -d "$(CONTROLLER_DIR)" ]; then \
	  docker buildx build --platform $(PLATFORMS) \
	    -t $(REGISTRY)/ctf-school-controller:$(TAG) -t $(REGISTRY)/ctf-school-controller:latest --push "$(CONTROLLER_DIR)"; fi
	@if [ -d "$(GUARD_DIR)" ]; then \
	  docker buildx build --platform $(PLATFORMS) \
	    -t $(REGISTRY)/ctf-school-guard:$(TAG) -t $(REGISTRY)/ctf-school-guard:latest --push "$(GUARD_DIR)"; fi
	@if [ -f "$(VPC_DIR)/Makefile" ]; then $(MAKE) -C "$(VPC_DIR)" push REGISTRY=$(REGISTRY) PLATFORMS="$(PLATFORMS)"; fi

# OCI manifest artifacts (tiny) — used by BOTH dev (working tree) and CI. Not images.
publish:
	$(require-user)
	$(call step,Publishing OCI manifest artifacts to $(REGISTRY) (tag $(TAG)))
	flux push artifact oci://$(REGISTRY)/ctf-school-deploy:$(TAG) \
	  --path=. --source=ctf-school-ctfd --revision=$(TAG)
	@if [ -d "$(CONTROLLER_DIR)" ]; then \
	  flux push artifact oci://$(REGISTRY)/ctf-school-controller-config:$(TAG) \
	    --path="$(CONTROLLER_DIR)" --source=ctf-school-controller --revision=$(TAG); fi

# Apply the OCI sources + root Kustomization (the bootstrap seed) for $(FLUX_PATH).
seed:
	$(require-user)
	$(call step,Applying Flux OCI sources → reconciling $(FLUX_PATH))
	@sed -e 's|$${REGISTRY}|$(REGISTRY)|g' \
	     -e 's|$${TAG}|$(TAG)|g' \
	     -e 's|$${OCI_INSECURE}|$(OCI_INSECURE)|g' \
	     $(FLUX_PATH)/flux-system.yaml | kubectl apply -f -

reconcile:
	$(call step,Reconciling Flux from the freshly-published artifacts)
	flux reconcile source oci flux-system
	-flux reconcile source oci controller
	flux reconcile kustomization flux-system --with-source

## Point /etc/hosts at the Gateway IP. Waits (~60s) for the LoadBalancer IP, and does
## NOTHING (no bad write, no sudo prompt) if it never appears.
hosts:
	$(call step,Updating /etc/hosts for $(DOMAIN))
	@ip=""; \
	for i in $$(seq 1 30); do \
	  ip=$$(kubectl -n $(NS) get gateway ctfd -o jsonpath='{.status.addresses[0].value}' 2>/dev/null); \
	  [ -n "$$ip" ] && break; \
	  [ $$i = 1 ] && echo "  waiting for the Gateway LoadBalancer IP…"; sleep 2; \
	done; \
	if [ -z "$$ip" ]; then \
	  echo "  Gateway still has no IP — is cloud-provider-kind running and has Flux reconciled the gateway?"; \
	  echo "  Check: kubectl -n $(NS) get gateway ctfd   — then re-run: make hosts"; \
	  exit 0; \
	fi; \
	grep -v '$(DOMAIN)' /etc/hosts > /tmp/hosts-ctfd.tmp || true; \
	printf '%s %s\n%s grafana.%s\n' "$$ip" "$(DOMAIN)" "$$ip" "$(DOMAIN)" >> /tmp/hosts-ctfd.tmp; \
	sudo cp /tmp/hosts-ctfd.tmp /etc/hosts && rm -f /tmp/hosts-ctfd.tmp; \
	echo "  → https://$(DOMAIN)   https://grafana.$(DOMAIN)  ($$ip; wildcard *.$(DOMAIN) via dnsmasq)"

ip:
	@kubectl -n $(NS) get gateway ctfd -o jsonpath='{.status.addresses[0].value}{"\n"}' 2>/dev/null \
	  || echo '(no IP yet — is cloud-provider-kind running?)'

logs:
	kubectl -n $(NS) logs -l app=ctfd -f --max-log-requests 10

destroy:
	$(call step,Deleting kind cluster '$(CLUSTER)')
	kind delete cluster --name $(CLUSTER) 2>/dev/null || true
