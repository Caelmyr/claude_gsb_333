/* Scene version management: timeline, arbitrary two-version diff, rollback,
   clone, named checkpoints and storage/prune controls. */

const PAGE = 25;
let scenes = [];
let stats = { scenes: [] };
let curId = null;
let versions = [];          // newest-first loaded page rows
let total = 0;
let offset = 0;
let allMeta = null;         // {id -> version row} used by the diff selects
let selected = { a: null, b: null };

const KIND_LABEL = {
  create: "创建", clone: "克隆", backfill: "历史补录",
  auto: "保存", rollback: "回滚", checkpoint: "命名快照",
};

function kindBadge(kind) {
  const cls = { create: "finished", clone: "running", backfill: "ready",
                auto: "ready", rollback: "paused", checkpoint: "finished" }[kind] || "ready";
  return `<span class="badge ${cls}">${esc(KIND_LABEL[kind] || kind)}</span>`;
}

function bytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

/* ------------------------------------------------------------------ scenes */
async function loadScenes() {
  const [{ scenes: list }, st] = await Promise.all(
    [get("/api/scenes"), get("/api/version-stats")]);
  scenes = list;
  stats = st;
  renderSceneList();
  renderStorage();
}

function renderSceneList() {
  const q = (el("sceneSearch").value || "").trim().toLowerCase();
  const countOf = Object.fromEntries(
    (stats.scenes || []).map((s) => [s.scene_id, s.versions]));
  const shown = scenes.filter((s) => !q || s.name.toLowerCase().includes(q));
  el("sceneList").innerHTML = shown.map((s) => `
    <div class="list-item" data-id="${esc(s.id)}">
      <div style="min-width:0">
        <div class="t">${esc(s.name)}</div>
        <div class="s">${DOMAIN_LABEL[s.domain] || s.domain} · ${MODEL_LABEL[s.model] || s.model}
          · ${countOf[s.id] != null ? countOf[s.id] + " 个版本" : "—"}</div>
      </div>
      <button class="btn small clone" data-id="${esc(s.id)}" title="克隆整个场景为副本">克隆</button>
    </div>`).join("") || '<p class="muted small">暂无场景。</p>';

  el("sceneList").querySelectorAll(".list-item").forEach((li) => {
    li.classList.toggle("selected", li.dataset.id === curId);
    li.onclick = (ev) => {
      if (ev.target.closest(".clone")) return;
      selectScene(li.dataset.id);
    };
  });
  el("sceneList").querySelectorAll(".clone").forEach((b) => {
    b.onclick = (ev) => { ev.stopPropagation(); cloneScene(b.dataset.id, null); };
  });
}

function renderStorage() {
  el("keepN").textContent = stats.auto_keep || 50;
  el("storageHint").textContent =
    `${stats.total_versions || 0} 版本 / ${bytes(stats.total_bytes || 0)}`;
  const mine = (stats.scenes || []).find((s) => s.scene_id === curId);
  const card = el("storageCard");
  if (!mine) { card.style.display = "none"; return; }
  card.style.display = "";
  el("storageStats").innerHTML = `
    <div class="row between"><span class="muted">版本总数</span><span class="mono">${mine.versions}</span></div>
    <div class="row between"><span class="muted">自动快照</span><span class="mono">${mine.auto}</span></div>
    <div class="row between"><span class="muted">受保护版本</span><span class="mono">${mine.protected}</span></div>
    <div class="row between"><span class="muted">本场景快照占用</span><span class="mono">${bytes(mine.bytes)}</span></div>
    <div class="row between" style="margin-top:6px"><span class="muted">全局内容块</span><span class="mono">${stats.total_blobs}（去重共享）</span></div>`;
}

