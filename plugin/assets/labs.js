// "My Labs" dashboard: shows the concurrency limit and every running lab with
// quick controls, so players understand their quota and why a new lab won't start.
(function () {
    "use strict";

    var ROOT = "labs-root";
    var csrf = (window.init && window.init.csrfNonce) || "";

    var PHASE_CSS = {
        Running:     "bg-success",
        Pending:     "bg-warning text-dark",
        Terminating: "bg-warning text-dark",
        Failed:      "bg-danger",
    };

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function expiresText(iso) {
        if (!iso) return "";
        var ms = new Date(iso).getTime() - Date.now();
        if (isNaN(ms)) return "";
        if (ms <= 0) return "expiring…";
        var m = Math.floor(ms / 60000), h = Math.floor(m / 60);
        return h > 0 ? "expires in " + h + "h " + (m % 60) + "m" : "expires in " + m + "m";
    }

    function quotaBar(active, limit) {
        var pct = limit ? Math.min(100, Math.round((active / limit) * 100)) : 0;
        var full = active >= limit;
        var bar = full ? "bg-danger" : (active > 0 ? "bg-warning" : "bg-success");
        return ''
            + '<div class="d-flex justify-content-between align-items-baseline mb-1">'
            +   '<span class="fw-semibold">Running labs</span>'
            +   '<span class="text-muted small">' + active + ' / ' + limit + ' used</span>'
            + '</div>'
            + '<div class="progress" style="height:8px"><div class="progress-bar ' + bar
            +   '" role="progressbar" style="width:' + pct + '%"></div></div>'
            + (full
                ? '<div class="small text-danger mt-1"><i class="fas fa-circle-info me-1"></i>'
                  + 'Limit reached — stop a lab below before starting a new one.</div>'
                : '<div class="small text-muted mt-1">You can run up to ' + limit
                  + ' labs at once.</div>');
    }

    function labRow(lab) {
        var badge = PHASE_CSS[lab.phase] || "bg-secondary";
        var ws = lab.workspace;
        var open = (lab.running && ws && ws.address)
            ? '<a href="/lab/' + lab.challenge_id + '/enter" target="_blank" rel="noopener" '
              + 'class="btn btn-primary btn-sm"><i class="fas fa-display me-1"></i>Open</a>'
            : '<button class="btn btn-primary btn-sm" disabled>'
              + '<span class="spinner-border spinner-border-sm me-1"></span>Starting…</button>';

        return ''
            + '<div class="list-group-item d-flex align-items-center justify-content-between flex-wrap gap-2">'
            +   '<div>'
            +     '<div class="fw-semibold">' + esc(lab.challenge_name) + '</div>'
            +     '<div class="small text-muted">'
            +       '<span class="badge ' + badge + ' me-1">' + esc(lab.phase) + '</span>'
            +       (lab.running ? esc(expiresText(lab.expires)) : esc(lab.message || ""))
            +     '</div>'
            +   '</div>'
            +   '<div class="d-flex gap-2">'
            +     open
            +     '<a href="/challenges#' + encodeURIComponent(lab.challenge_name + "-" + lab.challenge_id)
            +       '" class="btn btn-outline-secondary btn-sm">'
            +       '<i class="fas fa-flag me-1"></i>Challenge</a>'
            +     '<button class="btn btn-outline-danger btn-sm" data-stop="' + lab.challenge_id + '">'
            +       '<i class="fas fa-stop me-1"></i>Stop</button>'
            +   '</div>'
            + '</div>';
    }

    function render(data) {
        var root = document.getElementById(ROOT);
        if (!root) return;

        var html = '<div class="card mb-3"><div class="card-body">' + quotaBar(data.active, data.limit) + '</div></div>';

        if (!data.labs.length) {
            html += '<div class="text-center text-muted py-5">'
                 +  '<i class="fas fa-display fa-2x mb-2 opacity-50"></i>'
                 +  '<div>No labs running.</div>'
                 +  '<div class="small">Open a challenge and press “Start Lab”.</div>'
                 +  '</div>';
        } else {
            html += '<div class="list-group">' + data.labs.map(labRow).join("") + '</div>';
        }
        root.innerHTML = html;

        root.querySelectorAll("[data-stop]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                stopLab(parseInt(btn.getAttribute("data-stop"), 10), btn);
            });
        });
    }

    function load() {
        fetch("/api/v1/labs/mine", { credentials: "same-origin", cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(render)
            .catch(function () {
                var root = document.getElementById(ROOT);
                if (root) root.innerHTML = '<div class="alert alert-danger">Could not load your labs. Retrying…</div>';
            });
    }

    function stopLab(id, btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
        fetch("/api/v1/lab/" + id + "/stop", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "CSRF-Token": csrf },
            body: "{}",
        })
        .then(function (r) { return r.json(); })
        .finally(function () { setTimeout(load, 600); });
    }

    document.addEventListener("DOMContentLoaded", function () {
        load();
        // Refresh while labs are still coming up / shutting down.
        setInterval(load, 5000);
    });
})();
