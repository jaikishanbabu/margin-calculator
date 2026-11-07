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


  // ------------------------------------------------------------ dialogs + toasts (replace browser alert/prompt)
  let toastBox = null;
  function toast(title, kind = "ok", detail = "") {
    if (!toastBox) { toastBox = el("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(toastBox); }
    const icon = { ok: "✓", bad: "!", info: "i" }[kind] || "i";
    const t = el("div", { class: "toast " + kind },
      el("span", { class: "toast-icon", "aria-hidden": "true" }, icon),
      el("div", { class: "toast-body" }, el("div", { class: "toast-title" }, title), detail ? el("div", { class: "toast-detail" }, detail) : null),
      el("button", { type: "button", class: "toast-close", "aria-label": "Dismiss", onclick: () => t.remove() }, "×"));
    toastBox.append(t);
    requestAnimationFrame(() => t.classList.add("show"));
    setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, kind === "bad" ? 9000 : 4500);
  }

  function dialog({ title, message = "", input = null, confirmText = "OK", cancelText = "Cancel", danger = false }) {
    return new Promise((resolve) => {
      const prevFocus = document.activeElement;
      let field = null, err = el("p", { class: "msg bad" });
      if (input) {
        field = input.multiline
          ? el("textarea", { rows: "3", placeholder: input.placeholder || "", maxlength: "200" })
          : el("input", { type: input.type || "text", placeholder: input.placeholder || "", autocomplete: input.type === "password" ? "new-password" : "off" });
      }
      const ok = el("button", { type: "submit", class: danger ? "danger-btn" : "" }, confirmText);
      const cancel = el("button", { type: "button", class: "secondary" }, cancelText);
      const form = el("form", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "dlgTitle" },
        el("h2", { id: "dlgTitle" }, title),
        message ? el("p", { class: "dialog-msg" }, message) : null,
        field ? el("label", {}, input.label || "", field) : null,
        err,
        el("div", { class: "dialog-actions" }, cancel, ok));
      const overlay = el("div", { class: "overlay" }, form);
      function close(v) { overlay.classList.remove("show"); setTimeout(() => overlay.remove(), 180); document.removeEventListener("keydown", onKey); if (prevFocus) prevFocus.focus(); resolve(v); }
      function onKey(ev) { if (ev.key === "Escape") close(null); }
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        if (field) {
          const v = field.value.trim();
          if (input.required !== false && !v) { err.textContent = "This is required"; field.focus(); return; }
          if (input.minlength && v.length < input.minlength) { err.textContent = `At least ${input.minlength} characters`; field.focus(); return; }
          return close(v);
        }
        close(true);
      });
      cancel.addEventListener("click", () => close(null));
      overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(null); });
      document.addEventListener("keydown", onKey);
      document.body.append(overlay);
      requestAnimationFrame(() => { overlay.classList.add("show"); (field || ok).focus(); });
    });
  }

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
    $("#appView").classList.add("hidden"); $("#loginView").classList.remove("hidden");
    $("#profile").classList.add("hidden"); document.body.classList.remove("nav-on"); closeMenu(); closeDrawer();
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
    $("#avatarBtn").textContent = (me.username || "?").charAt(0).toUpperCase();
    $("#avatarBtn").setAttribute("aria-label", `Account menu for ${me.username}`);
    $("#menuName").textContent = me.username;
    $("#menuRole").textContent = (me.role === "superadmin" ? "Super admin" : "Admin") + (me.full_name && me.full_name.toLowerCase().replace(/\s/g, "") !== me.username ? " · " + me.full_name : "");
    $("#profile").classList.remove("hidden");
    document.querySelectorAll(".super-only").forEach(n => n.classList.toggle("hidden", me.role !== "superadmin"));
    if (me.must_change_password) {
      $("#nav").classList.add("hidden"); document.body.classList.remove("nav-on");
      showTab("account");
      setMsg($("#pwMsg"), "You are using a temporary password. Set your own password to continue.", false);
      return;
    }
    $("#nav").classList.remove("hidden"); document.body.classList.add("nav-on");
    const segSel = $("#logSegment");
    if (segSel.options.length === 1 && meta) for (const [k, v] of Object.entries(meta.segments)) segSel.append(el("option", { value: k }, `${k} - ${v.name}`));
    fillSegments(); refreshUploadForm();
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
  // ------------------------------------------------------------ profile menu (avatar)
  const avatarBtn = $("#avatarBtn"), pmenu = $("#profileMenu");
  function closeMenu() { pmenu.classList.add("hidden"); avatarBtn.setAttribute("aria-expanded", "false"); }
  avatarBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const open = pmenu.classList.contains("hidden");
    pmenu.classList.toggle("hidden", !open); avatarBtn.setAttribute("aria-expanded", String(open));
  });
  pmenu.addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    if (b.id === "menuLogout") logout(); else if (b.dataset.tab) showTab(b.dataset.tab);
    closeMenu();
  });
  document.addEventListener("click", (ev) => { if (!ev.target.closest("#profile")) closeMenu(); });

  // ------------------------------------------------------------ navigation drawer (phones)
  const nav = $("#nav"), scrim = $("#scrim"), menuBtn = $("#menuBtn");
  function openDrawer() { nav.classList.add("open"); scrim.classList.remove("hidden"); menuBtn.setAttribute("aria-expanded", "true"); }
  function closeDrawer() { nav.classList.remove("open"); scrim.classList.add("hidden"); menuBtn.setAttribute("aria-expanded", "false"); }
  menuBtn.addEventListener("click", () => nav.classList.contains("open") ? closeDrawer() : openDrawer());
  scrim.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { closeMenu(); closeDrawer(); } });

  // ------------------------------------------------------------ tabs
  function showTab(name) {
    document.querySelectorAll("#nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    closeDrawer(); window.scrollTo(0, 0);
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hidden", t.id !== "tab-" + name));
    ({ log: loadLog, audit: loadAudit, backups: loadBackups, users: loadUsers }[name] || (() => {}))();
  }
  $("#nav").addEventListener("click", (ev) => { const b = ev.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); });

  // ------------------------------------------------------------ upload form: segment -> file -> attach
  const upSeg = $("#upSegment"), upType = $("#upType"), upFile = $("#upFile"), upDate = $("#upDate"),
        upCycle = $("#upCycle"), upBtn = $("#upBtn");

  function fillSegments() {
    if (upSeg.options.length > 1) return;
    for (const [k, v] of Object.entries(meta.segments)) upSeg.append(el("option", { value: k }, `${v.name} (${k})`));
  }

  function refreshUploadForm() {
    const seg = upSeg.value;
    const sf = meta.segment_files[seg];
    // 2. file types for the chosen segment
    const keep = upType.value;
    upType.textContent = "";
    if (!sf) {
      upType.append(el("option", { value: "" }, "Choose segment first"));
      upType.disabled = true;
      $("#segFiles").classList.add("hidden");
    } else {
      upType.append(el("option", { value: "" }, "Choose file…"));
      for (const t of sf.required) upType.append(el("option", { value: t, selected: t === keep }, meta.file_types[t].label + "  · required"));
      for (const t of sf.optional) upType.append(el("option", { value: t, selected: t === keep }, meta.file_types[t].label + "  · optional"));
      upType.disabled = false;
      const box = $("#segFiles"); box.textContent = "";
      box.append(...sf.required.map(t => el("span", { class: "req" }, meta.file_types[t].label)),
                 ...sf.optional.map(t => el("span", {}, meta.file_types[t].label)));
      box.classList.remove("hidden");
    }
    // per-file details
    const t = upType.value;
    const isReq = sf && sf.required.includes(t);
    $("#upReqTag").classList.toggle("hidden", !t || !isReq);
    $("#upOptTag").classList.toggle("hidden", !t || isReq);
    $("#upTypeHint").textContent = t ? "Expected: " + (meta.file_hints[`${seg}:${t}`] || meta.file_types[t].label) : "";
    const isSpan = t === "span";
    $("#upCycleWrap").classList.toggle("hidden", !isSpan);
    $("#upDateHint").textContent = isSpan
      ? "Optional for SPAN: the date is read from inside the file. If you enter one, it must match."
      : "Required: the trading date this file is for.";
    upDate.required = !!t && !isSpan;
    upFile.disabled = !t;
    upFile.accept = t ? meta.file_types[t].ext.split(" ").join(",") : "";
    $("#upFileHint").textContent = t ? `Accepted: ${meta.file_types[t].ext} · up to ${meta.max_upload_mb} MB` : "";
    upBtn.disabled = !(seg && t && upFile.files.length);
  }
  upSeg.addEventListener("change", () => { upType.value = ""; refreshUploadForm(); });
  upType.addEventListener("change", refreshUploadForm);
  upFile.addEventListener("change", async () => {
    refreshUploadForm();
    const sug = $("#upCycleSuggest"); sug.textContent = "";
    const f = upFile.files[0];
    if (!f || upType.value !== "span") return;
    try {
      const s = await api("/api/admin/suggest", { method: "POST", json: { filename: f.name } });
      if (s.cycle_hint && [...upCycle.options].some(o => o.value === s.cycle_hint)) {
        upCycle.value = s.cycle_hint;
        sug.textContent = `Pre-selected "${s.cycle_hint}" from the file name — check it matches the published cycle.`;
      } else if (s.cycle_hint) {
        sug.textContent = `File name suggests "${s.cycle_hint}" — not in the list; contact the developer.`;
      }
    } catch (_) {}
  });

  $("#uploadForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const res = $("#upResult"); res.textContent = "";
    const fd = new FormData();
    fd.append("file", upFile.files[0], upFile.files[0].name);
    fd.append("segment", upSeg.value);
    fd.append("file_type", upType.value);
    fd.append("trade_date", (upDate.value || "").replaceAll("-", ""));
    if (upType.value === "span") fd.append("cycle", upCycle.value);
    if ($("#upReplace").checked) fd.append("replace", "1");
    upBtn.disabled = true; upBtn.textContent = "Uploading…";
    try {
      const { upload: u } = await api("/api/admin/uploads", { method: "POST", body: fd });
      res.append(el("div", { class: "result ok" },
        el("b", {}, `✓ Upload #${u.id} archived`), el("br"),
        `${meta.segments[u.segment].name} · ${meta.file_types[u.file_type].label} · trading date ${fmtDate(u.trade_date)} (from ${u.trade_date_source})`,
        u.cycle ? ` · cycle ${u.cycle} (from ${u.cycle_source})` : "", el("br"),
        `${(u.size_bytes / 1024).toFixed(0)} KB · checksum ${u.sha256.slice(0, 16)}… verified`));
      upFile.value = ""; $("#upReplace").checked = false;
    } catch (e) {
      res.append(el("div", { class: "result bad" }, el("b", {}, "Not uploaded: "), e.message));
    }
    upBtn.textContent = "Upload";
    refreshUploadForm();
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
            const reason = await dialog({ title: `Withdraw upload #${u.id}?`,
              message: `${u.filename} will no longer be used. Its record and the archived original are kept, and this is logged.`,
              input: { label: "Reason", placeholder: "e.g. wrong date uploaded", multiline: true }, confirmText: "Withdraw", danger: true });
            if (!reason) return;
            try { await api(`/api/admin/uploads/${u.id}/withdraw`, { method: "POST", json: { reason } }); toast(`Upload #${u.id} withdrawn`, "ok"); loadLog(); }
            catch (e) { toast("Could not withdraw", "bad", e.message); }
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
    try {
      const r = await api(path, { method: "POST" });
      if (r.checks) {
        const failed = r.checks.filter(c => !c.ok).map(c => c.check);
        r.ok ? toast("Restore test passed", "ok", `${r.checks.length}/${r.checks.length} checks · ${r.originals_checked} archived file(s) re-verified`)
             : toast("Restore test FAILED", "bad", failed.length ? "Failed: " + failed.join(", ") : (r.error || "See the table below"));
      } else if (r.row_counts) {
        const rows = Object.values(r.row_counts).reduce((a, b) => a + b, 0);
        toast("Backup created", "ok", `${rows} rows across ${Object.keys(r.row_counts).length} tables · checksum ${String(r.sha256).slice(0, 12)}…`);
      } else {
        r.ok ? toast("Done", "ok") : toast("Failed", "bad", r.error || "See the table below");
      }
    } catch (e) { toast("Failed", "bad", e.message); }
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
              if (u.active && !(await dialog({ title: `Disable ${u.username}?`, message: "They will be signed out and can't log in until you enable them again.", confirmText: "Disable", danger: true }))) return;
              try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: { active: !u.active } }); toast(`${u.username} ${u.active ? "disabled" : "enabled"}`, "ok"); loadUsers(); }
              catch (e) { toast("Could not update user", "bad", e.message); }
            } }, u.active ? "Disable" : "Enable"), " ",
            el("button", { class: "secondary small", onclick: async () => {
              const pw = await dialog({ title: `Reset password for ${u.username}`,
                message: "Give them this temporary password privately. They must change it at their next login, and their current sessions end now.",
                input: { label: "Temporary password", type: "password", minlength: 8, placeholder: "At least 8 characters" }, confirmText: "Reset password" });
              if (!pw) return;
              try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: { password: pw } }); toast("Password reset", "ok", `${u.username} must change it at next login`); }
              catch (e) { toast("Could not reset password", "bad", e.message); }
            } }, "Reset password"),
            u.locked ? el("button", { class: "secondary small", onclick: async () => {
              try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: { unlock: true } }); toast(`${u.username} unlocked`, "ok"); loadUsers(); }
              catch (e) { toast("Could not unlock", "bad", e.message); }
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
    try { const r = await api("/api/admin/users", { method: "POST", json: f }); setMsg($("#userMsg"), ""); toast(`Admin ${r.user.username} created`, "ok", "They must change the temporary password at first login"); ev.target.reset(); loadUsers(); }
    catch (e) { setMsg($("#userMsg"), e.message); }
  });

  // ------------------------------------------------------------ account
  $("#pwForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    try { await api("/api/auth/password", { method: "POST", json: f }); logout(); toast("Password changed", "ok", "Log in again with your new password"); }
    catch (e) { setMsg($("#pwMsg"), e.message); }
  });

  start();
})();
