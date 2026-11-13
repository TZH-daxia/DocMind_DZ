import { api } from "./api.js";
import { NODES, REASON_LABEL, STATUS_CLASS, STATUS_LABEL } from "./constants.js";
import {
  closeSubmitDialog,
  isSubmitDialogOpen,
  openSubmitDialog,
} from "./dialog.js";
import { esc, fileTypeTag, fmtTime, showToast } from "./ui.js";

const dom = {
  uploadDropzone: document.getElementById("uploadDropzone"),
  fileInput: document.getElementById("fileInput"),
  taskList: document.getElementById("taskList"),
  taskCount: document.getElementById("taskCount"),
  refreshButton: document.getElementById("refreshButton"),
  selectedStatus: document.getElementById("selectedStatus"),
  selectedTaskId: document.getElementById("selectedTaskId"),
  progressBar: document.getElementById("progressBar"),
  progressLabel: document.getElementById("progressLabel"),
  progressPercent: document.getElementById("progressPercent"),
  eventList: document.getElementById("eventList"),
  resultSummary: document.getElementById("resultSummary"),
  reviewArea: document.getElementById("reviewArea"),
  resultJson: document.getElementById("resultJson"),
  prepareButton: document.getElementById("prepareButton"),
  copyButton: document.getElementById("copyButton"),
  pauseButton: document.getElementById("pauseButton"),
  cancelButton: document.getElementById("cancelButton"),
};

// 单次批量上传的文件数上限（一个文件一个任务）
const MAX_UPLOAD_FILES = 5;

const state = {
  tasks: [],
  selectedId: null,
  snapshot: null,
  result: null,
  fields: { house: [], manifest: [] },
  events: [],
  unsubscribe: null,
  pollTimer: null,
  loadingResult: false,
};

// ---------------------------------------------------------------- catalog

async function loadFieldCatalog() {
  try {
    const catalog = await api.getFields();
    state.fields = {
      house: Array.isArray(catalog?.house) ? catalog.house : [],
      manifest: Array.isArray(catalog?.manifest) ? catalog.manifest : [],
    };
  } catch {
    state.fields = { house: [], manifest: [] };
  }
}

function fieldLabel(key) {
  const found =
    state.fields.house.find((field) => field.key === key) ||
    state.fields.manifest.find((field) => field.key === key);
  return found ? found.label : key;
}

// ---------------------------------------------------------------- tasks

async function refreshTasks() {
  try {
    state.tasks = await api.listTasks();
  } catch (error) {
    showToast(error.message, "error");
    return;
  }
  renderTaskList();
  if (!state.selectedId && state.tasks.length > 0) {
    selectTask(state.tasks[0].task_id);
  }
}

function renderTaskList() {
  dom.taskCount.textContent = String(state.tasks.length);
  if (state.tasks.length === 0) {
    dom.taskList.innerHTML = '<div class="empty-state">还没有任务</div>';
    return;
  }
  dom.taskList.innerHTML = state.tasks
    .map((task) => {
      const active = task.task_id === state.selectedId ? " selected" : "";
      let dot = STATUS_CLASS[task.status] || "idle";
      const submitted = task.submitted
        ? '<em class="file-flag submitted">已提交</em>'
        : "";
      // 已取消/已暂停用小标签表达，副标题留空避免同一状态写两遍（与 DocMind 一致）
      const stateFlag =
        task.status === "cancelled"
          ? '<em class="file-flag">已取消</em>'
          : task.status === "paused"
            ? '<em class="file-flag">已暂停</em>'
            : "";
      let detail =
        task.status === "cancelled" || task.status === "paused"
          ? ""
          : STATUS_LABEL[task.status] || task.status;
      if (task.status === "succeeded") {
        /*
         * 副标题只显示分单数量：有舱单时舱单行已经转成分单，两者最终都叫「分单」，
         * 不再单独列差异/舱单行数，避免同一份数据看两遍。
         */
        const houseCount = task.house_bill_count || 0;
        detail = `${houseCount} 分单`;
        // 0 分单（只有主单）：黄色，既不是正常结果也不是失败
        if (houseCount === 0) dot = "warn";
      }
      return `<button class="file-item${active}" data-task-id="${esc(task.task_id)}">
        <span class="file-type">${fileTypeTag(task.source_name)}</span>
        <span class="file-copy">
          <strong title="${esc(task.source_name)}">${esc(task.source_name)}</strong>
          <small>${esc(detail)}</small>
          ${stateFlag}${submitted}
        </span>
        <span class="status-dot ${dot}"></span>
      </button>`;
    })
    .join("");
}

