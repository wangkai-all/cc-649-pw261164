"use strict";

const state = {
  versions: [],
  activeVersion: null,
  building: false,
  maskLabels: {},
  allMasks: [],
  selectedMask: 7,
  drillCell: null,
};

// ---------- 工具 ----------
const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtMoney(cents) {
  if (cents === null || cents === undefined) return "—";
  return (cents / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isError ? " error" : "");
  setTimeout(() => { t.className = ""; }, 3200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

async function mutate(fn, okMsg) {
  try {
    await fn();
    if (okMsg) toast(okMsg);
    await refreshAll();
  } catch (e) {
    toast(e.status === 409 ? `冲突：${e.message}` : `失败：${e.message}`, true);
  }
}

// ---------- 维度值渲染：三种格子严格区分 ----------
function dimHtml(value, grouped) {
  if (grouped) return '<span class="tag grouped">（全部）</span>';
  if (value === null) return '<span class="tag null">∅ 空值</span>';
  if (value === "全部") return '<span class="tag literal" title="真实维度值，不是汇总标记">全部</span>';
  return esc(value);
}

function dimText(value, grouped) {
  if (grouped) return "（全部）";
  if (value === null) return "∅ 空值";
  return value;
}

// ---------- 加载与渲染 ----------
async function loadState() {
  const s = await api("/api/state");
  Object.assign(state, s);
  const active = s.versions.find((v) => v.id === s.active_version);
  $("#active-version").textContent = active
    ? `活动版本 v${active.id} · ${active.masks.length} 个分组集`
    : "无活动版本";
  $("#active-version").className = "pill" + (active ? "" : " busy");
  $("#build-status").textContent = s.building ? "构建中：写操作暂禁" : "空闲";
  $("#build-status").className = "pill" + (s.building ? " busy" : " muted");
  $("#sales-total").textContent = `销售 ${s.sales_total} 条`;
  $("#sales-total").className = "pill muted";
  renderVersions();
  renderMaskChecks();
  renderMaskTabs();
}

function renderVersions() {
  const box = $("#version-list");
  box.innerHTML = state.versions.map((v) => {
    const masks = v.masks.map((m) => state.maskLabels[m] || `mask=${m}`).join("；");
    let progress = "";
    if (v.status === "building" && v.total_sales) {
      const pct = Math.min(100, Math.round((v.last_sale_id / v.total_sales) * 100));
      progress = `<progress value="${v.last_sale_id}" max="${v.total_sales}"></progress> ${pct}%`;
    }
    const actions = [];
    if (v.status === "ready") {
      actions.push(`<button class="primary" data-act="activate" data-id="${v.id}">切换到此版本</button>`);
    }
    if (v.status === "superseded" || v.status === "ready") {
      actions.push(`<button class="danger" data-act="delver" data-id="${v.id}">删除</button>`);
    }
    const note = v.status === "superseded" ? '<span class="hint">已冻结，不再接收新数据</span>' : "";
    return `<div class="version-item ${v.status}">
      <span class="name">v${v.id} ${esc(v.name)}</span>
      <span class="status-badge ${v.status}">${v.status}</span>
      ${progress}
      <span class="hint">${esc(masks)}</span>
      ${note}
      <span>${actions.join(" ")}</span>
    </div>`;
  }).join("");
  box.querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      const id = b.dataset.id;
      if (b.dataset.act === "activate") {
        mutate(() => api(`/api/versions/${id}/activate`, { method: "POST" }), `已切换到 v${id}`);
      } else {
        mutate(() => api(`/api/versions/${id}`, { method: "DELETE" }), `已删除 v${id}`);
      }
    };
  });
}

function renderMaskChecks() {
  const box = $("#mask-checks");
  // 保留用户已勾选项（轮询刷新不打断选择）；首次渲染时默认勾选活动版本的分组集
  const prev = [...box.querySelectorAll("input:checked")].map((i) => Number(i.value));
  const activeMasks = (state.versions.find((v) => v.id === state.activeVersion) || {}).masks || state.allMasks;
  const checked = box.dataset.init ? prev : activeMasks;
  box.dataset.init = "1";
  box.innerHTML = state.allMasks.map((m) => `
    <label><input type="checkbox" value="${m}" ${checked.includes(m) ? "checked" : ""}>
    ${esc(state.maskLabels[m])}</label>`).join("");
}

