"use strict";
// Admin UI. All server data is inserted with textContent (never innerHTML) to prevent XSS.
// The login token lives in sessionStorage only (gone when the tab closes).
(function () {
  const API = (window.MARGIN_API_BASE || "").replace(/\/$/, "");
  const $ = (s) => document.querySelector(s);
  let token = sessionStorage.getItem("mc_token") || "";
  let me = null, meta = null;

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k instanceof Node ? k : String(k));
    return e;
  }
  const ist = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short" });
  };
  const fmtDate = (d) => d && d.length === 8 ? `${d.slice(6)}-${d.slice(4, 6)}-${d.slice(0, 4)}` : (d || "");
  const pill = (text, kind) => el("span", { class: "pill " + (kind || "") }, text);
  const setMsg = (node, text, good) => { node.textContent = text || ""; node.className = "msg " + (text ? (good ? "ok" : "bad") : ""); };

  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    if (token) headers["Authorization"] = "Bearer " + token;
    if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
    const res = await fetch(API + path, { method: opts.method || "GET", headers, body: opts.body, credentials: "omit" });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (res.status === 401 && path !== "/api/auth/login") { logout(); throw new Error("Session expired - log in again"); }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  // ------------------------------------------------------------ session
  function logout() {
    token = ""; me = null; sessionStorage.removeItem("mc_token");
    $("#appView").classList.add("hidden"); $("#loginView").classList.remove("hidden"); $("#who").textContent = "";
  }

  async function start() {
    try {
      const h = await fetch(API + "/api/health").then(r => r.json());
      $("#envBadge").textContent = h.env || "";
    } catch (_) { $("#envBadge").textContent = "offline"; }
    if (!token) return logout();
    try {
      me = (await api("/api/auth/me")).user;
      if (!me.must_change_password) meta = await api("/api/admin/meta");
    } catch (e) { return logout(); }
    $("#loginView").classList.add("hidden"); $("#appView").classList.remove("hidden");
    $("#who").textContent = `${me.username} · ${me.role}`;
    document.querySelectorAll(".super-only").forEach(n => n.classList.toggle("hidden", me.role !== "superadmin"));
    if (me.must_change_password) {
      document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("hidden", b.dataset.tab !== "account"));
      showTab("account");
      setMsg($("#pwMsg"), "You are using a temporary password. Set your own password to continue.", false);
      return;
    }
    document.querySelectorAll("#tabs button").forEach(b => { if (!b.classList.contains("super-only") || me.role === "superadmin") b.classList.remove("hidden"); });
    const segSel = $("#logSegment");
    if (segSel.options.length === 1 && meta) for (const [k, v] of Object.entries(meta.segments)) segSel.append(el("option", { value: k }, `${k} - ${v.name}`));
    showTab("upload");
  }

  $("#loginForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    setMsg($("#loginMsg"), "");
    try {
      const r = await api("/api/auth/login", { method: "POST", json: { username: f.get("username"), password: f.get("password") } });
      token = r.token; sessionStorage.setItem("mc_token", token); ev.target.reset(); start();
    } catch (e) { setMsg($("#loginMsg"), e.message); }
  });
  $("#logout").addEventListener("click", logout);

  // ------------------------------------------------------------ tabs
  function showTab(name) {
    document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hidden", t.id !== "tab-" + name));
    ({ log: loadLog, audit: loadAudit, backups: loadBackups, users: loadUsers }[name] || (() => {}))();
  }
  $("#tabs").addEventListener("click", (ev) => { if (ev.target.dataset.tab) showTab(ev.target.dataset.tab); });

  // ------------------------------------------------------------ upload queue
  const queue = [];
  $("#filePick").addEventListener("change", async (ev) => {
    for (const file of ev.target.files) {
      let s = {};
      try { s = await api("/api/admin/suggest", { method: "POST", json: { filename: file.name } }); } catch (_) {}
      queue.push({ file, s, node: null });
    }
    ev.target.value = "";
    renderQueue();
  });

  function typeOptions(seg, selected) {
    const sf = meta.segment_files[seg] || { required: [], optional: [] };
    return [...sf.required, ...sf.optional].map(t =>
      el("option", { value: t, selected: t === selected }, meta.file_types[t].label + (sf.required.includes(t) ? " *" : "")));
  }

  function renderQueue() {
    const box = $("#queue"); box.textContent = "";
    queue.forEach((q, i) => {
      if (q.done) return;
      const seg = el("select", { name: "segment" }, el("option", { value: "" }, "Segment…"),
        Object.entries(meta.segments).map(([k, v]) => el("option", { value: k, selected: k === q.s.segment }, `${k} - ${v.name}`)));
      const type = el("select", { name: "file_type" });
      const fillTypes = () => { type.textContent = ""; type.append(el("option", { value: "" }, "File type…"), ...typeOptions(seg.value, q.s.file_type)); toggleCycle(); };
      const dateIn = el("input", { type: "date", name: "trade_date" });
      if (q.s.trade_date_hint) dateIn.value = `${q.s.trade_date_hint.slice(0, 4)}-${q.s.trade_date_hint.slice(4, 6)}-${q.s.trade_date_hint.slice(6)}`;
      const cycleIn = el("input", { name: "cycle", placeholder: "e.g. i1, i2 … s", maxlength: "12", autocapitalize: "none" });
      if (q.s.cycle_hint) cycleIn.value = q.s.cycle_hint;
      const cycleLabel = el("label", {}, "SPAN cycle (as published)", cycleIn,
        el("span", { class: "src" }, q.s.cycle_hint ? "suggested from filename — confirm" : "required unless the file states it"));
      const toggleCycle = () => cycleLabel.classList.toggle("hidden", type.value !== "span");
      seg.addEventListener("change", fillTypes); type.addEventListener("change", toggleCycle);
      const replace = el("input", { type: "checkbox" });
      const status = el("p", { class: "msg" });
      const remove = el("button", { class: "secondary small", type: "button", onclick: () => { queue.splice(i, 1); renderQueue(); } }, "Remove");
      q.node = { seg, type, dateIn, cycleIn, replace, status };
      box.append(el("div", { class: "qitem" },
        el("div", { class: "fname" }, `${q.file.name} (${(q.file.size / 1048576).toFixed(2)} MB)`),
        el("div", { class: "qgrid" },
          el("label", {}, "Segment", seg), el("label", {}, "File type", type),
          el("label", {}, "Trading date", dateIn, el("span", { class: "src" }, "SPAN: read from file; others: confirm")),
          cycleLabel),
        el("label", { class: "check" }, replace, "Replace an existing file for the same date/cycle"),
        el("div", { class: "row-between" }, status, remove)));
      fillTypes();
    });
    $("#uploadAll").classList.toggle("hidden", !queue.some(q => !q.done));
  }

  $("#uploadAll").addEventListener("click", async () => {
    $("#uploadAll").disabled = true;
    for (const q of queue) {
      if (q.done || !q.node) continue;
      const n = q.node;
      if (!n.seg.value || !n.type.value) { setMsg(n.status, "Choose segment and file type"); continue; }
      const fd = new FormData();
      fd.append("file", q.file, q.file.name);
      fd.append("segment", n.seg.value); fd.append("file_type", n.type.value);
      fd.append("trade_date", (n.dateIn.value || "").replaceAll("-", ""));
      if (n.type.value === "span") fd.append("cycle", n.cycleIn.value.trim());
      if (n.replace.checked) fd.append("replace", "1");
      setMsg(n.status, "Uploading…", true);
      try {
        const r = await api("/api/admin/uploads", { method: "POST", body: fd });
        const u = r.upload;
        setMsg(n.status, `✓ #${u.id} archived · ${u.segment} ${fmtDate(u.trade_date)} (${u.trade_date_source})` +
          (u.cycle ? ` · cycle ${u.cycle} (${u.cycle_source})` : "") + ` · sha256 ${u.sha256.slice(0, 12)}…`, true);
        q.done = true;
      } catch (e) { setMsg(n.status, e.message); }
    }
    $("#uploadAll").disabled = false;
    if (queue.every(q => q.done)) { queue.length = 0; setTimeout(renderQueue, 4000); }
  });

  // ------------------------------------------------------------ upload log
  $("#logSegment").addEventListener("change", loadLog);
  async function loadLog() {
    const seg = $("#logSegment").value;
    const tb = $("#logTable tbody"); tb.textContent = "";
    try {
      const { uploads } = await api("/api/admin/uploads" + (seg ? "?segment=" + encodeURIComponent(seg) : ""));
      if (!uploads.length) tb.append(el("tr", {}, el("td", { colspan: "10" }, "No uploads yet")));
      for (const u of uploads) {
        const kind = u.status === "archived" ? "ok" : (u.status === "superseded" ? "warn" : "bad");
        const actions = el("td", {});
        if (u.status === "archived") {
          actions.append(el("button", { class: "secondary small", onclick: async (ev) => {
            ev.target.disabled = true;
            try { const r = await api(`/api/admin/uploads/${u.id}/verify`, { method: "POST" }); ev.target.textContent = r.ok ? "✓ checksum ok" : "✗ MISMATCH"; }
            catch (e) { ev.target.textContent = e.message; }
          } }, "Verify"), " ",
          el("button", { class: "secondary small", onclick: async () => {
            const reason = prompt("Reason for withdrawing this file? (the record and archived original are kept)");
            if (!reason) return;
            try { await api(`/api/admin/uploads/${u.id}/withdraw`, { method: "POST", json: { reason } }); loadLog(); } catch (e) { alert(e.message); }
          } }, "Withdraw"));
        }
        tb.append(el("tr", {},
          el("td", {}, u.id), el("td", {}, u.segment), el("td", {}, u.file_type),
          el("td", {}, fmtDate(u.trade_date), el("div", { class: "src" }, u.trade_date_source)),
          el("td", {}, u.cycle || "—", u.cycle ? el("div", { class: "src" }, u.cycle_source) : null),
          el("td", { class: "detail" }, u.filename, el("div", { class: "src" }, `${(u.size_bytes / 1024).toFixed(0)} KB · ${u.sha256.slice(0, 12)}…`)),
          el("td", {}, u.uploaded_by), el("td", {}, ist(u.uploaded_at)),
          el("td", {}, pill(u.status, kind), u.message ? el("div", { class: "src" }, u.message) : null), actions));
      }
    } catch (e) { tb.append(el("tr", {}, el("td", { colspan: "10" }, e.message))); }
  }

  // ------------------------------------------------------------ audit
  async function loadAudit() {
    const tb = $("#auditTable tbody"); tb.textContent = "";
    try {
      const { entries } = await api("/api/admin/audit?limit=300");
      for (const a of entries) tb.append(el("tr", {}, el("td", {}, a.seq), el("td", {}, ist(a.ts)), el("td", {}, a.username || ""),
        el("td", {}, a.action), el("td", { class: "detail" }, a.detail || ""), el("td", {}, a.ip || "")));
    } catch (e) { setMsg($("#auditMsg"), e.message); }
  }
  $("#verifyAudit").addEventListener("click", async () => {
    try {
      const r = await api("/api/admin/audit/verify");
      setMsg($("#auditMsg"), r.ok ? `✓ Chain intact: ${r.checked} entries verified` : `✗ Chain broken at seq ${r.first_bad_seq}: ${r.reason}`, r.ok);
    } catch (e) { setMsg($("#auditMsg"), e.message); }
  });

  // ------------------------------------------------------------ backups
  async function loadBackups() {
    const tb = $("#backupTable tbody"); tb.textContent = "";
    try {
      const { runs, status } = await api("/api/admin/backups");
      setMsg($("#backupStatus"),
        (status.last_backup_at ? `Last backup ${ist(status.last_backup_at)} (${status.last_backup_age_days} days ago). ` : "No backup yet. ") +
        (status.last_restore_test_at ? `Last restore test ${ist(status.last_restore_test_at)}: ${status.last_restore_test_ok ? "passed" : "FAILED"}.` : "No restore test yet."),
        !status.backup_overdue && status.last_restore_test_ok);
      for (const r of runs) {
        const d = r.details || {};
        const failed = (d.checks || []).filter(c => !c.ok).map(c => c.check);
        const summary = r.kind === "backup"
          ? (d.row_counts ? Object.entries(d.row_counts).map(([k, v]) => `${k}:${v}`).join(" ") : (d.error || ""))
          : (d.checks ? `${d.checks.length - failed.length}/${d.checks.length} checks passed` + (failed.length ? ` · failed: ${failed.join(", ")}` : "") : (d.error || ""));
        tb.append(el("tr", {}, el("td", {}, r.id), el("td", {}, r.kind), el("td", {}, ist(r.started_at)),
          el("td", {}, pill(r.status, r.status === "ok" ? "ok" : (r.status === "running" ? "warn" : "bad"))),
          el("td", {}, r.triggered_by || ""), el("td", { class: "detail" }, summary)));
      }
    } catch (e) { setMsg($("#backupStatus"), e.message); }
  }
  async function runOp(btn, path) {
    btn.disabled = true; const t = btn.textContent; btn.textContent = "Working…";
    try { const r = await api(path, { method: "POST" }); alert(r.ok ? "Done ✓" : "Failed: " + (r.error || "see table")); }
    catch (e) { alert(e.message); }
    btn.disabled = false; btn.textContent = t; loadBackups();
  }
  $("#runBackup").addEventListener("click", (ev) => runOp(ev.target, "/api/admin/backups/run"));
  $("#runRestore").addEventListener("click", (ev) => runOp(ev.target, "/api/admin/backups/restore-test"));

  // ------------------------------------------------------------ users
  async function loadUsers() {
    const tb = $("#userTable tbody"); tb.textContent = "";
    try {
      const { users } = await api("/api/admin/users");
      for (const u of users) {
        const act = el("td", {});
        if (u.username !== me.username && u.created_by !== "system") {
          act.append(
            el("button", { class: "secondary small", onclick: async () => {
              try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: { active: !u.active } }); loadUsers(); } catch (e) { alert(e.message); }
            } }, u.active ? "Disable" : "Enable"), " ",
            el("button", { class: "secondary small", onclick: async () => {
              const pw = prompt(`New temporary password for ${u.username} (12+ chars, 3 of: lower/upper/digit/symbol)`);
              if (!pw) return;
              try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: { password: pw } }); alert("Password reset. Their old sessions are signed out."); } catch (e) { alert(e.message); }
            } }, "Reset password"),
            u.locked ? el("button", { class: "secondary small", onclick: async () => {
              try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: { unlock: true } }); loadUsers(); } catch (e) { alert(e.message); }
            } }, "Unlock") : null);
        }
        tb.append(el("tr", {}, el("td", {}, u.username, u.full_name ? el("div", { class: "src" }, u.full_name) : null), el("td", {}, u.role),
          el("td", {}, pill(u.locked ? "locked" : (u.active ? "active" : "disabled"), u.active && !u.locked ? "ok" : "bad")),
          el("td", {}, u.created_by || ""), el("td", {}, ist(u.last_login_at)), act));
      }
    } catch (e) { tb.append(el("tr", {}, el("td", { colspan: "6" }, e.message))); }
  }
  $("#newUser").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    try { const r = await api("/api/admin/users", { method: "POST", json: f }); setMsg($("#userMsg"), `Created ${r.user.username}`, true); ev.target.reset(); loadUsers(); }
    catch (e) { setMsg($("#userMsg"), e.message); }
  });

  // ------------------------------------------------------------ account
  $("#pwForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    try { await api("/api/auth/password", { method: "POST", json: f }); alert("Password changed. Please log in again."); logout(); }
    catch (e) { setMsg($("#pwMsg"), e.message); }
  });

  start();
})();
