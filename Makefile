# CTF School — local bootstrap.
#
# The platform is deployed by **Flux GitOps** (deploy/), exactly like production.
# This Makefile only does what GitOps cannot: create the kind cluster, install the
# CNI (before Flux), build+load local dev images, bootstrap Flux, and set /etc/hosts.
# Local and prod differ ONLY in: `cluster`, `images` (local build vs registry),
# and `hosts`. Everything else is reconciled from Git by Flux on both.

# ── versions ──────────────────────────────────────────────────────────────────
# Cilium is the CNI — installed at bootstrap, before Flux can schedule. All other
# component versions are Flux-managed (deploy/infrastructure/operators.yaml).
CILIUM_VER ?= 1.19.0

# ── config ────────────────────────────────────────────────────────────────────
CLUSTER := ctfd
NS      := ctfd
DOMAIN  := ctf.school.local

# Flux GitOps source. The repo URL lives in $(FLUX_PATH)/flux-system.yaml
# (read-only, no bootstrap). Set it there + the controller/challenges URLs in
# deploy/clusters/kind/sources.yaml.
FLUX_PATH ?= deploy/clusters/kind

# Local dev images (build+load into kind). For prod, push these to a registry
# (e.g. GHCR / your cloud registry) and reference them in the manifests instead.
# These sibling paths are LOCAL — they assume the repos are checked out alongside.
IMAGE ?= ctfd-lab
TAG   ?= latest
# Sibling repo paths (no inline comments — Make keeps trailing spaces in values).
# For prod these become registry images, not local builds.
CONTROLLER_DIR ?= ../ctf-school-controller
GUARD_DIR      ?= ../workspace-guard
# Desktop builds the default Dockerfile; variants: Dockerfile.{pentest,programming}.
DESKTOP_DIR    ?= ../vpc/ctf-desktop

BOLD  := $(shell tput bold 2>/dev/null)
GREEN := $(shell tput setaf 2 2>/dev/null)
NC    := $(shell tput sgr0 2>/dev/null)
step  = @printf '\n$(BOLD)$(GREEN)▶ $(1)$(NC)\n'

.PHONY: all cluster cilium images build load flux hosts dev destroy ip logs

## Full local bootstrap (diverges from prod only in cluster/images/hosts).
all: cluster cilium images flux hosts
	@printf '\n$(BOLD)$(GREEN)✓ Done.$(NC) Flux is reconciling the platform from Git.\n'
	@printf '   Watch:  flux get kustomizations --watch\n'
	@$(MAKE) --no-print-directory ip

# ── cluster ───────────────────────────────────────────────────────────────────
cluster:
	$(call step,Ensuring Kind cluster '$(CLUSTER)' exists)
	@kind get clusters 2>/dev/null | grep -qx '$(CLUSTER)' \
	  && echo "  cluster already exists, skipping" \
	  || kind create cluster --config kind.yaml
	@printf '\n$(BOLD)NOTE:$(NC) start cloud-provider-kind for LoadBalancer IPs:\n'
	@echo '  sudo cloud-provider-kind   (keep running in a separate terminal)'

# ── CNI (Cilium) ───────────────────────────────────────────────────────────────
# kind.yaml disables the default CNI, so Cilium must be installed before any
# workload. It is the CNI (a Flux chicken-and-egg), so it stays a bootstrap step.
# It enforces the per-session lab NetworkPolicies.
cilium:
	$(call step,Installing Cilium $(CILIUM_VER) (CNI + NetworkPolicy))
	helm repo add cilium https://helm.cilium.io/ 2>/dev/null || true
	helm repo update cilium
	helm upgrade --install cilium cilium/cilium \
	  --namespace kube-system --version $(CILIUM_VER) \
	  --set image.pullPolicy=IfNotPresent \
	  --set ipam.mode=kubernetes \
	  --wait --timeout 180s
	kubectl -n kube-system rollout status ds/cilium --timeout=180s

# ── local dev images ───────────────────────────────────────────────────────────
build:
	$(call step,Building $(IMAGE):$(TAG))
	docker build -t $(IMAGE):$(TAG) .