function selectTask(taskId) {
  if (!taskId) return;
  if (isSubmitDialogOpen()) closeSubmitDialog();
  state.selectedId = taskId;
  state.snapshot = null;
  state.result = null;
  state.events = [];
  stopSubscription();
  stopPolling();
  renderTaskList();
  renderSelectedFile();
  renderEvents();
  dom.resultSummary.innerHTML = '<span class="muted">正在加载结果…</span>';
  dom.reviewArea.innerHTML = "";
  dom.resultJson.innerHTML = "<code>{}</code>";
  dom.prepareButton.disabled = true;
  dom.prepareButton.textContent = "开始提交";
  dom.copyButton.disabled = true;
  loadTaskDetail(taskId);
}

async function loadTaskDetail(taskId) {
  if (taskId !== state.selectedId) return;
  let snapshot;
  try {
    snapshot = await api.getTask(taskId);
  } catch (error) {
    showToast(error.message, "error");
    return;
  }
  if (taskId !== state.selectedId) return;
  state.snapshot = snapshot;
  renderSelectedFile();
  renderEvents();

  if (state.events.length === 0 && snapshot.status !== "running" && snapshot.status !== "queued") {
    try {
      state.events = await api.getEvents(taskId);
      renderEvents();
    } catch {
      state.events = [];
    }
  }

  if (snapshot.status === "succeeded") {
    await loadResult(taskId);
  } else if (snapshot.status === "failed") {
    renderFailure();
  } else if (snapshot.status === "cancelled") {
    renderCancelled();
  } else if (!isTerminal(snapshot.status)) {
    // 只有还在跑（或暂停）的任务才订阅实时事件；取消/失败/成功再订阅会重放历史
    subscribeTask(taskId);
  }
}

async function loadResult(taskId) {
  if (state.loadingResult) return;
  state.loadingResult = true;
  try {
    const result = await api.getResult(taskId);
    if (taskId !== state.selectedId) return;
    state.result = result;
    renderResult();
    /*
     * 运行过程的跳过原因要看 segments（有没有舱单/分单），
     * 结果加载完必须重绘一次，否则会停留在「无分单，已跳过」这类过期文案上。
     */
    renderEvents();
  } catch (error) {
    if (taskId !== state.selectedId) return;
    dom.resultSummary.innerHTML = `<span class="error-text">${esc(error.message)}</span>`;
  } finally {
    state.loadingResult = false;
  }
}

// ---------------------------------------------------------------- progress

function renderSelectedFile() {
  const snapshot = state.snapshot;
  const hasTask = Boolean(state.selectedId);
  // 暂停/继续/取消只在任务可操作时点亮（终态一律置灰）
  const controllable = ["queued", "running", "paused"].includes(snapshot?.status);
  dom.pauseButton.disabled = !hasTask || !controllable;
  dom.pauseButton.textContent = snapshot?.status === "paused" ? "继续" : "暂停";
  dom.cancelButton.disabled = !hasTask || !controllable;

  if (!snapshot) {
    dom.selectedStatus.className = "status-pill idle";
    dom.selectedStatus.textContent = hasTask ? "加载中" : "未选择任务";
    dom.selectedTaskId.textContent = hasTask ? "加载中…" : "任务 ID 未加载";
    dom.progressBar.style.width = "0%";
    dom.progressPercent.textContent = "0%";
    dom.progressLabel.textContent = "等待运行";
    return;
  }

  dom.selectedStatus.className = `status-pill ${STATUS_CLASS[snapshot.status] || "idle"}`;
  dom.selectedStatus.textContent = snapshot.submitted
    ? "已提交"
    : STATUS_LABEL[snapshot.status] || snapshot.status;
  /* 标题只放任务 ID（不带源文件名、不带创建时间） */
  dom.selectedTaskId.textContent = snapshot.task_id;
  const progress = snapshot.status === "succeeded" ? 100 : Number(snapshot.progress || 0);
  dom.progressBar.style.width = `${Math.max(0, Math.min(progress, 100))}%`;
  dom.progressPercent.textContent = `${progress}%`;
  dom.progressLabel.textContent = snapshot.step || STATUS_LABEL[snapshot.status];
}

