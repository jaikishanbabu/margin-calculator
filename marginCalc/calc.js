"use strict";
// F&O margin calculator (Stage 4). Talks to the engine's public endpoints:
//   GET  /api/margin/meta?segment=NFO              data in use, validation, instruments
//   GET  /api/margin/contracts?segment=NFO&symbol= expiries, lot sizes, strikes
//   POST /api/margin                               the basket's margin
(function () {
  const $ = s => document.querySelector(s);
  // Markets and their exchanges. live: margins are served (validated against brokers); the rest
  // are listed but greyed out until each passes its own validation.
  const MARKETS = {
    fo: { name: "Futures & Options", exchanges: [
      { ex: "NSE", seg: "NFO", label: "NSE F&O", live: true },
      { ex: "BSE", seg: "BFO", label: "BSE F&O" },
      { ex: "CDS", seg: "CDS", label: "NSE Currency" },
      { ex: "MCX", seg: "MCX", label: "MCX Commodity" }] },
    cash: { name: "Cash", exchanges: [
      { ex: "NSE", seg: "NSECM", label: "NSE Cash" },
      { ex: "BSE", seg: "BSECM", label: "BSE Cash" }] },
  };
  const POPULAR = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX", "SENSEX50",
                   "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "KOTAKBANK", "ITC", "LT"];
  const params = new URLSearchParams(location.search);
  let tab = MARKETS[params.get("tab")] ? params.get("tab") : "fo";
  let exch = MARKETS[tab].exchanges.find(x => x.ex === (params.get("ex") || "").toUpperCase()) || MARKETS[tab].exchanges[0];
  let SEG = exch.seg;
  const storeKey = () => `mc_basket_v2_${SEG}`;

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
      const r = await window.MarginBackend.fetch(path, { method: opts.method || "GET", headers: opts.body ? { "Content-Type": "application/json" } : {},
                                          body: opts.body ? JSON.stringify(opts.body) : undefined });
      let j = null; try { j = await r.json(); } catch (_) { /* not JSON */ }
      if (!r.ok) { const e = new Error((j && j.error) || `HTTP ${r.status}`); e.status = r.status; throw e; }
      return j;
    } finally { if (--pending <= 0) { pending = 0; $("#topBar").classList.remove("on"); } }
  }

  // ---------------------------------------------------------------- search dropdown
  // Shows matches under the box once 3 letters are typed: names starting with them first, then names
  // containing them (each group in the popular-first order of the list). Tap / Enter picks one.
  let foList = [], cashList = [];
  const MIN_CHARS = 3, MAX_SHOWN = 40;
  function suggest(input, box, items, onPick) {
    let rows = [], at = -1, inList = false;
    const close = () => { box.classList.add("hidden"); box.textContent = ""; input.setAttribute("aria-expanded", "false"); rows = []; at = -1; };
    const choose = r => { inList = false; input.value = r.symbol; close(); onPick(r); };
    const mark = () => [...box.children].forEach((c, i) => c.classList.toggle("on", i === at));
    function open() {
      const q = input.value.trim().toUpperCase();
      if (q.length < MIN_CHARS) return close();
      const all = items();
      const starts = all.filter(r => r.symbol.startsWith(q)), has = all.filter(r => !r.symbol.startsWith(q) && r.symbol.includes(q));
      rows = starts.concat(has).slice(0, MAX_SHOWN); at = -1;
      box.textContent = "";
      if (!rows.length) box.append(el("div", { class: "s-none" }, `No match for “${q}”`));
      for (const r of rows) {
        const i = q.length ? r.symbol.indexOf(q) : -1;
        box.append(el("div", { class: "s-row", role: "option",
          onclick: () => choose(r) },                    // click = a tap; a drag scrolls the list instead
          el("span", { class: "s-sym" }, r.symbol.slice(0, i), el("b", {}, r.symbol.slice(i, i + q.length)), r.symbol.slice(i + q.length)),
          el("span", { class: "s-tag" }, r.tag || "")));
      }
      box.classList.remove("hidden"); input.setAttribute("aria-expanded", "true");
    }
    input.addEventListener("input", open);
    input.addEventListener("focus", () => { if (input.value.trim().length >= MIN_CHARS && !items().some(r => r.symbol === input.value.trim().toUpperCase())) open(); });
    // Touching the list (to scroll it) may take focus from the box: keep the list open then; close it on a
    // touch anywhere else on the page.
    box.addEventListener("pointerdown", () => { inList = true; });
    box.addEventListener("mousedown", ev => ev.preventDefault());       // desktop: keep the cursor in the box
    input.addEventListener("blur", () => setTimeout(() => { if (!inList) close(); }, 200));
    document.addEventListener("pointerdown", ev => {
      if (ev.target !== input && !box.contains(ev.target)) { inList = false; close(); }
    });
    input.addEventListener("keydown", ev => {
      if (box.classList.contains("hidden") || !rows.length) return;
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault(); at = (at + (ev.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length; mark();
        box.children[at].scrollIntoView({ block: "nearest" });
      } else if (ev.key === "Enter") { ev.preventDefault(); choose(rows[Math.max(at, 0)]); }
      else if (ev.key === "Escape") close();
    });
  }

  // ---------------------------------------------------------------- state
  let meta = null, chain = null;                   // chain: contracts of the instrument being added
  let basket = [];
  function loadBasket() {
    try {
      const old = localStorage.getItem("mc_basket_v1");           // first version kept one NSE F&O basket
      if (old && !localStorage.getItem("mc_basket_v2_NFO")) localStorage.setItem("mc_basket_v2_NFO", old);
      basket = JSON.parse(localStorage.getItem(storeKey()) || "[]").filter(l => l && l.symbol && (l.expiry || l.cash));
    } catch (_) { basket = []; }
  }
  const save = () => { try { localStorage.setItem(storeKey(), JSON.stringify(basket)); } catch (_) { /* private mode */ } };
  let type = "CE", side = "S";

  // ---------------------------------------------------------------- start
  // ---------------------------------------------------------------- market tabs + exchange
  function drawPicker() {
    document.querySelectorAll(".seg-tabs .seg").forEach(b => {
      const on = b.dataset.tab === tab; b.classList.toggle("active", on); b.setAttribute("aria-selected", String(on));
    });
    const sel = $("#exchange"); sel.textContent = "";
    for (const x of MARKETS[tab].exchanges)
      sel.append(el("option", { value: x.ex, selected: x === exch, disabled: !x.live }, x.label + (x.live ? "" : " — coming soon")));
    $("#exHint").textContent = exch.live ? "" : "Coming after it passes validation against brokers";
    $("#fine").textContent = tab === "fo"
      ? "Figures follow the clearing corporation's SPAN method and exposure rules on the start-of-day risk file, the file brokers margin new orders on. Brokers may add their own extra margin. Not investment advice."
      : "Figures follow NSE's VaR file (VaR + exposure + ad hoc margin) with surveillance rules and the common 20% broker minimum for intraday. Brokers may charge more. Not investment advice.";
    $("#lead").textContent = tab === "fo"
      ? "SPAN and exposure margin for a basket of futures and options, worked out from the clearing corporation's own risk files — with hedge benefits across the basket."
      : "VaR and exposure margin for buying or selling shares, worked out from the exchange's daily VaR file for each stock.";
    const q = new URLSearchParams({ tab, ex: exch.ex });
    history.replaceState(null, "", `${location.pathname}?${q}`);
  }

  function switchTo(newTab, newEx) {
    tab = newTab;
    const list = MARKETS[tab].exchanges;
    exch = list.find(x => x.ex === newEx && x.live) || list.find(x => x.live) || list[0];
    SEG = exch.seg; meta = null; chain = null;
    drawPicker(); boot();
  }
  document.querySelectorAll(".seg-tabs .seg").forEach(b => b.addEventListener("click", () => { if (b.dataset.tab !== tab) switchTo(b.dataset.tab, exch.ex); }));
  $("#exchange").addEventListener("change", ev => switchTo(tab, ev.target.value));

  function showSoon() {
    $("#boot").classList.add("hidden"); $("#app").classList.add("hidden"); $("#unavailable").classList.add("hidden");
    const box = $("#soon"); box.classList.remove("hidden"); box.textContent = "";
    const live = Object.values(MARKETS).flatMap(m => m.exchanges.filter(x => x.live).map(x => x.label));
    box.append(el("h2", {}, `${exch.label}: coming soon`),
      el("p", { class: "muted" }, "Margins appear here once this segment's calculations match broker figures on its own test baskets — we don't show figures that haven't been checked."),
      el("p", { class: "muted" }, "Available now: " + live.join(", ") + "."));
  }

  async function boot() {
    $("#soon").classList.add("hidden"); $("#unavailable").classList.add("hidden"); $("#app").classList.add("hidden");
    if (!exch.live) return showSoon();
    $("#boot").classList.remove("hidden"); $("#bootText").textContent = "Loading today's risk data…";
    loadBasket();
    $("#fSymbol").value = "";
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
    const cash = SEG === "NSECM";
    $("#legForm").classList.toggle("hidden", cash); $("#cashForm").classList.toggle("hidden", !cash);
    $("#addTitle").textContent = cash ? "Add an order" : "Add a position";
    drawDataLine();
    if (cash) { setupCash(); drawBasket(); calc(); return; }
    const rank = s => { const i = POPULAR.indexOf(s.symbol); return i < 0 ? 1000 + (s.kind === "INDEX" ? 0 : 500) : i; };
    const syms = [...meta.symbols].sort((a, b) => rank(a) - rank(b) || a.symbol.localeCompare(b.symbol));
    foList = syms.map(x => ({ symbol: x.symbol, tag: x.kind === "INDEX" ? "Index" : (x.banned ? "Stock · in ban" : "Stock") }));
    if (!basket.length || !$("#fSymbol").value) $("#fSymbol").value = syms.length ? syms[0].symbol : "";
    await pickSymbol();
    drawBasket(); calc();
  }

  function drawDataLine() {
    const d = meta.data, box = $("#dataLine"); box.textContent = "";
    if (SEG === "NSECM") box.append(el("span", {}, `Data for ${day(d.trade_date)} · VaR batch ${d.span_cycle || ""} · prices: last close`));
    else box.append(el("span", {}, `Data for ${day(d.trade_date)} · SPAN ${d.span_cycle || ""}` +
      (d.span_day && d.span_day !== d.trade_date ? ` of ${day(d.span_day)}` : "") +
      (d.span_created ? ` · file created ${d.span_created}` : "") + ` · ${d.basis === "bod" ? "start-of-day file" : "latest file"}`));
    const n = (meta.evidence || {}).baskets;
    box.append(el("span", { class: "badge " + (meta.validated ? "ok" : "warn"), title: meta.validation_note || "" },
      meta.validated ? (SEG === "NSECM" && n ? `Validated (${n} Dhan baskets)` : "Validated against brokers") : "Not validated"));
    if (d.stale) box.append(el("span", { class: "badge warn" }, "Today's start-of-day file isn't out yet"));
    if (d.older_files) box.append(el("span", { class: "badge warn", title: d.older_files }, "Some reference files are from an earlier day"));
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
    for (const k of ks) sel.append(el("option", { value: k, selected: k === best }, strikeTxt(k) + (k === best ? " · ATM" : "")));
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
  suggest($("#fSymbol"), $("#fSymbolList"), () => foList, () => { clearTimeout(symTimer); pickSymbol(); });
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

  // ---------------------------------------------------------------- cash orders
  let cSide = "buy", cashReady = false;
  function setupCash() {
    const rank = s => { const i = POPULAR.indexOf(s.symbol); return i < 0 ? 1000 : i; };
    cashList = [...meta.symbols].sort((a, b) => rank(a) - rank(b) || a.symbol.localeCompare(b.symbol))
      .map(x => ({ symbol: x.symbol, tag: [x.series, x.flag].filter(Boolean).join(" · ") }));
    if (!$("#cSymbol").value) $("#cSymbol").value = "RELIANCE";
    pickStock();
    if (cashReady) return;
    cashReady = true;
    segmented($("#cSide"), v => { cSide = v; });
    $("#cSymbol").addEventListener("input", pickStock);
    suggest($("#cSymbol"), $("#cSymbolList"), () => cashList, pickStock);
    $("#cashForm").addEventListener("submit", ev => {
      ev.preventDefault();
      const s = stock(); const qty = parseInt($("#cQty").value, 10);
      if (!s || !(qty > 0)) return;
      const price = parseFloat($("#cPrice").value);
      basket.push({ cash: true, symbol: s.symbol, side: cSide, qty, price: price > 0 ? price : null, lot: s.lot, series: s.series });
      save(); drawBasket(); calc();
    });
  }
  const stock = () => meta && meta.symbols.find(s => s.symbol === $("#cSymbol").value.trim().toUpperCase());
  function pickStock() {
    const s = stock();
    $("#cAdd").disabled = !s;
    if (!s) { $("#cInfo").textContent = $("#cSymbol").value ? "Pick a stock from the list" : " "; return; }
    $("#cPrice").placeholder = s.close ? `last close ${s.close}` : "enter a price";
    $("#cQty").step = String(s.lot || 1); $("#cQty").min = String(s.lot || 1);
    if (s.lot > 1 && parseInt($("#cQty").value, 10) % s.lot) $("#cQty").value = String(s.lot);
    $("#cInfo").textContent = [s.series, s.lot > 1 ? `lot ${s.lot}` : null, s.flag].filter(Boolean).join(" · ");
  }

  // ---------------------------------------------------------------- basket
  const legName = l => `${l.symbol} ${expLabel(l.expiry)} ${l.type === "FUT" ? "FUT" : strikeTxt(l.strike) + " " + l.type}`;
  function drawBasket() {
    const box = $("#basket"); box.textContent = "";
    $("#legCount").textContent = basket.length ? `(${basket.length})` : "";
    $("#clearAll").classList.toggle("hidden", !basket.length);
    if (!basket.length) { box.append(el("p", { class: "empty" }, "No positions yet.")); return; }
    if (SEG === "NSECM") {
      basket.forEach((l, i) => {
        const qty = el("input", { type: "number", min: String(l.lot || 1), step: String(l.lot || 1), value: String(l.qty), inputmode: "numeric", "aria-label": "Quantity" });
        qty.addEventListener("change", () => { const n = parseInt(qty.value, 10); if (n > 0) { l.qty = n; save(); calc(); } else qty.value = l.qty; });
        const sideBtn = el("button", { type: "button", class: "side-pill " + (l.side === "buy" ? "buy" : "sell"), title: "Switch buy / sell" }, l.side.toUpperCase());
        sideBtn.addEventListener("click", () => { l.side = l.side === "buy" ? "sell" : "buy"; save(); drawBasket(); calc(); });
        box.append(el("div", { class: "leg" }, sideBtn,
          el("div", { class: "leg-name" }, l.symbol, el("small", {}, l.price ? `at ₹${l.price}` : "at last close")),
          el("label", { class: "leg-lots" }, qty, el("span", {}, "shares")),
          el("button", { type: "button", class: "icon-btn", "aria-label": "Remove", title: "Remove",
                         onclick: () => { basket.splice(i, 1); save(); drawBasket(); calc(); } }, "×")));
      });
      return;
    }
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

  function drawCash(box, m) {
    box.classList.remove("loading"); box.textContent = "";
    const T = m.total;
    box.append(
      el("div", { class: "total" }, el("span", { class: "lbl" }, "Intraday margin (MIS)"), el("strong", {}, inr(T.intraday_margin)),
        el("span", { class: "hint" }, T.leverage ? `${T.leverage}× leverage on ${inr(T.value)} of orders` : "")),
      el("div", { class: "split" },
        el("div", {}, el("span", { class: "lbl" }, "Delivery (CNC) — buys"), el("b", {}, inr(T.delivery_value))),
        el("div", {}, el("span", { class: "lbl" }, "Order value"), el("b", {}, inr(T.value))),
        el("div", {}, el("span", { class: "lbl" }, "At NSE's rate alone"), el("b", {}, inr(T.nse_margin)))));
    const t = el("div", { class: "legs-table" },
      el("div", { class: "tr head" }, el("span", {}, "Order"), el("span", {}, "Rate"), el("span", {}, "Intraday"), el("span", {}, "Delivery")));
    for (const g of m.legs) t.append(el("div", { class: "tr" },
      el("span", {}, `${g.side === "buy" ? "Buy" : "Sell"} ${g.qty.toLocaleString("en-IN")} ${g.symbol}${g.series !== "EQ" ? " (" + g.series + ")" : ""} @ ${inr(g.price, 2)}`),
      el("span", {}, `${g.intraday_pct}%`), el("span", {}, inr(g.intraday_margin)),
      el("span", {}, g.side === "buy" ? inr(g.delivery_value) : "holdings")));
    box.append(t);
    for (const g of m.legs) if (g.rate_reasons && g.rate_reasons.length)
      box.append(el("p", { class: "note" }, `${g.symbol}: ${g.rate_reasons.join("; ")}`));
    for (const w of m.warnings || []) box.append(el("p", { class: "note warn" }, w));
    const d = m.data || {}, n = (m.evidence || {}).baskets;
    box.append(el("p", { class: "note" + (m.validated ? "" : " warn") },
      (m.validated ? `Validated: matched Dhan on ${n || ""} test baskets. ` : `Not validated against brokers: ${m.validation_note} `) +
      `VaR batch ${d.span_cycle || ""} of ${day(d.trade_date)}. Intraday includes the 20% broker minimum; sells on delivery need the shares in your holdings.`));
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
    if (SEG === "NSECM") {
      try {
        m = await api("/api/margin", { method: "POST", body: { segment: SEG, positions: basket.map(l => ({
          symbol: l.symbol, side: l.side, qty: l.qty, price: l.price })) } });
      } catch (e) {
        if (seq !== calcSeq) return;
        box.classList.remove("loading"); box.textContent = "";
        box.append(el("p", { class: "error" }, e.status === 429 ? "Too many requests — wait a minute and try again." : e.message));
        return;
      }
      if (seq !== calcSeq) return;
      return drawCash(box, m);
    }
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

  (async () => {                        // which exchanges are served (validated) right now
    try {
      if (window.MarginBackend.multi()) await window.MarginBackend.init();   // pick Render/Railway
      const r = await api("/api/margin/segments");
      for (const m of Object.values(MARKETS)) for (const x of m.exchanges)
        if (r.segments[x.seg]) x.live = r.segments[x.seg].served;
      if (!exch.live) { const l = MARKETS[tab].exchanges.find(x => x.live); if (l) { exch = l; SEG = l.seg; } }
    } catch (_) { /* keep the defaults: NSE F&O only */ }
    drawPicker();
    boot();
  })();
})();
