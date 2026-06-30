#!/usr/bin/env bash
#
# Ephemeral wildcard DNS for *.ctf.school.local — turn it on for a test session,
# turn it off and leave the Mac clean. Nothing is installed on the host:
#
#   * the DNS server is a throwaway Docker container (dnsmasq in alpine, --rm)
#   * the only host-side file is /etc/resolver/<domain>, removed again on `down`
#
# macOS routes *.<domain> queries to 127.0.0.1 (the container), which answers
# every subdomain with the gateway IP. No /etc/hosts edits, no brew, no launchd.
#
# Usage:
#   ./dns.sh up           # start: container + /etc/resolver entry  (asks sudo)
#   ./dns.sh down          # stop : remove both, flush cache
#   ./dns.sh status        # show what's running / configured
#
#   GW_IP=192.168.97.7 ./dns.sh up     # pin the IP instead of asking kubectl
#
set -euo pipefail

DOMAIN="${DOMAIN:-ctf.school.local}"
NS="${NS:-ctfd}"
GATEWAY="${GATEWAY:-ctfd}"
NAME="ctf-dns"                       # container name
RESOLVER="/etc/resolver/$DOMAIN"

flush_cache() { sudo dscacheutil -flushcache 2>/dev/null || true; sudo killall -HUP mDNSResponder 2>/dev/null || true; }

gw_ip() {
  if [ -n "${GW_IP:-}" ]; then echo "$GW_IP"; return; fi
  kubectl -n "$NS" get gateway "$GATEWAY" \
    -o jsonpath='{.status.addresses[0].value}' 2>/dev/null
}

up() {
  command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }
  local ip; ip="$(gw_ip)"
  [ -n "$ip" ] || { echo "no gateway IP (is the cluster up?). Set GW_IP=... to override." >&2; exit 1; }

  echo "==> dnsmasq container: *.$DOMAIN -> $ip"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --rm --name "$NAME" \
    -p 127.0.0.1:53:53/udp -p 127.0.0.1:53:53/tcp \
    alpine sh -c "apk add --no-cache dnsmasq >/dev/null 2>&1 &&
      exec dnsmasq -k --log-facility=- --no-resolv --no-hosts \
        --address=/$DOMAIN/$ip" >/dev/null

  echo "==> /etc/resolver/$DOMAIN -> 127.0.0.1 (sudo)"
  sudo mkdir -p /etc/resolver
  printf 'nameserver 127.0.0.1\n' | sudo tee "$RESOLVER" >/dev/null

  flush_cache
  echo "ON. Test:  ping -c1 workspace-t3-c6.$DOMAIN   (should hit $ip)"
}

down() {
  echo "==> stopping container + removing resolver"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  sudo rm -f "$RESOLVER"
  flush_cache
  echo "OFF. Mac is clean (no container, no /etc/resolver entry)."
}

status() {
  echo "container:"
  docker ps --filter "name=^/${NAME}$" --format '  {{.Names}}  {{.Status}}' 2>/dev/null | grep . || echo "  (not running)"
  echo "resolver: $( [ -f "$RESOLVER" ] && echo "$RESOLVER present" || echo "(absent)" )"
  echo "gateway IP: $(gw_ip || true)"
}

case "${1:-}" in
  up)     up ;;
  down)   down ;;
  status) status ;;
  *) echo "usage: $0 {up|down|status}" >&2; exit 1 ;;
esac