/* 任务级事件，不是流水线节点，不放进节点列表 */
const IGNORED_STEPS = new Set(["处理失败"]);

/* 旧任务的节点名兼容：改名前的「抽取分单与舱单字段」并到新名上 */
const STEP_ALIASES = {
  抽取分单与舱单字段: "抽取分单或舱单字段",
};

function stepLabel(step) {
  return STEP_ALIASES[step] || step;
}

function nodeRows() {
  const byStep = new Map();
  const order = [];
  for (const event of state.events) {
    const step = stepLabel(event.step);
    if (!byStep.has(step)) {
      byStep.set(step, []);
      order.push(step);
    }
    byStep.get(step).push(event);
  }
  const succeeded = state.snapshot?.status === "succeeded";
  const failed = state.snapshot?.status === "failed";
  const failedStep = state.snapshot?.step;

  /*
   * 固定渲染 NODES 全部 7 行，再追加事件里出现的未知步骤。
   *
   * 条件边跳过、且没有 skipped 事件的节点（典型：只有舱单时的「分单局部精读」）
   * 也要保留一行，用「已跳过 + 原因」说明为什么没跑 —— 直接隐藏会让用户以为
   * 这个步骤不存在或漏了日志。
   */
  const known = new Set(NODES);
  const extra = order.filter(
    (label) => !known.has(label) && !IGNORED_STEPS.has(label)
  );
  const labels = NODES.concat(extra);

  return labels.map((label, index) => {
    const events = byStep.get(label) || [];
    const last = events[events.length - 1];
    // 节点耗时由节点完成事件（phase=succeeded/failed）带出
    const finished = [...events].reverse().find((event) => event.duration_ms != null);
    let status = "idle";
    let message = last ? last.message : "";
    let duration = finished ? finished.duration_ms : null;

    /*
     * 只要这个节点上报过 skipped，就一律按「已跳过」展示。
     * 典型：一致性核对已经取消，后端仍会先 started、再 skipped、最后 succeeded，
     * 取最后一条事件会把它画成「成功」，与事实不符。
     */
    const skippedEvent = events.find((event) => event.phase === "skipped");
    if (skippedEvent) {
      status = "skipped";
      message =
        label === "核对分单与舱单"
          ? "已取消一致性核对，分单字段直接取自舱单"
          : skippedEvent.message || skipReason(label);
      duration = null;
    } else if (last) {
      const nextStarted = labels.slice(index + 1).some((name) => byStep.has(name));
      if (last.level === "error" || (failed && failedStep === label)) {
        status = "failed";
      } else if (succeeded || nextStarted || finished?.phase === "succeeded") {
        status = "succeeded";
      } else {
        status = "started";
      }
    } else if (succeeded) {
      // 任务成功但没有事件 = 路由跳过了这个节点：保留一行并说明原因
      status = "skipped";
      message = skipReason(label);
    } else if (failed || state.snapshot?.status === "cancelled") {
      // 失败/取消后没走到的节点：留着但标「未执行」，不冒充已跳过
      status = "idle";
      message = "未执行";
    } else {
      // 还没轮到的节点：显示该步骤做什么，而不是干巴巴的「等待执行」
      message = NODE_HINTS[label] || "等待执行";
    }

    // 暂停/取消后不会再有完成事件：未跑完的节点退回"等待执行"
    const taskStatus = state.snapshot?.status;
    if ((taskStatus === "paused" || taskStatus === "cancelled") && status === "started") {
      status = "idle";
      message = "未执行";
      duration = null;
    }

    return {
      label,
      status,
      warning: last && last.level === "warning",
      message,
      at: last ? last.at : null,
      duration,
      busy: status === "started",
    };
  });
}

