import { api } from "./api.js";
import {
  BILL_STATUS,
  DEFAULT_HOUSE_FIELDS,
  DEFAULT_MANIFEST_FIELDS,
  GROUP_LABEL,
  HOUSE_MANUAL_KEYS,
  HOUSE_TEMPLATE,
  MANIFEST_GROUPS,
  MANIFEST_LABELS_EN,
  MANIFEST_REQUIRED_KEYS,
  PARTY_GROUP_LABEL,
  REQUIRED_KEYS,
} from "./constants.js";
import { esc, fmtDateTime, showToast, statusPill } from "./ui.js";

const ZOOM_STEPS = [1, 1.25, 1.5, 2, 3];

const state = {
  result: null,
  snapshot: null,
  houseFields: DEFAULT_HOUSE_FIELDS,
  manifestFields: DEFAULT_MANIFEST_FIELDS,
  onSubmitted: null,
  tabs: [],
  activeTab: "",
  edits: {},
  manifestEdits: {},
  errors: new Set(),
  submitting: false,
  submitted: false,
  submissionCode: "",
  zoomIndex: 0,
  requestId: "",
};

let root = null;

// ------------------------------------------------------------------ helpers

function houseCatalog() {
  return state.houseFields && state.houseFields.length
    ? state.houseFields
    : DEFAULT_HOUSE_FIELDS;
}

function manifestCatalog() {
  return state.manifestFields && state.manifestFields.length
    ? state.manifestFields
    : DEFAULT_MANIFEST_FIELDS;
}

function fieldByKey(catalog, key) {
  return catalog.find((field) => field.key === key) || null;
}

// 舱单表单仍按三分组平铺；分单走固定单证模板（HOUSE_TEMPLATE）
function manifestGroups() {
  const catalog = manifestCatalog();
  return MANIFEST_GROUPS.map((group) => ({
    key: group,
    label: GROUP_LABEL[group],
    fields: catalog
      .filter((field) => field.group === group)
      .map((field) => ({
        ...field,
        label: MANIFEST_LABELS_EN[field.key] || field.label,
      })),
  }));
}

function comparisonsByBill() {
  const map = new Map();
  for (const item of state.result.comparisons || []) {
    map.set(item.bill_id, item);
  }
  return map;
}

function comparisonsByEntry() {
  const map = new Map();
  for (const item of state.result.comparisons || []) {
    if (item.manifest_entry_id) map.set(item.manifest_entry_id, item);
  }
  return map;
}

function activeTab() {
  return state.tabs.find((tab) => tab.id === state.activeTab) || state.tabs[0];
}

// 制单栏位与核对用通用栏位同源：旧结果只有 pieces/weight/volume 时直接复用
const FIELD_FALLBACKS = {
  rcp_pieces: "pieces",
  rcp_gross_weight: "weight",
  rcp_volume: "volume",
};

function originalValue(tab, key) {
  if (tab.type === "manifest") return null;
  const fields = tab.bill.fields || {};
  const direct = fields[key];
  if (direct !== undefined && direct !== null && String(direct).trim() !== "") {
    return direct;
  }
  const alias = FIELD_FALLBACKS[key];
  return (alias && fields[alias]) ?? "";
}

function valueFor(tabId, key, fallback) {
  const bag = state.edits[tabId];
  if (bag && Object.prototype.hasOwnProperty.call(bag, key)) return bag[key] ?? "";
  return fallback ?? "";
}

function manifestValueFor(entryId, key, fallback) {
  const bag = state.manifestEdits[entryId];
  if (bag && Object.prototype.hasOwnProperty.call(bag, key)) return bag[key] ?? "";
  return fallback ?? "";
}

function fieldStatus(comparison, key) {
  if (!comparison) return null;
  return (comparison.fields || []).find((field) => field.key === key) || null;
}

function badgeFor(comparisonField) {
  if (!comparisonField) return "";
  const status = comparisonField.status;
  if (status === "mismatch") {
    return '<em class="doc-badge diff">差异</em>';
  }
  if (status === "similar") {
    return '<em class="doc-badge review">待审核</em>';
  }
  if (status === "missing_in_manifest" || status === "missing_in_house_bill") {
    return '<em class="doc-badge review">待审核</em>';
  }
  return "";
}

