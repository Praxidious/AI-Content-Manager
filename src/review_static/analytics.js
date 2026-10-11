"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="review-token"]').content;
let catalog, draft = null, busy = false;
function node(tag, text, className) { const el = document.createElement(tag); if (text != null) el.textContent = text; if (className) el.className = className; return el; }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {"X-Review-Token": token, ...options.headers}});
  let data; try { data = await response.json(); } catch { throw new Error(`Request failed (${response.status}). Check upload size or server availability.`); }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
const postJSON = (path, body) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
function message(text, error = false) { $(error ? "error" : "status").textContent = text; $(error ? "status" : "error").textContent = ""; }
async function action(fn) { if (busy) return; busy = true; document.querySelectorAll("button").forEach(b => b.disabled = true); try { await fn(); } catch (e) { message(e.message, true); } finally { busy = false; document.querySelectorAll("button").forEach(b => b.disabled = false); } }
function options(select, items, selected) { select.replaceChildren(); for (const [value, label] of items) { const option = node("option", label); option.value = value; select.append(option); } if (selected && [...select.options].some(o => o.value === selected)) select.value = selected; }
function localDate(date) { return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19); }
function iso(id) { if (!$(id).value) throw new Error("Enter both posting and capture times."); const date = new Date($(id).value); if (!Number.isFinite(date.getTime())) throw new Error("Invalid date/time"); return date.toISOString(); }
function context() {
  const base = {captured_at: iso("captured-at")};
  if ($("post").value) return {...base, post_id: $("post").value};
  const attributes = {};
  document.querySelectorAll("[data-feature]").forEach(el => { if (el.value !== "") attributes[el.dataset.feature] = el.type === "number" ? Number(el.value) : el.dataset.feature === "laughter" ? el.value === "true" : el.value; });
  return {...base, clip_id: $("clip").value, platform: $("platform").value, account: $("account").value,
    posted_at: iso("posted-at"), post_reference: $("post-reference").value, attributes};
}
function filterClips() {
  const query = $("clip-search").value.toLowerCase(), selected = $("clip").value;
  const clips = catalog.clips.filter(c => `${c.title} ${c.source} ${c.clip_id}`.toLowerCase().includes(query));
  options($("clip"), clips.map(c => [c.clip_id, `${c.title} · ${c.rating || "unrated"} · ${c.clip_id.slice(0, 8)}`]), selected);
}
function postLabel(p) { return `${p.features.metadata.title || p.clip_id} · ${p.platform} / ${p.account} · ${new Date(p.posted_at).toLocaleString()}`; }
async function loadCatalog() {
  catalog = await api("/api/analytics/catalog");
  for (const id of ["platform", "dash-platform"]) options($(id), catalog.platforms.map(p => [p.platform, p.label]), $(id).value);
  filterClips();
  options($("post"), [["", "New post"], ...catalog.posts.map(p => [p.post_id, postLabel(p)])], $("post").value);
  options($("history-post"), [["", "Select a post"], ...catalog.posts.map(p => [p.post_id, postLabel(p)])], $("history-post").value);
  $("metric-keys").replaceChildren(...[...Object.keys(catalog.metrics), "engagement_per_view_percent", "watch_duration_ratio_percent", "shares_per_view_percent", "saves_per_view_percent"].map(k => { const o = node("option"); o.value = k; return o; }));
}
function postChanged() {
  const post = catalog.posts.find(p => p.post_id === $("post").value);
  $("new-post-fields").hidden = Boolean(post); $("attributes").hidden = Boolean(post);
  document.querySelectorAll("#new-post-fields input, #new-post-fields select, [data-feature]").forEach(el => el.disabled = Boolean(post));
  $("post-identity").textContent = post ? `${postLabel(post)} · Clip ID: ${post.clip_id}` : "";
  ageChanged();
}
function ageChanged() {
  if (!$("capture-age").value) return;
  const post = catalog.posts.find(p => p.post_id === $("post").value);
  const date = new Date(post ? post.posted_at : $("posted-at").value);
  if (Number.isFinite(date.getTime())) $("captured-at").value = localDate(new Date(date.getTime() + Number($("capture-age").value) * 3600000));
}
function metricRow(metric = {}) {
  const tr = node("tr"); tr._metric = metric;
  const first = node("td"), key = node("input"), label = node("input");
  key.value = metric.key || ""; key.className = "metric-key"; key.placeholder = "metric_key"; key.setAttribute("list", "metric-keys"); key.setAttribute("aria-label", "Metric key");
  label.value = metric.label || ""; label.className = "metric-label"; label.placeholder = "Platform label"; label.setAttribute("aria-label", "Platform label"); first.append(key, label);
  const second = node("td"), value = node("input"); value.value = metric.value ?? ""; value.className = "metric-value"; value.setAttribute("aria-label", "Metric value"); second.append(value);
  const third = node("td"), unit = node("select"); unit.className = "metric-unit"; unit.setAttribute("aria-label", "Metric unit"); options(unit, ["count", "seconds", "percent", "number", "text"].map(u => [u,u]), metric.unit); third.append(unit);
  key.addEventListener("change", () => { if (catalog.metrics[key.value]) unit.value = catalog.metrics[key.value]; });
  const evidence = node("td"), original = node("input"); original.value = metric.displayed_text || ""; original.className = "metric-original"; original.setAttribute("aria-label", "Original displayed text");
  evidence.append(original, node("div", `Screenshots: ${(metric.sources || []).join(", ") || "manual"} · Extraction confidence: ${typeof metric.confidence === "number" ? (100 * metric.confidence).toFixed(0) + "%" : "unknown"}`, "evidence"));
  const last = node("td"), remove = node("button", "Remove"); remove.type = "button"; remove.addEventListener("click", () => { tr.remove(); $("confirmed").checked = false; }); last.append(remove);
  tr.append(first, second, third, evidence, last); $("metrics").querySelector("tbody").append(tr);
}
function metrics() { return [...document.querySelectorAll("#metrics tbody tr")].map(tr => {
  const unit = tr.querySelector(".metric-unit").value, text = tr.querySelector(".metric-value").value;
  if (!text.trim()) throw new Error("Enter a value or remove the metric row.");
  const value = unit === "text" ? text : Number(text);
  if (unit !== "text" && !Number.isFinite(value)) throw new Error("Numeric values must use plain numbers; convert K/M and durations before saving.");
  return {...tr._metric, key: tr.querySelector(".metric-key").value.trim(), label: tr.querySelector(".metric-label").value,
    unit, value, displayed_text: tr.querySelector(".metric-original").value};
}); }
async function startImport(manual) {
  if (draft) throw new Error("Confirm or discard the current import first.");
  if (!$("import-form").reportValidity()) return;
  const form = new FormData(); form.append("context", JSON.stringify(context())); form.append("manual", String(manual));
  for (const file of $("screenshots").files) form.append("screenshots", file);
  message(manual ? "Preparing manual entry…" : "Reading screenshots. Nothing has been saved yet…");
  draft = await api("/api/analytics/extract", {method: "POST", body: form});
  $("metrics").querySelector("tbody").replaceChildren(); draft.metrics.forEach(metricRow); if (!draft.metrics.length) metricRow();
  $("warnings").replaceChildren(...draft.warnings.map(w => node("li", w)));
  $("screenshots-preview").replaceChildren(...draft.images.map(i => {
    const figure = node("figure"), a = node("a"), img = node("img"); img.src = `/api/analytics/drafts/${draft.draft_id}/images/${i.index}`; img.alt = `Screenshot ${i.index}`;
    a.href = img.src; a.target = "_blank"; a.rel = "noopener"; a.append(img); figure.append(a, node("figcaption", `${i.index}. ${i.filename}`)); return figure;
  }));
  $("confirmed").checked = false; $("confirmation").hidden = false; $("derived-preview").replaceChildren();
  message("Review the editable values below. No analytics have been saved."); $("confirmation").scrollIntoView({behavior: "smooth"});
}
function renderDerived(data) { $("derived-preview").replaceChildren(...Object.entries(data).map(([key, x]) => node("p", `${key}: ${x.value.toFixed(2)} ${x.unit} — ${x.formula}`))); if (!Object.keys(data).length) $("derived-preview").textContent = "No calculations available with these inputs."; }
function clearDraft() { draft = null; $("confirmation").hidden = true; $("screenshots").value = ""; }
const fmt = value => value == null ? "—" : typeof value === "number" ? value.toLocaleString(undefined, {maximumFractionDigits: 2}) : String(value);
function tableRow(values) { const tr = node("tr"); values.forEach(v => tr.append(node("td", fmt(v)))); return tr; }
async function dashboard() {
  const params = new URLSearchParams({platform: $("dash-platform").value, account: $("dash-account").value, horizon: $("dash-age").value, metric: $("dash-metric").value});
  const data = await api(`/api/analytics/dashboard?${params}`);
  $("method").textContent = data.method;
  $("dashboard-summary").replaceChildren(node("div", `${data.post_count} posts at this capture age`), node("div", `${data.independent_clips} independent clips with a baseline`), node("div", `Minimum learning history: ${data.min_history} normalized clips`));
  $("associations").replaceChildren(...data.associations.map(a => { const row = tableRow([a.feature, a.group, a.n, `${a.mean_log2_lift >= 0 ? "+" : ""}${a.mean_log2_lift.toFixed(2)} (${a.direction})`, a.interval ? a.interval.map(fmt).join(" to ") : "Unavailable", a.confidence]); row.children[3].className = a.mean_log2_lift >= 0 ? "positive" : "negative"; return row; }));
  if (!data.associations.length) $("associations").append(tableRow(["No comparable history yet. Import snapshots from earlier posts at this same age."]));
  $("coverage").textContent = `Feature coverage among normalized clips: ${Object.entries(data.coverage).map(([k,v]) => `${k}: ${v}`).join(" · ") || "none yet"}. Missing features are not inferred.`;
  $("observations").replaceChildren(...data.observations.slice().reverse().map(o => tableRow([o.title, `${fmt(o.age_hours)} h`, o.value, o.baseline, o.baseline_n, o.percentile == null ? "Insufficient baseline" : `${fmt(o.percentile)}%`])));
}
async function history() {
  $("post-history").replaceChildren(); $("prediction").replaceChildren();
  if (!$("history-post").value) return;
  const data = await api(`/api/analytics/posts/${$("history-post").value}`);
  $("post-history").append(node("p", `${postLabel(data.post)} · Clip ID: ${data.post.clip_id}`));
  for (const snap of data.snapshots) {
    const detail = node("details"), summary = node("summary", `${new Date(snap.captured_at).toLocaleString()} · ${fmt(snap.age_hours)} hours after posting`);
    detail.append(summary, node("h3", "Platform metrics"));
    const table = node("table"); table.append(tableRow(["Metric", "Value", "Unit"])); snap.raw_metrics.forEach(m => table.append(tableRow([m.label || m.key, m.value, m.unit]))); detail.append(table, node("h3", "Calculated metrics"));
    for (const [key, m] of Object.entries(snap.derived_metrics)) detail.append(node("p", `${key}: ${fmt(m.value)} ${m.unit} — ${m.formula}`));
    const audit = node("details"); audit.append(node("summary", "Original extraction & warnings"), node("pre", JSON.stringify(snap.extraction, null, 2))); detail.append(audit);
    for (const image of snap.images || []) { const link = node("a", `Screenshot ${image.image_index}: ${image.filename}`); link.href = `/api/analytics/snapshots/${snap.snapshot_id}/images/${image.image_index}`; link.target = "_blank"; link.rel = "noopener"; const p = node("p"); p.append(link); detail.append(p); }
    $("post-history").append(detail);
  }
  const features = node("details"); features.append(node("summary", "Linked clip features & ratings at posting"), node("pre", JSON.stringify(data.post.features, null, 2))); $("post-history").append(features);
  const params = new URLSearchParams({platform: data.post.platform, account: data.post.account});
  const pred = await api(`/api/analytics/prediction/${data.post.clip_id}?${params}`), p = pred.audience;
  $("prediction").append(node("h3", "Two separate recommendation signals"), node("p", `Wes preference adjustment: ${fmt(pred.wes_preference_score)} · Current human rating: ${pred.human_rating || "unrated"}`),
    node("p", `Audience prediction at 24 h: ${fmt(p.predicted_log2_lift)} log₂ lift · ${p.history_n} independent historical clips · ${p.similar_n} similar clips · ${p.confidence} confidence`), node("p", p.reason, "muted"));
}
$("import-form").addEventListener("submit", e => { e.preventDefault(); action(() => startImport(false)); });
$("manual").addEventListener("click", () => action(() => startImport(true)));
$("clip-search").addEventListener("input", filterClips); $("post").addEventListener("change", postChanged);
$("capture-age").addEventListener("change", ageChanged); $("posted-at").addEventListener("change", ageChanged);
$("captured-at").addEventListener("input", () => { $("capture-age").value = ""; });
$("add-metric").addEventListener("click", () => { metricRow(); $("confirmed").checked = false; });
$("import").addEventListener("input", e => { if (e.target.id !== "confirmed") { $("confirmed").checked = false; $("derived-preview").textContent = "Values changed. Preview calculations again."; } });
$("derive").addEventListener("click", () => action(async () => renderDerived(await postJSON("/api/analytics/derive", {context: context(), metrics: metrics()}))));
$("discard").addEventListener("click", () => action(async () => { await postJSON(`/api/analytics/drafts/${draft.draft_id}/discard`, {}); clearDraft(); message("Import discarded. No analytics saved."); }));
$("save").addEventListener("click", () => action(async () => {
  if (!$("confirmed").checked) throw new Error("Check the confirmation box after reviewing the values.");
  const payload = {confirmed: true, context: context(), metrics: metrics()};
  const saved = await postJSON(`/api/analytics/drafts/${draft.draft_id}/confirm`, payload);
  clearDraft(); await loadCatalog(); await dashboard(); message(`Snapshot saved (${saved.snapshot_id.slice(0, 8)}). Existing clip ratings are unchanged.`);
}));
$("refresh-dashboard").addEventListener("click", () => action(dashboard)); $("history-post").addEventListener("change", () => action(history));
$("captured-at").value = localDate(new Date());
action(async () => { await loadCatalog(); await dashboard(); });
