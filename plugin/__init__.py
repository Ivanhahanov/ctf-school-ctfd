"""
CTFd Lab Manager Plugin
=======================
Challenge type «Lab» = Dynamic scoring + Kubernetes LabSession.

Flag derivation mirrors the controller's buildEnvVars (case "hmac"):
    CTF_FLAG_FORMAT % base64url(HMAC-SHA256(CTF_FLAG_SECRET, flagService+userId))[:24]

Secrets are split by purpose (security review finding #1):
  - CTF_FLAG_SECRET — HMAC key for flags, shared with the controller.
  - CTF_JWT_SECRET  — HS256 key for workspace tokens, shared with the guard.
Both fall back to the legacy shared CTF_SCHOOL_SECRET during migration (identically
on every side, so nothing drifts). CTF_FLAG_FORMAT (the wrapper, e.g. "CTF{%s}") is
likewise a platform-wide env var shared with the controller — set once, never
per-challenge.
"""

import base64
import hashlib
import hmac as _hmac
import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Blueprint, jsonify, redirect, render_template_string, request, Response

from CTFd.models import Challenges, Solves, Submissions, Teams, Users, db
from CTFd.plugins import (
    register_plugin_assets_directory,
    register_user_page_menu_bar,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES
from CTFd.plugins.dynamic_challenges import DynamicChallenge, DynamicValueChallenge
from CTFd.utils import get_config
from CTFd.utils.decorators import authed_only
from CTFd.utils.user import get_current_user

logger = logging.getLogger(__name__)

# ── Kubernetes constants ───────────────────────────────────────────────────────

MAX_CONCURRENT_LABS = int(os.environ.get("MAX_CONCURRENT_LABS", "2"))

K8S_API   = "https://kubernetes.default.svc"
SA_TOKEN  = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA     = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
SA_NS     = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

CRD_GROUP   = "core.ctf.school"
CRD_VERSION = "v1"
CRD_PLURAL  = "labsessions"

# ── Model ──────────────────────────────────────────────────────────────────────

class LabChallenge(DynamicChallenge):
    __mapper_args__ = {"polymorphic_identity": "lab"}
    __tablename__   = "lab_challenges"

    id = db.Column(
        db.Integer,
        db.ForeignKey("dynamic_challenge.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Cluster-scoped LabSpace name (the blueprint for this lab)
    lab_space_ref = db.Column(db.String(255), nullable=False, default="")
    # LabService CRD name whose hmac env-var becomes the flag
    flag_service  = db.Column(db.String(255), nullable=False, default="")
    # DEPRECATED, unused: the flag format is now platform-wide (CTF_FLAG_FORMAT).
    # Column kept so existing databases don't need a migration.
    flag_template = db.Column(db.String(255), nullable=False, default="CTF{%s}")


class LabAudit(db.Model):
    """Per (account-salt, challenge) anti-cheat ledger that CTFd's own tables don't
    keep: when the lab was FIRST started (restart-aware solve timing), how many
    times it was restarted, and how flags were submitted (transport + client).
    Read by the /lab-metrics exporter, joined in Grafana with guard engagement."""
    __tablename__ = "lab_audit"
    salt         = db.Column(db.String(64), primary_key=True)   # 't<team>' / 'u<user>'
    challenge_id = db.Column(db.Integer, primary_key=True)
    first_started = db.Column(db.DateTime)                       # FIRST ever start
    restarts      = db.Column(db.Integer, nullable=False, default=0)
    api_submits   = db.Column(db.Integer, nullable=False, default=0)
    ui_submits    = db.Column(db.Integer, nullable=False, default=0)
    last_ua       = db.Column(db.String(512), nullable=False, default="")
    last_ip       = db.Column(db.String(64), nullable=False, default="")


def _audit_get(salt: str, challenge_id: int) -> "LabAudit":
    row = LabAudit.query.filter_by(salt=salt, challenge_id=challenge_id).first()
    if row is None:
        row = LabAudit(salt=salt, challenge_id=challenge_id)
        db.session.add(row)
    return row


def _audit_record_start(salt: str, challenge_id: int) -> None:
    row = _audit_get(salt, challenge_id)
    if row.first_started is None:
        row.first_started = datetime.now(timezone.utc)
    else:
        row.restarts = (row.restarts or 0) + 1
    db.session.commit()


def _client_ip() -> str:
    # CTFd sits behind the Envoy gateway; the real client IP arrives in
    # X-Forwarded-For (left-most entry). Falls back to the socket peer.
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


def _audit_record_submit(salt: str, challenge_id: int) -> None:
    row = _audit_get(salt, challenge_id)
    via_api = "Authorization" in request.headers      # token auth => scripted
    if via_api:
        row.api_submits = (row.api_submits or 0) + 1
    else:
        row.ui_submits = (row.ui_submits or 0) + 1
    row.last_ua = (request.headers.get("User-Agent", "") or "")[:512]
    row.last_ip = _client_ip()
    db.session.commit()


# ── Salt helper ───────────────────────────────────────────────────────────────

def _current_salt() -> str:
    """
    In team mode  → 't<team_id>'   (one LabSession + one flag per team)
    In user mode  → 'u<user_id>'   (one LabSession + one flag per user)

    The same salt is passed to the controller as spec.userId, so the
    controller's HMAC derivation produces the same flag for the whole team.
    """
    user = get_current_user()
    if get_config("user_mode") == "teams" and user.team_id:
        return f"t{user.team_id}"
    return f"u{user.id}"


def _owner_name() -> str:
    """Human-readable owner (team name in team mode, else user name) — stamped on
    the LabSession so monitoring can show *which team* by name, not just salt."""
    user = get_current_user()
    if get_config("user_mode") == "teams" and getattr(user, "team", None):
        return user.team.name
    return user.name


# ── Flag derivation ────────────────────────────────────────────────────────────

# ── Workspace access token (HS256 JWT) ─────────────────────────────────────────
#
# Minted from the player's existing CTFd session, scoped to their team. Delivered
# as a parent-domain cookie so every *.<domain> workspace receives it, but each
# per-session guard accepts only tokens whose `team` matches its own — so a player
# cannot reach another team's desktop even though the URL is predictable.

WORKSPACE_TOKEN_TTL = 8 * 3600  # seconds


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _flag_secret() -> bytes:
    """HMAC key for flag derivation — must match the controller's flagSecret().
    Distinct from the JWT key (security finding #1). Falls back to the legacy shared
    CTF_SCHOOL_SECRET during migration, identically to the controller, so flags never
    drift while a cluster is still on the single secret."""
    return (os.environ.get("CTF_FLAG_SECRET")
            or os.environ.get("CTF_SCHOOL_SECRET")
            or "ctf-school-secret-key").encode()


def _jwt_secret() -> bytes:
    """HS256 key for workspace tokens — must match the guard's jwtSecret()."""
    return (os.environ.get("CTF_JWT_SECRET")
            or os.environ.get("CTF_SCHOOL_SECRET")
            or "ctf-school-secret-key").encode()


def _make_workspace_token(team: str) -> str:
    secret = _jwt_secret()
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(
        {"team": team, "exp": int(time.time()) + WORKSPACE_TOKEN_TTL},
        separators=(",", ":"),
    ).encode())
    signing = (header + "." + payload).encode()
    sig = _b64url(_hmac.new(secret, signing, hashlib.sha256).digest())
    return header + "." + payload + "." + sig


FLAG_TOKEN_LEN = 24  # must match the controller's flagTokenLen


def _flag_format() -> str:
    """Platform-wide flag wrapper (a %s format). Set once via env — the SAME value
    the controller uses (CTF_FLAG_FORMAT) — so authors never specify a per-challenge
    format and the two sides can never drift."""
    return os.environ.get("CTF_FLAG_FORMAT", "CTF{%s}")


def _derive_flag(flag_service: str, salt: str) -> str:
    """
    Mirrors the controller's hash() + flagFormat():
        CTF_FLAG_FORMAT % base64url(HMAC-SHA256(CTF_FLAG_SECRET, flag_service+salt))[:FLAG_TOKEN_LEN]
    base64url = mixed-case alphanumeric (+ -_), 24 chars ≈ 2^144 entropy.
    """
    secret = _flag_secret()
    digest = _hmac.new(secret, (flag_service + salt).encode(), hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()[:FLAG_TOKEN_LEN]
    return _flag_format() % token


# ── Challenge type ─────────────────────────────────────────────────────────────

class LabChallengeType(DynamicValueChallenge):
    id    = "lab"
    name  = "Lab Challenge"
    templates = {
        "create": "/plugins/lab_manager/assets/create.html",
        "update": "/plugins/lab_manager/assets/update.html",
        "view":   "/plugins/lab_manager/assets/view.html",
    }
    scripts = {
        "create": "/plugins/lab_manager/assets/create.js",
        "update": "/plugins/lab_manager/assets/update.js",
        "view":   "/plugins/lab_manager/assets/view.js",
    }
    route           = "/plugins/lab_manager/assets/"
    blueprint       = Blueprint("lab_manager", __name__,
                                template_folder="templates",
                                static_folder="assets")
    challenge_model = LabChallenge

    @classmethod
    def create(cls, request):
        data = request.form or request.get_json()
        challenge = LabChallenge(
            name          = data["name"],
            description   = data.get("description", ""),
            initial       = int(data.get("initial", 0)),
            minimum       = int(data.get("minimum", 0)),
            decay         = int(data.get("decay", 1)),
            function      = data.get("function", "logarithmic"),
            category      = data.get("category", ""),
            type          = "lab",
            state         = data.get("state", "hidden"),
            lab_space_ref = data.get("lab_space_ref", "").strip(),
            flag_service  = data.get("flag_service", "").strip(),
            # No flag_template: the format is platform-wide (CTF_FLAG_FORMAT).
        )
        db.session.add(challenge)
        db.session.commit()
        cls.calculate_value(challenge)
        return challenge

    @classmethod
    def read(cls, challenge):
        c    = LabChallenge.query.filter_by(id=challenge.id).first()
        data = super().read(challenge)
        if c:
            data["lab_space_ref"] = c.lab_space_ref
            data["flag_service"]  = c.flag_service
        data["type_data"].update({
            "id":        cls.id,
            "name":      cls.name,
            "templates": cls.templates,
            "scripts":   cls.scripts,
        })
        return data

    @classmethod
    def update(cls, challenge, request):
        data      = request.form or request.get_json()
        challenge = super().update(challenge, request)
        c         = LabChallenge.query.filter_by(id=challenge.id).first()
        if c:
            for field in ("lab_space_ref", "flag_service"):
                if field in data:
                    setattr(c, field, data[field].strip())
            db.session.commit()
        return challenge

    @classmethod
    def attempt(cls, challenge, request):
        """
        If flag_service is set → verify against HMAC-derived flag.
        Salt is team-scoped in team mode, user-scoped otherwise.
        Falls back to static CTFd flag check when flag_service is empty.
        """
        c = LabChallenge.query.filter_by(id=challenge.id).first()
        if not c or not c.flag_service:
            return super().attempt(challenge, request)

        data       = request.form or request.get_json()
        submission = (data.get("submission") or "").strip()
        salt       = _current_salt()
        expected   = _derive_flag(c.flag_service, salt)

        try:
            _audit_record_submit(salt, challenge.id)
        except Exception:
            logger.exception("audit: record submit failed")
            db.session.rollback()

        if submission == expected:
            return True, "Correct"
        return False, "Incorrect"


# ── Kubernetes helpers ─────────────────────────────────────────────────────────

def _read(path: str, fallback: str = "") -> str:
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        return fallback


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(SA_CA)
    except FileNotFoundError:
        pass
    return ctx


def _k8s(method: str, path: str, body=None,
         content_type: str = "application/json") -> tuple[dict, int]:
    req = Request(
        K8S_API + path,
        data    = json.dumps(body).encode() if body else None,
        method  = method,
        headers = {
            "Authorization": "Bearer " + _read(SA_TOKEN, "dummy"),
            "Content-Type":  content_type,
            "Accept":        "application/json",
        },
    )
    try:
        with urlopen(req, context=_ssl_ctx(), timeout=10) as r:
            return json.loads(r.read()), r.status
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        # 404 on GET is the normal "lab not running" signal (status polling),
        # not an error — keep it at debug so it doesn't spam the log.
        if e.code == 404 and method == "GET":
            logger.debug("[K8s] %s %s → 404", method, path)
        else:
            logger.error("[K8s] %s %s → %d: %s", method, path, e.code, raw)
        try:
            return json.loads(raw), e.code
        except json.JSONDecodeError:
            return {"message": raw}, e.code
    except Exception as exc:
        logger.exception("[K8s] %s %s failed: %s", method, path, exc)
        return {"message": str(exc)}, 500


def _ns() -> str:
    return _read(SA_NS, "default")


def _crd_path(name: str | None = None) -> str:
    base = f"/apis/{CRD_GROUP}/{CRD_VERSION}/{CRD_PLURAL}"
    return f"{base}/{name}" if name else base


def _session_name(salt: str, challenge_id: int) -> str:
    return f"lab-{salt}-c{challenge_id}"


def _lab_space_ref(challenge_id: int) -> str:
    c = LabChallenge.query.filter_by(id=challenge_id).first()
    return c.lab_space_ref if c and c.lab_space_ref else f"challenge-{challenge_id}"


# ── K8s CRUD ──────────────────────────────────────────────────────────────────

def k8s_get(salt: str, challenge_id: int):
    return _k8s("GET", _crd_path(_session_name(salt, challenge_id)))


def k8s_create(salt: str, challenge_id: int, lab_space_ref: str):
    name = _session_name(salt, challenge_id)
    return _k8s("POST", _crd_path(), {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "LabSession",
        "metadata": {
            "name": name,
            "labels": {
                "ctfd/salt":         salt,
                "ctfd/challenge-id": str(challenge_id),
            },
            "annotations": {
                # Human-readable team/user name for monitoring (kube-state-metrics
                # surfaces it on labsession_info → dashboards join guard metrics
                # by the `team` salt to show the actual team name).
                "ctf.school/owner": _owner_name(),
            },
        },
        "spec": {
            "userId":      salt,   # controller uses this as HMAC input → same flag for whole team
            "labSpaceRef": lab_space_ref,
        },
    })


def k8s_delete(salt: str, challenge_id: int):
    return _k8s("DELETE", _crd_path(_session_name(salt, challenge_id)))


def k8s_list_by_salt(salt: str) -> tuple[dict, int]:
    selector = quote(f"ctfd/salt={salt}", safe="=")
    return _k8s("GET", f"{_crd_path()}?labelSelector={selector}")


def k8s_reload(salt: str, challenge_id: int, lab_space_ref: str):
    k8s_delete(salt, challenge_id)
    return k8s_create(salt, challenge_id, lab_space_ref)


def k8s_restart(salt: str, challenge_id: int):
    """
    Fast in-place restart: bump an annotation on the LabSession. The controller
    sees a restart timestamp newer than the running pods and recreates just the
    pods (grace 2s) inside the existing namespace — no full teardown/rebuild.
    """
    # isoformat carries microseconds → unique per click (the controller compares
    # this against its own "handled" annotation by string equality).
    ts = datetime.now(timezone.utc).isoformat()
    body = {"metadata": {"annotations": {"ctf.school/restart-requested-at": ts}}}
    return _k8s(
        "PATCH",
        _crd_path(_session_name(salt, challenge_id)),
        body,
        content_type="application/merge-patch+json",
    )


# ── Status helper ──────────────────────────────────────────────────────────────

def _challenge_name(cid: int) -> str:
    if not cid:
        return "Unknown challenge"
    c = Challenges.query.filter_by(id=cid).first()
    return c.name if c else f"Challenge #{cid}"


def _my_labs(salt: str) -> dict:
    """All of this team's/user's active labs plus the concurrency limit, for the
    My Labs dashboard and the limit hint on the challenge card."""
    body, status = k8s_list_by_salt(salt)
    labs = []
    if status == 200:
        for item in body.get("items", []):
            meta = item.get("metadata", {}) or {}
            if meta.get("deletionTimestamp"):
                continue
            cid = int((meta.get("labels", {}) or {}).get("ctfd/challenge-id", 0) or 0)
            st = item.get("status", {}) or {}
            phase = st.get("phase", "Pending")
            workspace = next(
                (ep for ep in st.get("endpoints", []) if ep.get("serviceName") == "workspace"),
                None,
            )
            labs.append({
                "challenge_id":   cid,
                "challenge_name": _challenge_name(cid),
                "phase":          phase,
                "running":        phase == "Running",
                "message":        st.get("message", ""),
                "workspace":      workspace,
                "expires":        st.get("expiredTime"),
            })
    labs.sort(key=lambda l: l["challenge_name"].lower())
    return {"limit": MAX_CONCURRENT_LABS, "active": len(labs), "labs": labs}


def _count_active_sessions(salt: str) -> int:
    """Count LabSessions for this salt that are not being deleted."""
    body, status = k8s_list_by_salt(salt)
    if status != 200:
        return 0
    return sum(
        1 for item in body.get("items", [])
        if not item.get("metadata", {}).get("deletionTimestamp")
    )


def _status_payload(salt: str, challenge_id: int) -> dict:
    body, status = k8s_get(salt, challenge_id)

    if status == 404:
        return {"running": False, "phase": "stopped", "endpoints": [], "workspace": None}
    if status != 200:
        return {
            "running":   False,
            "phase":     "error",
            "endpoints": [],
            "workspace": None,
            "message":   body.get("message", "Kubernetes API error"),
        }

    s        = body.get("status") or {}
    phase    = s.get("phase", "Pending")
    all_eps  = s.get("endpoints", [])

    workspace = None
    services  = []
    for ep in all_eps:
        if ep.get("serviceName") == "workspace":
            workspace = ep
        else:
            services.append(ep)

    # Surface only the controller's high-level conditions (e.g. InfrastructureReady,
    # ServicesHealthy, Ready) — these describe progress without exposing the internal
    # workload composition (pods, images, namespaces).
    conditions = [
        {
            "type":    c.get("type", ""),
            "status":  c.get("status", ""),
            "reason":  c.get("reason", ""),
            "message": c.get("message", ""),
        }
        for c in (s.get("conditions") or [])
    ]

    return {
        "running":    phase == "Running",
        "phase":      phase,
        "workspace":  workspace,
        "endpoints":  services,
        "message":    s.get("message", ""),
        "conditions": conditions,
    }


# ── API routes ────────────────────────────────────────────────────────────────

lab_api = Blueprint("lab_api", __name__)


@lab_api.route("/api/v1/lab/<int:challenge_id>/status")
@authed_only
def lab_status(challenge_id):
    return jsonify(_status_payload(_current_salt(), challenge_id))


@lab_api.route("/api/v1/labs/mine")
@authed_only
def labs_mine():
    """This team's/user's active labs + the concurrency limit (dashboard data)."""
    return jsonify(_my_labs(_current_salt()))


@lab_api.route("/lab/<int:challenge_id>/enter")
@authed_only
def lab_enter(challenge_id):
    """
    Single sign-on into the workspace: mint a team-scoped token from the player's
    CTFd session, drop it as a parent-domain cookie, and redirect to the desktop.
    The per-session guard then admits the request only if the token's team matches.
    """
    salt    = _current_salt()
    payload = _status_payload(salt, challenge_id)
    ws      = payload.get("workspace")
    if not payload.get("running") or not ws or not ws.get("address"):
        return redirect("/challenges")

    domain = os.environ.get("CTF_DOMAIN", "ctf.school.local")
    secure = os.environ.get("CTF_SCHEME", "http") == "https"

    resp = redirect(ws["address"])
    resp.set_cookie(
        "lab_auth",
        _make_workspace_token(salt),
        max_age=WORKSPACE_TOKEN_TTL,
        domain="." + domain,   # sent to every *.<domain> workspace; guard scopes by team
        path="/",
        httponly=True,
        secure=secure,
        samesite="Lax",
    )
    return resp


_LABS_PAGE = """
{% extends "base.html" %}
{% block content %}
<div class="container py-4" style="max-width:820px">
  <div class="d-flex align-items-start justify-content-between flex-wrap gap-2 mb-1">
    <div>
      <h2 class="mb-1"><i class="fas fa-display me-2 opacity-75"></i>Labs</h2>
      <div class="text-muted small"><i class="fas fa-users me-1 opacity-75"></i>Workspaces are shared across your team.</div>
    </div>
    <a href="/challenges" class="btn btn-outline-secondary btn-sm">
      <i class="fas fa-flag me-1"></i>Browse challenges
    </a>
  </div>
  <div id="labs-root" class="mt-3">
    <div class="text-muted d-flex align-items-center gap-2">
      <span class="spinner-border spinner-border-sm"></span> Loading labs…
    </div>
  </div>
</div>
{% endblock %}

{% block scripts %}
{{ super() }}
<script src="/plugins/lab_manager/assets/labs.js" defer></script>
{% endblock %}
"""


@lab_api.route("/labs")
@authed_only
def labs_dashboard():
    return render_template_string(_LABS_PAGE)


@lab_api.route("/api/v1/lab/<int:challenge_id>/start", methods=["POST"])
@authed_only
def lab_start(challenge_id):
    salt = _current_salt()
    _, st = k8s_get(salt, challenge_id)
    if st == 200:
        return jsonify({"success": True, "message": "Already running"})

    active = _count_active_sessions(salt)
    if active >= MAX_CONCURRENT_LABS:
        return jsonify({
            "success":       False,
            "limit_reached": True,
            "active":        active,
            "limit":         MAX_CONCURRENT_LABS,
            "message": (
                f"You already have {active} of {MAX_CONCURRENT_LABS} labs running. "
                "Stop one from the Labs page before starting another."
            ),
        }), 429

    body, st = k8s_create(salt, challenge_id, _lab_space_ref(challenge_id))
    if st in (200, 201):
        try:
            _audit_record_start(salt, challenge_id)
        except Exception:
            logger.exception("audit: record start failed")
            db.session.rollback()
        return jsonify({"success": True, "message": "Lab started"})
    return jsonify({"success": False, "message": body.get("message", "Error")}), 500


@lab_api.route("/api/v1/lab/<int:challenge_id>/stop", methods=["POST"])
@authed_only
def lab_stop(challenge_id):
    body, st = k8s_delete(_current_salt(), challenge_id)
    if st in (200, 202, 404):
        return jsonify({"success": True, "message": "Lab stopped"})
    return jsonify({"success": False, "message": body.get("message", "Error")}), 500


@lab_api.route("/api/v1/lab/<int:challenge_id>/reload", methods=["POST"])
@authed_only
def lab_reload(challenge_id):
    salt = _current_salt()

    # If nothing is running yet, a restart is just a start.
    _, st_get = k8s_get(salt, challenge_id)
    if st_get == 404:
        body, st = k8s_create(salt, challenge_id, _lab_space_ref(challenge_id))
        if st in (200, 201):
            return jsonify({"success": True, "message": "Lab started"})
        return jsonify({"success": False, "message": body.get("message", "Error")}), 500

    # Fast in-place restart via annotation (no namespace teardown).
    body, st = k8s_restart(salt, challenge_id)
    if st in (200, 201):
        return jsonify({"success": True, "message": "Lab restarting"})
    return jsonify({"success": False, "message": body.get("message", "Error")}), 500


# SECURITY: there is deliberately NO endpoint that returns a challenge's flag.
# A previous /api/v1/lab/<id>/flag "pre-fill" route handed the derived flag to any
# authenticated player as soon as their lab was Running — i.e. every lab challenge
# could be scored without being solved (Start Lab → GET flag → Submit). It was
# removed. The flag is only ever obtained by reaching the challenge service inside
# the workspace and submitting it through the normal /attempt path. Do not re-add a
# server route that discloses _derive_flag() output to players.


# ── Anti-cheat metrics exporter ────────────────────────────────────────────────

def _team_prefix() -> str:
    return "t" if get_config("user_mode") == "teams" else "u"


def _levenshtein(a: str, b: str, cap: int = 64) -> int:
    a, b = a[:cap], b[:cap]
    if a == b:
        return 0
    if not a or not b:
        return len(a) + len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _utime(dt) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@lab_api.route("/lab-metrics")
def lab_metrics():
    """Prometheus exporter of per-(team, challenge) anti-cheat signals, derived
    from CTFd submissions + the LabAudit ledger. Holds client IPs/UAs, so it is
    bearer-protected; the VMServiceScrape passes the same CTF_METRICS_TOKEN."""
    tok = os.environ.get("CTF_METRICS_TOKEN", "")
    if not tok or request.headers.get("Authorization", "") != "Bearer " + tok:
        return Response("forbidden\n", status=403, mimetype="text/plain; version=0.0.4")

    prefix = _team_prefix()
    labs   = LabChallenge.query.all()
    audits = {(a.salt, a.challenge_id): a for a in LabAudit.query.all()}
    names  = ({t.id: t.name for t in Teams.query.all()} if prefix == "t"
              else {u.id: u.name for u in Users.query.all()})

    S = {k: [] for k in (
        "solved", "solve_unixtime", "solve_seconds", "first_start_unixtime",
        "lab_restarts", "wrong_attempts", "min_wrong_editdistance",
        "foreign_flag_submits", "submit_via_api", "submit_via_ui",
        "distinct_submit_ips", "client_info")}

    for c in labs:
        labspace = c.lab_space_ref or ("challenge-%d" % c.id)
        subs = Submissions.query.filter_by(challenge_id=c.id).all()
        by_acct = {}
        for s in subs:
            by_acct.setdefault(s.account_id, []).append(s)
        accts = set(by_acct) | {int(k[1:]) for (k, cid) in audits
                                if cid == c.id and k[1:].isdigit()}
        # flag -> account, to spot a team submitting ANOTHER team's flag
        flag_of, flagmap = {}, {}
        if c.flag_service:
            for aid in accts:
                f = _derive_flag(c.flag_service, prefix + str(aid))
                flag_of[aid], flagmap[f] = f, aid

        for aid in accts:
            salt   = prefix + str(aid)
            labels = {"team": salt, "owner": names.get(aid, salt), "labspace": labspace}
            asubs  = by_acct.get(aid, [])
            correct = [s for s in asubs if s.type == "correct"]
            wrong   = [s for s in asubs if s.type != "correct"]
            au      = audits.get((salt, c.id))

            if correct:
                t0 = min(_utime(s.date) for s in correct)
                S["solved"].append((labels, 1))
                S["solve_unixtime"].append((labels, "%.0f" % t0))
                if au and au.first_started:
                    S["solve_seconds"].append((labels, "%.0f" % max(0.0, t0 - _utime(au.first_started))))
            S["wrong_attempts"].append((labels, len(wrong)))
            if wrong and c.flag_service:
                S["min_wrong_editdistance"].append(
                    (labels, min(_levenshtein(s.provided or "", flag_of.get(aid, "")) for s in wrong)))
            foreign = sum(1 for s in asubs if s.provided in flagmap and flagmap[s.provided] != aid)
            if foreign:
                S["foreign_flag_submits"].append((labels, foreign))
            ips = {s.ip for s in asubs if s.ip}
            if ips:
                S["distinct_submit_ips"].append((labels, len(ips)))
            if au:
                if au.first_started:
                    S["first_start_unixtime"].append((labels, "%.0f" % _utime(au.first_started)))
                S["lab_restarts"].append((labels, au.restarts or 0))
                if au.api_submits:
                    S["submit_via_api"].append((labels, au.api_submits))
                if au.ui_submits:
                    S["submit_via_ui"].append((labels, au.ui_submits))
                info = dict(labels, ip=(au.last_ip or "?"), ua=(au.last_ua or "?")[:120])
                S["client_info"].append((info, 1))

    HELP = {
        "solved": ("gauge", "1 if the account solved this lab challenge."),
        "solve_unixtime": ("gauge", "Unix time of the correct solve."),
        "solve_seconds": ("gauge", "Seconds from FIRST lab start to solve (restart-aware)."),
        "first_start_unixtime": ("gauge", "Unix time the lab was first started."),
        "lab_restarts": ("gauge", "Lab restarts for this account+challenge."),
        "wrong_attempts": ("gauge", "Incorrect submissions (0 + solved fast = no trial/error)."),
        "min_wrong_editdistance": ("gauge", "Smallest edit distance of a wrong guess to the real flag."),
        "foreign_flag_submits": ("gauge", "Submissions equal to ANOTHER account's flag (sharing)."),
        "submit_via_api": ("gauge", "Flag submissions via API token (scripted)."),
        "submit_via_ui": ("gauge", "Flag submissions via the web UI."),
        "distinct_submit_ips": ("gauge", "Distinct client IPs that submitted."),
        "client_info": ("gauge", "Evidence: last client IP + UA per account/challenge."),
    }
    lines = []
    for key, samples in S.items():
        if not samples:
            continue
        typ, help_ = HELP[key]
        name = "ctf_" + key
        lines.append("# HELP %s %s" % (name, help_))
        lines.append("# TYPE %s %s" % (name, typ))
        for labels, val in samples:
            lbl = ",".join('%s="%s"' % (k, _esc(v)) for k, v in labels.items())
            lines.append("%s{%s} %s" % (name, lbl, val))
    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")


# ── Plugin entry point ────────────────────────────────────────────────────────

def _validate_secrets() -> None:
    """Fail closed at plugin load (security review #1/#2 follow-up): refuse to start if
    the flag or JWT secret is unset or the built-in dev default, so a misconfigured
    deployment can't validate flags / mint tokens with a source-code-known key. Set
    CTF_ALLOW_DEV_SECRETS=true to permit the dev default for local dev."""
    if os.environ.get("CTF_ALLOW_DEV_SECRETS") == "true":
        return
    dev = b"ctf-school-secret-key"
    for name, val in (("CTF_FLAG_SECRET", _flag_secret()), ("CTF_JWT_SECRET", _jwt_secret())):
        if not val or val == dev:
            raise RuntimeError(
                f"{name} is unset or the built-in dev default; set a real secret "
                "(or a real CTF_SCHOOL_SECRET), or CTF_ALLOW_DEV_SECRETS=true for local dev"
            )


def load(app):
    _validate_secrets()
    with app.app_context():
        db.create_all()

    app.register_blueprint(lab_api)
    register_plugin_assets_directory(app, base_path="/plugins/lab_manager/assets/")
    register_user_page_menu_bar("Labs", "/labs")
    CHALLENGE_CLASSES["lab"] = LabChallengeType
    logger.info("CTFd Lab Manager plugin loaded")
