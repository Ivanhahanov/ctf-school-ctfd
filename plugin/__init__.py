"""
CTFd Lab Manager Plugin
=======================
Challenge type «Lab» = Dynamic scoring + Kubernetes LabSession.

Flag derivation mirrors the controller's buildEnvVars (case "hmac"):
    HMAC-SHA256(CTF_SCHOOL_SECRET, flagService + userId)[:12]
wrapped in flag_template (default "CTF{%s}").

The secret must match the value used in the controller
(env CTF_SCHOOL_SECRET, fallback "ctf-school-secret-key").
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

from flask import Blueprint, jsonify, redirect, render_template_string

from CTFd.models import Challenges, db
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
    # Python %-format string wrapping the HMAC digest, e.g. "CTF{%s}"
    flag_template = db.Column(db.String(255), nullable=False, default="CTF{%s}")


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


def _make_workspace_token(team: str) -> str:
    secret = os.environ.get("CTF_SCHOOL_SECRET", "ctf-school-secret-key").encode()
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(
        {"team": team, "exp": int(time.time()) + WORKSPACE_TOKEN_TTL},
        separators=(",", ":"),
    ).encode())
    signing = (header + "." + payload).encode()
    sig = _b64url(_hmac.new(secret, signing, hashlib.sha256).digest())
    return header + "." + payload + "." + sig


def _derive_flag(flag_service: str, salt: str, template: str) -> str:
    """
    Mirrors controller buildEnvVars case "hmac":
        val = fmt.Sprintf(template, hash(labSvc.Name + session.Spec.UserId))
    where hash = HMAC-SHA256(CTF_SCHOOL_SECRET, input)[:12] hex.
    """
    secret = os.environ.get("CTF_SCHOOL_SECRET", "ctf-school-secret-key").encode()
    digest = _hmac.new(secret, (flag_service + salt).encode(), hashlib.sha256)
    return template % digest.hexdigest()[:12]


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
            flag_template = data.get("flag_template", "CTF{%s}").strip(),
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
            data["flag_template"] = c.flag_template
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
            for field in ("lab_space_ref", "flag_service", "flag_template"):
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
        expected   = _derive_flag(c.flag_service, salt, c.flag_template)

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
<div class="container py-4">
  <div class="d-flex align-items-center justify-content-between flex-wrap gap-2 mb-3">
    <h2 class="mb-0"><i class="fas fa-display me-2 opacity-75"></i>My Labs</h2>
    <a href="/challenges" class="btn btn-outline-secondary btn-sm">
      <i class="fas fa-flag me-1"></i>Browse challenges
    </a>
  </div>
  <div id="labs-root">
    <div class="text-muted d-flex align-items-center gap-2">
      <span class="spinner-border spinner-border-sm"></span> Loading your labs…
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
                "Stop one from “My Labs” before starting another."
            ),
        }), 429

    body, st = k8s_create(salt, challenge_id, _lab_space_ref(challenge_id))
    if st in (200, 201):
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


@lab_api.route("/api/v1/lab/<int:challenge_id>/flag")
@authed_only
def lab_flag_hint(challenge_id):
    """
    Returns the flag once the lab is Running — frontend can pre-fill the submission field.
    In team mode all team members get the same flag (same salt → same HMAC).
    """
    salt    = _current_salt()
    payload = _status_payload(salt, challenge_id)
    if not payload["running"]:
        return jsonify({"success": False, "message": "Lab is not running"}), 400

    c = LabChallenge.query.filter_by(id=challenge_id).first()
    if not c or not c.flag_service:
        return jsonify({"success": False, "message": "Dynamic flag not configured"}), 404

    return jsonify({"success": True, "flag": _derive_flag(c.flag_service, salt, c.flag_template)})


# ── Plugin entry point ────────────────────────────────────────────────────────

def load(app):
    with app.app_context():
        db.create_all()

    app.register_blueprint(lab_api)
    register_plugin_assets_directory(app, base_path="/plugins/lab_manager/assets/")
    register_user_page_menu_bar("My Labs", "/labs")
    CHALLENGE_CLASSES["lab"] = LabChallengeType
    logger.info("CTFd Lab Manager plugin loaded")
