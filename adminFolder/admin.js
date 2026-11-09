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
      const cancel = cancelText === null ? null : el("button", { type: "button", class: "secondary" }, cancelText);
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
      if (cancel) cancel.addEventListener("click", () => close(null));
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
    if (name !== "fetcher") clearTimeout(fxTimer);
    if (name !== "ready") clearTimeout(rdTimer);
    if (name !== "holidays") clearTimeout(hcTimer);
    ({ log: loadLog, audit: loadAudit, backups: loadBackups, users: loadUsers, fetcher: loadFetcher, ready: loadReady,
       holidays: loadHolidays }[name] || (() => {}))();
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


  // ------------------------------------------------------------ exchange file fetcher
  const FX_ICON = { archived: "✓", built: "✓", have: "↷", pending: "–", not_due: "–", queued: "·", running: "⟳", failed: "✗", blocked: "✗" };
  const FX_WORD = { archived: "Downloaded", have: "Already have", pending: "Not published yet", not_due: "Not due yet",
                    queued: "Waiting", running: "Downloading…", failed: "Failed", blocked: "Blocked by exchange", built: "Built" };
  const SOURCE_NAMES = {
    "nfo.span": "SPAN", "nfo.contract_master": "Contract master", "nfo.market_lots": "Market lots", "nfo.elm": "ELM / exposure",
    "nfo.bhavcopy": "Bhavcopy", "nfo.settlement_prices": "Settlement prices", "nfo.ban_list": "F&O ban list",
    "bfo.span_xml": "SPAN", "bfo.span_spn": "SPAN (.spn)", "bfo.elm": "ELM / exposure", "bfo.bhavcopy": "Bhavcopy",
    "mcx.span": "SPAN", "mcx.margins": "Margins (ELM, additional)", "dhan.scrip_master": "Scrip master (Dhan)", "mcx.bhavcopy": "Bhavcopy",
    "cds.span": "SPAN", "cds.contract_master": "Contract master", "cds.elm": "ELM", "cds.bhavcopy": "Bhavcopy",
    "nse.var": "VaR", "nse.bhavcopy": "Bhavcopy", "nse.regulatory_indicator": "Regulatory indicator (T2T/ASM)",
    "nse.security": "Security master", "nse.asm": "ASM list", "nse.gsm": "GSM list", "nse.security_list": "Security list (T2T)",
    "bse.var": "VaR", "bse.bhavcopy": "Bhavcopy" };
  const BASIS_NAME = { bod: "start-of-day", latest: "latest cycle" };
  function fxName(ref) {
    if (ref.startsWith("calendar:")) return `Holiday calendar ${ref.split(":")[2]}`;
    if (ref.startsWith("build:")) return `Parsed data · ${BASIS_NAME[ref.split(":")[2]] || ref.split(":")[2]}`;
    const [sid, variant] = ref.split(":");
    let n = SOURCE_NAMES[sid] || sid;
    if (variant) {
      const m = variant.match(/^(cycle|batch)=(.+)$/);
      n += m ? (m[1] === "cycle" ? ` ${m[2]}` : ` batch ${m[2]}`) : "";
    }
    return n;
  }
  let fx = null, fxViewId = null, fxTimer = null;

  async function loadFetcher() {
    try { fx = await api("/api/admin/fetcher"); } catch (e) { toast("Couldn't load fetcher", "bad", e.message); return; }
    $("#fxNotConfigured").classList.toggle("hidden", fx.configured);
    const seg = $("#fxSeg");
    if (seg.options.length === 1) for (const k of fx.segments) seg.append(el("option", { value: k }, `${fx.segment_names[k]} (${k})`));
    const st = fx.settings;
    $("#fxAuto").checked = !!st.fetch_auto_enabled; $("#fxAutoLabel").textContent = st.fetch_auto_enabled ? "On" : "Off";
    $("#fxFreq").value = String(st.fetch_frequency_min);
    $("#fxAuto").disabled = $("#fxFreq").disabled = !fx.can_edit;
    $("#fxFreq").closest("label").classList.toggle("hidden", !st.fetch_auto_enabled);
    $("#fxReadOnly").classList.toggle("hidden", fx.can_edit);
    $("#fxWindow").textContent = `Runs on trading days, ${st.fetch_window_start}–${st.fetch_window_end} IST.`;
    const lastAuto = fx.runs.find(r => r.trigger === "auto");
    $("#fxNext").textContent = st.fetch_auto_enabled
      ? (fx.next_due ? `Next run ≈ ${ist(fx.next_due)}` : "Next run: tomorrow's window") + (lastAuto ? ` · last auto run ${ist(lastAuto.created_at)} (${lastAuto.status})` : "")
      : "";
    $("#fxRun").disabled = !!fx.active;
    $("#fxRun").textContent = fx.active ? `Run #${fx.active.id} in progress…` : "Manually fetch exchange files";
    renderFxHistory();
    const show = fxViewId || (fx.active && fx.active.id) || (fx.runs[0] && fx.runs[0].id);
    if (show) await loadFxRun(show); else { $("#fxProgress").textContent = ""; $("#fxRunMeta").textContent = "No runs yet."; }
  }

  function renderFxHistory() {
    const tb = $("#fxHistory tbody"); tb.textContent = "";
    if (!fx.runs.length) { tb.append(el("tr", {}, el("td", { colspan: "7" }, "No runs yet"))); return; }
    for (const r of fx.runs) {
      const kind = r.status === "done" ? "ok" : (["queued", "running"].includes(r.status) ? "warn" : (r.status === "skipped" ? "" : "bad"));
      const counts = r.summary ? Object.entries(r.summary).map(([k, v]) => `${FX_WORD[k] || k} ${v}`).join(" · ") : (r.message || "");
      tb.append(el("tr", { class: "clickable", onclick: () => { fxViewId = r.id; loadFxRun(r.id); window.scrollTo({ top: 0, behavior: "smooth" }); } },
        el("td", {}, r.id), el("td", {}, r.trigger), el("td", {}, (r.segments || []).join(", ")),
        el("td", {}, ist(r.created_at)), el("td", {}, r.requested_by), el("td", {}, pill(r.status, kind)), el("td", { class: "detail" }, counts)));
    }
  }

  async function loadFxRun(id) {
    clearTimeout(fxTimer);
    let r;
    try { r = (await api(`/api/admin/fetcher/runs/${id}`)).run; } catch (e) { return; }
    const live = ["queued", "running"].includes(r.status);
    $("#fxRunTitle").textContent = `Run #${r.id}`;
    $("#fxRunStatus").textContent = "";
    $("#fxRunStatus").append(pill(live ? (r.status === "queued" ? "starting…" : "running") : r.status,
      r.status === "done" ? "ok" : (live ? "warn" : (r.status === "skipped" ? "" : "bad"))));
    $("#fxRunMeta").textContent = `${{ auto: "Automatic", manual: "Manual", rebuild: "Rebuild (no download)", calendar: "Holiday calendar import" }[r.trigger] || r.trigger} · ${(r.segments || []).join(", ")} · trading date ${fmtDate(r.trade_date)} · by ${r.requested_by} · started ${ist(r.started_at || r.created_at)}` +
      (r.finished_at ? ` · finished ${ist(r.finished_at)}` : "") + (r.message ? ` · ${r.message}` : "") +
      (r.status === "queued" ? " · waiting for GitHub to start the job (usually under a minute)" : "");
    const fxDetail = i => {
      const w = FX_WORD[i.status] || i.status;
      let d = (i.detail || "").trim();
      if (d.toLowerCase().startsWith(w.toLowerCase())) d = d.slice(w.length).replace(/^[\s\-—:·]+/, "");
      return d ? `${w} — ${d}` : w;
    };
    const box = $("#fxProgress"); box.textContent = "";
    const bySeg = {};
    for (const it of r.items || []) (bySeg[it.segment] = bySeg[it.segment] || []).push(it);
    const SEG_ORDER = ["NFO", "BFO", "CDS", "MCX", "NSECM", "BSECM"];
    const segs = Object.keys(bySeg).sort((a, b) => (SEG_ORDER.indexOf(a) + 99 * (SEG_ORDER.indexOf(a) < 0)) - (SEG_ORDER.indexOf(b) + 99 * (SEG_ORDER.indexOf(b) < 0)));
    for (const seg of segs) {
      const items = bySeg[seg], c = {};
      items.forEach(i => c[i.status] = (c[i.status] || 0) + 1);
      const name = (meta && meta.segments[seg]) ? meta.segments[seg].name : seg;
      box.append(el("div", { class: "fx-seg" },
        el("div", { class: "fx-seg-head" }, el("span", {}, `${name} (${seg})` +
            ((items[0].trade_date && items[0].trade_date !== r.trade_date) ? ` · for ${fmtDate(items[0].trade_date)}` : "")),
          el("span", { class: "counts" }, Object.entries(c).map(([k, v]) => `${FX_ICON[k] || ""} ${v} ${FX_WORD[k].toLowerCase()}`).join(" · "))),
        ...items.map(i => el("div", { class: "fx-item" },
          el("span", { class: "fx-icon " + (i.status === "built" ? "archived" : i.status), title: FX_WORD[i.status] }, i.status === "running" ? el("span", { class: "spin" }, "⟳") : FX_ICON[i.status]),
          el("span", { class: "fx-name" }, fxName(i.source_ref)),
          el("span", { class: "fx-detail" }, fxDetail(i))))));
    }
    if (!(r.items || []).length && live) box.append(el("p", { class: "hint" }, "Waiting for the first file…"));
    if (live) fxTimer = setTimeout(async () => { await loadFxRun(id); if (!document.getElementById("tab-fetcher").classList.contains("hidden")) refreshFxHeader(); }, 3000);
    else if (fx && fx.active && fx.active.id === r.id) refreshFxHeader();
  }

  async function refreshFxHeader() {
    try { fx = await api("/api/admin/fetcher"); } catch (_) { return; }
    $("#fxRun").disabled = !!fx.active;
    $("#fxRun").textContent = fx.active ? `Run #${fx.active.id} in progress…` : "Manually fetch exchange files";
    renderFxHistory();
  }

  async function saveFx(changes, okMsg) {
    try { await api("/api/admin/fetcher/settings", { method: "PUT", json: changes }); toast(okMsg, "ok"); }
    catch (e) { toast("Couldn't save", "bad", e.message); }
    loadFetcher();
  }
  $("#fxAuto").addEventListener("change", (ev) => {
    const on = ev.target.checked;
    saveFx({ fetch_auto_enabled: on, fetch_frequency_min: +$("#fxFreq").value },
      on ? `Auto fetch on · every ${$("#fxFreq").selectedOptions[0].text.replace("Every ", "")}` : "Auto fetch off");
  });
  $("#fxFreq").addEventListener("change", (ev) => saveFx({ fetch_frequency_min: +ev.target.value }, `Frequency: ${ev.target.selectedOptions[0].text.toLowerCase()}`));
  $("#fxRun").addEventListener("click", async () => {
    const seg = $("#fxSeg").value, label = $("#fxSeg").selectedOptions[0].text;
    $("#fxRun").disabled = true;
    try {
      const r = await api("/api/admin/fetcher/run", { method: "POST", json: { segment: seg } });
      toast(`Fetch started · ${label}`, "ok", `Run #${r.run.id}. Progress appears below.`);
      fxViewId = r.run.id; await loadFetcher();
    } catch (e) { toast("Couldn't start the fetch", "bad", e.message); $("#fxRun").disabled = false; }
  });

  // ------------------------------------------------------------ data readiness (Stage 2C)
  const RD_STATUS = {
    ready: ["Ready", "ok"], stale: ["Stale", "warn"], fallback: ["Using start-of-day (fallback)", "warn"],
    missing_inputs: ["Missing inputs", "bad"], check_failed: ["Check failed", "bad"], error: ["Build error", "bad"],
    not_built: ["Not built yet", ""], no_parser: ["No parser yet", ""] };
  let rd = null, rdTimer = null;

  function snapLine(s) {
    if (!s) return "—";
    const day = s.trade_date ? fmtDate(s.trade_date) : "?";
    const cyc = s.span_cycle ? ` · ${s.segment === "NSECM" ? "VaR batch" : "SPAN"} ${s.span_cycle}` + (s.span_day && s.span_day !== s.trade_date ? ` of ${fmtDate(s.span_day)}` : "") : "";
    const made = s.span_created ? ` · file created ${s.span_created}` : "";
    return `Data ${day}${cyc}${made} · built ${ist(s.built_at)}`;
  }

  async function loadReady() {
    clearTimeout(rdTimer);
    try { rd = await api("/api/admin/readiness"); } catch (e) { toast("Couldn't load readiness", "bad", e.message); return; }
    const box = $("#rdList"); box.textContent = "";
    $("#rdReadOnly").classList.toggle("hidden", rd.can_edit);
    for (const r of rd.segments) {
      const [word, kind] = RD_STATUS[r.status] || [r.status, ""];
      const card = el("div", { class: "rd-seg" });
      card.append(el("div", { class: "rd-head" },
        el("div", {}, el("div", { class: "rd-name" }, `${rd.segment_names[r.segment] || r.segment} (${r.segment})`),
          el("div", { class: "hint" }, r.has_parser ? (r.in_use ? snapLine(r.in_use) : (r.status === "not_built" ? r.message : "No usable data yet — see below")) : r.message)),
        pill(word, kind)));
      if (!r.has_parser) { box.append(card); continue; }
      if (r.in_use && r.message) card.append(el("p", { class: "rd-note warn" }, r.message));

      // basis toggle (SuperAdmin) - only for segments with both bases
      if (r.bases.length > 1) {
        const on = r.basis === "latest";
        const inp = el("input", { type: "checkbox" }); inp.checked = on; inp.disabled = !rd.can_edit;
        inp.addEventListener("change", () => setBasis(r.segment, inp.checked ? "latest" : "bod", inp));
        card.append(el("div", { class: "rd-basis" },
          el("label", { class: "switch" }, inp, el("span", { class: "slider" }),
            el("span", { class: "switch-label" }, "Use latest SPAN cycle")),
          el("span", { class: "hint" }, on ? "Margins use the newest cycle — not validated against brokers yet."
                                             : "Margins use the start-of-day (i1) file, as brokers do.")));
      }
      // both snapshots
      const tbl = el("div", { class: "rd-snaps" });
      for (const b of r.bases) {
        const sn = r.snapshots[b], lv = sn.live, last = sn.last;
        const failedNewer = last && last.status === "failed" && (!lv || last.id > lv.id);
        tbl.append(el("div", { class: "rd-snap" + (r.basis === b ? " chosen" : "") },
          el("div", { class: "rd-snap-name" }, BASIS_NAME[b][0].toUpperCase() + BASIS_NAME[b].slice(1) + (r.basis === b ? " · in use" : "")),
          el("div", { class: "rd-snap-detail" }, lv ? snapLine(lv) : "Not built yet",
            failedNewer ? el("div", { class: "bad-text" }, `Last build failed: ${last.message || last.error_kind}`) : null),
          lv ? el("button", { type: "button", class: "link", onclick: () => showSnapshot(lv.id) }, "Files used") : el("span")));
      }
      card.append(tbl);
      const btn = el("button", { type: "button", class: "secondary small" }, "Rebuild");
      btn.disabled = !!rd.active;
      btn.addEventListener("click", () => rebuild(r.segment, btn));
      card.append(el("div", { class: "rd-actions" }, btn,
        el("span", { class: "hint" }, r.newest_input_date ? `Newest file archived for ${fmtDate(r.newest_input_date)}` : "No files archived yet")));
      box.append(card);
    }
    renderValidation();
    $("#rdActive").classList.toggle("hidden", !rd.active);
    if (rd.active) {
      $("#rdActive").textContent = `Run #${rd.active.id} (${rd.active.trigger}) is in progress — this page refreshes when it finishes.`;
      rdTimer = setTimeout(loadReady, 8000);
    }
  }

  // validation against brokers (Stage 3)
  let vrep = null;
  const money = x => x == null ? "—" : Number(x).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  const V_STATUS = { passed: ["Passed", "ok"], lagged: ["Passed (older file)", "ok"], failed: ["Failed", "bad"],
                     error: ["Could not run", "bad"], pending: ["No broker figures", ""] };

  function renderValidation() {
    const box = $("#rdValidation"); box.textContent = "";
    const v = (rd.validation || {}).NFO;
    if (!v) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    const L = v.latest, s = L ? L.summary : {};
    box.append(el("div", { class: "eyebrow" }, "Accuracy"),
      el("div", { class: "rd-head" }, el("h2", {}, "NSE F&O: validation against brokers"),
        pill(v.validated ? "Validated" : "Not validated", v.validated ? "ok" : "warn")),
      el("p", { class: "lead" }, "Your engine tests and the 202 Zerodha / Angel One baskets captured 26–28 Oct, re-run on GitHub with those days' exchange files. " +
        "A basket passes when SPAN, exposure and total are each within 0.05% or ₹5 of the broker's figures."));
    if (!v.validated) box.append(el("p", { class: "rd-note warn" }, v.reason));
    if (L) box.append(el("div", { class: "v-chips" },
      chip(`${(s.baskets_passed || 0) + (s.baskets_lagged || 0)} baskets passed`, "ok"),
      s.baskets_failed ? chip(`${s.baskets_failed} failed`, "bad") : null,
      s.baskets_error ? chip(`${s.baskets_error} could not run`, "bad") : null,
      s.baskets_pending ? chip(`${s.baskets_pending} without figures`, "") : null,
      chip(`tests ${s.tests_passed || 0} passed` + (s.tests_failed ? `, ${s.tests_failed} failed` : ""), s.tests_failed ? "bad" : "ok"),
      el("span", { class: "hint" }, `run #${L.run_id} · ${ist(L.created_at)} · code ${(L.code_version || "").slice(0, 7)}`)));
    const run = el("button", { type: "button", class: "small" }, L ? "Run validation again" : "Run validation");
    run.disabled = !!rd.active;
    run.addEventListener("click", async () => {
      run.disabled = true;
      try { const r = await api("/api/admin/validation/run", { method: "POST", json: { segment: "NFO" } });
        toast("Validation started", "ok", `Run #${r.run.id} on GitHub. The first run downloads the 22–28 Oct files (about 10–20 minutes); later runs reuse them.`); }
      catch (e) { toast("Couldn't start", "bad", e.message); }
      loadReady();
    });
    const actions = el("div", { class: "rd-actions" }, run);
    if (L) actions.append(el("button", { type: "button", class: "secondary small", onclick: () => showReport() }, vrep ? "Refresh report" : "Show report"));
    box.append(actions, el("div", { id: "vReport" }));
    if (vrep) drawReport();
  }
  const chip = (t, k) => el("span", { class: "v-chip " + (k || "") }, t);

  async function showReport() {
    try { vrep = (await api("/api/admin/validation?segment=NFO")).report; } catch (e) { toast("Couldn't load the report", "bad", e.message); return; }
    drawReport();
  }

  function basketRow(b) {
    const [w, k] = V_STATUS[b.status] || [b.status, ""];
    const legs = b.legs.map(l => `${l.side === "sell" ? "−" : "+"}${l.qty} ${l.symbol} ${l.type}${l.strike ? " " + l.strike : ""} ${l.expiry ? l.expiry.slice(5) : ""}`).join(", ");
    const row = el("details", { class: "v-basket " + b.status },
      el("summary", {}, el("span", { class: "v-id" }, b.id), pill(w, k), el("span", { class: "hint" }, `${b.broker} · ${b.captured_at || ""}`)));
    row.append(el("div", { class: "hint" }, legs), el("div", { class: "hint" }, b.detail || ""));
    if (b.want) {
      const keys = Object.keys(b.want);
      row.append(el("div", { class: "v-table" },
        el("div", { class: "v-tr head" }, el("span", {}, ""), el("span", {}, "Broker"), el("span", {}, "Ours"), el("span", {}, "Diff")),
        ...keys.map(key => el("div", { class: "v-tr" + ((b.diff || {})[key] != null && Math.abs(b.diff[key]) > Math.max(5, Math.abs(b.want[key]) * 0.0005) ? " off" : "") },
          el("span", {}, key), el("span", {}, money(b.want[key])), el("span", {}, money((b.ours || {})[key])), el("span", {}, money((b.diff || {})[key]))))));
    }
    if (b.tried && b.tried.length) row.append(el("div", { class: "hint" }, "SPAN files tried: " + b.tried.map(t => t.cycle + (t.error ? " (error)" : "")).join(", ")));
    return row;
  }

  function drawReport() {
    const box = $("#vReport"); if (!box || !vrep) return;
    box.textContent = "";
    const R = vrep.report;
    if (R.crash) box.append(el("p", { class: "rd-note bad" }, "The validation crashed: " + R.crash.slice(-300)));
    const days = Object.entries(R.data || {}).map(([d, c]) => `${d.slice(8)}-${d.slice(5, 7)}: ${c.join(" ") || "none"}`).join(" · ");
    box.append(el("p", { class: "hint" }, "SPAN files used: " + (days || "none")));
    const bad = R.baskets.filter(b => ["failed", "error"].includes(b.status));
    const good = R.baskets.filter(b => ["passed", "lagged"].includes(b.status));
    const pend = R.baskets.filter(b => b.status === "pending");
    const ft = R.tests.filter(t => t.status === "failed");
    if (ft.length) box.append(el("h3", {}, `Engine tests failed (${ft.length})`),
      ...ft.map(t => el("details", { class: "v-basket failed" }, el("summary", {}, el("span", { class: "v-id" }, t.name), pill("Failed", "bad")),
        el("pre", { class: "v-pre" }, t.detail))));
    if (bad.length) box.append(el("h3", {}, `Baskets that didn't match (${bad.length})`), ...bad.map(basketRow));
    if (good.length) { const d = el("details", { class: "v-group" }, el("summary", {}, `Baskets that matched (${good.length})`)); good.forEach(b => d.append(basketRow(b))); box.append(d); }
    if (pend.length) box.append(el("p", { class: "hint" }, `Without broker figures: ${pend.map(b => b.id).join(", ")}`));
    const okTests = R.tests.filter(t => t.status !== "failed");
    box.append(el("p", { class: "hint" }, `Engine tests: ${okTests.filter(t => t.status === "passed").length} passed` +
      (okTests.some(t => t.status === "skipped") ? `, ${okTests.filter(t => t.status === "skipped").length} skipped (files missing)` : "")));
  }

  async function setBasis(seg, basis, inp) {
    const ok = await dialog({ title: basis === "latest" ? `Use the latest SPAN cycle for ${seg}?` : `Go back to start-of-day for ${seg}?`,
      message: basis === "latest"
        ? "Margins will use the newest SPAN cycle of the day instead of the start-of-day file. Results may differ from Zerodha / Angel One between their updates, and this basis isn't validated yet. The change is recorded in the audit log."
        : "Margins will use the trade date's start-of-day (i1) file — the basis validated against brokers. The change is recorded in the audit log.",
      confirmText: basis === "latest" ? "Use latest cycle" : "Use start-of-day", danger: basis === "latest" });
    if (!ok) { inp.checked = !inp.checked; return; }
    try { await api("/api/admin/readiness/basis", { method: "PUT", json: { segment: seg, basis } });
      toast(`${seg}: ${BASIS_NAME[basis]}`, "ok"); }
    catch (e) { toast("Couldn't change", "bad", e.message); }
    loadReady();
  }

  async function rebuild(seg, btn) {
    btn.disabled = true;
    try { const r = await api("/api/admin/readiness/rebuild", { method: "POST", json: { segment: seg } });
      toast(`Rebuild started · ${seg}`, "ok", `Run #${r.run.id}: parses the archived files again (nothing is downloaded).`);
    } catch (e) { toast("Couldn't start the rebuild", "bad", e.message); }
    loadReady();
  }

  async function showSnapshot(id) {
    let s;
    try { s = (await api(`/api/admin/snapshots/${id}`)).snapshot; } catch (e) { toast("Couldn't load", "bad", e.message); return; }
    const used = (s.inputs || []).filter(i => i.used);
    const lines = used.map(i => `#${i.upload_id} ${i.filename} (${fmtDate(i.trade_date)})`).join("\n") || "(not recorded)";
    const rules = Object.entries(s.rules || {}).map(([f, h]) => `${f} @ ${h.slice(0, 12)}`).join("\n");
    await dialog({ title: `Snapshot #${s.id} · ${s.segment} ${BASIS_NAME[s.basis]}`,
      message: `${snapLine(s)}\n\nFiles used:\n${lines}\n\nRules:\n${rules}\nCode: ${(s.code_version || "").slice(0, 12)}\nSHA-256: ${(s.sha256 || "").slice(0, 16)}… · ${((s.size_bytes || 0) / 1e6).toFixed(1)} MB`,
      confirmText: "Close", cancelText: null });
  }

  // ------------------------------------------------------------ holiday calendar
  let hc = null, hcYear = null, hcTimer = null, hcEx = "NSE", hcBusy = false;
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const HC_SHORT = { NSE: "NSE equity & F&O", NSE_CD: "NSE currency", BSE: "BSE", MCX: "MCX" };
  const kindPill = k => pill(hc.kinds[k] || k, { closed: "bad", morning_closed: "warn", evening_closed: "warn", special_session: "ok" }[k] || "");
  const dayLabel = d => { const x = new Date(d + "T00:00:00"); return `${fmtDate(d.replace(/-/g, ""))} · ${DOW[x.getDay()]}`; };
  const host = u => { try { return new URL(u).hostname; } catch (e) { return u || ""; } };

  async function loadHolidays() {
    clearTimeout(hcTimer);
    hcYear = hcYear || new Date().getFullYear();
    try { hc = await api(`/api/admin/calendar?year=${hcYear}`); } catch (e) { toast("Couldn't load the calendar", "bad", e.message); return; }
    $("#hcYear").textContent = hcYear;
    $("#hcImport").disabled = !!hc.active;
    $("#hcReadOnly").classList.toggle("hidden", hc.can_edit);
    $("#hcForm").classList.toggle("hidden", !hc.can_edit);
    if (!$("#hcfEx").options.length) {
      for (const [code] of Object.entries(hc.exchanges)) $("#hcfEx").append(el("option", { value: code }, HC_SHORT[code] || code));
      for (const [k, v] of Object.entries(hc.kinds)) $("#hcfKind").append(el("option", { value: k }, v));
    }
    $("#hcfDay").min = `${hcYear}-01-01`; $("#hcfDay").max = `${hcYear}-12-31`;
    if (hc.active) hcTimer = setTimeout(loadHolidays, 8000);
    renderHc();
  }

  function renderHc() {
    const pending = new Set(hc.proposals.map(p => p.exchange));
    const tabs = $("#hcTabs"); tabs.textContent = "";
    for (const [code, ex] of Object.entries(hc.exchanges)) {
      const b = el("button", { type: "button", role: "tab", class: "hc-tab" + (code === hcEx ? " active" : ""), "aria-selected": String(code === hcEx) },
        HC_SHORT[code] || code, el("span", { class: "hc-count" }, String(ex.days.length)),
        pending.has(code) ? el("span", { class: "hc-dot", title: "Import waiting for review" }) : null);
      b.addEventListener("click", () => { hcEx = code; $("#hcfEx").value = code; renderHc(); });
      tabs.append(b);
    }
    const ex = hc.exchanges[hcEx];
    if (!$("#hcfEx").dataset.touched) $("#hcfEx").value = hcEx;

    const warn = $("#hcWarnings"); warn.textContent = "";
    if (hc.active) warn.append(el("p", { class: "rd-note warn" }, `Run #${hc.active.id} (${hc.active.trigger}) is in progress — this page refreshes when it finishes.`));
    for (const w of hc.warnings.filter(w => w.exchange === hcEx)) warn.append(el("p", { class: "rd-note " + (w.level === "bad" ? "bad" : "warn") }, w.text));

    // import waiting for review (this exchange)
    const pr = $("#hcProposal"); pr.textContent = "";
    const p = hc.proposals.find(x => x.exchange === hcEx);
    if (p) {
      const n = p.diff.add.length + p.diff.change.length, failed = p.error && !p.found.length;
      const box = el("div", { class: "hc-prop" });
      box.append(el("div", { class: "rd-head" },
        el("div", {}, el("div", { class: "rd-name" }, failed ? "Import failed" : `Import waiting for review: ${n} change${n === 1 ? "" : "s"}`),
          el("div", { class: "hint" }, `${host(p.source)} · fetched ${ist(p.fetched_at)}`)),
        hc.can_edit ? el("div", { class: "hc-prop-actions" },
          n ? el("button", { type: "button", class: "small", onclick: e => decide("approve", p, e.currentTarget) }, "Approve") : null,
          el("button", { type: "button", class: "secondary small", onclick: e => decide("discard", p, e.currentTarget) }, "Discard")) : null));
      if (failed) box.append(el("p", { class: "rd-note bad" }, p.error));
      const rows = [...p.diff.add.map(e => ["New", e]), ...p.diff.change.map(e => ["Changed", e])];
      if (rows.length) {
        const det = el("details", { class: "hc-details" }, el("summary", {}, "Show what changes"));
        det.append(el("div", { class: "hc-table" }, ...rows.map(([what, e]) => el("div", { class: "hc-row" },
          el("span", { class: "hc-date" }, dayLabel(e.day)), kindPill(e.kind),
          el("span", { class: "hc-desc" }, (e.description || "") + (e.was ? ` (was: ${hc.kinds[e.was] || e.was})` : "")),
          el("span", { class: "hint" }, what)))));
        box.append(det);
      }
      pr.append(box);
    }

    // the exchange's days
    const list = $("#hcList"); list.textContent = "";
    list.append(el("div", { class: "hc-list-head" },
      el("span", {}, `${ex.name} · used for ${ex.segments.join(", ")}`),
      el("a", { href: ex.source.reference, target: "_blank", rel: "noopener" }, "Exchange's page ↗")));
    const tbl = el("div", { class: "hc-table" });
    if (!ex.days.length) tbl.append(el("div", { class: "hc-row" }, el("span", { class: "hint hc-empty" }, `No ${hcYear} days entered — only weekends count as closed.`)));
    for (const d of ex.days) {
      const weekend = [0, 6].includes(new Date(d.day + "T00:00:00").getDay()) && d.kind === "closed";
      const row = el("div", { class: "hc-row" + (hc.can_edit ? " clickable" : "") + (weekend ? " muted" : "") },
        el("span", { class: "hc-date" }, dayLabel(d.day)), kindPill(d.kind),
        el("span", { class: "hc-desc" }, (d.description || "") + (weekend ? " · weekend, closed anyway" : "")),
        el("span", { class: "hint hc-src" }, d.source === "manual" ? `added by ${d.updated_by}` : "imported"));
      if (hc.can_edit) row.addEventListener("click", () => {
        $("#hcfEx").value = hcEx; $("#hcfDay").value = d.day; $("#hcfKind").value = d.kind; $("#hcfDesc").value = d.description || "";
        $("#hcForm").scrollIntoView({ behavior: "smooth", block: "center" });
      });
      tbl.append(row);
    }
    list.append(tbl);
  }

  async function decide(action, p, btn) {
    if (hcBusy) return;
    const name = HC_SHORT[p.exchange];
    if (action === "approve") {
      const ok = await dialog({ title: `Apply the ${name} ${p.year} import?`,
        message: "The fetcher and the parsers use these days from the next run. Every change is recorded in the audit log.", confirmText: "Approve" });
      if (!ok) return;
    }
    hcBusy = true; if (btn) btn.disabled = true;
    try { await api(`/api/admin/calendar/${action}`, { method: "POST", json: { exchange: p.exchange, year: p.year } });
      toast(action === "approve" ? `${name} ${p.year} updated` : "Import discarded", "ok"); }
    catch (e) { toast("Couldn't " + action, "bad", e.message); }
    hcBusy = false;
    loadHolidays();
  }

  $("#hcfEx").addEventListener("change", () => { $("#hcfEx").dataset.touched = "1"; });
  $("#hcForm").addEventListener("submit", async e => {
    e.preventDefault();
    if (hcBusy) return;
    const exch = $("#hcfEx").value, day = $("#hcfDay").value;
    if (!day) { toast("Pick a date", "bad"); return; }
    hcBusy = true; $("#hcfSave").disabled = true;
    try { await api("/api/admin/calendar/day", { method: "PUT", json: { exchange: exch, day, kind: $("#hcfKind").value, description: $("#hcfDesc").value } });
      toast(`${HC_SHORT[exch]}: ${fmtDate(day.replace(/-/g, ""))} saved`, "ok");
      hcEx = exch; delete $("#hcfEx").dataset.touched; $("#hcfDay").value = ""; $("#hcfDesc").value = ""; }
    catch (err) { toast("Couldn't save", "bad", err.message); }
    hcBusy = false; $("#hcfSave").disabled = false;
    loadHolidays();
  });
  $("#hcfRemove").addEventListener("click", async () => {
    if (hcBusy) return;
    const exch = $("#hcfEx").value, day = $("#hcfDay").value;
    if (!day) { toast("Pick the date to remove", "bad"); return; }
    const ok = await dialog({ title: `Remove ${fmtDate(day.replace(/-/g, ""))} from ${HC_SHORT[exch]}?`,
      message: "The day counts as a normal trading day again (weekends stay closed).", confirmText: "Remove", danger: true });
    if (!ok) return;
    hcBusy = true;
    try { await api("/api/admin/calendar/day", { method: "DELETE", json: { exchange: exch, day } });
      toast("Removed", "ok"); hcEx = exch; $("#hcfDay").value = ""; $("#hcfDesc").value = ""; }
    catch (err) { toast("Couldn't remove", "bad", err.message); }
    hcBusy = false;
    loadHolidays();
  });
  $("#hcPrev").addEventListener("click", () => { hcYear -= 1; loadHolidays(); });
  $("#hcNext").addEventListener("click", () => { hcYear += 1; loadHolidays(); });
  $("#hcImport").addEventListener("click", async () => {
    $("#hcImport").disabled = true;
    try { const r = await api("/api/admin/calendar/import", { method: "POST", json: {} });
      toast("Import started", "ok", `Run #${r.run.id} reads the exchanges' holiday pages on GitHub. Review the result here — nothing changes until you approve it.`); }
    catch (e) { toast("Couldn't start the import", "bad", e.message); }
    loadHolidays();
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
