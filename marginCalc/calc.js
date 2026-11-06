"use strict";
(async function () {
  const API = (window.MARGIN_API_BASE || "").replace(/\/$/, "");
  const status = document.getElementById("status"), list = document.getElementById("segments");
  try {
    const s = await fetch(API + "/api/public/status").then(r => r.json());
    status.textContent = s.message;
    for (const seg of s.segments) {
      const li = document.createElement("li");
      li.textContent = `${seg.code} – ${seg.name}`;
      const tag = document.createElement("span"); tag.className = seg.validated ? "tag ok" : "tag"; tag.textContent = seg.validated ? "validated" : "not validated";
      li.append(tag); list.append(li);
    }
  } catch (e) { status.textContent = "Service is starting up or unavailable. Try again in a minute."; }
})();
