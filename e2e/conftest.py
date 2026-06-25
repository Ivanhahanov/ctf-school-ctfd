"""
Shared fixtures & helpers for the platform e2e tests.

Prerequisites:
  - kubectl pointed at the cluster (for the cluster/security tests)
  - For the CTFd-integration tests: a reachable CTFd and an admin access token.

Environment:
  CTFD_URL          default https://ctf.school.local
  CTFD_TOKEN        CTFd admin access token (integration tests skip without it)
  CTFD_INSECURE     "1" to skip TLS verify (dev self-signed)
  CTF_SCHOOL_SECRET shared HMAC secret (default ctf-school-secret-key)
  GW_IP             gateway IP (auto-detected via kubectl if unset)
"""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import time

import pytest
import requests
import urllib3

urllib3.disable_warnings()  # self-signed dev certs


def gw_request(gw_ip, host, path="/", cookie=None, accept=None):
    """
    GET through the gateway: connect to the gateway IP but present `host` as both
    the TLS SNI and the Host header, so Envoy routes by the workspace hostname
    without needing real DNS. Returns an urllib3 HTTPResponse (.status, .data).
    """
    pool = urllib3.HTTPSConnectionPool(
        gw_ip, port=443, cert_reqs="CERT_NONE", assert_hostname=False,
        server_hostname=host, retries=False, timeout=8.0,
    )
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = f"lab_auth={cookie}"
    if accept:
        headers["Accept"] = accept
    return pool.request("GET", path, headers=headers, redirect=False)

CTFD_URL = os.environ.get("CTFD_URL", "https://ctf.school.local").rstrip("/")
CTFD_TOKEN = os.environ.get("CTFD_TOKEN")
INSECURE = os.environ.get("CTFD_INSECURE", "1") == "1"
SECRET = os.environ.get("CTF_SCHOOL_SECRET", "ctf-school-secret-key").encode()
VERIFY = not INSECURE


# ── crypto helpers (must mirror the platform) ──────────────────────────────────
def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(team, ttl=3600):
    h = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    p = _b64(json.dumps({"team": team, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    s = _b64(hmac.new(SECRET, (h + "." + p).encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"


def derive_flag(service, salt, template="CTF{%s}"):
    digest = hmac.new(SECRET, (service + salt).encode(), hashlib.sha256).hexdigest()[:12]
    return template % digest


# ── kubectl helpers ────────────────────────────────────────────────────────────
def kubectl(*args, check=True):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=check)


def gateway_ip():
    if os.environ.get("GW_IP"):
        return os.environ["GW_IP"]
    out = kubectl("-n", "ctfd", "get", "gateway", "ctfd",
                  "-o", "jsonpath={.status.addresses[0].value}").stdout.strip()
    if not out:
        pytest.skip("no gateway IP available")
    return out


def apply_labsession(name, salt, challenge_id, labspace):
    manifest = f"""apiVersion: core.ctf.school/v1
kind: LabSession
metadata:
  name: {name}
  labels: {{ ctfd/salt: "{salt}", ctfd/challenge-id: "{challenge_id}" }}
spec: {{ userId: "{salt}", labSpaceRef: "{labspace}" }}
"""
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, check=True)


def wait_running(name, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = kubectl("get", "labsession", name, "-o", "jsonpath={.status.phase}", check=False)
        if r.stdout.strip() == "Running":
            return True
        time.sleep(3)
    return False


def delete_labsession(name):
    kubectl("delete", "labsession", name, "--ignore-not-found", "--wait=false", check=False)


# ── fixtures ───────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def gw():
    return gateway_ip()


@pytest.fixture(scope="session")
def admin():
    """An admin requests session, or skip the test if no token is configured."""
    if not CTFD_TOKEN:
        pytest.skip("CTFD_TOKEN not set — skipping CTFd integration test")
    s = requests.Session()
    s.verify = VERIFY
    s.headers.update({"Authorization": f"Token {CTFD_TOKEN}",
                      "Content-Type": "application/json"})
    return s


@pytest.fixture()
def lab_session():
    """A throwaway LabSession (team te2e) torn down after the test. The name must
    follow the plugin convention lab-<salt>-c<id>, since the workspace hostname
    is derived from the session name."""
    salt, cid, space = "te2e", "999", "joy"
    name = f"lab-{salt}-c{cid}"
    # ensure the joy LabSpace/Service exist for this to schedule
    delete_labsession(name)
    apply_labsession(name, salt, cid, space)
    try:
        assert wait_running(name, timeout=150), "lab did not reach Running"
        yield {"name": name, "salt": salt, "challenge_id": cid}
    finally:
        delete_labsession(name)