function hintFor(comparisonField) {
  if (!comparisonField) return "";
  const { status, manifest_value: manifestValue, note, similarity } = comparisonField;
  if (status === "match") return "";
  if (status === "missing_in_manifest") {
    return '<small class="doc-hint review">舱单中该字段为空</small>';
  }
  if (status === "missing_in_house_bill") {
    return `<small class="doc-hint review">分单中该字段为空 · 舱单：${esc(manifestValue || "—")}</small>`;
  }
  if (status === "similar") {
    return `<small class="doc-hint review">舱单：${esc(
      manifestValue || "—"
    )} · 文本近似 ${Math.round((similarity || 0) * 100)}%</small>`;
  }
  if (status === "mismatch") {
    return `<small class="doc-hint diff">舱单：${esc(manifestValue || "—")}${
      note ? ` · ${esc(note)}` : ""
    }</small>`;
  }
  return "";
}

function buildTabs() {
  const billComparisons = comparisonsByBill();
  const tabs = (state.result.house_bills || []).map((bill, index) => ({
    id: bill.bill_id,
    type: "house_bill",
    label: `分单 ${index + 1}`,
    sub: bill.fields?.hawb_no || "未识别分单号",
    bill,
    comparison: billComparisons.get(bill.bill_id) || null,
    pageIndexes: bill.page_indexes || [],
  }));
  if (state.result.manifest) {
    tabs.push({
      id: "manifest",
      type: "manifest",
      label: "舱单",
      sub: `${(state.result.manifest.entries || []).length} 行`,
      manifest: state.result.manifest,
      pageIndexes: state.result.manifest.page_indexes || [],
    });
  }
  return tabs;
}

function activeTabPages() {
  const tab = activeTab();
  return tab ? tab.pageIndexes : [];
}

// ------------------------------------------------------------------ render

function renderTabs() {
  const submitted = state.submitted;
  return `<div class="doc-tabs" role="tablist">
    ${state.tabs
      .map((tab) => {
        const isActive = tab.id === state.activeTab;
        const icon = submitted
          ? '<svg viewBox="0 0 24 24" class="doc-tab-check"><circle cx="12" cy="12" r="10.5"/><path d="M7.2 12.3l3.3 3.3 6.3-6.6"/></svg>'
          : "";
        return `<button class="doc-tab${isActive ? " is-active" : ""}${
          submitted ? " is-submitted" : ""
        }" role="tab" aria-selected="${isActive}" data-tab="${esc(tab.id)}">
          ${icon}
          <span class="doc-tab-text">${esc(tab.label)}</span>
          <span class="doc-tab-sub">${esc(tab.sub)}</span>
        </button>`;
      })
      .join("")}
  </div>`;
}

function houseCell(tab, comparison, key, label, pos = {}, scope = "row") {
  const disabled = state.submitted ? " disabled" : "";
  const fallback = originalValue(tab, key);
  const value = valueFor(tab.id, key, fallback);
  const comparisonField = fieldStatus(comparison, key);
  const status = comparisonField?.status || "missing_both";
  const classes = ["awb-cell"];
  if (status === "mismatch") classes.push("is-diff");
  if (state.errors.has(`${tab.id}:${key}`)) classes.push("has-error");
  const required = REQUIRED_KEYS.includes(key) ? '<i class="doc-required">*</i>' : "";
  const span = pos.span || 12;
  // 位置用 CSS 变量传给样式表，便于窄屏统一降为单列
  const position =
    scope === "party"
      ? `--span:${span}`
      : `--col:${pos.col || 1};--span:${span}`;
  const inputId = `awb-${tab.id}-${key}`;
  const controlClass = pos.textarea ? "doc-input doc-textarea" : "doc-input";
  // 日期只能从日历选择：输入框只读，点击输入框或图标都会打开日历
  const dateAttr = pos.date ? ` readonly data-date-input="${esc(inputId)}"` : "";
  const attrs = `id="${esc(inputId)}" class="${controlClass}" data-tab="${esc(
    tab.id
  )}" data-key="${esc(key)}"${dateAttr}${disabled}`;
  const field = pos.textarea
    ? `<textarea ${attrs} rows="1">${esc(value)}</textarea>`
    : `<input ${attrs} type="text" value="${esc(value)}" />`;
  const control = pos.date
    ? `<div class="awb-date-wrap">
        ${field}
        <button type="button" class="awb-date-btn" data-date-input="${esc(
          inputId
        )}" aria-label="选择航班日期"${disabled}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5.5" width="17" height="15" rx="2.5"/><path d="M3.5 10h17M8 3.5v4M16 3.5v4"/></svg>
        </button>
      </div>`
    : field;
  // 用 div 包裹、label 只绑定文字，避免点击单元格空白处就聚焦输入框
  return `<div class="${classes.join(" ")}" style="${position}" data-row="${esc(
    tab.id
  )}:${esc(key)}">
      <label class="awb-cell-label" for="${esc(inputId)}">${esc(label).replaceAll(
        "/",
        "/<wbr>"
      )}${required}
        <span class="doc-markers">${badgeFor(comparisonField)}</span>
      </label>
      ${control}
      ${hintFor(comparisonField)}
    </div>`;
}