/* --------------------------------------------------------------- selection */
async function selectScene(id) {
  curId = id;
  offset = 0;
  versions = [];
  allMeta = null;
  selected = { a: null, b: null };
  el("emptyCard").style.display = "none";
  el("versionPanel").style.display = "";
  el("diffOut").innerHTML = "";
  renderSceneList();
  renderStorage();
  const scene = scenes.find((s) => s.id === id);
  el("vTitle").textContent = scene.name;
  el("vSub").textContent =
    `${DOMAIN_LABEL[scene.domain] || scene.domain} · ${MODEL_LABEL[scene.model] || scene.model} · 最近保存 ${esc(scene.updated_at || "")}`;
  el("editLink").href = `/config.html?scene=${esc(id)}`;
  await loadPage(true);
}

async function loadPage(reset) {
  const data = await get(`/api/scenes/${curId}/versions?limit=${PAGE}&offset=${offset}`);
  total = data.total;
  if (reset) versions = data.versions; else versions = versions.concat(data.versions);
  el("listMeta").textContent = `共 ${total} 个版本`;
  renderTimeline();
  if (reset) await buildDiffSelects(data.head);
  renderStorage();
}

/* ---------------------------------------------------------------- timeline */
function changePills(changes) {
  if (!changes) return '<span class="muted small">初始版本</span>';
  const order = ["params", "map", "initial", "interventions", "meta"];
  const label = { params: "参数", map: "地图", initial: "初始布局",
                  interventions: "干预", meta: "信息" };
  const parts = order.filter((k) => changes[k] > 0)
    .map((k) => `<span class="pill" style="margin-right:6px">${label[k]} ${changes[k]}</span>`);
  return parts.join("") || '<span class="muted small">与上一版内容相同</span>';
}

function renderTimeline() {
  el("pageInfo").textContent = `已加载 ${versions.length} / ${total}`;
  el("olderBtn").style.display = versions.length < total ? "" : "none";
  el("timeline").innerHTML = versions.map((v) => {
    const src = v.source || {};
    let srcLine = "";
    if (v.kind === "rollback" && src.rollback_seq != null) {
      srcLine = `<div class="muted small">↩ 内容恢复自 v${src.rollback_seq}（其后版本仍完整保留）</div>`;
    } else if (v.kind === "clone" && src.scene_name) {
      srcLine = `<div class="muted small">⎘ 克隆自《${esc(src.scene_name)}》${src.version_seq ? ` v${src.version_seq}` : ""}</div>`;
    }
    const headTag = v.is_head ? ' <span class="badge running">当前</span>' : "";
    return `
    <div class="ver-item" data-id="${esc(v.id)}">
      <div class="ver-node"></div>
      <div class="card" style="margin-bottom:0;padding:12px 14px">
        <div class="row between" style="gap:8px">
          <div style="min-width:0">
            <div style="font-weight:600">
              <span class="mono muted small">v${v.seq}</span>
              ${kindBadge(v.kind)}${headTag}
              ${v.name ? `<span style="margin-left:6px">${esc(v.name)}</span>` : ""}
              ${v.protected ? '<span class="muted small" style="margin-left:6px">🔒 受保护</span>' : ""}
            </div>
            <div class="muted small" style="margin-top:3px">${esc(v.created_at)} · ${esc(v.short_id)}</div>
            ${srcLine}
            <div style="margin-top:6px">${changePills(v.changes)}</div>
          </div>
          <div class="row gap-6" style="flex-shrink:0">
            <button class="btn small use-a" data-id="${esc(v.id)}">作为 A</button>
            <button class="btn small use-b" data-id="${esc(v.id)}">作为 B</button>
            <button class="btn small view" data-id="${esc(v.id)}">查看</button>
            <button class="btn small clonev" data-id="${esc(v.id)}">克隆</button>
            <button class="btn small danger rollback" data-id="${esc(v.id)}"
                    ${v.is_head ? "disabled title='当前版本，无需回滚'" : ""}>回滚到此版</button>
          </div>
        </div>
        <div class="ver-detail" data-id="${esc(v.id)}" style="display:none;margin-top:10px"></div>
      </div>
    </div>`;
  }).join("");

  el("timeline").querySelectorAll(".use-a").forEach((b) => {
    b.onclick = () => { selected.a = b.dataset.id; markAB(); autoDiff(); };
  });
  el("timeline").querySelectorAll(".use-b").forEach((b) => {
    b.onclick = () => { selected.b = b.dataset.id; markAB(); autoDiff(); };
  });
  el("timeline").querySelectorAll(".rollback").forEach((b) => {
    b.onclick = () => rollback(b.dataset.id);
  });
  el("timeline").querySelectorAll(".clonev").forEach((b) => {
    b.onclick = () => cloneScene(curId, b.dataset.id);
  });
  el("timeline").querySelectorAll(".view").forEach((b) => {
    b.onclick = () => toggleView(b.dataset.id);
  });
  markAB();
}