function renderMaskTabs() {
  const active = state.versions.find((v) => v.id === state.activeVersion);
  const tabs = $("#mask-tabs");
  if (!active) {
    tabs.innerHTML = '<span class="hint">暂无活动版本</span>';
    return;
  }
  if (!active.masks.includes(state.selectedMask)) {
    state.selectedMask = active.masks[active.masks.length - 1];
  }
  tabs.innerHTML = active.masks.map((m) =>
    `<button data-mask="${m}" class="${m === state.selectedMask ? "active" : ""}">${esc(state.maskLabels[m])}</button>`
  ).join("");
  tabs.querySelectorAll("button").forEach((b) => {
    b.onclick = () => { state.selectedMask = Number(b.dataset.mask); renderMaskTabs(); loadCells(); };
  });
  $("#cube-version-note").textContent = `（活动版本 v${active.id}，点击行可下钻）`;
}

async function loadCells() {
  const tbody = $("#cube-table tbody");
  const data = await api(`/api/cells?mask=${state.selectedMask}`);
  if (!data.version_id) {
    tbody.innerHTML = '<tr><td colspan="8" class="hint">暂无数据</td></tr>';
    return;
  }
  tbody.innerHTML = data.cells.map((c, i) => {
    const g = c.grouping;
    return `<tr data-i="${i}">
      <td>${dimHtml(c.region, g.region === 1)}</td>
      <td>${dimHtml(c.channel, g.channel === 1)}</td>
      <td>${dimHtml(c.category, g.category === 1)}</td>
      <td class="num">${c.sale_count}</td>
      <td class="num">${c.amount_count}</td>
      <td class="num">${fmtMoney(c.sum_cents)}</td>
      <td class="num">${c.avg_cents === null ? "—" : fmtMoney(Math.round(c.avg_cents))}</td>
      <td><button data-drill="${i}">下钻</button></td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll("button[data-drill]").forEach((b) => {
    b.onclick = () => drill(data.cells[Number(b.dataset.drill)]);
  });
}

async function drill(cell) {
  state.drillCell = cell;
  const g = cell.grouping;
  const descParts = ["region", "channel", "category"].map((d) => {
    const label = { region: "地区", channel: "渠道", category: "类别" }[d];
    return `${label}=${g[d] === 1 ? "（全部，不限制）" : dimText(cell[d], false)}`;
  });
  const res = await api("/api/drilldown", {
    method: "POST",
    body: JSON.stringify({
      mask: cell.mask, region: cell.region, channel: cell.channel, category: cell.category,
    }),
  });
  $("#drill-desc").innerHTML =
    `分组集 <b>${esc(state.maskLabels[cell.mask])}</b>；条件：${descParts.join("，")} → ` +
    `<b>${res.count}</b> 条来源记录（格子销售笔数=${cell.sale_count}）`;
  $("#drill-table tbody").innerHTML = res.rows.map((r) => `<tr>
    <td class="num">${r.id}</td>
    <td>${dimHtml(r.region, false)}</td>
    <td>${dimHtml(r.channel, false)}</td>
    <td>${dimHtml(r.category, false)}</td>
    <td class="num">${fmtMoney(r.amount_cents)}</td>
    <td>${esc(r.updated_at)}</td>
  </tr>`).join("");
}

// ---------- 销售明细 ----------
async function loadSales() {
  const data = await api("/api/sales?limit=500");
  const tbody = $("#sales-table tbody");
  tbody.innerHTML = data.rows.map((r) => saleRowHtml(r)).join("");
  tbody.querySelectorAll("button").forEach((b) => {
    const id = Number(b.dataset.id);
    b.onclick = () => onSaleAction(b.dataset.act, id, b);
  });
}

function saleRowHtml(r) {
  const cls = r.revoked ? ' class="revoked"' : "";
  return `<tr${cls} data-id="${r.id}">
    <td class="num">${r.id}</td>
    <td>${dimHtml(r.region, false)}</td>
    <td>${dimHtml(r.channel, false)}</td>
    <td>${dimHtml(r.category, false)}</td>
    <td class="num">${fmtMoney(r.amount_cents)}</td>
    <td>${r.revoked ? "已撤销" : "正常"}</td>
    <td>
      <button data-act="edit" data-id="${r.id}">编辑</button>
      <button data-act="${r.revoked ? "unrevoke" : "revoke"}" data-id="${r.id}">${r.revoked ? "恢复" : "撤销"}</button>
      <button data-act="del" data-id="${r.id}" class="danger">删除</button>
    </td>
  </tr>`;
}

function parseDimInput(v) { return v.trim() === "" ? null : v.trim(); }

function parseAmountInput(v) {
  v = v.trim();
  if (v === "") return null;
  const n = Number(v);
  if (!Number.isFinite(n)) throw new Error("金额必须是数字");
  return Math.round(n * 100);
}

function onSaleAction(act, id, btn) {
  if (act === "revoke" || act === "unrevoke") {
    mutate(() => api(`/api/sales/${id}/${act}`, { method: "POST" }),
      act === "revoke" ? `已撤销 #${id}` : `已恢复 #${id}`);
  } else if (act === "del") {
    if (!confirm(`确认删除销售 #${id}？（硬删除，所有粒度同步扣减）`)) return;
    mutate(() => api(`/api/sales/${id}`, { method: "DELETE" }), `已删除 #${id}`);
  } else if (act === "edit") {
    enterEdit(id, btn.closest("tr"));
  }
}

function enterEdit(id, tr) {
  api("/api/sales?limit=2000").then((data) => {
    const r = data.rows.find((x) => x.id === id);
    if (!r) return;
    tr.innerHTML = `
      <td class="num">${r.id}</td>
      <td><input id="e-region" value="${esc(r.region ?? "")}" placeholder="留空=NULL"></td>
      <td><input id="e-channel" value="${esc(r.channel ?? "")}" placeholder="留空=NULL"></td>
      <td><input id="e-category" value="${esc(r.category ?? "")}" placeholder="留空=NULL"></td>
      <td><input id="e-amount" value="${r.amount_cents === null ? "" : (r.amount_cents / 100)}" placeholder="留空=NULL"></td>
      <td>${r.revoked ? "已撤销" : "正常"}</td>
      <td>
        <button class="primary" id="e-save">保存</button>
        <button id="e-cancel">取消</button>
      </td>`;
    tr.querySelector("#e-save").onclick = () => {
      let fields;
      try {
        fields = {
          region: parseDimInput(tr.querySelector("#e-region").value),
          channel: parseDimInput(tr.querySelector("#e-channel").value),
          category: parseDimInput(tr.querySelector("#e-category").value),
          amount_cents: parseAmountInput(tr.querySelector("#e-amount").value),
        };
      } catch (e) { toast(e.message, true); return; }
      mutate(() => api(`/api/sales/${id}`, { method: "PUT", body: JSON.stringify(fields) }),
        `已更新 #${id}，所有受影响粒度已同步`);
    };
    tr.querySelector("#e-cancel").onclick = () => loadSales();
  });
}

// ---------- 核对 ----------
async function verify() {
  const box = $("#verify-result");
  box.innerHTML = '<span class="hint">核对中…</span>';
  try {
    const r = await api("/api/verify", { method: "POST", body: "{}" });
    if (r.ok) {
      box.innerHTML = `<div class="verify-ok">✔ 版本 v${r.version_id} 与同快照分组查询完全一致：` +
        `${r.actual_cells} 个格子全部匹配（销售笔数 / 非空金额笔数 / 精确合计）。</div>`;
    } else {
      const fmt = (o) => esc(JSON.stringify(o, null, 1)).slice(0, 1200);
      box.innerHTML = `<div class="verify-bad">✘ 版本 v${r.version_id} 不一致：` +
        `缺失 ${Object.keys(r.missing).length}，多出 ${Object.keys(r.extra).length}，` +
        `不符 ${Object.keys(r.mismatch).length}\n` +
        `missing: ${fmt(r.missing)}\nextra: ${fmt(r.extra)}\nmismatch: ${fmt(r.mismatch)}</div>`;
    }
  } catch (e) {
    box.innerHTML = `<div class="verify-bad">核对失败：${esc(e.message)}</div>`;
  }
}

// ---------- 刷新 ----------
async function refreshAll() {
  await loadState();
  await Promise.all([loadCells(), loadSales()]);
}

let pollTimer = null;
function setupPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (state.building) await refreshAll();
  }, 1500);
}

// ---------- 启动 ----------
window.addEventListener("DOMContentLoaded", async () => {
  $("#add-form").onsubmit = (e) => {
    e.preventDefault();
    const f = e.target;
    let body;
    try {
      body = JSON.stringify({
        region: parseDimInput(f.region.value),
        channel: parseDimInput(f.channel.value),
        category: parseDimInput(f.category.value),
        amount_cents: parseAmountInput(f.amount.value),
      });
    } catch (err) { toast(err.message, true); return; }
    mutate(async () => {
      await api("/api/sales", { method: "POST", body });
      f.reset();
    }, "已新增销售并同步所有粒度");
  };
  $("#btn-build").onclick = () => {
    const masks = [...document.querySelectorAll("#mask-checks input:checked")]
      .map((i) => Number(i.value));
    if (!masks.length) { toast("至少选择一个分组集", true); return; }
    mutate(() => api("/api/versions", {
      method: "POST",
      body: JSON.stringify({ name: `自定义（${masks.length} 个分组集）`, masks }),
    }), "已开始构建新版本");
  };
  $("#btn-verify").onclick = verify;
  await refreshAll();
  setupPolling();
});