// 分单摘要：单号、核对状态、页码/舱单行/置信度
function houseMeta(tab, comparison) {
  const meta = [];
  if (tab.pageIndexes.length) meta.push(`第 ${tab.pageIndexes.join("、")} 页`);
  if (comparison?.manifest_line_no) meta.push(`舱单第 ${comparison.manifest_line_no} 行`);
  if (comparison && comparison.manifest_entry_id === null) meta.push("舱单未匹配");
  meta.push(`置信度 ${Math.round((tab.bill.confidence || 0) * 100)}%`);
  return meta;
}

function renderHouseInfo(tab, comparison) {
  const statusInfo = comparison
    ? BILL_STATUS[comparison.status] || BILL_STATUS.incomplete
    : BILL_STATUS.incomplete;
  const meta = houseMeta(tab, comparison);
  return `<div class="awb-row-info" style="--col:1;--span:8">
      <div class="awb-info-head">
        <span class="awb-info-no">${esc(tab.sub)}</span>
        ${statusPill(statusInfo.label, statusInfo.className)}
      </div>
      <div class="awb-info-meta">${meta.map((item) => esc(item)).join(" · ")}</div>
    </div>`;
}

// 单证模板按「行」渲染：row.party 是当事人块，其余为一行的若干栏位
function renderHouseRow(tab, comparison, row) {
  const catalog = houseCatalog();
  const info = row.info ? renderHouseInfo(tab, comparison) : "";
  if (row.party) {
    const cells = row.fields
      .map((item) => {
        const key = `${row.party}_${item.key}`;
        const spec = fieldByKey(catalog, key);
        if (!spec) return "";
        return houseCell(tab, comparison, key, item.label || spec.label, item, "party");
      })
      .join("");
    return `<div class="awb-row">
        ${info}
        <section class="awb-party" style="--col:${row.col || 1};--span:${row.span || 12}">
          <div class="awb-party-head">${esc(PARTY_GROUP_LABEL[row.party] || row.party)}</div>
          <div class="awb-party-grid">${cells}</div>
        </section>
      </div>`;
  }

  const cells = row.fields
    .map((item) => {
      const spec = fieldByKey(catalog, item.key);
      // manual 字段不在后端字段目录里，需要人工填写
      if (!spec && !item.manual) return "";
      const label = item.label || (spec ? spec.label : item.key);
      return houseCell(tab, comparison, item.key, label, item, "row");
    })
    .join("");
  return `<div class="awb-row" style="--cols:${row.cols || 12}">${info}${cells}</div>`;
}

function renderHouseForm(tab) {
  const rows = HOUSE_TEMPLATE.map((row) =>
    renderHouseRow(tab, tab.comparison, row)
  ).join("");
  return `<div class="doc-form-scroll">
      <article class="awb-sheet">${rows}</article>
    </div>`;
}

function renderManifestForm(tab) {
  const disabled = state.submitted ? " disabled" : "";
  const entryComparisons = comparisonsByEntry();
  const entries = tab.manifest.entries || [];
  if (entries.length === 0) {
    return `<div class="doc-form-head"><div class="doc-form-title">舱单</div></div>
      <div class="doc-form-empty">舱单未抽取出数据行</div>`;
  }

  const cards = entries
    .map((entry) => {
      const comparison = entryComparisons.get(entry.entry_id);
      const statusInfo = comparison
        ? BILL_STATUS[comparison.status] || BILL_STATUS.incomplete
        : { label: "无对应分单", className: "idle" };
      const fields = manifestGroups()
        .flatMap((group) => group.fields)
        .map((field) => {
          const fallback = (entry.fields || {})[field.key] ?? "";
          const value = manifestValueFor(entry.entry_id, field.key, fallback);
          const comparisonField = comparison
            ? fieldStatus(comparison, field.key)
            : null;
          const errorKey = `manifest:${entry.entry_id}:${field.key}`;
          const rowClass = [
            comparisonField?.status === "mismatch" ? "is-diff" : "",
            state.errors.has(errorKey) ? "has-error" : "",
          ]
            .filter(Boolean)
            .join(" ");
          return `<label class="mft-field ${rowClass}" data-row="manifest:${esc(
            entry.entry_id
          )}:${esc(field.key)}">
            <span class="mft-label">${esc(field.label)}${
              MANIFEST_REQUIRED_KEYS.includes(field.key)
                ? '<i class="doc-required">*</i>'
                : ""
            }</span>
            <input class="doc-input" data-manifest-entry="${esc(
              entry.entry_id
            )}" data-key="${esc(field.key)}" value="${esc(value)}"${disabled} />
            ${hintFor(comparisonField)}
          </label>`;
        })
        .join("");
      return `<article class="mft-entry">
        <header>
          <span class="mft-line">第 ${entry.line_no ?? "—"} 行</span>
          <b>${esc(entry.fields?.hawb_no || "未识别分单号")}</b>
          ${statusPill(statusInfo.label, statusInfo.className)}
        </header>
        <div class="mft-grid">${fields}</div>
      </article>`;
    })
    .join("");

  return `<div class="doc-form-head">
      <div class="doc-form-title">舱单</div>
      ${statusPill(`${entries.length} 行`, "idle")}
      <div class="doc-form-meta">第 ${(tab.pageIndexes || []).join("、")} 页 · 逐行核对</div>
    </div>
    <div class="doc-form-scroll mft-scroll">${cards}</div>`;
}