function markAB() {
  el("timeline").querySelectorAll(".ver-item").forEach((item) => {
    item.classList.toggle("is-a", item.dataset.id === selected.a);
    item.classList.toggle("is-b", item.dataset.id === selected.b);
  });
  if (allMeta) {
    if (selected.a) el("diffA").value = selected.a;
    if (selected.b) el("diffB").value = selected.b;
  }
}

/* ------------------------------------------------------------------ views */
function cssEsc(s) {
  return (window.CSS && CSS.escape) ? CSS.escape(s)
    : String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
}

async function toggleView(id) {
  const box = document.querySelector(`.ver-detail[data-id="${cssEsc(id)}"]`);
  if (box.style.display === "none") {
    if (!box.dataset.loaded) {
      const { scene } = await get(`/api/scenes/${curId}/versions/${id}`);
      box.innerHTML = renderSceneContent(scene);
      box.dataset.loaded = "1";
    }
    box.style.display = "";
  } else {
    box.style.display = "none";
  }
}

function renderSceneContent(scene) {
  const cfg = Object.entries(scene.config || {})
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(fmt(v))}</td></tr>`)
    .join("");
  const itvs = (scene.interventions || []).map((i) =>
    `<li>第 <b>${esc(i.at_step)}</b> 步 · ${esc(i.type)}
      <span class="muted small">${esc(JSON.stringify(i.params || {}))}</span></li>`).join("")
    || '<span class="muted small">无</span>';
  return `
    <div class="grid grid-2">
      <div>
        <div class="muted small">描述</div>
        <div>${esc(scene.description || "—")}</div>
        <div class="muted small" style="margin-top:8px">参数（${Object.keys(scene.config || {}).length}）</div>
        <table><tbody>${cfg}</tbody></table>
      </div>
      <div>
        <div class="muted small">定时干预（${(scene.interventions || []).length}）</div>
        <ul class="small" style="margin:6px 0 0 18px">${itvs}</ul>
      </div>
    </div>`;
}

/* ------------------------------------------------------------------ diffs */
async function buildDiffSelects(headId) {
  // Load all lightweight version rows for the selectors (one compact request;
  // rows carry no payloads, just metadata + change summaries).
  const data = await get(`/api/scenes/${curId}/versions?limit=0`);
  allMeta = {};
  data.versions.forEach((v) => { allMeta[v.id] = v; });
  const opt = (v) => `<option value="${esc(v.id)}">v${v.seq} · ${esc(KIND_LABEL[v.kind] || v.kind)}${v.name ? " · " + esc(v.name) : ""} · ${esc(v.created_at)}</option>`;
  el("diffA").innerHTML = data.versions.map(opt).join("");
  el("diffB").innerHTML = data.versions.map(opt).join("");

  const ids = new Set(data.versions.map((v) => v.id));
  const keep = (id) => (id && ids.has(id) ? id : null);
  selected.b = keep(selected.b);
  selected.a = keep(selected.a);
  if (!selected.b) {
    // Default: newest (B) vs its immediate predecessor (A).
    selected.b = headId;
    const head = allMeta[headId];
    selected.a = (head && keep(head.parent_id))
      || (data.versions[1] ? data.versions[1].id : "");
  }
  el("diffB").value = selected.b || "";
  el("diffA").value = selected.a || "";
  markAB();
  await runDiff();
}

function autoDiff() {
  if (selected.a && selected.b) runDiff();
}

async function runDiff() {
  if (!selected.b) { el("diffOut").innerHTML = '<p class="muted small">请选择新版本 B。</p>'; return; }
  if (selected.a && selected.a === selected.b) {
    el("diffOut").innerHTML = '<div class="notice warn">A 与 B 是同一个版本，请选择两个不同版本。</div>';
    return;
  }
  let url = `/api/scenes/${curId}/diff?b=${selected.b}`;
  if (selected.a) url += `&a=${selected.a}`;
  const d = await get(url);
  const a = d.a, b = d.b;
  const head = `
    <div class="row between" style="margin-bottom:10px">
      <div class="small">A：${a ? `<b>v${a.seq}</b> ${esc(KIND_LABEL[a.kind] || "")} · ${esc(a.created_at)}` : '<span class="muted">空（无父版本）</span>'}</div>
      <div class="small">B：<b>v${b.seq}</b> ${esc(KIND_LABEL[b.kind] || "")} · ${esc(b.created_at)}</div>
    </div>`;
  if (d.summary.total === 0) {
    el("diffOut").innerHTML = head +
      '<div class="notice">两个版本内容完全相同，没有差异。</div>';
    return;
  }
  const pills = d.sections.filter((s) => s.changes.length)
    .map((s) => `<span class="pill" style="margin-right:8px">${s.label} ${s.changes.length}</span>`).join("");
  el("diffOut").innerHTML = head +
    `<div style="margin-bottom:12px">${pills}</div>` +
    d.sections.filter((s) => s.changes.length).map(renderSection).join("");
}

function valCell(v) {
  return v == null ? '<span class="muted">—</span>'
    : `<span class="mono">${esc(typeof v === "object" ? JSON.stringify(v) : fmt(v))}</span>`;
}

function renderSection(sec) {
  if (sec.key === "interventions") return renderItvSection(sec);
  const rows = sec.changes.map((c) => `
    <tr>
      <td>${esc(c.label || c.key)}</td>
      <td>${valCell(c.old)}</td>
      <td class="diff-arrow">→</td>
      <td>${valCell(c.new)}</td>
    </tr>`).join("");
  return `<div class="diff-section">
    <h4>${esc(sec.label)} <span class="muted small">${sec.changes.length} 项</span></h4>
    <table><thead><tr><th>项目</th><th>A（旧）</th><th></th><th>B（新）</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function itvSummary(v) {
  if (!v) return "";
  return `${esc(v.label || v.type)} · 第 ${v.at_step} 步` +
    (Object.keys(v.params || {}).length
      ? ` · ${esc(Object.entries(v.params).map(([k, x]) => `${k}=${fmt(x)}`).join("，"))}` : "");
}

