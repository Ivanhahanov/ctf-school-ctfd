"""
CTFd Hidden Tournaments plugin
==============================
A "hidden tournament" = a CTFd Bracket whose members are EXCLUDED from the main
scoreboard but compete against each other on a PRIVATE board only they (and admins)
can see. Built on CTFd's native Brackets (3.7+) — we add only what core lacks.

Standalone plugin (no dependency on lab_manager):
  - Foundation: native Brackets. Teams/Users already carry `bracket_id`.
  - Exclude from the general scoreboard: members of a hidden bracket get
    `hidden=True`. Core `get_standings()` filters `hidden==False` for non-admins,
    so the whole public surface (board, graph, top-N) drops them automatically —
    no core patch, no monkeypatching.
  - Private dashboard: /tournament renders standings for the caller's own hidden
    bracket via get_standings(bracket_id=…, admin=True) (admin=True re-includes
    the hidden members). Access = a member of that bracket, or a site admin.
  - Admin UI: /admin/tournaments, linked from the Admin Panel menubar
    (register_admin_plugin_menu_bar) and rendered inside the admin theme
    ({% extends "admin/base.html" %}) — NOT part of the user-facing theme.

Which brackets are "hidden" is stored in CTFd config as a JSON list of bracket ids
(CONFIG_KEY) — the single source of truth the admin toggles.

The user theme participates only in presentation: an optional navbar item for
members (via the `tournament_bracket_id` context-processor variable) and hiding a
hidden bracket's empty tab on the public scoreboard.

NOTE: every hand-written POST form MUST include
`<input type="hidden" name="nonce" value="{{ Session.nonce }}">` — CTFd rejects
non-GET requests without the CSRF nonce with a 403.
"""

import json
import logging

from flask import Blueprint, redirect, render_template_string, request, abort

from CTFd.models import Brackets, Teams, Users, db
from CTFd.plugins import register_admin_plugin_menu_bar
from CTFd.utils import get_config, set_config
from CTFd.utils.decorators import admins_only, authed_only
from CTFd.utils.scores import get_standings
from CTFd.utils.user import authed, get_current_user, is_admin

logger = logging.getLogger(__name__)

CONFIG_KEY = "plugin_hidden_bracket_ids"


# ── hidden-bracket registry (CTFd config) ───────────────────────────────────────

def hidden_bracket_ids():
    """Set[int] of bracket ids marked as hidden tournaments."""
    raw = get_config(CONFIG_KEY)
    if not raw:
        return set()
    try:
        return {int(i) for i in json.loads(raw)}
    except Exception:
        logger.warning("tournaments: bad %s config: %r", CONFIG_KEY, raw)
        return set()


def _save_hidden(ids):
    set_config(CONFIG_KEY, json.dumps(sorted({int(i) for i in ids})))


def _account_model():
    """Brackets attach to whichever account type the CTF runs as."""
    return Teams if get_config("user_mode") == "teams" else Users


def _current_account():
    """The scoring account for the logged-in user (their team in team mode)."""
    user = get_current_user()
    if user is None:
        return None
    if get_config("user_mode") == "teams":
        return getattr(user, "team", None)
    return user


def _my_bracket_id():
    acct = _current_account()
    return getattr(acct, "bracket_id", None) if acct is not None else None


# ── context processor: expose the caller's hidden bracket to templates ───────────
# Lets the (theme-owned) navbar show a "Tournament" link ONLY to members of a hidden
# bracket — instead of a global menu item every player would see.

def _tournament_ctx():
    try:
        if not authed():
            return {}
        bid = _my_bracket_id()
        if bid is not None and bid in hidden_bracket_ids():
            return {"tournament_bracket_id": bid}
    except Exception:
        logger.debug("tournaments: ctx processor failed", exc_info=True)
    return {}


# ── blueprint ───────────────────────────────────────────────────────────────────

tournaments_bp = Blueprint("hidden_tournaments", __name__)


def _standings_rows(bracket_id):
    """Ranked members of a bracket, INCLUDING the hidden ones (admin=True), so the
    private board shows the tournament even though the public board hides them."""
    standings = get_standings(bracket_id=bracket_id, admin=True)
    rows = []
    for i, s in enumerate(standings):
        rows.append(
            {
                "place": i + 1,
                "account_id": getattr(s, "account_id", None),
                "name": getattr(s, "name", "?"),
                "score": getattr(s, "score", 0),
            }
        )
    return rows


