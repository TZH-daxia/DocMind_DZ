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
  partyFieldKey,
} from "./constants.js";
import { esc, fmtDateTime, showToast, statusPill } from "./ui.js";

const ZOOM_STEPS = [1, 1.25, 1.5, 2, 3];

const state = {
  result: null,
  snapshot: null,
  // 弹窗顶部文件页签：最近完成的 5 个任务，可切换查看/提交
  fileTabs: [],
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

// 弹窗顶部最多显示的已完成任务数（与 docmind 一致）
const MAX_FILE_TABS = 5;

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

// 核对结果仍按舱单口径的 key 输出，分单侧这几个字段名不同
const COMPARISON_KEY_ALIASES = {
  mdg: "dest_port",
  zdpiece: "pieces",
  zdweight: "weight",
  jfweight: "chargeable_weight",
  zdvolume: "volume",
};
const HOUSE_KEY_ALIASES = Object.fromEntries(
  Object.entries(COMPARISON_KEY_ALIASES).map(([house, manifest]) => [manifest, house])
);

function originalValue(tab, key) {
  if (tab.type === "manifest") return null;
  const fields = tab.bill.fields || {};
  const direct = fields[key];
  if (direct !== undefined && direct !== null && String(direct).trim() !== "") {
    return direct;
  }
  return "";
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
  const fields = comparison.fields || [];
  return (
    fields.find((field) => field.key === key) ||
    fields.find((field) => field.key === COMPARISON_KEY_ALIASES[key]) ||
    null
  );
}

// 必填提示里显示字段中文名（分单 key 与舱单 key 都能查）
function labelForKey(key) {
  const spec =
    houseCatalog().find((field) => field.key === key) ||
    manifestCatalog().find((field) => field.key === key);
  return spec ? spec.label : key;
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
  return "";
}

function hintFor(comparisonField) {
  if (!comparisonField) return "";
  const { status, manifest_value: manifestValue, note, similarity } = comparisonField;
  // 空值不再提示：只有真实差异与文本近似需要人工看
  if (status === "match" || status === "missing_in_manifest" || status === "missing_in_house_bill") {
    return "";
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

// 低置信度/文本近似的字段：取待审核条目里的原文，显示在字段下方
function reviewEvidence(tabId, key) {
  const items = state.result?.review_items || [];
  const found = items.find(
    (item) => item.tab_id === tabId && item.key === key && item.evidence
  );
  return found ? found.evidence : "";
}

// 字段级待审核徽标（如港口无法确定三字码，等待人工填写）
function reviewBadge(tabId, key) {
  const items = state.result?.review_items || [];
  const found = items.some(
    (item) => item.tab_id === tabId && item.key === key && item.reason === "low_confidence"
  );
  return found ? '<em class="doc-badge review">待审核</em>' : "";
}

function buildTabs() {
  const billComparisons = comparisonsByBill();
  const tabs = (state.result.house_bills || []).map((bill, index) => ({
    id: bill.bill_id,
    type: "house_bill",
    label: `分单 ${index + 1}`,
    sub: bill.fields?.hawb || "未识别分单号",
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

// 舱单是否存在「未校验（缺值）」或「异常差异」字段
function manifestHasAlerts() {
  for (const item of state.result?.comparisons || []) {
    if (!item.manifest_entry_id) continue;
    for (const field of item.fields || []) {
      if (
        field.status === "mismatch" ||
        field.status === "missing_in_manifest" ||
        field.status === "missing_in_house_bill"
      ) {
        return true;
      }
    }
  }
  return false;
}

function renderTabs() {
  const submitted = state.submitted;
  const manifestAlert = manifestHasAlerts() && !state.manifestSeen;
  return `<div class="doc-tabs" role="tablist">
    ${state.tabs
      .map((tab) => {
        const isActive = tab.id === state.activeTab;
        const icon = submitted
          ? '<svg viewBox="0 0 24 24" class="doc-tab-check"><circle cx="12" cy="12" r="10.5"/><path d="M7.2 12.3l3.3 3.3 6.3-6.6"/></svg>'
          : "";
        const dot =
          tab.type === "manifest" && manifestAlert
            ? '<i class="doc-tab-dot" aria-label="有未校验或差异字段"></i>'
            : "";
        return `<button class="doc-tab${isActive ? " is-active" : ""}${
          submitted ? " is-submitted" : ""
        }" role="tab" aria-selected="${isActive}" data-tab="${esc(tab.id)}">
          ${icon}
          <span class="doc-tab-text">${esc(tab.label)}</span>
          <span class="doc-tab-sub">${esc(tab.sub)}</span>
          ${dot}
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
  const evidence = reviewEvidence(tab.id, key);
  // 用 div 包裹、label 只绑定文字，避免点击单元格空白处就聚焦输入框
  return `<div class="${classes.join(" ")}" style="${position}" data-row="${esc(
    tab.id
  )}:${esc(key)}">
      <span class="awb-cell-label">${esc(label).replaceAll(
        "/",
        "/<wbr>"
      )}${required}
        <span class="doc-markers">${badgeFor(comparisonField)}${reviewBadge(tab.id, key)}</span>
      </span>
      ${control}
      ${hintFor(comparisonField)}
      ${evidence ? `<small class="doc-hint evidence">原文：${esc(evidence)}</small>` : ""}
    </div>`;
}

function renderHouseInfo(tab, comparison) {
  // 只标「不一致」；一致与缺值都不再显示标记
  const statusInfo =
    comparison && comparison.status === "inconsistent"
      ? BILL_STATUS.inconsistent
      : null;
  // 不再展示分单号与页码：置信度占据原分单号的位置，字号与标记保持一致
  const confidence = Math.round((tab.bill.confidence || 0) * 100);
  return `<div class="awb-row-info" style="--col:1;--span:8">
      <div class="awb-info-head">
        <span class="awb-info-no">置信度 ${confidence}%</span>
        ${statusInfo ? statusPill(statusInfo.label, statusInfo.className) : ""}
      </div>
    </div>`;
}

// 单证模板按「行」渲染：row.party 是当事人块，其余为一行的若干栏位
function renderHouseRow(tab, comparison, row) {
  const catalog = houseCatalog();
  const info = row.info ? renderHouseInfo(tab, comparison) : "";
  if (row.party) {
    const cells = row.fields
      .map((item) => {
        const key = partyFieldKey(row.party, item.key);
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
      // 只标「不一致」与「无对应分单」；一致、缺值都不再显示标记
      const statusInfo =
        comparison === undefined
          ? { label: "无对应分单", className: "idle" }
          : comparison.status === "inconsistent"
            ? BILL_STATUS.inconsistent
            : null;
      const fields = manifestGroups()
        .flatMap((group) => group.fields)
        .map((field) => {
          const fallback = (entry.fields || {})[field.key] ?? "";
          const value = manifestValueFor(entry.entry_id, field.key, fallback);
          const comparisonField = comparison
            ? fieldStatus(comparison, field.key)
            : null;
          const errorKey = `manifest:${entry.entry_id}:${field.key}`;
          // 分单或舱单缺值 → 该字段没有完成校验，只在舱单侧标「未校验」
          const unchecked =
            comparisonField?.status === "missing_in_manifest" ||
            comparisonField?.status === "missing_in_house_bill";
          // 说明缺的是哪一侧：舱单没有值 or 分单没有值
          const uncheckedHint =
            comparisonField?.status === "missing_in_manifest"
              ? '<small class="doc-hint unchecked">舱单缺少该字段，未校验</small>'
              : comparisonField?.status === "missing_in_house_bill"
                ? '<small class="doc-hint unchecked">分单缺少该字段，未校验</small>'
                : "";
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
            }${unchecked ? '<em class="doc-badge unchecked">未校验</em>' : ""}</span>
            <input class="doc-input" data-manifest-entry="${esc(
              entry.entry_id
            )}" data-key="${esc(field.key)}" value="${esc(value)}"${disabled} />
            ${hintFor(comparisonField)}
            ${uncheckedHint}
          </label>`;
        })
        .join("");
      return `<article class="mft-entry">
        <header>
          <span class="mft-line">第 ${entry.line_no ?? "—"} 行</span>
          ${statusInfo ? statusPill(statusInfo.label, statusInfo.className) : ""}
        </header>
        <div class="mft-grid">${fields}</div>
      </article>`;
    })
    .join("");

  return `<div class="doc-form-head">
      <div class="doc-form-title">舱单</div>
      ${statusPill(`${entries.length} 行`, "idle")}
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
      <span class="doc-preview-eyebrow">SOURCE DOCUMENT</span>
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

// ---------------------------------------------------------------- field locate

function clearFieldHighlight() {
  root?.querySelectorAll(".doc-locate-box").forEach((node) => node.remove());
  root
    ?.querySelectorAll(".doc-page.is-located")
    .forEach((node) => node.classList.remove("is-located"));
}

// 滚动到指定页；给了 box 就滚到框中心（放大/缩小时按页面实时尺寸重算）
function scrollPreviewToBox(pageIndex, box) {
  const body = root?.querySelector("#docPreviewBody");
  const page = root?.querySelector(`.doc-page[data-page="${pageIndex}"]`);
  if (!body || !page) return null;

  const doScroll = () => {
    const inner = page.querySelector(".doc-page-inner") || page;
    const bodyRect = body.getBoundingClientRect();
    const innerRect = inner.getBoundingClientRect();
    const bbox = box?.bbox || [0.5, 0.5, 0, 0];
    const centerX =
      innerRect.left - bodyRect.left + (bbox[0] + bbox[2] / 2) * innerRect.width;
    const centerY =
      innerRect.top - bodyRect.top + (bbox[1] + bbox[3] / 2) * innerRect.height;
    body.scrollTo({
      left: Math.max(0, body.scrollLeft + centerX - body.clientWidth / 2),
      top: Math.max(0, body.scrollTop + centerY - body.clientHeight / 2),
      behavior: "smooth",
    });
  };

  const image = page.querySelector("img");
  // 图片未加载完时页面高度为 0，滚动会落空：等首图加载后再定位
  if (image && !image.complete) {
    image.addEventListener("load", doScroll, { once: true });
    setTimeout(doScroll, 900);
  } else {
    doScroll();
  }
  return page;
}

// 把定位框画到对应的页面图上（分单/舱单共用）
function drawLocateBoxes(boxes) {
  boxes.forEach((box) => {
    const inner = root?.querySelector(
      `.doc-page[data-page="${box.page}"] .doc-page-inner`
    );
    const [x, y, width, height] = box.bbox || [];
    if (!inner || width <= 0 || height <= 0) return;
    const mark = document.createElement("span");
    mark.className = "doc-locate-box";
    mark.style.left = `${x * 100}%`;
    mark.style.top = `${y * 100}%`;
    mark.style.width = `${width * 100}%`;
    mark.style.height = `${height * 100}%`;
    inner.appendChild(mark);
  });
}

// 定位不到（如扫描件）时退化为整页高亮
function locateEmptyFallback(pageIndex) {
  const page = pageIndex ? scrollPreviewToBox(pageIndex) : null;
  page?.classList.add("is-located");
}

// 点击/聚焦表单字段时，在左侧原图上定位并高亮该字段值
function highlightFieldLocation(tabId, key) {
  if (!root || !state.result) return;
  const input = root.querySelector(
    `.doc-input[data-key="${key}"][data-tab="${tabId}"]`
  );
  // 空输入框没有可定位的内容：只清掉上一次的高亮，不滚动预览区
  if (!input || !String(input.value || "").trim()) {
    clearFieldHighlight();
    return;
  }
  const bill = (state.result.house_bills || []).find((item) => item.bill_id === tabId);
  const boxes = (bill?.field_locations || {})[key] || [];
  clearFieldHighlight();
  if (!boxes.length) {
    locateEmptyFallback((bill?.page_indexes || [])[0]);
    return;
  }
  // 滚到高亮框中心，避免框落在可视区外被裁掉（放大后同样按实时尺寸计算）
  scrollPreviewToBox(boxes[0].page, boxes[0]);
  drawLocateBoxes(boxes);
  // 高亮常显：下次点击其他字段、切换页签或关闭弹窗时才清除
}

// 舱单行字段：点击时同样在左侧原图上定位高亮
function highlightManifestField(entryId, key) {
  if (!root || !state.result) return;
  const input = root.querySelector(
    `.doc-input[data-manifest-entry="${entryId}"][data-key="${key}"]`
  );
  if (!input || !String(input.value || "").trim()) {
    clearFieldHighlight();
    return;
  }
  const entry = (state.result.manifest?.entries || []).find(
    (item) => item.entry_id === entryId
  );
  const boxes = (entry?.field_locations || {})[key] || [];
  clearFieldHighlight();
  if (!boxes.length) {
    const pageIndex = (
      entry?.page_indexes || state.result.manifest?.page_indexes || []
    )[0];
    locateEmptyFallback(pageIndex);
    return;
  }
  scrollPreviewToBox(boxes[0].page, boxes[0]);
  drawLocateBoxes(boxes);
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
    <button type="button" class="doc-submit" data-action="submit" ${
      state.submitted || state.submitting ? "disabled" : ""
    }>${submitLabel}</button>
  </div>`;
}

// ---------------------------------------------------------------- file tabs

function renderFileTabs() {
  const tabs = state.fileTabs || [];
  if (tabs.length <= 1) return "";
  const activeId = state.result?.task_id || "";
  const items = tabs
    .map(
      (tab) => `<button type="button" role="tab" data-file-tab="${esc(tab.taskId)}"
        class="doc-file-tab${tab.taskId === activeId ? " is-active" : ""}${
          tab.submitted ? " is-submitted" : ""
        }"
        aria-selected="${tab.taskId === activeId}"
        title="${esc(tab.name)}${tab.submitted ? "（已提交）" : ""}">
        ${tab.submitted ? '<span class="doc-file-tab-icon" aria-hidden="true">✓</span>' : ""}
        <span class="doc-file-tab-name">${esc(tab.name)}</span>
      </button>`
    )
    .join("");
  return `<div class="doc-file-tabs" role="tablist" aria-label="最近完成的任务">${items}</div>`;
}

function refreshFileTabs() {
  const strip = root?.querySelector(".doc-file-tabs");
  const html = renderFileTabs();
  if (!strip) {
    if (html) {
      const header = root?.querySelector(".doc-dialog-head");
      header?.insertAdjacentHTML("afterend", html);
    }
    return;
  }
  if (!html) {
    strip.remove();
    return;
  }
  strip.outerHTML = html;
}

async function loadFileTabs(activeTaskId) {
  try {
    const tasks = await api.listTasks();
    const done = (tasks || [])
      // 只显示有结果、且识别出了分单的任务（0 分单的任务不进页签）
      .filter(
        (task) =>
          task.status === "succeeded" &&
          task.result_available &&
          (task.house_bill_count || 0) > 0
      )
      .sort((left, right) =>
        String(right.finished_at || "").localeCompare(String(left.finished_at || ""))
      );
    const tabs = done.slice(0, MAX_FILE_TABS).map((task) => ({
      taskId: task.task_id,
      name: task.source_name || task.task_id,
      submitted: Boolean(task.submitted),
    }));
    // 当前任务不在最近 5 个里时，占掉最后一格，保证当前项一定能高亮
    if (activeTaskId && !tabs.some((tab) => tab.taskId === activeTaskId)) {
      const current = done.find((task) => task.task_id === activeTaskId);
      if (current) {
        const item = {
          taskId: current.task_id,
          name: current.source_name || current.task_id,
          submitted: Boolean(current.submitted),
        };
        if (tabs.length >= MAX_FILE_TABS) tabs[MAX_FILE_TABS - 1] = item;
        else tabs.push(item);
      }
    }
    state.fileTabs = tabs;
  } catch {
    state.fileTabs = [];
  }
}

function markFileTabSubmitted(taskId) {
  state.fileTabs = (state.fileTabs || []).map((tab) =>
    tab.taskId === taskId ? { ...tab, submitted: true } : tab
  );
  refreshFileTabs();
}

async function switchTask(taskId) {
  if (!taskId || taskId === state.result?.task_id || state.submitting) return;
  try {
    const snapshot = await api.getTask(taskId);
    if (snapshot.status !== "succeeded") {
      showToast("该任务尚未完成，暂不能查看", "error");
      return;
    }
    const result = await api.getResult(taskId);
    const fileTabs = state.fileTabs;
    const onSubmitted = state.onSubmitted;
    await openSubmitDialog({
      result,
      snapshot,
      fields: { house: state.houseFields, manifest: state.manifestFields },
      onSubmitted,
      fileTabs,
    });
  } catch (error) {
    showToast(error.message || "切换任务失败", "error");
  }
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
        </div>
        <button type="button" class="doc-dialog-close" data-action="close" aria-label="关闭">×</button>
      </header>
      ${renderFileTabs()}
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
  // 进入舱单页签即视为人工已查看，红点消失
  if (tabId === "manifest") state.manifestSeen = true;
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
  if (event.key !== "Escape") return;
  closeCalendar();
  // 阻止继续冒泡：否则弹窗自己的 ESC 处理会顺手把对话框也关掉
  event.stopPropagation();
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
    const missingKey = first.split(":").pop();
    showToast(
      `必填未填：${labelForKey(missingKey)}（共 ${errors.size} 项），请补充后再提交`,
      "error"
    );
    return;
  }

  // 暂时不做真实提交：只做必填校验，通过后仅提示
  state.submitting = false;
  state.errors = new Set();
  refreshForm();
  refreshFooter();
  showToast("必填字段校验通过");
  void documents;
}

function bindEvents() {
  if (root.dataset.bound === "1") return;
  root.dataset.bound = "1";
  // 日期输入框：第一次点击只聚焦并高亮定位，再点一次才打开日历
  let lastDateClick = { id: "", at: 0 };
  root.addEventListener("click", (event) => {
    const fileTab = event.target.closest("[data-file-tab]");
    if (fileTab) {
      switchTask(fileTab.dataset.fileTab);
      return;
    }
    const dateButton = event.target.closest("[data-date-input]");
    if (dateButton) {
      const inputId = dateButton.dataset.dateInput;
      const input = document.getElementById(inputId);
      if (!input || input.disabled) return;
      const isIcon = dateButton.classList.contains("awb-date-btn");
      const now = Date.now();
      const again = lastDateClick.id === inputId && now - lastDateClick.at < 1500;
      lastDateClick = { id: inputId, at: now };
      input.focus();
      if (isIcon || again) openCalendar(input);
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

  // 聚焦表单字段 → 左侧原图滚动到对应页并高亮字段值（分单/舱单都支持）
  root.addEventListener("focusin", (event) => {
    const input = event.target.closest(".doc-input[data-key]");
    if (!input) return;
    const tab = activeTab();
    if (!tab) return;
    if (tab.type === "house_bill") {
      highlightFieldLocation(tab.id, input.dataset.key);
    } else if (tab.type === "manifest" && input.dataset.manifestEntry) {
      highlightManifestField(input.dataset.manifestEntry, input.dataset.key);
    }
  });

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

// 焦点不在弹窗内时也能用 ESC 关闭；日历打开时 ESC 只关日历
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || calendarPop) return;
  if (isSubmitDialogOpen() && !state.submitting) {
    close();
  }
});

// ------------------------------------------------------------------ public

export async function openSubmitDialog({
  result,
  snapshot,
  fields,
  onSubmitted,
  focus,
  fileTabs,
}) {
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
  // 页签：切换任务时沿用同一批；首次打开按「最近完成」重新加载
  if (Array.isArray(fileTabs) && fileTabs.length) {
    state.fileTabs = fileTabs;
  } else {
    await loadFileTabs(result.task_id);
  }
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
  // 默认就打开舱单时不算「未查看」，红点不出现
  state.manifestSeen = state.activeTab === "manifest";

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
      const focusKey = HOUSE_KEY_ALIASES[focus.key] || focus.key;
      const row =
        root.querySelector(`[data-row="${focus.tabId}:${focusKey}"]`) ||
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
  const houseKey = HOUSE_KEY_ALIASES[key] || key;
  const row = key.startsWith("manifest:")
    ? root.querySelector(`[data-row="${key}"]`)
    : root.querySelector(`[data-row="${tabId}:${houseKey}"]`);
  const fallbackRow = row || root.querySelector("#docFormPane");
  fallbackRow?.scrollIntoView({ block: "center", behavior: "smooth" });
  fallbackRow?.classList.add("is-highlight");
  setTimeout(() => fallbackRow?.classList.remove("is-highlight"), 1600);
}