/* 各节点在「还没跑到」时的一句话说明 */
const NODE_HINTS = {
  解析文档: "渲染页图与文本层",
  "图像转写": "逐页视觉模型转写",
  "识别主单/分单/舱单": "规则 + 模型判定单据类型",
  "分单局部精读": "只对分单页做四象限放大转写",
  "抽取分单或舱单字段": "有舱单只抽舱单、没有舱单才抽分单",
  "核对分单与舱单": "一致性核对已取消",
  "生成结果": "装配结果与待审核清单",
};

/* 任务成功但该节点没有事件时的跳过原因 */
function skipReason(label) {
  const kinds = new Set((state.result?.segments || []).map((item) => item.kind));
  /*
   * 结果文件可能还没加载完（运行过程先渲染、结果后到），
   * 这时不能断言「无分单」——用抽取事件的文案兜底判断有没有舱单/分单。
   */
  const extractMessage =
    (state.events.find(
      (event) => stepLabel(event.step) === "抽取分单或舱单字段" && event.message
    )?.message || "");
  const hasManifest =
    kinds.has("manifest") || /舱单\s*\d+\s*行/.test(extractMessage);
  const hasHouse = kinds.has("house_bill") || /分单\s*\d+\s*份/.test(extractMessage);

  if (label === "分单局部精读") {
    if (hasManifest) return "有舱单，以舱单为准，已跳过";
    if (hasHouse) return "分单页无需精读，已跳过";
    if (kinds.size > 0) return "无分单，已跳过";
    return "按路由跳过";
  }
  if (label === "抽取分单或舱单字段" && kinds.size === 0 && !hasHouse && !hasManifest) {
    return "无分单与舱单，已跳过";
  }
  if (label === "核对分单与舱单") return "已取消一致性核对";
  return "已跳过";
}

function formatDuration(milliseconds) {
  const value = Number(milliseconds);
  if (!Number.isFinite(value)) return "";
  if (value >= 1000) return `${(value / 1000).toFixed(1)} s`;
  return `${value} ms`;
}

function renderEvents() {
  if (!state.selectedId) {
    dom.eventList.innerHTML = '<div class="empty-state">选择左侧任务查看运行过程</div>';
    return;
  }
  const rows = nodeRows();
  if (!rows.length) {
    dom.eventList.innerHTML = '<div class="empty-state">暂无运行事件</div>';
    return;
  }
  dom.eventList.innerHTML = rows
    .map(
      (row) => `<div class="event-row ${row.status}">
        <span class="event-marker ${row.status}"></span>
        <div class="event-copy">
          <strong>${esc(row.label)}</strong>
          <small class="${row.warning ? "warning-text" : ""}${
            row.status === "skipped" ? " is-skipped" : ""
          }">${esc(row.message || (row.status === "idle" ? "等待执行" : "—"))}</small>
        </div>
        <span class="event-value" title="${row.at ? esc(fmtTime(row.at)) : ""}">${
          row.duration != null
            ? formatDuration(row.duration)
            : row.status === "skipped"
              ? "已跳过"
              : row.status === "started"
                ? "进行中"
                : row.status === "idle" && row.message !== "未执行"
                  ? "等待"
                  : ""
        }</span>
      </div>`
    )
    .join("");
}

// ---------------------------------------------------------------- result

function compactPayload(result) {
  return {
    task_id: result.task_id,
    source_name: result.source_name,
    master_bills: (result.master_bills || []).map((item) => ({
      master_no: item.master_no,
      page_indexes: item.page_indexes,
      skipped: item.skipped,
    })),
    house_bills: (result.house_bills || []).map((bill) => ({
      bill_id: bill.bill_id,
      hawb_no: bill.fields?.hawb,
      page_indexes: bill.page_indexes,
      confidence: bill.confidence,
      fields: bill.fields,
      candidates: bill.candidates || [],
    })),
    summary: result.summary,
    totals_checks: result.totals_checks,
    review_items: result.review_items,
  };
}

