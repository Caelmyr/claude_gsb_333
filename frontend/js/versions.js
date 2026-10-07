/* Scene version management: version chain, diff viewer, rollback, clone. */

const PAGE = 10;
const SOURCE_LABEL = { create: "创建", edit: "编辑", clone: "克隆", rollback: "回滚" };
const SOURCE_CLASS = { create: "finished", edit: "running", clone: "paused", rollback: "stopped" };

let sceneId = null;
let scenes = [];
let chain = { latest: 0, total: 0, versions: [] };   // accumulated (newest first)
let shown = 0;

/* ------------------------------------------------------------------ */
/* Loading                                                             */
/* ------------------------------------------------------------------ */
async function loadScenes() {
  const data = await get("/api/scenes");
  scenes = data.scenes;
  const sel = el("sceneSel");
  sel.innerHTML = '<option value="">— 选择场景 —</option>' + scenes.map((s) =>
    `<option value="${esc(s.id)}">${esc(s.name)} (${DOMAIN_LABEL[s.domain] || s.domain} · ${MODEL_LABEL[s.model] || s.model})</option>`).join("");
  const qs = new URLSearchParams(window.location.search).get("scene");
  if (qs && scenes.some((s) => s.id === qs)) sel.value = qs;
  if (sel.value) await selectScene(sel.value);
}

async function selectScene(id) {
  sceneId = id;
  chain = { latest: 0, total: 0, versions: [] };
  shown = 0;
  el("diffOut").innerHTML = "";
  el("contentCard").style.display = "none";
  if (!id) {
    el("verList").innerHTML = '<p class="muted small">请先选择一个场景。</p>';
    el("sceneMeta").textContent = "";
    el("verCount").textContent = "";
    el("moreBtn").style.display = "none";
    fillDiffSelects();
    return;
  }
  await loadMore();
  const s = scenes.find((x) => x.id === id);
  el("sceneMeta").textContent = s
    ? `共 ${chain.total} 个版本 · 最新 v${chain.latest} · 更新于 ${s.updated_at}` : "";
  el("verCount").textContent = chain.total ? `共 ${chain.total} 版 · 最新 v${chain.latest}` : "";
}

async function loadMore() {
  if (!sceneId) return;
  const data = await get(`/api/scenes/${sceneId}/versions?limit=${PAGE}&offset=${shown}`);
  chain.latest = data.latest;
  chain.total = data.total;
  chain.versions = chain.versions.concat(data.versions);
  shown = chain.versions.length;
  renderList();
  fillDiffSelects();
  el("moreBtn").style.display = shown < chain.total ? "" : "none";
}

/* ------------------------------------------------------------------ */
/* Version list                                                        */
/* ------------------------------------------------------------------ */
function verItem(v) {
  const src = SOURCE_LABEL[v.source] || v.source;
  const cls = SOURCE_CLASS[v.source] || "ready";
  const restored = v.restored_from != null ? ` · 恢复自 v${v.restored_from}` : "";
  const parent = v.parent != null ? ` · 基于 v${v.parent}` : "";
  const pruned = v.pruned ? '<span class="badge error">快照已清理</span>' : "";
  const sum = v.summary ? `参数 ${v.summary.config_keys} 项 · 干预 ${v.summary.interventions} 条` : "";
  const actions = v.pruned ? "" : `
      <button class="btn small view" data-n="${v.n}">查看</button>
      <button class="btn small pick-a" data-n="${v.n}">设为 A</button>
      <button class="btn small pick-b" data-n="${v.n}">设为 B</button>
      <button class="btn danger small rollback" data-n="${v.n}">回滚到此</button>`;
  return `
    <div class="list-item ver-item ${v.pruned ? "pruned" : ""}" data-n="${v.n}">
      <div style="min-width:0">
        <div class="t"><span class="pill">v${v.n}</span> <span class="badge ${cls}">${src}</span> ${pruned}</div>
        <div class="s">${esc(v.message || "")}${restored}${parent}</div>
        <div class="s">${esc(v.created_at)} · ${sum}</div>
      </div>
      <div class="row gap-6" style="flex-shrink:0">${actions}</div>
    </div>`;
}