function renderForm() {
  const tab = activeTab();
  if (!tab) return '<div class="doc-form-empty">没有可核对的分单或舱单</div>';
  return tab.type === "manifest" ? renderManifestForm(tab) : renderHouseForm(tab);
}

function renderPreview() {
  const pages = state.result.pages || [];
  const activePages = new Set(activeTabPages());
  const zoom = ZOOM_STEPS[state.zoomIndex];
  if (pages.length === 0) {
    return '<div class="doc-preview-empty">没有可预览的页面</div>';
  }
  const figures = pages
    .map((page) => {
      const active = activePages.has(page.index) ? " is-active-doc" : "";
      const master = page.kind === "master_bill" ? " is-master" : "";
      const body = page.image_rel
        ? `<img src="${api.pageImageUrl(
            state.result.task_id,
            page.index
          )}" alt="第 ${page.index} 页" loading="lazy" draggable="false" />`
        : `<pre class="doc-page-text">${esc((page.text || "（本页无文本）").slice(0, 4000))}</pre>`;
      return `<figure class="doc-page${active}${master}" data-page="${page.index}">
        <div class="doc-page-inner">${body}</div>
      </figure>`;
    })
    .join("");

  return `<header class="doc-preview-head">
      <div class="doc-preview-tools">
        <small>共 ${pages.length} 页</small>
        <div class="doc-zoom">
          <button type="button" data-zoom="out" aria-label="缩小">−</button>
          <button type="button" data-zoom="reset" class="doc-zoom-value">${Math.round(
            zoom * 100
          )}%</button>
          <button type="button" data-zoom="in" aria-label="放大">＋</button>
        </div>
      </div>
    </header>
    <div class="doc-preview-body" id="docPreviewBody">
      <div class="doc-pages" style="--doc-zoom:${zoom}">${figures}</div>
    </div>`;
}

function renderFooter() {
  const submittedInfo =
    state.submitted && state.submissionCode
      ? `<span class="doc-submitted-code">已提交 · ${esc(state.submissionCode)}</span>`
      : "";
  const submitLabel = state.submitted
    ? "已提交"
    : state.submitting
      ? '<span class="doc-spinner"></span>提交中'
      : "提交";
  return `<div class="doc-foot-left">${submittedInfo}${
    state.submitted && state.submittedAt
      ? `<span class="doc-submitted-time">${esc(fmtDateTime(state.submittedAt))}</span>`
      : ""
  }</div>
  <div class="doc-foot-right">
    <button type="button" class="doc-btn" data-action="close">${
      state.submitted ? "关闭" : "取消"
    }</button>
    <button type="button" class="doc-submit" data-action="submit" ${
      state.submitted || state.submitting ? "disabled" : ""
    }>${submitLabel}</button>
  </div>`;
}

function renderDialog() {
  closeCalendar();
  const tab = activeTab();
  const submittedClass = state.submitted ? " is-submitted" : "";
  root.innerHTML = `<div class="doc-dialog-mask" data-action="mask">
    <section class="doc-dialog${submittedClass}" role="dialog" aria-modal="true" aria-label="核对并提交">
      <header class="doc-dialog-head">
        <div class="doc-dialog-head-main">
          <div class="doc-dialog-head-title">
            <span class="doc-dialog-eyebrow">RESULT REVIEW</span>
            <span class="doc-dialog-task">${esc(state.result.task_id)}</span>
          </div>
          <h1>${esc(state.result.source_name)}</h1>
        </div>
        <button type="button" class="doc-dialog-close" data-action="close" aria-label="关闭">×</button>
      </header>
      <div class="doc-dialog-body">
        <section class="doc-dialog-preview">${renderPreview()}</section>
        <section class="doc-dialog-form">
          ${renderTabs()}
          <div class="doc-form-pane" id="docFormPane">${renderForm()}</div>
        </section>
      </div>
      <footer class="doc-dialog-foot">${renderFooter()}</footer>
    </section>
  </div>`;
  highlightActivePages(false);
  requestAnimationFrame(autoSizeTextareas);
}