function renderResult() {
  const result = state.result;
  const snapshot = state.snapshot;
  if (!result || !snapshot) return;

  if (snapshot.status === "failed") {
    renderFailure();
    return;
  }

  const summary = result.summary || {};
  // 只放单据数量（分单数 + 舱单来源行数）；待审核明细在下方单独一块
  const pills = [
    `<span class="summary-pill">分单 <b>${summary.total_house_bills ?? 0}</b></span>`,
    `<span class="summary-pill">舱单 <b>${summary.total_manifest_entries ?? 0}</b> 行</span>`,
    `<span class="summary-pill">主单跳过 <b>${(result.master_bills || []).length}</b></span>`,
  ];
  if (snapshot.submitted) {
    pills.push(
      `<span class="summary-pill submitted-pill">已提交 · ${esc(
        snapshot.submission_code || ""
      )}</span>`
    );
  }
  dom.resultSummary.innerHTML = pills.join("");

  renderReviewList(result);
  dom.resultJson.innerHTML = `<code>${esc(
    JSON.stringify(compactPayload(result), null, 2)
  )}</code>`;

  dom.copyButton.disabled = false;
  // 没有分单就没有可提交的内容：按钮只置灰，文案保持原样
  dom.prepareButton.disabled = (result.house_bills || []).length === 0;
  dom.prepareButton.textContent = snapshot.submitted ? "已提交 · 查看" : "开始提交";
}

function renderReviewList(result) {
  // 一致性核对已取消，不再有「差异」；结果区只保留待审核并整行铺满
  const items = result.review_items || [];
  const warnings = result.warnings || [];
  const summary = result.summary || {};
  const hasHouse = (result.house_bills || []).length > 0;
  /*
   * 只有「分单和舱单都没抽到」才算没可提取的内容。
   * 有舱单时舱单行已转成分单，行数看 summary，不能再按「0 分单」判死。
   */
  const hasManifest = (summary.total_manifest_entries || 0) > 0;
  const notice =
    !hasHouse && !hasManifest && warnings.length
      ? `<div class="result-notice">
          <strong>没有可提取的内容</strong>
          <span>${esc(warnings.join("；"))}</span>
        </div>`
      : "";
  dom.reviewArea.innerHTML = `${notice}
    ${renderReviewBlock("待审核", items, "review")}
  `;
}

function renderReviewBlock(title, items, kind) {
  if (items.length === 0) {
    return `<div class="review-block">
      <div class="review-head"><span>${title}</span><b class="muted">0</b></div>
      <div class="review-empty">没有${title}项</div>
    </div>`;
  }
  const rows = items
    .map((item) => {
      const tab = state.result.house_bills.find((bill) => bill.bill_id === item.tab_id);
      const hawb = tab?.fields?.hawb || (item.tab_id === "manifest" ? "舱单" : item.tab_id);
      return `<button class="review-item ${kind}" data-tab="${esc(
        item.tab_id
      )}" data-key="${esc(item.key)}">
        <span class="review-tag">${esc(REASON_LABEL[item.reason] || item.reason)}</span>
        <span class="review-copy">
          <strong>${esc(hawb)} · ${esc(item.label || fieldLabel(item.key))}</strong>
          <small>${esc(item.message)}</small>
        </span>
      </button>`;
    })
    .join("");
  return `<div class="review-block">
    <div class="review-head"><span>${title}</span><b>${items.length}</b></div>
    <div class="review-list">${rows}</div>
  </div>`;
}

function renderCancelled() {
  // 取消没有核对结果：结果区清空，状态交给进度区与列表标签表达（同 DocMind）
  dom.resultSummary.innerHTML = "";
  dom.reviewArea.innerHTML = "";
  dom.resultJson.innerHTML = "<code>{}</code>";
  dom.copyButton.disabled = true;
  dom.prepareButton.disabled = true;
  dom.prepareButton.textContent = "开始提交";
}

