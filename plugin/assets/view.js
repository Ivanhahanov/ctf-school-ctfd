CTFd._internal.challenge.data = undefined;
CTFd._internal.challenge.renderer = null;
CTFd._internal.challenge.preRender = function () {};
CTFd._internal.challenge.render = null;

CTFd._internal.challenge.postRender = function () {
    var id = parseInt(CTFd.lib.$("#challenge-id").val());
    if (id) _labInit(id);
};

CTFd._internal.challenge.submit = function (preview) {
    var challenge_id = parseInt(CTFd.lib.$("#challenge-id").val());
    var submission   = CTFd.lib.$("#challenge-input").val();
    var params = preview ? { preview: true } : {};
    return CTFd.api.post_challenge_attempt(params, { challenge_id: challenge_id, submission: submission })
        .then(function (r) { return r; });
};

// ── per-challenge state ────────────────────────────────────────────────────
var _labState = {};   // id -> { timer, interval }
var _labSeen  = {};   // id -> have we ever rendered a real status?
var _labFails = {};   // id -> consecutive fetch failures

function _labInit(id) {
    _labStop(id);
    _labSeen[id]  = false;
    _labFails[id] = 0;
    _labRefreshStatus(id);     // immediate first check
    _labEnsurePoll(id, 3000);  // and keep polling until the card reflects reality
}

function _labStop(id) {
    if (_labState[id] && _labState[id].timer) clearInterval(_labState[id].timer);
    delete _labState[id];
}

// True once the card's modal has been closed (CTFd keeps the node in the DOM but
// hides it — a hidden element has no offsetParent). Also true if the node is gone.
function _labCardClosed(id) {
    var el = document.getElementById("lab-card-" + id);
    return !el || el.offsetParent === null;
}

// A single self-terminating poll loop. It stops itself once the card's modal is
// closed. The interval adapts to the lab phase.
function _labEnsurePoll(id, intervalMs) {
    var st = _labState[id];
    if (st && st.interval === intervalMs) return;
    if (st && st.timer) clearInterval(st.timer);
    _labState[id] = {
        interval: intervalMs,
        timer: setInterval(function () {
            if (_labCardClosed(id)) { _labStop(id); return; }
            _labRefreshStatus(id);
        }, intervalMs),
    };
}

// ── small DOM helpers ──────────────────────────────────────────────────────
function _el(id, suffix) { return document.getElementById(suffix + "-" + id); }
// Hide with !important so an inline style always beats Bootstrap's display
// utility classes (e.g. .d-flex is `display:flex!important`, which a plain
// inline `display:none` cannot override).
function _show(el, on) {
    if (!el) return;
    if (on) el.style.removeProperty("display");
    else    el.style.setProperty("display", "none", "important");
}

var _LAB_PHASE_CSS = {
    Running:      "bg-success",
    Pending:      "bg-warning text-dark",
    Provisioning: "bg-warning text-dark",
    Terminating:  "bg-warning text-dark",
    Failed:       "bg-danger",
    error:        "bg-danger",
};

function _labBadge(id, phase, show) {
    var el = _el(id, "lab-badge");
    if (!el) return;
    if (!show) { _show(el, false); return; }
    el.textContent = phase.charAt(0).toUpperCase() + phase.slice(1);
    el.className   = "badge rounded-pill " + (_LAB_PHASE_CSS[phase] || "bg-secondary");
    _show(el, true);
}

function _labSetBusy(id, busy) {
    ["start", "stop", "reload"].forEach(function (a) {
        var b = _el(id, "lab-btn-" + a);
        if (b) b.disabled = busy;
    });
}

function _labMsg(id, text, ok) {
    var el = _el(id, "lab-msg");
    if (!el) return;
    el.className     = "mt-2 alert py-1 px-2 small mb-0 " + (ok ? "alert-success" : "alert-danger");
    el.textContent   = text;
    el.style.display = "";
    setTimeout(function () { el.style.display = "none"; }, 6000);
}

// ── render the whole card from a status payload ────────────────────────────
function _labUpdateUI(id, data) {
    _labSeen[id]  = true;
    _labFails[id] = 0;
    var phase   = data.phase || (data.running ? "Running" : "idle");
    var running = !!data.running;
    var booting = !running && phase !== "idle" && phase !== "stopped" && phase !== "error" && phase !== "Failed";
    var failed  = phase === "error" || phase === "Failed";
    var idle    = !running && !booting && !failed;

    // first successful check resolves the "Checking…" placeholder
    _show(_el(id, "lab-checking"), false);

    _labBadge(id, phase, running || booting || failed);
    _show(_el(id, "lab-idle"), idle);

    // single provisioning status line (no log box — keeps the card from jumping)
    var infoText = _el(id, "lab-info-text");
    if (infoText) infoText.textContent = data.message || "Starting…";
    _show(_el(id, "lab-info"), booting);

    // workspace link (single entry point)
    var wsDiv  = _el(id, "lab-workspace");
    var wsLink = _el(id, "lab-ws-link");
    if (wsDiv && wsLink) {
        var ws = data.workspace;
        if (running && ws && ws.address) {
            // Go through /enter so CTFd mints the team-scoped access cookie first.
            wsLink.href = "/lab/" + id + "/enter";
            var icon  = ws.type === "VNC" ? "fa-display" : "fa-terminal";
            var label = ws.type === "VNC" ? "Open Desktop Workspace" : "Open Terminal Workspace";
            wsLink.innerHTML = '<i class="fas ' + icon + '"></i><span>' + label + '</span>';
            _show(wsDiv, true);
        } else {
            _show(wsDiv, false);
        }
    }

    // controls
    _show(_el(id, "lab-btn-start"),  idle || failed);
    _show(_el(id, "lab-btn-stop"),   running || booting);
    _show(_el(id, "lab-btn-reload"), running);

    // adapt poll rate: fast while booting, relaxed once stable
    _labEnsurePoll(id, booting ? 3000 : 8000);
}