@tournaments_bp.route("/tournament")
@authed_only
def tournament_board():
    hidden = hidden_bracket_ids()
    admin = is_admin()

    bid = request.args.get("bracket_id", type=int)

    if admin and bid is None:
        # Admin with no target → pick from the list of hidden tournaments.
        brackets = (
            Brackets.query.filter(Brackets.id.in_(hidden)).all() if hidden else []
        )
        return render_template_string(_ADMIN_PICK_PAGE, brackets=brackets)

    if not admin:
        my = _my_bracket_id()
        # Members may only see their OWN hidden tournament.
        if bid is None:
            bid = my
        if bid is None or bid != my or bid not in hidden:
            return redirect("/scoreboard")

    bracket = Brackets.query.filter_by(id=bid).first()
    if bracket is None:
        abort(404)

    return render_template_string(
        _TOURNAMENT_PAGE,
        bracket_name=bracket.name,
        bracket_id=bid,
        rows=_standings_rows(bid),
        is_admin=admin,
    )


# ── admin: assignment UI (Admin Panel pages) ─────────────────────────────────────

@tournaments_bp.route("/admin/tournaments")
@admins_only
def admin_tournaments():
    mode = get_config("user_mode")
    hidden = hidden_bracket_ids()
    Model = _account_model()
    brackets = []
    for b in Brackets.query.filter_by(type=mode).order_by(Brackets.name).all():
        members = Model.query.filter_by(bracket_id=b.id).order_by(Model.name).all()
        brackets.append(
            {
                "id": b.id,
                "name": b.name,
                "hidden": b.id in hidden,
                "members": [
                    {"id": m.id, "name": m.name, "acct_hidden": bool(m.hidden)}
                    for m in members
                ],
            }
        )
    return render_template_string(_ADMIN_PAGE, brackets=brackets, mode=mode)


@tournaments_bp.route("/admin/tournaments/<int:bracket_id>/toggle", methods=["POST"])
@admins_only
def admin_toggle(bracket_id):
    ids = hidden_bracket_ids()
    now_hidden = bracket_id not in ids
    if now_hidden:
        ids.add(bracket_id)
    else:
        ids.discard(bracket_id)
    _save_hidden(ids)
    # Keep member visibility in sync with the tournament's hidden state.
    for m in _account_model().query.filter_by(bracket_id=bracket_id).all():
        m.hidden = now_hidden
    db.session.commit()
    return redirect("/admin/tournaments")


@tournaments_bp.route("/admin/tournaments/<int:bracket_id>/add", methods=["POST"])
@admins_only
def admin_add(bracket_id):
    Model = _account_model()
    q = (request.form.get("q") or "").strip()
    m = None
    if q.isdigit():
        m = Model.query.filter_by(id=int(q)).first()
    if m is None and q:
        m = Model.query.filter_by(name=q).first()
    if m is not None:
        m.bracket_id = bracket_id
        m.hidden = bracket_id in hidden_bracket_ids()
        db.session.commit()
    return redirect("/admin/tournaments")


@tournaments_bp.route(
    "/admin/tournaments/<int:bracket_id>/remove/<int:account_id>", methods=["POST"]
)
@admins_only
def admin_remove(bracket_id, account_id):
    Model = _account_model()
    m = Model.query.filter_by(id=account_id).first()
    if m is not None and m.bracket_id == bracket_id:
        m.bracket_id = None
        m.hidden = False
        db.session.commit()
    return redirect("/admin/tournaments")


# ── templates ────────────────────────────────────────────────────────────────────
# The member board is a USER page (extends the active user theme); the admin pages
# are ADMIN PANEL pages (extend admin/base.html → render inside the admin chrome).

_TOURNAMENT_PAGE = """
{% extends "base.html" %}
{% set _ru = (get_locale()|string).lower().startswith('ru') %}
{% macro t(en, ru) %}{{ ru if _ru else en }}{% endmacro %}
{% block content %}
<div class="container py-4" style="max-width:820px">
  <div class="d-flex align-items-center justify-content-between flex-wrap gap-2 mb-3">
    <h2 class="mb-0">
      <i class="fas fa-trophy me-2 opacity-75"></i>{{ bracket_name }}
      <span class="badge bg-secondary ms-2">{{ t('Private tournament', 'Закрытый турнир') }}</span>
    </h2>
    {% if is_admin %}<a href="/admin/tournaments" class="btn btn-outline-secondary btn-sm">
      <i class="fas fa-wrench me-1"></i>{{ t('Manage', 'Управление') }}</a>{% endif %}
  </div>
  <div class="text-muted small mb-3">
    <i class="fas fa-eye-slash me-1 opacity-75"></i>
    {{ t('This group is scored separately and does not appear on the main scoreboard.',
         'Эта группа считается отдельно и не отображается в общем зачёте.') }}
  </div>
  {% if rows %}
  <table class="table table-striped align-middle">
    <thead>
      <tr>
        <th style="width:64px" class="text-center">{{ t('Place', 'Место') }}</th>
        <th>{{ t('Team', 'Команда') }}</th>
        <th style="width:120px">{{ t('Score', 'Очки') }}</th>
      </tr>
    </thead>
    <tbody>
      {% for r in rows %}
      <tr>
        <th scope="row" class="text-center">{{ r.place }}</th>
        <td>{{ r.name }}</td>
        <td>{{ r.score }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="text-muted">{{ t('No members yet.', 'Пока нет участников.') }}</div>
  {% endif %}
</div>
{% endblock %}
"""