function renderFailure() {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  const error = snapshot.error || { code: "INTERNAL_ERROR", message: "任务执行失败" };
  dom.resultSummary.innerHTML = "";
  dom.reviewArea.innerHTML = `<div class="result-error">
    <div class="result-error-title">任务失败 · ${esc(error.code)}</div>
    <div class="result-error-body">${esc(error.message)}</div>
  </div>`;
  dom.resultJson.innerHTML = "<code>{}</code>";
  dom.prepareButton.disabled = true;
  dom.prepareButton.textContent = "开始提交";
  dom.copyButton.disabled = true;
}

// ---------------------------------------------------------------- dialog

function openDialog(focus) {
  if (!state.result || !state.snapshot) {
    showToast("结果尚未加载完成", "error");
    return;
  }
  if (state.snapshot.status !== "succeeded") {
    showToast("任务尚未成功完成", "error");
    return;
  }
  openSubmitDialog({
    result: state.result,
    snapshot: state.snapshot,
    fields: state.fields,
    focus,
    onSubmitted(response, taskId) {
      // 在弹窗里切换任务后提交：只刷新列表，不覆盖当前选中任务的状态
      if (taskId && taskId !== state.selectedId) {
        refreshTasks();
        return;
      }
      state.snapshot = {
        ...state.snapshot,
        submitted: true,
        submission_code: response.submission_code,
        submitted_at: response.submitted_at,
      };
      refreshTasks();
      renderSelectedFile();
      renderResult();
    },
  }).catch((error) => showToast(error.message || "弹窗打开失败", "error"));
}

// ---------------------------------------------------------------- requests

async function uploadFiles(input) {
  const files = (input ? Array.from(input) : []).filter(Boolean);
  if (files.length === 0) return;
  // 超过上限整批拒绝：提示后清空选择，等用户重新选，不做部分上传
  if (files.length > MAX_UPLOAD_FILES) {
    showToast(
      `单次最多上传 ${MAX_UPLOAD_FILES} 个文件，当前选中 ${files.length} 个，请重新选择`,
      "warning"
    );
    dom.fileInput.value = "";
    return;
  }
  const batch = files;
  const maxBytes = 50 * 1024 * 1024;
  const tooLarge = batch.find((file) => file.size > maxBytes);
  if (tooLarge) {
    showToast(`${tooLarge.name} 超过 50MB 限制`, "error");
    dom.fileInput.value = "";
    return;
  }
  showToast(batch.length > 1 ? `正在上传 ${batch.length} 个文件…` : `正在上传 ${batch[0].name}…`);

  // 一个文件一个任务：逐个创建（串行，避免瞬时打满后端并发）
  const created = [];
  const failed = [];
  try {
    for (const file of batch) {
      try {
        const snapshot = await api.createTask(file);
        created.push(snapshot.task_id);
      } catch (error) {
        failed.push(`${file.name}：${error.message}`);
      }
    }
    if (created.length) {
      showToast(
        `已创建 ${created.length} 个任务${failed.length ? `，${failed.length} 个失败` : ""}`
      );
      await refreshTasks();
      selectTask(created[created.length - 1]);
    } else {
      showToast(failed[0] || "上传失败", "error");
    }
    if (failed.length > 1) {
      showToast(failed.join("；"), "error");
    }
  } finally {
    dom.fileInput.value = "";
  }
}

