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

// ── i18n ───────────────────────────────────────────────────────────────────
// Locale comes from <html lang> (set by the theme from CTFd's get_locale()), so
// the card follows the site language switcher. Falls back to English.
var _LAB_LANG = (document.documentElement.lang || "en").toLowerCase().slice(0, 2);
var _LAB_STR = {
    ru: {
        "Idle": "Ожидание",
        "Running": "Работает",
        "Starting": "Запуск",
        "Stopping": "Остановка",
        "Failed": "Ошибка",
        "Checking status…": "Проверка статуса…",
        "Start Lab": "Запустить стенд",
        "Starting…": "Запуск…",
        "Stopping…": "Остановка…",
        "Restarting…": "Перезапуск…",
        "Open Terminal Workspace": "Открыть терминал",
        "Open Desktop Workspace": "Открыть рабочий стол",
        "Connecting to lab service…": "Подключение к сервису…",
        "Retrying…": "Повтор…",
        "Services are reachable only from inside your workspace.":
            "Сервисы доступны только внутри вашего окружения.",
        "Provisioning your workspace…": "Разворачиваем ваше окружение…",
        "Shutting the workspace down…": "Останавливаем окружение…",
        "Recreating your workspace…": "Пересоздаём ваше окружение…",
        "Requesting a workspace…": "Запрашиваем окружение…",
        "The lab failed to start.": "Не удалось запустить стенд.",
        " Press Start to try again.": " Нажмите «Запустить», чтобы попробовать снова.",
        "An isolated desktop workspace — start it to solve this challenge.":
            "Изолированное рабочее окружение — запустите его, чтобы решить задание.",
        "Something went wrong. Try again.": "Что-то пошло не так. Попробуйте снова.",
        "You’re running the maximum number of labs": "Вы запустили максимальное число стендов",
        ". Stop one to start this challenge. ": ". Остановите один, чтобы запустить это задание. ",
        "Manage your labs →": "Управление стендами →",
    },
};
function T(s) { var m = _LAB_STR[_LAB_LANG]; return (m && m[s]) || s; }

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

// A single self-terminating poll loop. Stops once the card's modal is closed.
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
function _esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
// Hide with !important so an inline style always beats Bootstrap's display utils.
function _show(el, on) {
    if (!el) return;
    if (on) el.style.removeProperty("display");
    else    el.style.setProperty("display", "none", "important");
}

// status pill — colour + label only; the shape never changes (no reflow)
var _PILL = {
    idle:    ["is-idle", T("Idle")],
    Running: ["is-run",  T("Running")],
    Pending: ["is-busy", T("Starting")],
    Provisioning: ["is-busy", T("Starting")],
    Terminating:  ["is-busy", T("Stopping")],
    Failed:  ["is-fail", T("Failed")],
    error:   ["is-fail", T("Failed")],
};
function _pill(id, phaseKey, textOverride) {
    var el = _el(id, "lab-pill"), t = _el(id, "lab-pill-text");
    var def = _PILL[phaseKey] || ["is-idle", phaseKey];
    if (el) el.className = "lab-pill " + def[0];
    if (t)  t.textContent = textOverride || def[1];
}

// The ONE morphing primary action. Same element & height in every state.
var _SPIN = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';
function _primary(id, mode, opts) {
    var a = _el(id, "lab-primary");
    if (!a) return;
    opts = opts || {};
    a.className = "btn lab-primary";
    a.onclick = null; a.removeAttribute("target"); a.removeAttribute("rel"); a.setAttribute("href", "#");
    function body(icon, label) { return icon + '<span id="lab-primary-text-' + id + '">' + _esc(label) + "</span>"; }
    if (mode === "checking") {
        a.classList.add("btn-secondary", "disabled");
        a.innerHTML = body(_SPIN, opts.label || T("Checking status…"));
    } else if (mode === "start") {
        a.classList.add("btn-success");
        a.innerHTML = body('<i class="fas fa-play"></i>', opts.label || T("Start Lab"));
        a.onclick = function (e) { e.preventDefault(); labAction(id, "start"); };
    } else if (mode === "starting") {
        a.classList.add("btn-secondary", "disabled");
        a.innerHTML = body(_SPIN, opts.label || T("Starting…"));
    } else if (mode === "stopping") {
        a.classList.add("btn-secondary", "disabled");
        a.innerHTML = body(_SPIN, opts.label || T("Stopping…"));
    } else if (mode === "open") {
        a.classList.add("btn-primary");
        a.setAttribute("href", "/lab/" + id + "/enter");
        a.setAttribute("target", "_blank"); a.setAttribute("rel", "noopener");
        a.innerHTML = body('<i class="fas ' + (opts.terminal ? "fa-terminal" : "fa-display") + '"></i>',
                           opts.label || (opts.terminal ? T("Open Terminal Workspace") : T("Open Desktop Workspace")));
    }
}

function _secondary(id, showStop, showRestart) {
    _show(_el(id, "lab-secondary"), showStop || showRestart);
    _show(_el(id, "lab-btn-stop"), showStop);
    _show(_el(id, "lab-btn-reload"), showRestart);
}

// fixed-height hint slot; mode: "" | "muted" | "warn" | "err"
function _hint(id, html, mode) {
    var el = _el(id, "lab-hint");
    if (!el) return;
    var wrap = (mode === "warn" || mode === "err") ? " wrap" : "";
    el.className = "lab-hint" + (mode ? " " + mode : "") + wrap;
    el.innerHTML = html || "&nbsp;";
}

