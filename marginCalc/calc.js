"use strict";
// F&O margin calculator (Stage 4). Talks to the engine's public endpoints:
//   GET  /api/margin/meta?segment=NFO              data in use, validation, instruments
//   GET  /api/margin/contracts?segment=NFO&symbol= expiries, lot sizes, strikes
//   POST /api/margin                               the basket's margin
(function () {
  const API = (window.MARGIN_API_BASE || "").replace(/\/$/, "");
  const SEG = "NFO";
  const $ = s => document.querySelector(s);
  const STORE = "mc_basket_v1";

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k instanceof Node ? k : String(k));
    return e;
  }
  const inr = (x, d = 0) => x == null ? "—" : "₹" + Number(x).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d });
  const day = s => { s = String(s || "").replace(/-/g, ""); return s.length === 8 ? `${s.slice(6)}-${s.slice(4, 6)}-${s.slice(0, 4)}` : s; };
  const expLabel = e => { const d = new Date(e + "T00:00:00"); return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }); };
  const strikeTxt = k => Number(k).toLocaleString("en-IN", { maximumFractionDigits: 2 });

  // ---------------------------------------------------------------- requests + loading bar
  let pending = 0;
  async function api(path, opts = {}) {
    pending++; $("#topBar").classList.add("on");
    try {
      const r = await fetch(API + path, { method: opts.method || "GET", headers: opts.body ? { "Content-Type": "application/json" } : {},
                                          body: opts.body ? JSON.stringify(opts.body) : undefined });
      let j = null; try { j = await r.json(); } catch (_) { /* not JSON */ }
      if (!r.ok) { const e = new Error((j && j.error) || `HTTP ${r.status}`); e.status = r.status; throw e; }
      return j;
    } finally { if (--pending <= 0) { pending = 0; $("#topBar").classList.remove("on"); } }
  }

  // ---------------------------------------------------------------- state
  let meta = null, chain = null;                   // chain: contracts of the instrument being added
  let basket = [];
  try { basket = JSON.parse(localStorage.getItem(STORE) || "[]").filter(l => l && l.symbol && l.expiry); } catch (_) { basket = []; }
  const save = () => { try { localStorage.setItem(STORE, JSON.stringify(basket)); } catch (_) { /* private mode */ } };
  let type = "CE", side = "S";

  // ---------------------------------------------------------------- start
  async function boot() {
    const slow = setTimeout(() => { $("#bootText").textContent = "Waking the server… on the free plan this can take up to a minute."; }, 4000);
    try { meta = await api(`/api/margin/meta?segment=${SEG}`); }
    catch (e) {
      clearTimeout(slow);
      $("#boot").classList.add("hidden");
      const u = $("#unavailable"); u.classList.remove("hidden");
      u.textContent = e.status === 503 ? `NSE F&O margins aren't available right now: ${e.message}.` :
        "The margin service is starting up or unavailable. Try again in a minute.";
      u.append(el("button", { type: "button", class: "secondary", onclick: () => location.reload() }, "Try again"));
      return;
    }
    clearTimeout(slow);
    $("#boot").classList.add("hidden"); $("#app").classList.remove("hidden");
    drawDataLine();
    const dl = $("#symbolList");
    for (const s of meta.symbols) dl.append(el("option", { value: s.symbol }, s.kind === "INDEX" ? "Index" : "Stock"));
    if (!basket.length) $("#fSymbol").value = "NIFTY";
    await pickSymbol();
    drawBasket(); calc();
  }

  function drawDataLine() {
    const d = meta.data, box = $("#dataLine"); box.textContent = "";
    box.append(el("span", {}, `Data for ${day(d.trade_date)} · SPAN ${d.span_cycle || ""}` +
      (d.span_day && d.span_day !== d.trade_date ? ` of ${day(d.span_day)}` : "") +
      (d.span_created ? ` · file created ${d.span_created}` : "") + ` · ${d.basis === "bod" ? "start-of-day file" : "latest file"}`));
    box.append(el("span", { class: "badge " + (meta.validated ? "ok" : "warn"), title: meta.validation_note || "" },
      meta.validated ? "Validated against brokers" : "Not validated"));
    if (d.stale) box.append(el("span", { class: "badge warn" }, "Today's start-of-day file isn't out yet"));
  }

  // ---------------------------------------------------------------- the add form
  async function pickSymbol() {
    const sym = $("#fSymbol").value.trim().toUpperCase();
    const known = meta.symbols.find(s => s.symbol === sym);
    $("#fExpiry").disabled = $("#fStrike").disabled = $("#fAdd").disabled = true;
    $("#fExpiry").textContent = ""; $("#fStrike").textContent = ""; $("#fLotInfo").innerHTML = "&nbsp;";
    chain = null;
    if (!known) { if (sym) $("#fLotInfo").textContent = "Pick an instrument from the list"; return; }
    $("#fLotInfo").textContent = "Loading contracts…";
    try { chain = await api(`/api/margin/contracts?segment=${SEG}&symbol=${encodeURIComponent(sym)}`); }
    catch (e) { $("#fLotInfo").textContent = e.message; return; }
    if ($("#fSymbol").value.trim().toUpperCase() !== sym) return;          // typed on meanwhile
    for (const x of chain.expiries) $("#fExpiry").append(el("option", { value: x.expiry }, expLabel(x.expiry)));
    $("#fExpiry").disabled = false;
    fillStrikes();
  }

  function fillStrikes() {
    const x = chain && chain.expiries.find(e => e.expiry === $("#fExpiry").value);
    const sel = $("#fStrike"); sel.textContent = "";
    $("#strikeWrap").classList.toggle("off", type === "FUT");
    if (!x) return;
    $("#fLotInfo").textContent = `Lot size ${x.lot_size}` + (meta.symbols.find(s => s.symbol === chain.symbol)?.banned ? " · in F&O ban" : "");
    if (type === "FUT") {
      sel.disabled = true;
      $("#fAdd").disabled = !x.future;
      if (!x.future) $("#fLotInfo").textContent = "No future for this expiry";
      return;
    }
    const ks = x[type];
    if (!ks.length) { sel.disabled = true; $("#fAdd").disabled = true; return; }
    let best = ks[Math.floor(ks.length / 2)];
    if (chain.ref) best = ks.reduce((a, b) => Math.abs(b - chain.ref) < Math.abs(a - chain.ref) ? b : a, ks[0]);
    for (const k of ks) sel.append(el("option", { value: k, selected: k === best }, strikeTxt(k) + (k === best ? "  (near the money)" : "")));
    sel.disabled = false; $("#fAdd").disabled = false;
  }

  function segmented(box, set) {
    box.addEventListener("click", ev => {
      const b = ev.target.closest("button"); if (!b) return;
      box.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
      set(b.dataset.v);
    });
  }
  segmented($("#fType"), v => { type = v; fillStrikes(); });
  segmented($("#fSide"), v => { side = v; });
  let symTimer = null;
  $("#fSymbol").addEventListener("input", () => { clearTimeout(symTimer); symTimer = setTimeout(pickSymbol, 350); });
  $("#fSymbol").addEventListener("change", () => { clearTimeout(symTimer); pickSymbol(); });
  $("#fExpiry").addEventListener("change", fillStrikes);

  $("#legForm").addEventListener("submit", ev => {
    ev.preventDefault();
    const x = chain && chain.expiries.find(e => e.expiry === $("#fExpiry").value);
    const lots = parseInt($("#fLots").value, 10);
    if (!x || !(lots > 0)) return;
    basket.push({ symbol: chain.symbol, expiry: x.expiry, type, strike: type === "FUT" ? null : +$("#fStrike").value,
                  side, lots, lot: x.lot_size });
    save(); drawBasket(); calc();
  });
  $("#clearAll").addEventListener("click", () => { basket = []; save(); drawBasket(); calc(); });

  // ---------------------------------------------------------------- basket
  const legName = l => `${l.symbol} ${expLabel(l.expiry)} ${l.type === "FUT" ? "FUT" : strikeTxt(l.strike) + " " + l.type}`;
  function drawBasket() {
    const box = $("#basket"); box.textContent = "";
    $("#legCount").textContent = basket.length ? `(${basket.length})` : "";
    $("#clearAll").classList.toggle("hidden", !basket.length);
    if (!basket.length) { box.append(el("p", { class: "empty" }, "No positions yet.")); return; }
    basket.forEach((l, i) => {
      const lots = el("input", { type: "number", min: "1", step: "1", value: String(l.lots), inputmode: "numeric", "aria-label": "Lots" });
      lots.addEventListener("change", () => { const n = parseInt(lots.value, 10); if (n > 0) { l.lots = n; save(); calc(); } else lots.value = l.lots; });
      const sideBtn = el("button", { type: "button", class: "side-pill " + (l.side === "B" ? "buy" : "sell"), title: "Switch buy / sell" }, l.side === "B" ? "BUY" : "SELL");
      sideBtn.addEventListener("click", () => { l.side = l.side === "B" ? "S" : "B"; save(); drawBasket(); calc(); });
      box.append(el("div", { class: "leg" }, sideBtn,
        el("div", { class: "leg-name" }, legName(l), el("small", {}, `${l.lots * l.lot} units · lot ${l.lot}`)),
        el("label", { class: "leg-lots" }, lots, el("span", {}, "lots")),
        el("button", { type: "button", class: "icon-btn", "aria-label": "Remove", title: "Remove",
                       onclick: () => { basket.splice(i, 1); save(); drawBasket(); calc(); } }, "×")));
    });
  }

  // ---------------------------------------------------------------- margin
  let calcTimer = null, calcSeq = 0;
  function calc() { clearTimeout(calcTimer); calcTimer = setTimeout(runCalc, 250); }

  async function runCalc() {
    const box = $("#result");
    if (!basket.length) { box.textContent = ""; box.append(el("p", { class: "empty" }, "Add a position to see its margin.")); return; }
    const seq = ++calcSeq;
    box.classList.add("loading");
    if (!box.querySelector(".total")) { box.textContent = ""; box.append(el("div", { class: "loading-ph" }, el("span", { class: "spinner" }), "Working out the margin…")); }
    let m;
    try {
      m = await api("/api/margin", { method: "POST", body: { segment: SEG, positions: basket.map(l => ({
        symbol: l.symbol, expiry: l.expiry, strike: l.strike, option_type: l.type === "FUT" ? null : l.type,
        qty: (l.side === "B" ? 1 : -1) * l.lots * l.lot })) } });
    } catch (e) {
      if (seq !== calcSeq) return;
      box.classList.remove("loading"); box.textContent = "";
      box.append(el("p", { class: "error" }, e.status === 429 ? "Too many requests — wait a minute and try again." : e.message));
      return;
    }
    if (seq !== calcSeq) return;
    box.classList.remove("loading"); box.textContent = "";
    const np = m.net_premium || 0;
    box.append(
      el("div", { class: "total" }, el("span", { class: "lbl" }, "Total margin"), el("strong", {}, inr(m.total))),
      el("div", { class: "split" },
        el("div", {}, el("span", { class: "lbl" }, "SPAN"), el("b", {}, inr(m.span))),
        el("div", {}, el("span", { class: "lbl" }, "Exposure"), el("b", {}, inr(m.exposure))),
        m.other ? el("div", {}, el("span", { class: "lbl" }, "Other"), el("b", {}, inr(m.other))) : null,
        basket.length > 1 ? el("div", {}, el("span", { class: "lbl" }, "Hedge benefit"), el("b", { class: "good" }, inr(m.benefit))) : null,
        np ? el("div", {}, el("span", { class: "lbl" }, np > 0 ? "Premium paid" : "Premium received"), el("b", {}, inr(Math.abs(np)))) : null));
    if (m.legs && m.legs.length) {
      const t = el("div", { class: "legs-table" },
        el("div", { class: "tr head" }, el("span", {}, "Position"), el("span", {}, "Price"), el("span", {}, "Exposure"), el("span", {}, basket.length > 1 ? "Margin alone" : "Margin")));
      for (const g of m.legs) t.append(el("div", { class: "tr" },
        el("span", {}, `${g.qty > 0 ? "+" : ""}${g.lots != null ? g.lots + " lot" + (Math.abs(g.lots) === 1 ? "" : "s") : g.qty} ${g.contract}`),
        el("span", {}, inr(g.price, 2)), el("span", {}, inr(g.exposure)), el("span", {}, inr(g.total_alone))));
      box.append(t);
    }
    for (const w of m.warnings || []) box.append(el("p", { class: "note warn" }, w));
    const d = m.data || {};
    box.append(el("p", { class: "note" + (m.validated ? "" : " warn") },
      (m.validated ? "Validated: matched Zerodha / Angel One on 201 test baskets. " : `Not validated against brokers: ${m.validation_note} `) +
      `SPAN ${d.span_cycle || ""} of ${day(d.span_day || d.trade_date)}` + (d.span_created ? `, created ${d.span_created}` : "") + "."));
  }

  boot();
})();
