"use strict";

/* ==========================================================================
   TQ 卡密管理后台
   - 管理密钥只存 sessionStorage，所有请求带 X-Admin-Key
   - 所有用户数据只用 textContent 写入，不拼 innerHTML
   ========================================================================== */

const KEY_STORAGE = "ck_admin_key";
const THEME_STORAGE = "ck_theme";

const state = {
  key: "",
  view: "",
  products: [],
  generatedCards: [],
  generatedBatch: null,
  resultDirty: false,
  serverOffsetMs: 0,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ 工具 */

function el(tag, props, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "className") node.className = value;
    else if (key === "value") node.value = value;
    else if (key === "checked") { if (value) node.checked = true; }
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) { node.replaceChildren(); }

function td(text, className) { return el("td", { className, text }); }

function parseUtc(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return new Date(value * 1000);
  const text = String(value);
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(text) ? text : text + "Z");
}

function fmtTime(value) {
  const date = parseUtc(value);
  if (!date || Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function fmtNum(value) { return Number(value || 0).toLocaleString("zh-CN"); }

function fmtDuration(days) { return Number(days) > 0 ? Number(days) + " 天" : "永久"; }

function statusBadge(status) {
  const map = {
    0: ["badge-ok", "未使用"],
    1: ["badge-used", "已使用"],
    2: ["badge-danger", "已作废"],
  };
  const [cls, text] = map[status] || ["", "未知"];
  return el("span", { className: "badge " + cls, text });
}

/* ------------------------------------------------------------------ 主题 */

function applyTheme(value) {
  const theme = value === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_STORAGE, theme);
  const toggle = $("theme-toggle");
  if (toggle) toggle.textContent = theme === "dark" ? "浅色" : "深色";
  const select = $("s-theme");
  if (select) select.value = theme;
}

function initTheme() {
  const saved = localStorage.getItem(THEME_STORAGE);
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
}

/* ------------------------------------------------------------------ 提示 */

function toast(message, kind = "info", timeout = 4200) {
  const node = el("div", { className: "toast " + kind, text: message });
  const box = $("toasts");
  box.append(node);
  setTimeout(() => node.remove(), timeout);
}

function confirmDialog(message, title = "请确认") {
  return new Promise((resolve) => {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-message").textContent = message;
    if (dialog.open) dialog.close();
    dialog.returnValue = "";
    const onClose = () => {
      dialog.removeEventListener("close", onClose);
      resolve(dialog.returnValue === "ok");
    };
    dialog.addEventListener("close", onClose);
    dialog.showModal();
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制", "success", 1800);
    return;
  } catch (error) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "sr-only";
    document.body.append(area);
    area.select();
    try {
      document.execCommand("copy");
      toast("已复制", "success", 1800);
    } catch (fallbackError) {
      toast("复制失败，请手动选择", "error");
    }
    area.remove();
  }
}

/* ------------------------------------------------------------------ API */

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.key) headers.set("X-Admin-Key", state.key);
  let body = options.body;
  if (body && typeof body !== "string") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    body,
    cache: "no-store",
  });
  if (response.status === 401 && state.key) {
    logout("管理密钥已失效，请重新登录。");
    throw new Error("__UNAUTHORIZED__");
  }
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const detail = data && (data.msg || data.detail);
    const message = detail
      ? (typeof detail === "string" ? detail : JSON.stringify(detail))
      : "HTTP " + response.status;
    throw new Error(message);
  }
  return data;
}

function triggerDownload(blob, name) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function downloadCsv(path, fallbackName) {
  const response = await fetch(path, {
    headers: { "X-Admin-Key": state.key },
    cache: "no-store",
  });
  if (response.status === 401) {
    logout("管理密钥已失效，请重新登录。");
    throw new Error("__UNAUTHORIZED__");
  }
  if (!response.ok) {
    let message = "下载失败（HTTP " + response.status + "）";
    try {
      const data = await response.json();
      if (data && data.msg) message = data.msg;
    } catch (error) { /* 非 JSON */ }
    throw new Error(message);
  }
  const disposition = response.headers.get("content-disposition") || "";
  const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const name = match ? decodeURIComponent(match[1]) : fallbackName || "export.csv";
  triggerDownload(await response.blob(), name);
}

/* ------------------------------------------------------------------ 登录 */

