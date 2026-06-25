# Monitoring

Observability for the cluster, the labs, and the anti-AI guards — all IaC.

## Stack

`victoria-metrics-k8s-stack` (Helm) provides everything:

| Component | Role |
|-----------|------|
| **VMSingle** | metrics storage (Prometheus-compatible), 7d retention |
| **vmagent** | scrapes node-exporter, kubelet/cAdvisor, kube-state-metrics, and our guards |
| **kube-state-metrics** | k8s object metrics **+ our `LabSession` CRD metrics** |
| **node-exporter** | per-node CPU/mem/disk/net |
| **Grafana** | dashboards (built-in cluster ones + ours) |
| **VM operator** | reconciles the VM* CRs (incl. our `VMPodScrape`) |

## Install / update (GitOps)

Monitoring is deployed by **Flux**, not a separate command. The stack is a
HelmRelease in `deploy/infrastructure/operators.yaml` (values from
`deploy/apps/monitoring/values.yaml`), and the guard scrape config, Grafana route
and dashboards are the `deploy/apps/monitoring` Kustomization. Both reconcile as
part of the normal platform sync — editing those files and committing is the
update path. (See `deploy/README.md`.)

## Access

Grafana → **https://grafana.ctf.school.local** (self-signed in dev).
Anonymous view is enabled (read-only); admin login is `admin` / `ctfschool`
(change for production via `values.yaml`).

Add `grafana.ctf.school.local` to your wildcard DNS / hosts → gateway IP.

## Dashboards

- **Built-in** (cluster, nodes, namespaces, pods) — for capacity & resource
  pressure ("do we have enough?").
- **CTF School — Lab Operations** (`uid: ctf-lab-ops`) — running labs by
  phase/team, per-lab-namespace CPU/memory, cluster CPU/mem used %, active-lab
  table. Answers "how many labs, by whom, and what are they costing".
- **CTF School — Workspace Anti-AI** (`uid: ctf-anti-ai`) — per-team/-session
  anomaly score, automation (webdriver/headless) flags, robotic-timing event
  rate, input activity, and guard auth-denial rate. Report-only for the
  organizer; nothing is auto-blocked.

## How the custom metrics are produced

### Lab inventory (no controller code)
kube-state-metrics is given a `customResourceState` config (in `values.yaml`)
that reads the `LabSession` CRD and emits:

```
labsession_info{name, team, labspace, phase} 1
```

Query examples: `count by (phase) (labsession_info)`,
`count by (team) (labsession_info{phase="Running"})`.

### Guard / anti-AI signals
Each per-session **guard** exposes Prometheus metrics on `:8080/metrics`,
scraped via `VMPodScrape` selecting `ctf.school/role: workspace` across all lab
namespaces. `guard.js` posts weak signals to the guard's `/_guard/beacon`; the
guard aggregates them:

```
workspace_guard_anomaly_score{team, sid}      # heuristic [0,1]
workspace_guard_webdriver{team, sid}          # 1 if automation reported
workspace_guard_robotic_events_total{team,sid}# superhuman-timing input events
workspace_guard_mouse_events_total / _key_events_total
workspace_guard_auth_denied_total{team, sid}  # guard rejections
workspace_guard_beacons_total{team, sid}
```

See [SECURITY.md](SECURITY.md) for what these signals mean and their limits.

## Notes

- Retention is 7d (`vmsingle.spec.retentionPeriod`) — bump in `values.yaml` for
  longer history.
- The guard metrics are per-pod and disappear when a lab is stopped; the anomaly
  signal is meaningful only while a session is live (or via recorded history).