function highlightActivePages(scroll = true) {
  if (!root) return;
  const activePages = new Set(activeTabPages());
  root.querySelectorAll(".doc-page").forEach((element) => {
    element.classList.toggle(
      "is-active-doc",
      activePages.has(Number(element.dataset.page))
    );
  });
  if (!scroll) return;
  const firstPage = [...activePages][0];
  if (!firstPage) return;
  const target = root.querySelector(`.doc-page[data-page="${firstPage}"]`);
  const body = root.querySelector("#docPreviewBody");
  if (!target || !body) return;

  const doScroll = () => {
    // 用容器内相对位置滚动，避免 scrollIntoView 把外层对话框一起滚动
    const bodyRect = body.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    body.scrollTo({
      top: body.scrollTop + (targetRect.top - bodyRect.top) - 12,
      behavior: "smooth",
    });
  };
  const image = target.querySelector("img");
  // 图片未加载完时页面高度为 0，滚动会落空：等首图加载后再定位
  if (image && !image.complete) {
    image.addEventListener("load", doScroll, { once: true });
    setTimeout(doScroll, 900);
  } else {
    doScroll();
  }
}

function refreshForm() {
  closeCalendar();
  const pane = root?.querySelector("#docFormPane");
  if (pane) {
    pane.innerHTML = renderForm();
    requestAnimationFrame(autoSizeTextareas);
  }
}

function refreshTabs() {
  const strip = root?.querySelector(".doc-tabs");
  if (strip) {
    const scrollTop = strip.scrollLeft;
    strip.outerHTML = renderTabs();
    const next = root.querySelector(".doc-tabs");
    if (next) next.scrollLeft = scrollTop;
  }
}

function refreshFooter() {
  const footer = root?.querySelector(".doc-dialog-foot");
  if (footer) footer.innerHTML = renderFooter();
}

function switchTab(tabId) {
  state.activeTab = tabId;
  refreshTabs();
  refreshForm();
  refreshFooter();
  highlightActivePages(true);
}

// ------------------------------------------------------------------ date picker

const WEEK_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

let calendarPop = null;

function pad2(value) {
  return String(value).padStart(2, "0");
}

function parseDateInput(value) {
  const match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(String(value || "").trim());
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? null : date;
}

function onCalendarOutside(event) {
  if (calendarPop && !calendarPop.contains(event.target)) closeCalendar();
}

function onCalendarKeydown(event) {
  if (event.key === "Escape") closeCalendar();
}

function closeCalendar() {
  if (!calendarPop) return;
  calendarPop.remove();
  calendarPop = null;
  document.removeEventListener("click", onCalendarOutside, true);
  document.removeEventListener("keydown", onCalendarKeydown, true);
  window.removeEventListener("resize", closeCalendar);
}

function positionCalendar(pop, anchor) {
  const rect = anchor.getBoundingClientRect();
  const width = pop.offsetWidth || 232;
  const height = pop.offsetHeight || 268;
  let left = rect.left;
  let top = rect.bottom + 6;
  if (left + width > window.innerWidth - 8) left = window.innerWidth - width - 8;
  if (top + height > window.innerHeight - 8) top = rect.top - height - 6;
  pop.style.left = `${Math.max(8, left)}px`;
  pop.style.top = `${Math.max(8, top)}px`;
}

function applyDate(input, value) {
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  closeCalendar();
  input.focus();
}