function renderItvSection(sec) {
  const blocks = sec.changes.map((c) => {
    if (c.kind === "added") {
      return `<div class="diff-add">＋ 新增：${itvSummary(c.new)}</div>`;
    }
    if (c.kind === "removed") {
      return `<div class="diff-del">－ 删除：${itvSummary(c.old)}</div>`;
    }
    const metas = (c.meta_changes || []).map((m) =>
      `<div class="small">${esc(m.label)}：${valCell(m.old)} <span class="diff-arrow">→</span> ${valCell(m.new)}</div>`).join("");
    const params = (c.param_changes || []).map((p) =>
      `<tr><td>${esc(p.label || p.key)}</td><td>${valCell(p.old)}</td><td class="diff-arrow">→</td><td>${valCell(p.new)}</td></tr>`).join("");
    return `<div class="diff-mod">
      <div class="small" style="margin-bottom:4px">✎ 修改第 ${c.index + 1} 条干预：${itvSummary(c.old)}</div>
      ${metas}
      ${params ? `<table style="margin-top:4px"><tbody>${params}</tbody></table>` : ""}
    </div>`;
  }).join("");
  return `<div class="diff-section">
    <h4>${esc(sec.label)} <span class="muted small">${sec.changes.length} 处</span></h4>
    ${blocks}</div>`;
}

