// Shared helpers for Workbench pages. el() builds DOM from text only (no innerHTML -> no injection).
const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" },
                                body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
  }
  for (const k of kids) if (k !== null && k !== undefined && k !== false) e.append(k);
  return e;
}

function btn(label, fn, cls = "", disabled = false, after) {
  const b = el("button", { class: cls, onclick: async (ev) => {
    ev.stopPropagation(); b.disabled = true;
    try { await fn(); } catch (e) { alert(e.message); }
    b.disabled = false; if (after) after();
  } }, label);
  b.disabled = disabled;
  return b;
}

function pill(text, cls) { return el("span", { class: "pill " + (cls || text) }, text); }
function when(iso) { return iso ? new Date(iso).toLocaleString() : ""; }

function tableOf(headers, rows) {
  return el("table", {}, el("thead", {}, el("tr", {}, ...headers.map(h => el("th", {}, h)))), el("tbody", {}, ...rows));
}

// Header nav; returns /api/me
async function initHeader(active) {
  const me = await api("GET", "/api/me");
  const nav = $("nav");
  const links = [["/", "Sessions"], ["/packages", "Packages"]];
  if (me.level === "admin" || me.level === "auditor") links.push(["/admin", "Admin"]);
  for (const [href, label] of links) nav.append(el("a", { href, class: href === active ? "on" : "" }, label));
  $("user").replaceChildren(me.user, el("span", { class: "lvl" }, me.level));
  return me;
}