function openCalendar(input) {
  closeCalendar();
  const selected = parseDateInput(input.value);
  const initial = selected || new Date();
  let year = initial.getFullYear();
  let month = initial.getMonth();

  const pop = document.createElement("div");
  pop.className = "awb-calendar";
  calendarPop = pop;

  const paint = () => {
    const lead = (new Date(year, month, 1).getDay() + 6) % 7; // 周一开头
    const days = new Date(year, month + 1, 0).getDate();
    const today = new Date();
    const cells = [];
    for (let i = 0; i < lead; i += 1) cells.push("<span></span>");
    for (let day = 1; day <= days; day += 1) {
      const isSelected =
        selected &&
        selected.getFullYear() === year &&
        selected.getMonth() === month &&
        selected.getDate() === day;
      const isToday =
        today.getFullYear() === year &&
        today.getMonth() === month &&
        today.getDate() === day;
      cells.push(
        `<button type="button" class="${isSelected ? "is-selected" : ""}${
          isToday ? " is-today" : ""
        }" data-day="${day}">${day}</button>`
      );
    }
    pop.innerHTML = `<div class="awb-cal-head">
        <span class="awb-cal-title">${year} 年 ${month + 1} 月</span>
        <span class="awb-cal-nav">
          <button type="button" data-nav="-1" aria-label="上个月">‹</button>
          <button type="button" data-nav="1" aria-label="下个月">›</button>
        </span>
      </div>
      <div class="awb-cal-week">${WEEK_LABELS.map((week) => `<span>${week}</span>`).join("")}</div>
      <div class="awb-cal-grid">${cells.join("")}</div>
      <div class="awb-cal-foot">
        <button type="button" class="awb-cal-link" data-cal="today">今天</button>
        <button type="button" class="awb-cal-link" data-cal="clear">清除</button>
      </div>`;
  };

  paint();
  document.body.appendChild(pop);
  positionCalendar(pop, input);

  pop.addEventListener("click", (event) => {
    const nav = event.target.closest("[data-nav]");
    if (nav) {
      month += Number(nav.dataset.nav);
      if (month < 0) {
        month = 11;
        year -= 1;
      } else if (month > 11) {
        month = 0;
        year += 1;
      }
      paint();
      return;
    }
    const dayButton = event.target.closest("[data-day]");
    if (dayButton) {
      applyDate(input, `${year}-${pad2(month + 1)}-${pad2(Number(dayButton.dataset.day))}`);
      return;
    }
    const action = event.target.closest("[data-cal]")?.dataset.cal;
    if (action === "today") {
      const now = new Date();
      applyDate(
        input,
        `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`
      );
    } else if (action === "clear") {
      applyDate(input, "");
    }
  });

  setTimeout(() => {
    document.addEventListener("click", onCalendarOutside, true);
    document.addEventListener("keydown", onCalendarKeydown, true);
    window.addEventListener("resize", closeCalendar);
  }, 0);
}

// ------------------------------------------------------------------ actions

function autoSizeTextarea(element) {
  element.style.height = "auto";
  element.style.height = `${element.scrollHeight}px`;
}

function autoSizeTextareas() {
  root?.querySelectorAll(".doc-textarea").forEach(autoSizeTextarea);
}

function onInput(event) {
  const input = event.target.closest(".doc-input");
  if (!input) return;
  if (input.tagName === "TEXTAREA") autoSizeTextarea(input);
  const key = input.dataset.key;
  const entryId = input.dataset.manifestEntry;
  if (entryId) {
    state.manifestEdits[entryId] = state.manifestEdits[entryId] || {};
    state.manifestEdits[entryId][key] = input.value;
    state.errors.delete(`manifest:${entryId}:${key}`);
  } else {
    const tabId = input.dataset.tab;
    state.edits[tabId] = state.edits[tabId] || {};
    state.edits[tabId][key] = input.value;
    state.errors.delete(`${tabId}:${key}`);
  }
  const tab = activeTab();
  if (tab && tab.type === "house_bill") {
    const row = input.closest(".awb-cell");
    const marker = row?.querySelector(".doc-markers");
    if (marker) marker.innerHTML = badgeFor(fieldStatus(tab.comparison, key));
    row?.classList.toggle("has-error", false);
  } else {
    input.closest(".mft-field")?.classList.remove("has-error");
  }
}