function isTerminal(status) {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

function subscribeTask(taskId) {
  stopSubscription();
  state.unsubscribe = api.subscribe(taskId, {
    onEvent(event) {
      if (taskId !== state.selectedId) return;
      // 重连会从头重放历史事件，按 seq 去重，避免进度文案与时间线反复跳变
      if (state.events.some((item) => item.seq === event.seq)) return;
      state.events.push(event);
      const latest = state.events[state.events.length - 1];
      if (latest) {
        dom.progressLabel.textContent = latest.message || latest.step;
        dom.progressBar.style.width = `${latest.progress}%`;
        dom.progressPercent.textContent = `${latest.progress}%`;
      }
      renderEvents();
    },
    onDone(snapshot) {
      if (taskId !== state.selectedId) return;
      if (snapshot) state.snapshot = snapshot;
      renderSelectedFile();
      renderEvents();
      // 终态不再重新订阅（否则会重放历史、反复刷新进度文案）
      if (snapshot?.status === "succeeded") {
        loadResult(taskId);
      } else if (snapshot?.status === "failed") {
        renderFailure();
      }
      refreshTasks();
    },
    onError() {
      if (taskId !== state.selectedId) return;
      startPolling(taskId);
    },
  });
}

function stopSubscription() {
  if (state.unsubscribe) {
    state.unsubscribe();
    state.unsubscribe = null;
  }
}

function startPolling(taskId) {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    if (taskId !== state.selectedId) {
      stopPolling();
      return;
    }
    try {
      const snapshot = await api.getTask(taskId);
      state.snapshot = snapshot;
      renderSelectedFile();
      renderEvents();
      if (snapshot.status === "succeeded") {
        stopPolling();
        await loadResult(taskId);
        refreshTasks();
      } else if (snapshot.status === "failed") {
        stopPolling();
        renderFailure();
        refreshTasks();
      } else if (snapshot.status === "cancelled") {
        stopPolling();
        renderCancelled();
        refreshTasks();
      }
    } catch {
      stopPolling();
    }
  }, 2000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function togglePauseTask() {
  if (!state.selectedId) return;
  const paused = state.snapshot?.status === "paused";
  try {
    const snapshot = paused
      ? await api.resumeTask(state.selectedId)
      : await api.pauseTask(state.selectedId);
    if (snapshot) state.snapshot = snapshot;
    renderSelectedFile();
    renderEvents();
    await refreshTasks();
    showToast(paused ? "任务已继续" : "任务已暂停");
    // 暂停后 SSE 不会结束；只有连接意外断开时才重新订阅
    if (paused && !state.unsubscribe) subscribeTask(state.selectedId);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function cancelTask() {
  if (!state.selectedId) return;
  if (!window.confirm("取消后不可恢复，确定取消该任务？")) return;
  try {
    const snapshot = await api.cancelTask(state.selectedId);
    if (snapshot) state.snapshot = snapshot;
    stopSubscription();
    stopPolling();
    await refreshTasks();
    renderSelectedFile();
    renderEvents();
    renderCancelled();
    showToast("任务已取消");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function copyJson() {
  if (!state.result) return;
  try {
    await navigator.clipboard.writeText(
      JSON.stringify(compactPayload(state.result), null, 2)
    );
    showToast("JSON 已复制");
  } catch {
    showToast("复制失败，请检查浏览器权限", "error");
  }
}

// ---------------------------------------------------------------- events

function bindEvents() {
  dom.fileInput.addEventListener("change", (event) => uploadFiles(event.target.files));

  ["dragenter", "dragover"].forEach((type) => {
    dom.uploadDropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dom.uploadDropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    dom.uploadDropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dom.uploadDropzone.classList.remove("dragover");
    });
  });
  dom.uploadDropzone.addEventListener("drop", (event) => {
    uploadFiles(event.dataTransfer && event.dataTransfer.files);
  });

  dom.taskList.addEventListener("click", (event) => {
    const item = event.target.closest(".file-item");
    if (item) selectTask(item.dataset.taskId);
  });

  dom.reviewArea.addEventListener("click", (event) => {
    const item = event.target.closest(".review-item");
    if (!item) return;
    openDialog({ tabId: item.dataset.tab, key: item.dataset.key });
  });

  dom.prepareButton.addEventListener("click", () => openDialog());
  dom.copyButton.addEventListener("click", copyJson);
  dom.refreshButton.addEventListener("click", refreshTasks);
  dom.pauseButton.addEventListener("click", togglePauseTask);
  dom.cancelButton.addEventListener("click", cancelTask);

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isSubmitDialogOpen()) {
      // 弹窗内部自行处理，这里只避免快捷键冲突
    }
  });
}

async function init() {
  bindEvents();
  renderEvents();
  await loadFieldCatalog();
  await refreshTasks();
  const activeStatuses = ["running", "queued", "paused"];
  setInterval(() => {
    if (state.tasks.some((task) => activeStatuses.includes(task.status))) {
      refreshTasks();
    }
  }, 5000);
}

init();