function logout(message) {
  state.key = "";
  sessionStorage.removeItem(KEY_STORAGE);
  clearResult();
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  const status = $("key-status");
  if (status) status.textContent = "";
  location.hash = "#/login";
  if (message) toast(message, "info", 5000);
}

async function tryLogin(key) {
  const response = await fetch("/admin/stats", {
    headers: { "X-Admin-Key": key },
    cache: "no-store",
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) return { ok: false, message: "管理密钥无效" };
  if (!response.ok) {
    return { ok: false, message: data.msg || data.detail || "HTTP " + response.status };
  }
  return { ok: true, data };
}

/* ------------------------------------------------------------------ 路由 */

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "") || "overview";
  const [nameRaw, queryRaw] = raw.split("?");
  const name = nameRaw === "batches/new" ? "new" : (nameRaw || "overview");
  return { name, params: new URLSearchParams(queryRaw || "") };
}

function setHash(name, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value !== null && value !== undefined) {
      query.set(key, value);
    }
  }
  const suffix = query.toString();
  location.hash = "#/" + name + (suffix ? "?" + suffix : "");
}

async function route() {
  const { name, params } = parseHash();

  if (!state.key && name !== "login") {
    location.hash = "#/login";
    return;
  }
  if (state.key && name === "login") {
    location.hash = "#/overview";
    return;
  }

  state.view = name;
  $("login-view").hidden = name !== "login";
  $("app-view").hidden = name === "login";

  document.querySelectorAll(".sidebar a").forEach((link) => {
    link.setAttribute("aria-current", link.dataset.nav === name ? "page" : "false");
  });
  document.querySelectorAll(".main > section").forEach((section) => {
    section.hidden = true;
  });

  const section = $("view-" + name);
  if (!section) {
    location.hash = "#/overview";
    return;
  }
  section.hidden = false;

  try {
    if (name === "overview") await showOverview();
    else if (name === "batches") await showBatches(params);
    else if (name === "new") await showGenerate();
    else if (name === "cards") await showCards(params);
    else if (name === "logs") await showLogs(params);
    else if (name === "settings") showSettings();
  } catch (error) {
    if (error && error.message === "__UNAUTHORIZED__") return;
    toast(error.message || "加载失败", "error", 6000);
  }
}

/* ------------------------------------------------------------------ 概览 */

function statCard(label, value, tone) {
  return el("div", { className: "stat" + (tone ? " tone-" + tone : "") }, [
    el("div", { className: "stat-label", text: label }),
    el("div", { className: "stat-value", text: fmtNum(value) }),
  ]);
}

async function showOverview() {
  const [stats, products, logs] = await Promise.all([
    api("/admin/stats"),
    api("/admin/products"),
    api("/admin/logs?success=0&size=10"),
  ]);

  const data = stats.data;
  state.serverOffsetMs = data.server_time * 1000 - Date.now();

  const grid = $("stat-grid");
  clear(grid);
  grid.append(
    statCard("卡密总数", data.cards_total),
    statCard("未使用", data.unused, "ok"),
    statCard("已使用", data.used, "used"),
    statCard("已作废", data.disabled, "danger"),
    statCard("已过期", data.expired, "warn"),
    statCard("7 天内到期", data.expiring_7d, "warn"),
    statCard("核销失败", data.redeem_failed, "danger"),
    statCard("校验失败", data.verify_failed, "danger"),
  );

  const productBody = $("product-body");
  clear(productBody);
  if (!products.data.items.length) {
    productBody.append(el("tr", {}, [
      el("td", { colspan: "6", className: "empty-cell", text: "暂无数据" }),
    ]));
  }
  for (const item of products.data.items) {
    productBody.append(el("tr", {}, [
      td(item.product),
      td(fmtNum(item.total), "num"),
      td(fmtNum(item.unused), "num"),
      td(fmtNum(item.used), "num"),
      td(fmtNum(item.disabled), "num"),
      td(fmtNum(item.expired), "num"),
    ]));
  }

  renderRecentLogs(logs.data.items);
}

function renderRecentLogs(items) {
  const body = $("recent-log-body");
  clear(body);
  if (!items.length) {
    body.append(el("tr", {}, [
      el("td", { colspan: "5", className: "empty-cell", text: "暂无失败记录" }),
    ]));
    return;
  }
  for (const item of items) {
    body.append(el("tr", {}, [
      td(fmtTime(item.created_at), "muted"),
      td(item.card_no, "mono"),
      td(item.reason || "—"),
      td(item.ip || "—", "mono"),
      td(item.device_id || "—", "mono"),
    ]));
  }
}