async function onSubmit() {
  if (state.submitting || state.submitted) return;

  const errors = new Set();
  const documents = [];
  for (const tab of state.tabs) {
    if (tab.type === "house_bill") {
      const fields = {};
      for (const item of houseCatalog()) {
        const fallback = (tab.bill.fields || {})[item.key] ?? "";
        fields[item.key] = valueFor(tab.id, item.key, fallback);
      }
      // 模板上的制单栏位与核对用的通用栏位同源，空值时互相兜底
      for (const [general, template] of [
        ["pieces", "rcp_pieces"],
        ["weight", "rcp_gross_weight"],
        ["volume", "rcp_volume"],
      ]) {
        if (!String(fields[general] || "").trim() && String(fields[template] || "").trim()) {
          fields[general] = fields[template];
        }
      }
      // 人工填写栏位不在字段目录中，单独收集后随表单提交
      for (const key of HOUSE_MANUAL_KEYS) {
        const fallback = (tab.bill.fields || {})[key] ?? "";
        fields[key] = valueFor(tab.id, key, fallback);
      }
      for (const key of REQUIRED_KEYS) {
        if (!String(fields[key] || "").trim()) errors.add(`${tab.id}:${key}`);
      }
      documents.push({
        tab_id: tab.id,
        kind: "house_bill",
        fields,
      });
    } else if (tab.type === "manifest") {
      const entries = (tab.manifest.entries || []).map((entry) => {
        const fields = {};
        for (const item of manifestCatalog()) {
          fields[item.key] = manifestValueFor(
            entry.entry_id,
            item.key,
            (entry.fields || {})[item.key] ?? ""
          );
        }
        for (const key of MANIFEST_REQUIRED_KEYS) {
          if (!String(fields[key] || "").trim()) {
            errors.add(`manifest:${entry.entry_id}:${key}`);
          }
        }
        return {
          entry_id: entry.entry_id,
          line_no: entry.line_no,
          fields,
        };
      });
      documents.push({ tab_id: "manifest", kind: "manifest", entries });
    }
  }

  if (errors.size > 0) {
    state.errors = errors;
    refreshForm();
    const first = [...errors][0];
    const parts = first.split(":");
    const tabId = parts[0];
    if (tabId !== state.activeTab) {
      const targetTab =
        state.tabs.find((tab) => tab.id === tabId) ||
        state.tabs.find((tab) => tab.id === "manifest");
      if (targetTab) switchTab(targetTab.id);
    }
    const row =
      root.querySelector("[data-row].has-error") ||
      root.querySelector(".mft-field.has-error") ||
      root.querySelector(".awb-cell.has-error");
    row?.scrollIntoView({ block: "center", behavior: "smooth" });
    row?.querySelector("input")?.focus();
    showToast("有必填字段为空，请补充后再提交", "error");
    return;
  }

  state.submitting = true;
  refreshFooter();
  try {
    const response = await api.submitTask(state.result.task_id, {
      request_id: state.requestId,
      // 弹窗不再要求二次勾选，提交即视为已核对差异
      acknowledged: true,
      documents,
    });
    state.submitted = true;
    state.submissionCode = response.submission_code;
    state.submittedAt = response.submitted_at;
    state.submitting = false;
    state.snapshot = {
      ...(state.snapshot || {}),
      submitted: true,
      submission_code: response.submission_code,
      submitted_at: response.submitted_at,
    };
    renderDialog();
    requestAnimationFrame(() => highlightActivePages(true));
    showToast(
      response.duplicated
        ? "该单此前已提交成功"
        : `提交成功，编号 ${response.submission_code}`
    );
    if (state.onSubmitted) state.onSubmitted(response);
  } catch (error) {
    state.submitting = false;
    refreshFooter();
    showToast(error.message, "error");
  }
}

function bindEvents() {
  if (root.dataset.bound === "1") return;
  root.dataset.bound = "1";
  root.addEventListener("click", (event) => {
    const dateButton = event.target.closest("[data-date-input]");
    if (dateButton) {
      const input = document.getElementById(dateButton.dataset.dateInput);
      if (input && !input.disabled) openCalendar(input);
      return;
    }
    const tab = event.target.closest("[data-tab]");
    if (tab && tab.classList.contains("doc-tab")) {
      switchTab(tab.dataset.tab);
      return;
    }
    const zoom = event.target.closest("[data-zoom]");
    if (zoom) {
      const action = zoom.dataset.zoom;
      if (action === "in") state.zoomIndex = Math.min(state.zoomIndex + 1, ZOOM_STEPS.length - 1);
      if (action === "out") state.zoomIndex = Math.max(state.zoomIndex - 1, 0);
      if (action === "reset") state.zoomIndex = 0;
      const pages = root.querySelector(".doc-pages");
      if (pages) pages.style.setProperty("--doc-zoom", ZOOM_STEPS[state.zoomIndex]);
      const value = root.querySelector(".doc-zoom-value");
      if (value) value.textContent = `${Math.round(ZOOM_STEPS[state.zoomIndex] * 100)}%`;
      return;
    }
    const action = event.target.closest("[data-action]");
    if (action) {
      const name = action.dataset.action;
      if (name === "close" && !state.submitting) close();
      if (name === "mask" && event.target === action && !state.submitting) close();
      if (name === "submit") onSubmit();
    }
  });

  root.addEventListener("input", onInput);

  // 原单预览支持按住拖动平移（类似图片查看器）
  let drag = null;
  root.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const body = event.target.closest("#docPreviewBody");
    if (!body) return;
    drag = {
      body,
      x: event.clientX,
      y: event.clientY,
      left: body.scrollLeft,
      top: body.scrollTop,
    };
    body.classList.add("is-dragging");
    body.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  root.addEventListener("pointermove", (event) => {
    if (!drag) return;
    drag.body.scrollLeft = drag.left - (event.clientX - drag.x);
    drag.body.scrollTop = drag.top - (event.clientY - drag.y);
  });
  const stopDrag = () => {
    if (!drag) return;
    drag.body.classList.remove("is-dragging");
    drag = null;
  };
  root.addEventListener("pointerup", stopDrag);
  root.addEventListener("pointercancel", stopDrag);
  root.addEventListener("pointerleave", stopDrag);

  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !state.submitting) close();
  });
}