load: build
	$(call step,Loading $(IMAGE):$(TAG) into kind '$(CLUSTER)')
	kind load docker-image $(IMAGE):$(TAG) --name $(CLUSTER)

## Build + load ALL local images (ctfd, controller, guard, desktop) — dev only.
images: load
	$(call step,Building + loading controller / guard / desktop (LOCAL dev images))
	@if [ -f "$(CONTROLLER_DIR)/Dockerfile" ]; then \
	  docker build -t ctf-school-controller:latest "$(CONTROLLER_DIR)" && \
	  kind load docker-image ctf-school-controller:latest --name $(CLUSTER); \
	else echo "  (skip controller: $(CONTROLLER_DIR) not found)"; fi
	@if [ -f "$(GUARD_DIR)/Dockerfile" ]; then \
	  docker build -t ctf-school-guard:latest "$(GUARD_DIR)" && \
	  kind load docker-image ctf-school-guard:latest --name $(CLUSTER); \
	else echo "  (skip guard: $(GUARD_DIR) not found)"; fi
	@if [ -f "$(DESKTOP_DIR)/Dockerfile" ]; then \
	  docker build -t vpc/ctf-desktop:latest "$(DESKTOP_DIR)" && \
	  kind load docker-image vpc/ctf-desktop:latest --name $(CLUSTER); \
	else echo "  (skip desktop: $(DESKTOP_DIR) not found)"; fi

# ── Flux (the deployment) ──────────────────────────────────────────────────────
# Installs the Flux controllers and points them at this repo READ-ONLY — no
# `flux bootstrap`, so Flux never writes to your repo and needs no broad PAT.
# Public repo → no credentials. Private repo → create a read-only deploy key
# first (see deploy/README.md), then re-run.  Same flow for prod (clusters/cloud).
flux:
	$(call step,Installing Flux controllers)
	flux check --pre
	flux install
	$(call step,Pointing Flux at the repo (read-only) → reconciling $(FLUX_PATH))
	kubectl apply -f $(FLUX_PATH)/flux-system.yaml
	$(call step,Waiting for Flux to reconcile the platform (MariaDB ~5 min))
	kubectl -n flux-system wait kustomization/gateway --for=condition=Ready --timeout=900s || \
	  echo "  gateway not ready yet — check: flux get kustomizations"

# ── helpers ────────────────────────────────────────────────────────────────────
## Local image iteration: rebuild ctfd + restart (Flux keeps managing the rest).
dev: load
	kubectl -n $(NS) rollout restart deployment/ctfd

hosts:
	$(call step,Updating /etc/hosts for $(DOMAIN))
	$(eval GW_IP := $(shell kubectl -n $(NS) get gateway ctfd -o jsonpath='{.status.addresses[0].value}' 2>/dev/null))
	@test -n "$(GW_IP)" || (echo "Gateway has no IP yet — is cloud-provider-kind running / Flux reconciled?"; exit 1)
	@grep -v ' $(DOMAIN)' /etc/hosts > /tmp/hosts-ctfd.tmp
	@echo '$(GW_IP) $(DOMAIN)' >> /tmp/hosts-ctfd.tmp
	@echo '$(GW_IP) grafana.$(DOMAIN)' >> /tmp/hosts-ctfd.tmp
	@sudo cp /tmp/hosts-ctfd.tmp /etc/hosts
	@rm -f /tmp/hosts-ctfd.tmp
	@echo "  → https://$(DOMAIN)   https://grafana.$(DOMAIN)"
	@echo "  NOTE: per-session workspaces use *.$(DOMAIN) subdomains — use dnsmasq"
	@echo "        (address=/$(DOMAIN)/$(GW_IP)) for wildcard resolution."

ip:
	@kubectl -n $(NS) get gateway ctfd \
	  -o jsonpath='{.status.addresses[0].value}{"\n"}' 2>/dev/null \
	  || echo '(no IP yet — is cloud-provider-kind running?)'

logs:
	kubectl -n $(NS) logs -l app=ctfd -f --max-log-requests 10

destroy:
	$(call step,Deleting Kind cluster '$(CLUSTER)')
	kind delete cluster --name $(CLUSTER) 2>/dev/null || true