_ADMIN_PICK_PAGE = """
{% extends "admin/base.html" %}
{% block content %}
<div class="container py-4" style="max-width:820px">
  <h2 class="mb-3"><i class="fas fa-trophy me-2 opacity-75"></i>Hidden tournaments</h2>
  {% if brackets %}
  <ul class="list-group">
    {% for b in brackets %}
    <li class="list-group-item d-flex justify-content-between align-items-center">
      <a href="/tournament?bracket_id={{ b.id }}">{{ b.name }}</a>
      <a class="btn btn-sm btn-outline-secondary" href="/admin/tournaments">Manage</a>
    </li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="text-muted">No hidden tournaments yet. Create one at
    <a href="/admin/tournaments">/admin/tournaments</a>.</p>
  {% endif %}
</div>
{% endblock %}
"""

_ADMIN_PAGE = """
{% extends "admin/base.html" %}
{% block content %}
<div class="container py-4" style="max-width:960px">
  <h2 class="mb-1"><i class="fas fa-trophy me-2 opacity-75"></i>Hidden tournaments</h2>
  <p class="text-muted">
    Mark a bracket as a <b>hidden tournament</b>: its {{ mode }} are removed from the
    main scoreboard (set <code>hidden</code>) and compete on a private board at
    <code>/tournament</code>. Create the brackets themselves in
    <a href="/admin/brackets">Admin → Brackets</a>.
  </p>

  {% if not brackets %}
  <div class="alert alert-info">
    No <b>{{ mode }}</b> brackets exist yet. Create one in Admin → Brackets, then reload.
  </div>
  {% endif %}

  {% for b in brackets %}
  <div class="card mb-3">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>
        <b>{{ b.name }}</b>
        {% if b.hidden %}<span class="badge bg-danger ms-2">hidden</span>
        {% else %}<span class="badge bg-secondary ms-2">public</span>{% endif %}
        <span class="text-muted ms-2">{{ b.members|length }} member(s)</span>
      </span>
      <form method="post" action="/admin/tournaments/{{ b.id }}/toggle" class="m-0">
        <input type="hidden" name="nonce" value="{{ Session.nonce }}">
        <button class="btn btn-sm {{ 'btn-outline-success' if b.hidden else 'btn-outline-danger' }}">
          {{ 'Make public' if b.hidden else 'Make hidden' }}
        </button>
      </form>
    </div>
    <div class="card-body">
      <form method="post" action="/admin/tournaments/{{ b.id }}/add" class="row g-2 mb-3">
        <input type="hidden" name="nonce" value="{{ Session.nonce }}">
        <div class="col-auto flex-grow-1">
          <input name="q" class="form-control form-control-sm"
                 placeholder="Add {{ mode[:-1] }} by exact name or id">
        </div>
        <div class="col-auto">
          <button class="btn btn-sm btn-primary">Add</button>
        </div>
      </form>
      {% if b.members %}
      <table class="table table-sm align-middle mb-0">
        <tbody>
          {% for m in b.members %}
          <tr>
            <td>{{ m.name }} <span class="text-muted">#{{ m.id }}</span>
              {% if not m.acct_hidden and b.hidden %}
                <span class="badge bg-warning text-dark ms-1">visible!</span>{% endif %}</td>
            <td class="text-end">
              <form method="post" action="/admin/tournaments/{{ b.id }}/remove/{{ m.id }}" class="m-0">
                <input type="hidden" name="nonce" value="{{ Session.nonce }}">
                <button class="btn btn-sm btn-outline-secondary">Remove</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="text-muted small">No members.</div>
      {% endif %}
    </div>
  </div>
  {% endfor %}
</div>
{% endblock %}
"""


def load(app):
    app.register_blueprint(tournaments_bp)
    app.context_processor(_tournament_ctx)
    register_admin_plugin_menu_bar("Tournaments", "/admin/tournaments")
    logger.info("Hidden Tournaments plugin loaded")