function _labSetBusy(id, busy) {
    ["lab-btn-stop", "lab-btn-reload"].forEach(function (s) {
        var b = _el(id, s); if (b) b.disabled = busy;
    });
}

// ── render the whole card from a status payload ────────────────────────────
function _labUpdateUI(id, data) {
    _labSeen[id]  = true;
    _labFails[id] = 0;
    var phase   = data.phase || (data.running ? "Running" : "idle");
    var running = !!data.running;
    var stopping = phase === "Terminating";
    var booting = !running && !stopping && phase !== "idle" && phase !== "stopped"
                  && phase !== "error" && phase !== "Failed";
    var failed  = phase === "error" || phase === "Failed";
    var idle    = !running && !booting && !stopping && !failed;

    if (running) {
        var ws = data.workspace || {};
        _pill(id, "Running");
        _primary(id, "open", { terminal: ws.type && ws.type !== "VNC" });
        _secondary(id, true, true);
        _hint(id, '<i class="fas fa-lock me-1 opacity-75"></i>' + T("Services are reachable only from inside your workspace."));
    } else if (booting) {
        _pill(id, phase);
        _primary(id, "starting");
        _secondary(id, true, false);   // allow cancelling a provisioning lab
        _hint(id, _esc(data.message || T("Provisioning your workspace…")));
    } else if (stopping) {
        _pill(id, "Terminating");
        _primary(id, "stopping");
        _secondary(id, false, false);
        _hint(id, _esc(data.message || T("Shutting the workspace down…")));
    } else if (failed) {
        _pill(id, "Failed");
        _primary(id, "start", { label: T("Start Lab") });
        _secondary(id, false, false);
        _hint(id, '<i class="fas fa-triangle-exclamation me-1"></i>'
              + _esc(data.message || T("The lab failed to start.")) + T(" Press Start to try again."), "err");
    } else { // idle
        _pill(id, "idle");
        _primary(id, "start");
        _secondary(id, false, false);
        _hint(id, T("An isolated desktop workspace — start it to solve this challenge."));
    }

    _labEnsurePoll(id, booting || stopping ? 3000 : 8000);
}

// ── fetch status ───────────────────────────────────────────────────────────
// Transient connection blips must not freeze the card or flash a hard error.
function _labRefreshStatus(id) {
    fetch("/api/v1/lab/" + id + "/status", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (d) { _labUpdateUI(id, d); })
        .catch(function () {
            _labFails[id] = (_labFails[id] || 0) + 1;
            if (_labSeen[id]) return;            // keep last good UI, just retry silently
            if (_labFails[id] >= 2) {
                _primary(id, "checking", { label: T("Connecting to lab service…") });
                _hint(id, T("Retrying…"));
            }
        });
}

// ── start / stop / reload ──────────────────────────────────────────────────
function labAction(id, action) {
    _labSetBusy(id, true);
    // optimistic UI — reflect the intent immediately (no waiting for the poll)
    if (action === "start" || action === "reload") {
        _pill(id, "Pending");
        _primary(id, "starting", { label: action === "reload" ? T("Restarting…") : T("Starting…") });
        _secondary(id, true, false);
        _hint(id, action === "reload" ? T("Recreating your workspace…") : T("Requesting a workspace…"));
        _labEnsurePoll(id, 3000);
    } else if (action === "stop") {
        _pill(id, "Terminating");
        _primary(id, "stopping");
        _secondary(id, false, false);
        _hint(id, T("Shutting the workspace down…"));
    }

    fetch("/api/v1/lab/" + id + "/" + action, {
        method: "POST", credentials: "same-origin",
        // Content-Type must be JSON so CTFd validates the CSRF header token.
        headers: { "Content-Type": "application/json", "CSRF-Token": CTFd.config.csrfNonce },
        body: "{}",
    })
    .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { e.__status = r.status; throw e; })
                               .catch(function (e) { throw (e && e.message ? e : { message: "HTTP " + r.status }); });
        return r.json();
    })
    .then(function () { _labRefreshStatus(id); })
    .catch(function (err) {
        if (err && err.limit_reached) _labLimit(id, err);
        else {
            // revert the optimistic UI and explain the failure on the card
            _labRefreshStatus(id);
            _hint(id, '<i class="fas fa-circle-exclamation me-1"></i>'
                  + _esc((err && err.message) || T("Something went wrong. Try again.")), "err");
        }
    })
    .finally(function () { _labSetBusy(id, false); });
}

// At the lab limit → a calm, explicit card state (not a red alarm), with the way out.
function _labLimit(id, err) {
    _pill(id, "idle");
    _primary(id, "start");     // Start stays available; the hint says why it was blocked
    _secondary(id, false, false);
    var have = err.active, max = err.limit;
    var count = (have != null && max != null) ? (" (" + have + "/" + max + ")") : "";
    _hint(id, '<i class="fas fa-triangle-exclamation me-1"></i>' + T("You’re running the maximum number of labs")
          + count + T(". Stop one to start this challenge. ")
          + '<a href="/labs">' + T("Manage your labs →") + '</a>', "warn");
}

// First-open race: CTFd renders the challenge view at a moment not synchronised
// with this script or postRender, so the status request may never fire. A
// MutationObserver fires the instant the card is inserted. _labInit is idempotent.
(function () {
    function initCard(el) {
        var id = parseInt(el.id.slice("lab-card-".length), 10);
        if (id) _labInit(id);
    }
    document.querySelectorAll('[id^="lab-card-"]').forEach(initCard);
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
