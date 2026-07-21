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
    so the whole public surface (board, graph, top-N, per-challenge solves list)
    drops them automatically — no core patch, no monkeypatching.
  - Private dashboard: /tournament renders a bracket-scoped scoreboard for the
    caller's own hidden bracket (see "Score isolation" below). Access = a member
    of that bracket, or a site admin.
  - Admin UI: /admin/tournaments, linked from the Admin Panel menubar
    (register_admin_plugin_menu_bar) and rendered inside the admin theme
    ({% extends "admin/base.html" %}) — NOT part of the user-facing theme.

Which brackets are "hidden" is stored in CTFd config as a JSON list of bracket ids
(CONFIG_KEY) — the single source of truth the admin toggles.

The user theme participates only in presentation: an optional navbar item for
members (via the `tournament_bracket_id` context-processor variable) and hiding a
hidden bracket's empty tab on the public scoreboard.

Score isolation (why this doesn't just call core's get_standings())
--------------------------------------------------------------------
`hidden=True` keeps a bracket off the public board (core filters `hidden==False`
everywhere: get_standings(), get_solves_for_challenge_id(), /teams, /teams/<id>).
But for a DYNAMIC-VALUE challenge, CTFd core also does this:

  - CTFd.plugins.dynamic_challenges.decay.get_solve_count() filters
    `hidden==False` too, so a hidden team's OWN solves never move the
    challenge's shared, global `Challenges.value` — good, that direction of
    isolation is already free.
  - BUT CTFd.utils.scores.get_standings() scores EVERY account, hidden or
    not, as `SUM(Challenges.value)` — that one shared, live number. So a
    hidden bracket's displayed points were still 100% driven by the MAIN
    competition's decay (their own solves contributed nothing to it), and two
    hidden teams solving the same challenge always got identical, non-decaying
    credit against each other — there was no tournament dynamic at all, just a
    mirror of whatever the public board's value happened to be at that moment.

That breaks both "own rating" and "must not influence each other". The fix:
_bracket_scores()/_decay_value() below recompute each DYNAMIC challenge's value
from a solve count scoped to ONLY the viewed bracket's own members, reusing the
challenge's own initial/minimum/decay/function — never reading or writing the
shared Challenges.value. Static (non-dynamic) challenges are unaffected: they
don't decay, so sharing their flat value is correct and was never in question.

