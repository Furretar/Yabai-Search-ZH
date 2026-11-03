// Picks this visitor's "local host" and shows it in the hamburger menu.
//
// Every 15 seconds (only while the tab is visible) the page asks the Worker's
// /__probe route how long each host takes to answer from the Cloudflare edge nearest
// this visitor. The local host is the one that answers searches for this visitor; it
// is saved in the yabai_local cookie, which the Worker reads. The rule:
//   - the local host changes when another host has been at least 20% faster
//     (median of the last 5 probes) for 30 seconds in a row;
//   - it changes at once if the current local host stops answering.
// The global host (the only one that takes account writes) is chosen by the Worker's
// Durable Object, not here; this script only displays it.
(function () {
    "use strict";
    var EVERY_MS = 15000, KEEP = 5, MARGIN = 0.2, HOLD_MS = 30000;
    var KEY = "yabai.hosts";

    function median(list) {
        var a = list.slice().sort(function (x, y) { return x - y; });
        var mid = a.length >> 1;
        return a.length % 2 ? a[mid] : (a[mid - 1] + a[mid]) / 2;
    }

    // Pure rule (also unit-tested in scripts/test_hosts.mjs). `st` is
    // {local, challenger:{host,since}|null}; `medians` maps each answering host to
    // its median time. Returns the updated state.
    function pickLocal(st, medians, now) {
        var names = Object.keys(medians);
        if (!names.length) return st;
        var fastest = names.reduce(function (a, b) { return medians[b] < medians[a] ? b : a; });
        if (!st.local || !(st.local in medians)) {
            return { local: fastest, challenger: null }; // none yet, or it stopped answering
        }
        if (fastest !== st.local && medians[fastest] <= medians[st.local] * (1 - MARGIN)) {
            var c = st.challenger;
            if (!c || c.host !== fastest) return { local: st.local, challenger: { host: fastest, since: now } };
            if (now - c.since >= HOLD_MS) return { local: fastest, challenger: null };
            return st;
        }
        return { local: st.local, challenger: null };
    }

    if (typeof window === "undefined") { // loaded by the Node test
        globalThis.__pickLocal = pickLocal;
        return;
    }

    function load() {
        try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
    }
    function save(v) {
        try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* private mode */ }
    }

    function show(local, writer) {
        var box = document.getElementById("menu-hosts");
        if (!box) return;
        box.hidden = false;
        document.getElementById("menu-local-host").textContent = local || "-";
        document.getElementById("menu-global-host").textContent = writer || "-";
    }

    function probe() {
        if (document.visibilityState === "hidden") return;
        fetch("/__probe", { cache: "no-store" })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) {
                if (!data || !data.t) return; // dev copy or no Worker: nothing to do
                var mem = load();
                mem.samples = mem.samples || {};
                var medians = {};
                Object.keys(data.t).forEach(function (h) {
                    var list = mem.samples[h] || [];
                    if (data.t[h] === null) { mem.samples[h] = list; return; } // down: no median now
                    list.push(data.t[h]);
                    mem.samples[h] = list.slice(-KEEP);
                    medians[h] = median(mem.samples[h]);
                });
                var next = pickLocal({ local: mem.local, challenger: mem.challenger || null }, medians, Date.now());
                mem.local = next.local;
                mem.challenger = next.challenger;
                save(mem);
                if (next.local) {
                    document.cookie = "yabai_local=" + next.local +
                        "; Max-Age=2592000; Path=/; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
                }
                show(next.local, data.writer);
            })
            .catch(function () { /* offline: keep what we have */ });
    }

    probe();
    setInterval(probe, EVERY_MS);
})();