// ── fetch status ───────────────────────────────────────────────────────────
// Transient connection blips (e.g. ERR_EMPTY_RESPONSE while a proxy recycles a
// connection) must not freeze the card or flash a hard error. We keep the last
// good state, retry on the normal poll cadence, and only surface a soft hint.
function _labRefreshStatus(id) {
    fetch("/api/v1/lab/" + id + "/status", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
        })
        .then(function (d) { _labUpdateUI(id, d); })
        .catch(function () {
            _labFails[id] = (_labFails[id] || 0) + 1;
            if (_labSeen[id]) return;            // keep last good UI, just retry silently
            // Never rendered a real status yet: keep the spinner, but after a few
            // failures soften the wording so the user knows we're still trying.
            var txt = _el(id, "lab-checking-text");
            if (txt && _labFails[id] >= 2) txt.textContent = "Connecting to lab service… retrying";
        });
}

// ── start / stop / reload ──────────────────────────────────────────────────
function labAction(id, action) {
    _labSetBusy(id, true);
    if (action === "start" || action === "reload") {
        _show(_el(id, "lab-idle"), false);
        _show(_el(id, "lab-workspace"), false);   // hide stale link during (re)start
        _labBadge(id, "Pending", true);
        var infoText = _el(id, "lab-info-text");
        if (infoText) infoText.textContent = action === "reload" ? "Restarting — recreating the workspace…" : "Starting…";
        _show(_el(id, "lab-info"), true);
        _labEnsurePoll(id, 3000);
    }
    if (action === "stop") {
        _labBadge(id, "Terminating", true);
        _show(_el(id, "lab-workspace"), false);
    }

    fetch("/api/v1/lab/" + id + "/" + action, {
        method:      "POST",
        credentials: "same-origin",
        // Content-Type must be application/json so CTFd validates the CSRF token
        // from the header (otherwise it looks for a form nonce and returns 403).
        headers: { "Content-Type": "application/json", "CSRF-Token": CTFd.config.csrfNonce },
        body: "{}",
    })
    .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw e; }).catch(function () { throw { message: "HTTP " + r.status }; });
        return r.json();
    })
    .then(function (d) {
        _labMsg(id, d.message || action + (d.success ? " OK" : " failed"), !!d.success);
        _labRefreshStatus(id);
    })
    .catch(function (err) {
        if (err && err.limit_reached) _labLimitMsg(id, err);
        else _labMsg(id, (err && err.message) ? err.message : "Network error", false);
        _labRefreshStatus(id);
    })
    .finally(function () { _labSetBusy(id, false); });
}

// Persistent, actionable message when the team is at its lab limit.
function _labLimitMsg(id, err) {
    var el = _el(id, "lab-msg");
    if (!el) return;
    var msg = (err && err.message) ? err.message : "You have reached your lab limit.";
    el.className   = "mt-2 alert alert-warning py-2 px-2 small mb-0";
    el.innerHTML   = '<i class="fas fa-triangle-exclamation me-1"></i>'
        + msg.replace(/</g, "&lt;")
        + ' <a href="/labs" class="alert-link">Manage your labs →</a>';
    el.style.display = "";   // persistent — do not auto-hide
}

// First-open race: CTFd renders the challenge view (Alpine x-html) at a moment
// that is not synchronised with this script loading or with postRender, so on
// the very first open neither reliably fires _labInit (the status request is
// simply never made — confirmed in the Network panel). A MutationObserver fires
// the instant the card is inserted, independent of all that ordering. _labInit
// is idempotent, so overlapping with postRender is harmless.
(function () {
    function initCard(el) {
        var id = parseInt(el.id.slice("lab-card-".length), 10);
        if (id) _labInit(id);
    }
    // Already in the DOM (script evaluated after render).
    document.querySelectorAll('[id^="lab-card-"]').forEach(initCard);
    // Inserted later (script evaluated before render — the first-open race).
    if (!window.__labCardObserver) {
        window.__labCardObserver = new MutationObserver(function (muts) {
            muts.forEach(function (m) {
                m.addedNodes.forEach(function (n) {
                    if (n.nodeType !== 1) return;
                    if (n.id && n.id.indexOf("lab-card-") === 0) initCard(n);
                    else if (n.querySelectorAll) n.querySelectorAll('[id^="lab-card-"]').forEach(initCard);
                });
            });
        });
        window.__labCardObserver.observe(document.body, { childList: true, subtree: true });
    }
})();