NOTE: every hand-written POST form MUST include
`<input type="hidden" name="nonce" value="{{ Session.nonce }}">` — CTFd rejects
non-GET requests without the CSRF nonce with a 403.
"""

import datetime
import json
import logging
import math

from flask import Blueprint, redirect, render_template_string, request, abort

from CTFd.cache import clear_standings
from CTFd.models import Awards, Brackets, Challenges, Solves, Teams, Users, db
from CTFd.plugins import register_admin_plugin_menu_bar
from CTFd.plugins.dynamic_challenges import DynamicChallenge
from CTFd.utils import get_config, set_config
from CTFd.utils.decorators import admins_only, authed_only
from CTFd.utils.dates import unix_time_to_utc
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


# ── bracket-scoped scoring (independent of the shared Challenges.value) ─────────
# See the "Score isolation" note in the module docstring for WHY this exists
# instead of a call to CTFd.utils.scores.get_standings().

def _decay_value(challenge, solve_count):
    """CTFd.plugins.dynamic_challenges.decay's linear()/logarithmic() math, but
    parameterised on an explicit solve_count instead of their built-in
    get_solve_count() (which always counts GLOBALLY). Passing a bracket-local
    count here is what gives a hidden bracket its own, independent decay curve.
    """
    if solve_count != 0:
        solve_count -= 1  # first (bracket-local) solver gets the max value, like core

    initial = challenge.initial
    minimum = challenge.minimum
    decay = challenge.decay or 1  # guard divide-by-zero, like core

    if challenge.function == "linear":
        value = initial - (decay * solve_count)
    else:  # "logarithmic": CTFd's default, and its fallback for unknown functions
        value = (((minimum - initial) / (decay ** 2)) * (solve_count ** 2)) + initial

    return max(math.ceil(value), minimum)


def _bracket_scores(bracket_id, admin=False):
    """{account_id: score} for a bracket's own members, plus per-account
    (last_id, last_date) for tie-breaking — computed ENTIRELY from that
    bracket's own Solves/Awards. Never reads Challenges.value for a dynamic
    challenge; never writes it either (no calculate_value()/db writes at all).
    """
    Model = _account_model()
    member_ids = [m.id for m in Model.query.filter_by(bracket_id=bracket_id).all()]
    if not member_ids:
        return {}, {}, {}

    freeze = get_config("freeze")
    freeze_dt = unix_time_to_utc(freeze) if (freeze and not admin) else None

    scores = {mid: 0 for mid in member_ids}
    last_id = {}
    last_date = {}

    solves_q = Solves.query.filter(Solves.account_id.in_(member_ids))
    if freeze_dt is not None:
        solves_q = solves_q.filter(Solves.date < freeze_dt)
    solves = solves_q.order_by(Solves.id.asc()).all()

    # Bracket-local solve count per challenge — the whole point: this NEVER
    # touches the global count core uses for the shared Challenges.value.
    counts = {}
    for s in solves:
        counts[s.challenge_id] = counts.get(s.challenge_id, 0) + 1

    chal_cache = {}
    for s in solves:
        chal = chal_cache.get(s.challenge_id)
        if chal is None:
            chal = Challenges.query.filter_by(id=s.challenge_id).first()
            chal_cache[s.challenge_id] = chal
        if chal is None or chal.value == 0:
            continue  # deleted / zero-value challenges don't score, like core

        if isinstance(chal, DynamicChallenge):
            value = _decay_value(chal, counts[s.challenge_id])
        else:
            value = chal.value  # static value: no decay, sharing it is correct

        scores[s.account_id] = scores.get(s.account_id, 0) + value
        last_id[s.account_id] = s.id
        last_date[s.account_id] = s.date

    awards_q = Awards.query.filter(Awards.account_id.in_(member_ids))
    if freeze_dt is not None:
        awards_q = awards_q.filter(Awards.date < freeze_dt)
    for a in awards_q.order_by(Awards.id.asc()).all():
        if not a.value:
            continue
        scores[a.account_id] = scores.get(a.account_id, 0) + a.value
        if a.id > last_id.get(a.account_id, -1):
            last_id[a.account_id] = a.id
            last_date[a.account_id] = a.date

    return scores, last_id, last_date


# ── blueprint ───────────────────────────────────────────────────────────────────

tournaments_bp = Blueprint("hidden_tournaments", __name__)


def _standings_rows(bracket_id, admin=False):
    """Ranked members of a bracket, scored independently of the main scoreboard
    (see _bracket_scores) so the private board reflects THIS bracket's own
    decay, not the public one's."""
    Model = _account_model()
    members = Model.query.filter_by(bracket_id=bracket_id).all()
    scores, last_id, last_date = _bracket_scores(bracket_id, admin=admin)

    ranked = sorted(
        members,
        key=lambda m: (
            -scores.get(m.id, 0),
            last_date.get(m.id) or datetime.datetime.max,
            last_id.get(m.id) or 0,
        ),
    )

    rows = []
    for i, m in enumerate(ranked):
        rows.append(
            {
                "place": i + 1,
                "account_id": m.id,
                "name": m.name,
                "score": scores.get(m.id, 0),
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
        rows=_standings_rows(bid, admin=admin),
        # NOTE: do NOT name this `is_admin` — that would shadow CTFd's template
        # global `is_admin()` (a callable) inside base.html/navbar and blow up with
        # "'bool' object is not callable".
        viewer_is_admin=admin,
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
    # get_standings() (and the public /api/v1/scoreboard, /teams, etc. that key off
    # it) is @cache.memoize(60) in core — without this, a just-hidden/un-hidden
    # bracket keeps showing the PRE-toggle visibility on the public board for up to
    # a minute. Core's own admin routes (PATCH /api/v1/teams/<id>, /users/<id>) call
    # this same helper whenever they touch `hidden`; we do the same here.
    clear_standings()
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
        clear_standings()  # see admin_toggle: this also flips `hidden`
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
        clear_standings()  # see admin_toggle: this also flips `hidden`
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
    {% if viewer_is_admin %}<a href="/admin/tournaments" class="btn btn-outline-secondary btn-sm">
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