/* ------------------------------------------------------------------ 批次 */

async function showBatches(params) {
  const page = Math.max(1, Number(params.get("page")) || 1);
  const requestedSize = Number(params.get("size"));
  const size = [50, 100, 200].includes(requestedSize) ? requestedSize : 50;

  const data = await api(`/admin/batches?page=${page}&size=${size}`);
  const body = $("batch-body");
  clear(body);

  if (!data.data.items.length) {
    body.append(el("tr", {}, [
      el("td", { colspan: "11", className: "empty-cell", text: "暂无批次，点击「生成批次」开始" }),
    ]));
  }

  for (const item of data.data.items) {
    body.append(el("tr", {}, [
      el("td", { className: "mono" }, el("a", {
        href: "#/cards?batch=" + encodeURIComponent(item.batch_no),
        text: item.batch_no,
      })),
      td(item.product),
      td(item.note || "—"),
      td(fmtNum(item.total), "num"),
      td(fmtNum(item.unused), "num"),
      td(fmtNum(item.used), "num"),
      td(fmtNum(item.disabled), "num"),
      td(fmtNum(item.expired), "num"),
      td(fmtDuration(item.duration_days)),
      td(fmtTime(item.created_at), "muted"),
      el("td", { className: "actions" }, [
        el("a", {
          className: "btn btn-sm",
          href: "#/cards?batch=" + encodeURIComponent(item.batch_no),
          text: "卡密",
        }),
        " ",
        exportButton("下载", item.batch_no, false),
        " ",
        exportButton("含密码", item.batch_no, true),
      ]),
    ]));
  }

  renderPager($("batch-pager"), data.data.total, page, size, (nextPage, nextSize) => {
    setHash("batches", { page: nextPage, size: nextSize });
  });
}