function renderList() {
  el("verList").innerHTML = chain.versions.map(verItem).join("") ||
    '<p class="muted small">该场景还没有版本记录。</p>';
  el("verList").querySelectorAll(".view").forEach((b) => { b.onclick = () => viewVersion(+b.dataset.n); });
  el("verList").querySelectorAll(".rollback").forEach((b) => { b.onclick = () => rollbackTo(+b.dataset.n); });
  el("verList").querySelectorAll(".pick-a").forEach((b) => { b.onclick = () => { el("diffFrom").value = b.dataset.n; }; });
  el("verList").querySelectorAll(".pick-b").forEach((b) => { b.onclick = () => { el("diffTo").value = b.dataset.n; }; });
}

/* ------------------------------------------------------------------ */
/* Version content viewer                                              */
/* ------------------------------------------------------------------ */
async function viewVersion(n) {
  try {
    const v = await get(`/api/scenes/${sceneId}/versions/${n}`);
    el("contentTitle").textContent = `版本 v${n} 内容快照`;
    el("contentBody").textContent = JSON.stringify(v.content, null, 2);
    el("contentCard").style.display = "";
  } catch (e) { notify(e.message, "error"); }
}

/* ------------------------------------------------------------------ */
/* Diff                                                                */
/* ------------------------------------------------------------------ */
function fillDiffSelects() {
  const usable = chain.versions.filter((v) => !v.pruned);
  const opts = usable.map((v) => `<option value="${v.n}">v${v.n} · ${esc(v.created_at)}</option>`).join("");
  const from = el("diffFrom"), to = el("diffTo");
  const keepA = from.value, keepB = to.value;
  from.innerHTML = opts;
  to.innerHTML = opts;
  if (usable.length) {
    from.value = keepA || usable[usable.length - 1].n;   // oldest loaded
    to.value = keepB || usable[0].n;                     // newest
  }
}

function changeRow(label, fromV, toV, change) {
  const cls = { added: "diff-add", removed: "diff-del", modified: "diff-mod" }[change] || "diff-mod";
  const text = change === "added" ? `∅ → ${fmt(toV)}`
    : change === "removed" ? `${fmt(fromV)} → ∅`
    : `${fmt(fromV)} → ${fmt(toV)}`;
  return `<div class="diff-row ${cls}"><span class="diff-k">${esc(label)}</span><span class="mono">${esc(text)}</span></div>`;
}

function diffGroup(title, rows) {
  if (!rows.length) return "";
  return `<div class="diff-group"><h4>${title}</h4>${rows.join("")}</div>`;
}

function itvText(itv) {
  const params = Object.entries(itv.params || {}).map(([k, v]) => `${k}=${fmt(v)}`).join(", ");
  return `${itv.label} @ 第${itv.at_step}步${params ? "（" + params + "）" : ""}`;
}

function renderDiff(d) {
  if (!d.changed) {
    el("diffOut").innerHTML = '<p class="muted">两个版本内容完全一致，没有任何差异。</p>';
    return;
  }
  const parts = [];
  parts.push(`<p class="muted small">v${d.from.n}（${esc(d.from.created_at)}） → v${d.to.n}（${esc(d.to.created_at)}）</p>`);
  parts.push(diffGroup("基本信息", d.meta.map((m) => changeRow(m.label, m.from, m.to, "modified"))));
  parts.push(diffGroup("地图与布局", d.config.map.map((c) => changeRow(c.label, c.from, c.to, c.change))));
  parts.push(diffGroup("初始布局", d.config.initial.map((c) => changeRow(c.label, c.from, c.to, c.change))));
  parts.push(diffGroup("模型参数", d.config.params.map((c) => changeRow(c.label, c.from, c.to, c.change))));
  const itv = d.interventions;
  const itvRows = []
    .concat(itv.added.map((i) => `<div class="diff-row diff-add">+ ${esc(itvText(i))}</div>`))
    .concat(itv.removed.map((i) => `<div class="diff-row diff-del">− ${esc(itvText(i))}</div>`))
    .concat(itv.modified.map((i) => `<div class="diff-row diff-mod">~ ${esc(itvText(i))}：${i.param_changes.map((p) => `${esc(p.key)} ${fmt(p.from)}→${fmt(p.to)}`).join("，")}</div>`));
  parts.push(diffGroup("干预措施", itvRows));
  el("diffOut").innerHTML = parts.join("");
}