// 焦点不在弹窗内时也能用 ESC 关闭
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && isSubmitDialogOpen() && !state.submitting) {
    close();
  }
});

// ------------------------------------------------------------------ public

export async function openSubmitDialog({ result, snapshot, fields, onSubmitted, focus }) {
  close(true);
  state.result = result;
  state.snapshot = snapshot;
  state.houseFields =
    fields && Array.isArray(fields.house) && fields.house.length
      ? fields.house
      : DEFAULT_HOUSE_FIELDS;
  state.manifestFields =
    fields && Array.isArray(fields.manifest) && fields.manifest.length
      ? fields.manifest
      : DEFAULT_MANIFEST_FIELDS;
  state.onSubmitted = onSubmitted || null;
  state.tabs = buildTabs();
  state.edits = {};
  state.manifestEdits = {};
  state.errors = new Set();
  state.submitting = false;
  state.submitted = Boolean(snapshot?.submitted);
  state.submissionCode = snapshot?.submission_code || "";
  state.submittedAt = snapshot?.submitted_at || "";
  state.zoomIndex = 0;
  state.requestId = `ui-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  state.activeTab =
    (focus && state.tabs.find((tab) => tab.id === focus.tabId)?.id) ||
    state.tabs[0]?.id ||
    "";

  // 已提交的任务：用提交时保存的人工修改值回填，保证再次打开看到的是提交版本
  if (state.submitted) {
    try {
      const payload = await api.getSubmission(result.task_id);
      applySubmission(payload?.submission);
    } catch {
      /* 读取失败时退回抽取结果 */
    }
  }

  root = document.getElementById("dialogRoot");
  if (!root) return;
  renderDialog();
  bindEvents();
  document.body.classList.add("dialog-open");
  // 首次渲染时页面图片尚未加载，等首图就位后再把当前单据的页面滚到可视区
  requestAnimationFrame(() => highlightActivePages(true));

  if (focus && focus.key) {
    requestAnimationFrame(() => {
      const row =
        root.querySelector(`[data-row="${focus.tabId}:${focus.key}"]`) ||
        root.querySelector(".awb-cell.has-error");
      row?.scrollIntoView({ block: "center", behavior: "smooth" });
      row?.classList.add("is-highlight");
      setTimeout(() => row?.classList.remove("is-highlight"), 1600);
    });
  }
}

function applySubmission(record) {
  if (!record || !Array.isArray(record.documents)) return;
  for (const document of record.documents) {
    if (document.kind === "house_bill" && document.fields) {
      state.edits[document.tab_id] = { ...document.fields };
    }
    if (document.kind === "manifest" && Array.isArray(document.entries)) {
      for (const entry of document.entries) {
        if (entry.entry_id) {
          state.manifestEdits[entry.entry_id] = { ...(entry.fields || {}) };
        }
      }
    }
  }
}

function close(silent = false) {
  if (!silent && state.submitting) return;
  closeCalendar();
  const node = document.getElementById("dialogRoot");
  if (node) node.innerHTML = "";
  document.body.classList.remove("dialog-open");
  state.result = null;
  state.tabs = [];
  root = null;
}

export function closeSubmitDialog() {
  close();
}

export function isSubmitDialogOpen() {
  return Boolean(root && state.result);
}

// 供主页面「差异列表」直接定位到字段
export function focusReviewItem(tabId, key) {
  if (!isSubmitDialogOpen()) return;
  if (tabId !== state.activeTab) {
    const tab = state.tabs.find((item) => item.id === tabId);
    if (tab) switchTab(tab.id);
  }
  const row = key.startsWith("manifest:")
    ? root.querySelector(`[data-row="${key}"]`)
    : root.querySelector(`[data-row="${tabId}:${key}"]`);
  const fallbackRow = row || root.querySelector("#docFormPane");
  fallbackRow?.scrollIntoView({ block: "center", behavior: "smooth" });
  fallbackRow?.classList.add("is-highlight");
  setTimeout(() => fallbackRow?.classList.remove("is-highlight"), 1600);
}