function exportButton(label, batchNo, includePwd) {
  const button = el("button", { className: "btn btn-sm", type: "button", text: label });
  button.addEventListener("click", async () => {
    if (includePwd && !(await confirmDialog("将导出明文卡密密码，确定继续？"))) return;
    button.disabled = true;
    try {
      const path = "/admin/batches/" + encodeURIComponent(batchNo)
        + "/export?include_pwd=" + (includePwd ? "true" : "false");
      await downloadCsv(path, batchNo + ".csv");
    } catch (error) {
      if (error.message !== "__UNAUTHORIZED__") toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

/* ------------------------------------------------------------------ 分页 */

function renderPager(container, total, page, size, onChange) {
  clear(container);
  const pages = Math.max(1, Math.ceil(total / size));

  container.append(el("span", {
    className: "page-info",
    text: `共 ${fmtNum(total)} 条 · 第 ${page}/${pages} 页`,
  }));

  const sizeSelect = el("select", { className: "btn btn-sm", "aria-label": "每页条数" },
    [50, 100, 200].map((n) => el("option", {
      value: n,
      text: n + " 条/页",
      selected: n === size,
    })));
  sizeSelect.addEventListener("change", () => onChange(1, Number(sizeSelect.value)));
  container.append(sizeSelect);

  const previous = el("button", {
    className: "btn btn-sm", type: "button", text: "上一页", disabled: page <= 1,
  });
  previous.addEventListener("click", () => onChange(page - 1, size));
  container.append(previous);

  const next = el("button", {
    className: "btn btn-sm", type: "button", text: "下一页", disabled: page >= pages,
  });
  next.addEventListener("click", () => onChange(page + 1, size));
  container.append(next);
}

/* ------------------------------------------------------------------ 生成 */

async function showGenerate() {
  const products = await api("/admin/products");
  state.products = products.data.items;
  const list = $("product-list");
  clear(list);
  for (const item of state.products) {
    list.append(el("option", { value: item.product }));
  }
}

function bindGenerateForm() {
  $("g-with-pwd").addEventListener("change", () => {
    $("g-pwd-field").hidden = !$("g-with-pwd").checked;
  });

  $("g-count").addEventListener("input", () => {
    $("g-hint").hidden = !(Number($("g-count").value) > 5000);
  });

  $("generate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $("generate-error");
    error.textContent = "";

    const product = $("g-product").value.trim();
    const count = Number($("g-count").value);
    const days = Number($("g-days").value);
    const withPwd = $("g-with-pwd").checked;
    const pwdLength = Number($("g-pwd-length").value);

    if (!product) { error.textContent = "请填写产品"; return; }
    if (!Number.isInteger(count) || count < 1 || count > 100000) {
      error.textContent = "数量需为 1–100000 的整数"; return;
    }
    if (!Number.isInteger(days) || days < 0 || days > 3650) {
      error.textContent = "时长需为 0–3650 天"; return;
    }
    if (withPwd && (!Number.isInteger(pwdLength) || pwdLength < 4 || pwdLength > 32)) {
      error.textContent = "密码长度需为 4–32"; return;
    }

    const button = event.submitter || $("generate-form").querySelector("button[type=submit]");
    button.disabled = true;
    button.textContent = "生成中…";
    try {
      const data = await api("/admin/batches", {
        method: "POST",
        body: {
          product,
          count,
          note: $("g-note").value.trim(),
          duration_days: days,
          with_pwd: withPwd,
          pwd_length: withPwd ? pwdLength : 8,
        },
      });
      showResult(data.data);
    } catch (requestError) {
      if (requestError.message !== "__UNAUTHORIZED__") {
        error.textContent = requestError.message;
      }
    } finally {
      button.disabled = false;
      button.textContent = "生成";
    }
  });
}

function showResult(batch) {
  state.generatedCards = batch.cards || [];
  state.generatedBatch = batch;
  state.resultDirty = true;

  $("result-summary").textContent =
    `批次 ${batch.batch_no} · 产品 ${batch.product} · 共 ${batch.total} 张 · 有效期 ${fmtDuration(batch.duration_days)}`;
  $("result-text").value = state.generatedCards
    .map((card) => card.card_pwd ? `${card.card_no}  ${card.card_pwd}` : card.card_no)
    .join("\n");

  const dialog = $("result-dialog");
  if (dialog.open) dialog.close();
  dialog.showModal();
}

function resultCsv() {
  const withPwd = state.generatedCards.some((card) => card.card_pwd);
  const header = withPwd ? ["card_no", "card_pwd", "duration_days"] : ["card_no", "duration_days"];
  const lines = [header.join(",")];
  for (const card of state.generatedCards) {
    const row = withPwd
      ? [card.card_no, card.card_pwd, card.duration_days]
      : [card.card_no, card.duration_days];
    lines.push(row.map(csvCell).join(","));
  }
  return "﻿" + lines.join("\r\n") + "\r\n";
}

function csvCell(value) {
  const text = value === null || value === undefined ? "" : String(value);
  if (/[",\r\n]/.test(text)) return '"' + text.replace(/"/g, '""') + '"';
  return text;
}

function clearResult() {
  state.generatedCards = [];
  state.generatedBatch = null;
  state.resultDirty = false;
  const dialog = $("result-dialog");
  if (dialog && dialog.open) dialog.close();
  const area = $("result-text");
  if (area) area.value = "";
}

async function closeResult() {
  if (state.generatedCards.length
      && !(await confirmDialog("关闭后明文卡密将无法再次查看，确定已经保存？"))) {
    return;
  }
  clearResult();
}

/* ------------------------------------------------------------------ 卡密 */

async function loadProductsForFilter() {
  const select = $("f-product");
  const current = select.value;
  const data = await api("/admin/products");
  clear(select);
  select.append(el("option", { value: "", text: "全部产品" }));
  for (const item of data.data.items) {
    select.append(el("option", { value: item.product, text: item.product }));
  }
  select.value = current;
}

function cardFilters() {
  return {
    q: $("f-card-no").value.trim(),
    batch: $("f-batch").value.trim(),
    product: $("f-product").value,
    status: $("f-status").value,
  };
}

async function showCards(params) {
  const page = Math.max(1, Number(params.get("page")) || 1);
  const requestedSize = Number(params.get("size"));
  const size = [50, 100, 200].includes(requestedSize) ? requestedSize : 50;

  const filters = {
    q: params.get("q") || "",
    batch: params.get("batch") || "",
    product: params.get("product") || "",
    status: params.get("status") || "",
  };

  await loadProductsForFilter();
  $("f-card-no").value = filters.q;
  $("f-batch").value = filters.batch;
  $("f-product").value = filters.product;
  $("f-status").value = filters.status;

  const query = new URLSearchParams({ page, size });
  if (filters.q) query.set("card_no", filters.q);
  if (filters.batch) query.set("batch_no", filters.batch);
  if (filters.product) query.set("product", filters.product);
  if (filters.status) query.set("status", filters.status);

  const data = await api("/admin/cards?" + query.toString());
  renderCards(data.data.items);
  renderPager($("card-pager"), data.data.total, page, size, (nextPage, nextSize) => {
    setHash("cards", { ...cardFilters(), page: nextPage, size: nextSize });
  });
}

function renderCards(items) {
  const body = $("card-body");
  clear(body);

  if (!items.length) {
    body.append(el("tr", {}, [
      el("td", { colspan: "9", className: "empty-cell", text: "没有匹配的卡密" }),
    ]));
    return;
  }

  for (const card of items) {
    const actions = [];
    const reveal = el("button", { className: "btn btn-sm", type: "button", text: "查看" });
    reveal.addEventListener("click", () => revealCard(card.id));
    actions.push(reveal);

    if (card.status === 0) {
      const disable = el("button", { className: "btn btn-sm btn-danger", type: "button", text: "作废" });
      disable.addEventListener("click", () => mutateCard("disable", card.id));
      actions.push(" ", disable);
    }
    if (card.status !== 2) {
      const revoke = el("button", { className: "btn btn-sm btn-danger", type: "button", text: "撤销" });
      revoke.addEventListener("click", () => mutateCard("revoke", card.id));
      actions.push(" ", revoke);
    }

    body.append(el("tr", {}, [
      td(card.card_no, "mono"),
      td(card.batch_no || "—", "mono"),
      td(card.product),
      el("td", {}, statusBadge(card.status)),
      td(fmtDuration(card.duration_days)),
      expiryCell(card),
      td(card.used_at ? fmtTime(card.used_at) : "—", "muted"),
      td(card.used_by || "—", "mono"),
      el("td", { className: "actions" }, actions),
    ]));
  }
}

function expiryCell(card) {
  if (card.expires_at === null || card.expires_at === undefined) {
    return td("永久", "muted");
  }
  const text = fmtTime(card.expires_at);
  if (card.is_expired) {
    return el("td", {}, el("span", { className: "badge badge-danger", text: text + "（已过期）" }));
  }
  return td(text, "muted");
}

async function revealCard(cardId) {
  if (!(await confirmDialog("将显示该卡密的明文，确定继续？"))) return;
  const data = await api(`/admin/cards/by-id/${cardId}/reveal`, { method: "POST" });
  $("reveal-card").value = data.data.card_no;
  $("reveal-pwd").value = data.data.card_pwd || "";
  $("reveal-pwd-field").hidden = !data.data.card_pwd;
  const dialog = $("reveal-dialog");
  if (dialog.open) dialog.close();
  dialog.showModal();
}

async function mutateCard(action, cardId) {
  const label = action === "disable" ? "作废" : "撤销";
  const extra = action === "disable"
    ? "作废后该卡无法再激活。"
    : "撤销后已激活的客户端下次校验即失效。";
  if (!(await confirmDialog(`确定${label}该卡密？${extra}`))) return;

  try {
    await api(`/admin/cards/by-id/${cardId}/${action}`, { method: "POST" });
    toast(`已${label}`, "success");
    await route();
  } catch (error) {
    if (error.message !== "__UNAUTHORIZED__") toast(error.message, "error", 6000);
  }
}

/* ------------------------------------------------------------------ 日志 */

async function showLogs(params) {
  const page = Math.max(1, Number(params.get("page")) || 1);
  const requestedSize = Number(params.get("size"));
  const size = [50, 100, 200].includes(requestedSize) ? requestedSize : 50;

  const filters = {
    ok: params.get("ok") || "",
    prefix: params.get("prefix") || "",
    card: params.get("card") || "",
  };

  $("l-ok").value = filters.ok;
  $("l-prefix").value = filters.prefix;
  $("l-card").value = filters.card;

  const query = new URLSearchParams({ page, size });
  if (filters.ok !== "") query.set("success", filters.ok);
  if (filters.prefix) query.set("reason_prefix", filters.prefix);
  if (filters.card) query.set("card_no", filters.card);

  const data = await api("/admin/logs?" + query.toString());
  renderLogs(data.data.items);
  renderPager($("log-pager"), data.data.total, page, size, (nextPage, nextSize) => {
    setHash("logs", {
      ok: $("l-ok").value,
      prefix: $("l-prefix").value,
      card: $("l-card").value.trim(),
      page: nextPage,
      size: nextSize,
    });
  });
}

function renderLogs(items) {
  const body = $("log-body");
  clear(body);

  if (!items.length) {
    body.append(el("tr", {}, [
      el("td", { colspan: "7", className: "empty-cell", text: "暂无日志" }),
    ]));
    return;
  }

  for (const item of items) {
    const isVerify = String(item.reason || "").startsWith("verify:");
    body.append(el("tr", {}, [
      td(fmtTime(item.created_at), "muted"),
      td(item.card_no, "mono"),
      td(isVerify ? "校验" : "核销"),
      el("td", {}, el("span", {
        className: "badge " + (item.success ? "badge-ok" : "badge-danger"),
        text: item.success ? "成功" : "失败",
      })),
      td(item.reason || "—"),
      td(item.ip || "—", "mono"),
      td(item.device_id || "—", "mono"),
    ]));
  }
}

/* ------------------------------------------------------------------ 设置 */

function showSettings() {
  $("s-theme").value = document.documentElement.dataset.theme;
  $("s-key-state").textContent = state.key ? "已登录（当前标签页）" : "未登录";
}

/* ------------------------------------------------------------------ 启动 */

function bindNavigationGuard() {
  const sidebar = document.querySelector(".sidebar");
  sidebar.addEventListener("click", async (event) => {
    const link = event.target.closest("a[href^='#/']");
    if (!link || !state.resultDirty) return;
    event.preventDefault();
    if (await confirmDialog("尚未保存明文卡密，确定离开？")) {
      clearResult();
      location.hash = link.getAttribute("href");
    }
  });
}

function bindGlobal() {
  $("theme-toggle").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
  $("s-theme").addEventListener("change", () => applyTheme($("s-theme").value));

  $("logout").addEventListener("click", () => logout("已退出登录"));
  $("s-logout").addEventListener("click", () => logout("已退出登录"));

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("admin-key");
    const key = input.value.trim();
    const error = $("login-error");
    error.textContent = "";
    if (!key) { error.textContent = "请输入管理密钥"; return; }

    const button = event.submitter || $("login-form").querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const result = await tryLogin(key);
      if (!result.ok) {
        error.textContent = result.message;
        input.select();
        return;
      }
      state.key = key;
      sessionStorage.setItem(KEY_STORAGE, key);
      input.value = "";
      location.hash = "#/overview";
    } catch (requestError) {
      error.textContent = "无法连接服务端，请检查网络后重试";
    } finally {
      button.disabled = false;
    }
  });

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $(button.dataset.copy);
      if (input) copyText(input.value);
    });
  });

  bindGenerateForm();
  bindNavigationGuard();

  $("card-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    setHash("cards", { ...cardFilters(), page: 1 });
  });
  $("f-reset").addEventListener("click", () => {
    $("f-card-no").value = "";
    $("f-batch").value = "";
    $("f-product").value = "";
    $("f-status").value = "";
    setHash("cards", { page: 1 });
  });

  $("log-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    setHash("logs", {
      ok: $("l-ok").value,
      prefix: $("l-prefix").value,
      card: $("l-card").value.trim(),
      page: 1,
    });
  });

  $("result-copy").addEventListener("click", () => {
    copyText($("result-text").value);
  });
  $("result-download").addEventListener("click", () => {
    if (!state.generatedCards.length) return;
    const name = (state.generatedBatch && state.generatedBatch.batch_no
      ? state.generatedBatch.batch_no : "cards") + ".csv";
    triggerDownload(new Blob([resultCsv()], { type: "text/csv;charset=utf-8" }), name);
  });
  $("result-close").addEventListener("click", closeResult);
  $("result-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeResult();
  });

  window.addEventListener("beforeunload", (event) => {
    if (state.resultDirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  window.addEventListener("hashchange", route);
}

function boot() {
  initTheme();

  state.key = sessionStorage.getItem(KEY_STORAGE) || "";
  $("login-view").hidden = !!state.key;
  $("app-view").hidden = !state.key;
  $("key-status").textContent = state.key ? "已登录" : "";

  bindGlobal();

  if (!location.hash) {
    location.hash = state.key ? "#/overview" : "#/login";
  }
  route();
}

document.addEventListener("DOMContentLoaded", boot);