async function runDiff() {
  const a = el("diffFrom").value, b = el("diffTo").value;
  if (!a || !b) { notify("请先选择两个可对比的版本", "warn"); return; }
  if (a === b) { notify("请选择两个不同的版本", "warn"); return; }
  try {
    const d = await get(`/api/scenes/${sceneId}/versions/diff?from=${a}&to=${b}`);
    renderDiff(d);
  } catch (e) { notify(e.message, "error"); }
}

/* ------------------------------------------------------------------ */
/* Rollback / clone / prune                                            */
/* ------------------------------------------------------------------ */
async function rollbackTo(n) {
  if (!confirm(`确认回滚到 v${n}？\n当前状态不会丢失：回滚会生成一个新版本，随时可再回滚回来。`)) return;
  try {
    const s = await post(`/api/scenes/${sceneId}/rollback`, { version: n });
    notify(`已回滚到 v${n} 的内容，并记录为新版本 v${s.version ? s.version.n : "?"}。`, "ok");
    await refresh();
  } catch (e) { notify("回滚失败：" + e.message, "error"); }
}

async function cloneScene() {
  if (!sceneId) { notify("请先选择场景", "warn"); return; }
  const s = scenes.find((x) => x.id === sceneId);
  const name = prompt("副本名称：", `${s ? s.name : "场景"} · 副本`);
  if (name === null) return;
  try {
    const c = await post(`/api/scenes/${sceneId}/clone`, { name: name || undefined });
    notify(`已克隆为新场景「${c.name}」，版本链从 v1 重新开始。`);
    await loadScenes();
    el("sceneSel").value = c.id;
    await selectScene(c.id);
  } catch (e) { notify("克隆失败：" + e.message, "error"); }
}

async function pruneVersions() {
  if (!sceneId) { notify("请先选择场景", "warn"); return; }
  const input = prompt(`保留最近多少个版本？（当前共 ${chain.total} 个，更早的快照将被清理，版本号保留）`, "20");
  if (input === null) return;
  const keep = parseInt(input, 10);
  if (!keep || keep < 1) { notify("请输入正整数", "warn"); return; }
  try {
    const r = await post(`/api/scenes/${sceneId}/versions/prune`, { keep });
    notify(`已清理 ${r.pruned} 个旧快照，保留最近 ${r.keep} 个版本。`);
    await refresh();
  } catch (e) { notify("清理失败：" + e.message, "error"); }
}

async function refresh() {
  const { scenes: fresh } = await get("/api/scenes");
  scenes = fresh;
  await selectScene(sceneId);
}

function notify(html, kind = "") {
  const n = el("notice");
  n.innerHTML = html;
  n.style.display = html ? "block" : "none";
  n.className = `notice ${kind}`;
  if (html && kind !== "error") setTimeout(() => { n.style.display = "none"; }, 6000);
}

/* ------------------------------------------------------------------ */
function init() {
  el("sceneSel").onchange = () => selectScene(el("sceneSel").value);
  el("moreBtn").onclick = loadMore;
  el("diffBtn").onclick = runDiff;
  el("cloneBtn").onclick = cloneScene;
  el("pruneBtn").onclick = pruneVersions;
  el("contentClose").onclick = () => { el("contentCard").style.display = "none"; };
  loadScenes().catch((e) => notify("加载失败：" + e.message, "error"));
}

init();
