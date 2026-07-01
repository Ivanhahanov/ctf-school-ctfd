// Labs dashboard: the team's concurrency quota + every lab with quick controls,
// so players see their quota and why a new lab won't start. Visual language matches
// the challenge card (status pills, calm states).
(function () {
    "use strict";

    var ROOT = "labs-root";
    var csrf = (window.init && window.init.csrfNonce) || "";

    // Inject the shared styling once.
    if (!document.getElementById("labs-style")) {
        var st = document.createElement("style");
        st.id = "labs-style";
        st.textContent = [
            ".lab-pill{font-size:.68rem;font-weight:700;letter-spacing:.03em;text-transform:uppercase;padding:.22rem .6rem;border-radius:999px;white-space:nowrap;display:inline-flex;align-items:center;gap:.35rem}",
            ".lab-pill .dot{width:.5rem;height:.5rem;border-radius:50%;background:currentColor}",
            ".lab-pill.is-idle{color:#6c757d;background:rgba(108,117,125,.15)}",
            ".lab-pill.is-run{color:#198754;background:rgba(25,135,84,.15)}",
            ".lab-pill.is-busy{color:#b8860b;background:rgba(255,193,7,.18)}",
            ".lab-pill.is-fail{color:#dc3545;background:rgba(220,53,69,.15)}",
            ".lab-pill.is-busy .dot{animation:labpulse 1s ease-in-out infinite}",
            "@keyframes labpulse{0%,100%{opacity:1}50%{opacity:.25}}",
            ".labs-quota{border:1px solid var(--bs-border-color,#dee2e6);border-radius:.8rem;padding:1rem 1.1rem;background:var(--bs-tertiary-bg,#f8f9fa);margin-bottom:1rem}",
            ".labs-quota-head{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:.55rem}",
            ".labs-quota-head .t{font-weight:600}",
            ".labs-quota-head .c{font-variant-numeric:tabular-nums;color:var(--bs-secondary-color,#6c757d);font-size:.85rem}",
            ".labs-slots{display:flex;gap:.35rem}",
            ".labs-slot{flex:1;height:7px;border-radius:999px;background:rgba(127,127,127,.2)}",
            ".labs-slot.on{background:#198754}.labs-slot.full{background:#dc3545}",
            ".labs-quota-note{font-size:.8rem;margin-top:.55rem;color:var(--bs-secondary-color,#6c757d)}",
            ".labs-quota-note.warn{color:#997404}",
            ".lab-tile{border:1px solid var(--bs-border-color,#dee2e6);border-radius:.7rem;background:var(--bs-body-bg,#fff);padding:.85rem 1rem;display:flex;align-items:center;justify-content:space-between;gap:.75rem;flex-wrap:wrap;margin-bottom:.6rem}",
            ".lab-tile-name{font-weight:600;text-decoration:none;color:inherit}",
            ".lab-tile-name:hover{text-decoration:underline}",
            ".lab-tile-sub{display:flex;align-items:center;gap:.6rem;margin-top:.35rem;font-size:.8rem;color:var(--bs-secondary-color,#6c757d)}",
            ".lab-tile-actions{display:flex;gap:.4rem;flex-wrap:wrap}",
            ".labs-empty{text-align:center;color:var(--bs-secondary-color,#6c757d);padding:3rem 1rem;border:1px dashed var(--bs-border-color,#dee2e6);border-radius:.8rem}",
        ].join("");
        document.head.appendChild(st);
    }

    var PILL = {
        Running:     ["is-run",  "Running"],
        Pending:     ["is-busy", "Starting"],
        Provisioning:["is-busy", "Starting"],
        Terminating: ["is-busy", "Stopping"],
        Failed:      ["is-fail", "Failed"],
        error:       ["is-fail", "Failed"],
    };

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }
    function pill(phase) {
        var d = PILL[phase] || ["is-idle", phase || "Idle"];
        return '<span class="lab-pill ' + d[0] + '"><span class="dot"></span>' + esc(d[1]) + "</span>";
    }
    function expiresText(iso) {
        if (!iso) return "";
        var ms = new Date(iso).getTime() - Date.now();
        if (isNaN(ms)) return "";
        if (ms <= 0) return "expiring…";
        var m = Math.floor(ms / 60000), h = Math.floor(m / 60);
        return h > 0 ? "expires in " + h + "h " + (m % 60) + "m" : "expires in " + m + "m";
    }

    function quota(active, limit) {
        var full = active >= limit, free = Math.max(0, limit - active), slots = "";
        for (var i = 0; i < limit; i++)
            slots += '<div class="labs-slot ' + (i < active ? (full ? "full" : "on") : "") + '"></div>';
        var note = full
            ? '<div class="labs-quota-note warn"><i class="fas fa-triangle-exclamation me-1"></i>Limit reached — stop a lab below to start a new one.</div>'
            : '<div class="labs-quota-note">' + free + ' of ' + limit + ' slot' + (free === 1 ? "" : "s") + ' free.</div>';
        return '<div class="labs-quota">'
            + '<div class="labs-quota-head"><span class="t">Running labs</span><span class="c">' + active + ' / ' + limit + '</span></div>'
            + '<div class="labs-slots">' + slots + '</div>' + note + '</div>';
    }

    function tile(lab) {
        var anchor = encodeURIComponent(lab.challenge_name + "-" + lab.challenge_id);
        var open = (lab.running && lab.workspace && lab.workspace.address)
            ? '<a href="/lab/' + lab.challenge_id + '/enter" target="_blank" rel="noopener" class="btn btn-primary btn-sm"><i class="fas fa-display me-1"></i>Open</a>'
            : '<button class="btn btn-secondary btn-sm" disabled><span class="spinner-border spinner-border-sm me-1"></span>Starting…</button>';
        var meta = lab.running ? esc(expiresText(lab.expires)) : esc(lab.message || "");
        return '<div class="lab-tile">'
            + '<div><a class="lab-tile-name" href="/challenges#' + anchor + '">' + esc(lab.challenge_name) + '</a>'
            +   '<div class="lab-tile-sub">' + pill(lab.phase) + (meta ? '<span><i class="far fa-clock me-1"></i>' + meta + '</span>' : '') + '</div></div>'
            + '<div class="lab-tile-actions">' + open
            +   '<button class="btn btn-outline-danger btn-sm" data-stop="' + lab.challenge_id + '"><i class="fas fa-stop me-1"></i>Stop</button>'
            + '</div></div>';
    }

    function render(data) {
        var root = document.getElementById(ROOT);
        if (!root) return;
        var html = quota(data.active, data.limit);
        html += data.labs.length
            ? data.labs.map(tile).join("")
            : '<div class="labs-empty"><i class="fas fa-display fa-2x mb-2 opacity-50"></i>'
              + '<div class="fw-semibold">No labs running</div>'
              + '<div class="small">Open a challenge and press “Start Lab”.</div></div>';
        root.innerHTML = html;
        root.querySelectorAll("[data-stop]").forEach(function (btn) {
            btn.addEventListener("click", function () { stopLab(parseInt(btn.getAttribute("data-stop"), 10), btn); });
        });
    }

    function load() {
        fetch("/api/v1/labs/mine", { credentials: "same-origin", cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(render)
            .catch(function () {
                var root = document.getElementById(ROOT);
                if (root) root.innerHTML = '<div class="alert alert-danger">Could not load labs. Retrying…</div>';
            });
    }

    function stopLab(id, btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
        fetch("/api/v1/lab/" + id + "/stop", {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json", "CSRF-Token": csrf }, body: "{}",
        }).then(function (r) { return r.json(); }).finally(function () { setTimeout(load, 600); });
    }

    document.addEventListener("DOMContentLoaded", function () {
        load();
        setInterval(load, 5000);   // refresh while labs come up / shut down
    });
})();
