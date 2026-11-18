"use strict";
// Which engine host to talk to (Render or Railway), with automatic fallback.
// Same file in adminFolder/ and marginCalc/ - keep them identical.
//
// config.js sets either
//   window.MARGIN_API_BASES = { render: "https://…onrender.com", railway: "https://…up.railway.app" }
// or (one host / same origin) window.MARGIN_API_BASE = "".
// Both hosts share one database, so whichever answers gives the same data. The SuperAdmin picks the
// preferred host in Admin -> Hosting; it is stored in the database and reported by /api/health.
(function () {
  const multi = window.MARGIN_API_BASES && typeof window.MARGIN_API_BASES === "object"
    ? Object.entries(window.MARGIN_API_BASES).filter(([, u]) => u) : [];
  const bases = multi.length ? Object.fromEntries(multi.map(([k, u]) => [k, String(u).replace(/\/$/, "")]))
                             : { default: String(window.MARGIN_API_BASE || "").replace(/\/$/, "") };
  const names = Object.keys(bases);
  const KEY = "mc_backend";
  let store = null; try { store = window.localStorage; } catch (_) { /* private mode */ }
  const remembered = () => { try { return store && store.getItem(KEY); } catch (_) { return null; } };
  let current = names.includes(remembered()) ? remembered() : names[0];
  let probes = {};
  const listeners = [];

  function use(name) {
    if (!bases[name] || name === current) return;
    current = name;
    try { store && store.setItem(KEY, name); } catch (_) { /* ignore */ }
    listeners.forEach(fn => { try { fn(name); } catch (_) { /* ignore */ } });
  }

  function timeout(ms) {
    const c = new AbortController();
    setTimeout(() => c.abort(), ms);
    return c.signal;
  }

  async function probe(name, ms) {
    const t0 = Date.now();
    try {
      const r = await fetch(bases[name] + "/api/health", { signal: timeout(ms), credentials: "omit" });
      const j = await r.json();
      return probes[name] = Object.assign({ reachable: true, ms: Date.now() - t0 }, j);
    } catch (e) {
      return probes[name] = { reachable: false, ok: false, error: e && e.name === "AbortError" ? "no answer" : "unreachable" };
    }
  }

  // Ask every host at once; go with the SuperAdmin's choice if it answers, else whichever answers.
  // A sleeping free-plan host can take up to a minute to wake, so the wait is long when nothing answers.
  async function init() {
    if (names.length === 1) { await probe(current, 90000); return probes[current]; }
    const results = names.map(n => probe(n, 12000));
    let first = null;
    try { first = await Promise.any(results.map((p, i) => p.then(r => (r.reachable ? names[i] : Promise.reject())))); }
    catch (_) { /* none answered quickly */ }
    if (!first) {                          // all asleep or down: give them longer, one more round
      const again = await Promise.all(names.map(n => probe(n, 75000)));
      const i = again.findIndex(r => r.reachable);
      if (i >= 0) first = names[i];
    }
    if (!first) return probes[current];
    const wanted = probes[first].active_backend;
    if (wanted && bases[wanted] && wanted !== first) {
      const w = probes[wanted] && probes[wanted].reachable ? probes[wanted] : await probe(wanted, 8000);
      use(w.reachable ? wanted : first);
    } else use(first);
    return probes[current];
  }

  // Our engine always answers JSON. A non-JSON error page (host suspended, gateway error) or no
  // answer at all means "this host is down" -> try the other one once. JSON errors are real answers.
  async function hostDown(res) {
    if (res.ok) return false;
    const ct = res.headers.get("content-type") || "";
    return !ct.includes("application/json");
  }

  async function apiFetch(path, init) {
    const order = [current, ...names.filter(n => n !== current)];
    let lastErr = null;
    for (const name of order) {
      try {
        const res = await fetch(bases[name] + path, Object.assign({ credentials: "omit" }, init || {}));
        if (names.length > 1 && await hostDown(res) && name !== order[order.length - 1]) { lastErr = new Error(`HTTP ${res.status}`); continue; }
        use(name);
        return res;
      } catch (e) {
        lastErr = e;
        if (names.length === 1) break;
      }
    }
    throw lastErr || new Error("Server unreachable");
  }

  window.MarginBackend = {
    fetch: apiFetch, init, probe,
    names: () => names.slice(), base: () => bases[current], current: () => current,
    url: (n) => bases[n], probes: () => Object.assign({}, probes), onChange: (fn) => listeners.push(fn),
    multi: () => names.length > 1,
  };
})();