/* -------------------------------------------------------------- rollback */
async function rollback(id) {
  const v = allMeta && allMeta[id];
  const label = v ? `v${v.seq}` : "该版本";
  if (!confirm(`确认把当前场景内容回滚到 ${label}？\n\n`
      + "· 回滚不会删除任何历史版本；\n"
      + "· 会在时间线末端新增一条「回滚」版本（之后可随时再回到现在）；\n"
      + "· 回滚后继续编辑、保存，新版本接在回滚版本之后，新改动不会丢失。")) return;
  const note = prompt("给这次回滚加个备注（可留空）：", `回滚到 ${label}`);
  const btn = document.querySelector(`.rollback[data-id="${cssEsc(id)}"]`);
  if (btn) btn.disabled = true;
  try {
    await post(`/api/scenes/${curId}/versions/${id}/rollback`,
               { note: note || undefined });
    await afterHistoryChange(`已回滚到 ${label}`);
  } catch (e) {
    alert("回滚失败：" + e.message);
    if (btn) btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ clone */
async function cloneScene(sceneId, versionId) {
  const name = prompt("副本名称：", "");
  if (name === null) return;
  try {
    const clone = await post(`/api/scenes/${sceneId}/clone`,
                             { name: name || undefined,
                               version_id: versionId || undefined });
    if (sceneId === curId) {
      if (confirm(`副本《${clone.name}》已创建（拥有独立的版本历史）。\n是否立即打开副本？`)) {
        selectScene(clone.id);
        return;
      }
      await loadScenes();
    } else {
      alert(`副本《${clone.name}》已创建。`);
      await loadScenes();
    }
  } catch (e) { alert("克隆失败：" + e.message); }
}

/* ------------------------------------------------------------ checkpoints */
async function checkpoint() {
  const name = prompt("命名快照（会被永久保留，不参与自动清理）：", "");
  if (name === null) return;
  try {
    await post(`/api/scenes/${curId}/checkpoint`, { name: name || undefined });
    await afterHistoryChange("已保存命名快照");
  } catch (e) { alert("命名快照失败：" + e.message); }
}

async function pruneNow() {
  const keep = prompt("每个场景自动保留最近多少个普通快照？（创建/克隆/回滚/命名快照不受影响）",
                      String(stats.auto_keep || 50));
  if (keep === null) return;
  const r = await post(`/api/scenes/${curId}/prune`, { keep: Number(keep) || 50 });
  alert(`清理完成：移除 ${r.removed} 个旧快照，回收 ${r.deleted_blobs} 个内容块，保留 ${r.kept} 个版本。`);
  offset = 0;
  await Promise.all([loadScenes(), loadPage(true)]);
}

async function afterHistoryChange(msg) {
  offset = 0;
  await Promise.all([loadScenes(), loadPage(true)]);
  showFlash(msg);
}

function showFlash(text) {
  const div = document.createElement("div");
  div.className = "notice";
  div.textContent = text + " ✓";
  div.style.cssText = "position:fixed;right:20px;bottom:20px;z-index:100;border-color:#2e5b3a;color:#2ecc71";
  document.body.appendChild(div);
  setTimeout(() => div.remove(), 2600);
}

/* ------------------------------------------------------------------- init */
function init() {
  el("sceneSearch").oninput = renderSceneList;
  el("olderBtn").onclick = async () => {
    offset += PAGE;
    await loadPage(false);
  };
  el("diffBtn").onclick = () => {
    selected.a = el("diffA").value || null;
    selected.b = el("diffB").value || null;
    markAB();
    runDiff();
  };
  el("cloneHeadBtn").onclick = () => cloneScene(curId, null);
  el("checkpointBtn").onclick = checkpoint;
  el("pruneBtn").onclick = pruneNow;

  loadScenes().then(() => {
    const qs = new URLSearchParams(window.location.search).get("scene");
    if (qs && scenes.some((s) => s.id === qs)) selectScene(qs);
  }).catch((e) => alert("加载失败：" + e.message));
}

init();
