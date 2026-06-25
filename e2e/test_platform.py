"""
Platform end-to-end tests.

Two groups:
  • security/cluster  — run with just kubectl + the shared secret (no CTFd token).
  • ctfd integration  — registration, challenge creation, flag submission; these
                        skip automatically unless CTFD_TOKEN is set.

Run:  pytest ctfd/e2e -v
"""
import uuid

import requests
from conftest import (
    CTFD_URL, VERIFY, derive_flag, gw_request, kubectl, make_token,
)


def _host(lab):
    return f"workspace-{lab['salt']}-c{lab['challenge_id']}.ctf.school.local"


# ════════════════════════════════════════════════════════════════════════════
# Security: workspace authorization (the per-session guard)
# ════════════════════════════════════════════════════════════════════════════
class TestWorkspaceAuthorization:
    def test_guard_rejects_without_token(self, gw, lab_session):
        r = gw_request(gw, _host(lab_session), "/vnc.html")
        assert r.status == 403, f"expected 403 without token, got {r.status}"

    def test_guard_accepts_own_team_token(self, gw, lab_session):
        r = gw_request(gw, _host(lab_session), "/vnc.html", cookie=make_token(lab_session["salt"]))
        assert r.status == 200, f"own-team token should be 200, got {r.status}"

    def test_guard_rejects_other_team_token(self, gw, lab_session):
        r = gw_request(gw, _host(lab_session), "/vnc.html", cookie=make_token("t-someone-else"))
        assert r.status == 403, f"wrong-team token must be 403, got {r.status}"

    def test_guard_rejects_tampered_token(self, gw, lab_session):
        r = gw_request(gw, _host(lab_session), "/vnc.html", cookie=make_token(lab_session["salt"]) + "x")
        assert r.status == 403, f"tampered token must be 403, got {r.status}"


# ════════════════════════════════════════════════════════════════════════════
# Anti-AI: watermark injection
# ════════════════════════════════════════════════════════════════════════════
class TestAntiAI:
    def test_watermark_injected(self, gw, lab_session):
        r = gw_request(gw, _host(lab_session), "/vnc.html", cookie=make_token(lab_session["salt"]))
        body = r.data.decode("utf-8", "ignore")
        assert "__GUARD" in body and "guard.js" in body, "watermark not injected"
        assert lab_session["salt"] in body, "watermark missing the session team label"


# ════════════════════════════════════════════════════════════════════════════
# Flags: generation (controller) == validation (CTFd)
# ════════════════════════════════════════════════════════════════════════════
class TestFlags:
    def test_injected_flag_matches_derivation(self, lab_session):
        ns = f"lab-session-{lab_session['name']}"
        # the joy service is named "joy"; flag = HMAC(secret, "joy"+team)
        out = kubectl("-n", ns, "get", "pod", "joy",
                      "-o", "jsonpath={range .spec.containers[0].env[?(@.name=='FLAG')]}{.value}{end}",
                      check=False).stdout.strip()
        expected = derive_flag("joy", lab_session["salt"])
        assert out == expected, f"injected {out!r} != derived {expected!r}"

    def test_flag_is_per_team_unique(self):
        a = derive_flag("joy", "t1")
        b = derive_flag("joy", "t2")
        assert a != b, "flags must differ per team"


# ════════════════════════════════════════════════════════════════════════════
# CTFd integration: registration, challenge creation, flag submission
# (skips automatically unless CTFD_TOKEN is set — see conftest `admin` fixture)
# ════════════════════════════════════════════════════════════════════════════
def _csrf(session):
    # CTFd exposes the nonce inline in every page as window.init.csrfNonce
    page = session.get(f"{CTFD_URL}/login", verify=VERIFY, timeout=10).text
    import re
    m = re.search(r"'csrfNonce':\s*\"([0-9a-f]+)\"", page)
    assert m, "could not read csrfNonce"
    return m.group(1)


class TestCTFdIntegration:
    def test_registration(self, admin):
        s = requests.Session()
        s.verify = VERIFY
        nonce = _csrf(s)
        name = "e2e_" + uuid.uuid4().hex[:8]
        r = s.post(f"{CTFD_URL}/register",
                   data={"name": name, "email": f"{name}@e2e.local",
                         "password": "e2e-pass-123", "nonce": nonce},
                   verify=VERIFY, timeout=10, allow_redirects=True)
        # success → redirected into the app (challenges/team selection), not back to register
        assert r.status_code in (200, 302)
        assert "/register" not in r.url, "registration appears to have failed"

    def test_challenge_creation(self, admin):
        r = admin.get(f"{CTFD_URL}/api/v1/challenges?view=admin", timeout=10)
        assert r.ok
        names = [c["name"] for c in r.json().get("data", [])]
        assert "Joy" in names, "Joy challenge not found — run tools/load_challenge.py first"

    def test_lab_challenge_has_dynamic_flag_config(self, admin):
        data = admin.get(f"{CTFD_URL}/api/v1/challenges?view=admin", timeout=10).json()["data"]
        joy = next((c for c in data if c["name"] == "Joy"), None)
        assert joy, "Joy challenge missing"
        detail = admin.get(f"{CTFD_URL}/api/v1/challenges/{joy['id']}", timeout=10).json()["data"]
        assert detail.get("type") == "lab"
        assert detail.get("flag_service") == "joy"
