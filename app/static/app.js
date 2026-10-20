import { api } from "./api.js";
import { NODES, REASON_LABEL, STATUS_CLASS, STATUS_LABEL } from "./constants.js";
import {
  closeSubmitDialog,
  isSubmitDialogOpen,
  openSubmitDialog,
} from "./dialog.js";
import {
  esc,
  fileTypeTag,
  fmtDateTime,
  fmtTime,
  showToast,
} from "./ui.js";

const dom = {
  uploadDropzone: document.getElementById("uploadDropzone"),
  fileInput: document.getElementById("fileInput"),
  taskList: document.getElementById("taskList"),
  taskCount: document.getElementById("taskCount"),
  refreshButton: document.getElementById("refreshButton"),
  selectedStatus: document.getElementById("selectedStatus"),
  selectedType: document.getElementById("selectedType"),
  selectedName: document.getElementById("selectedName"),
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
      const dot = STATUS_CLASS[task.status] || "idle";
      const submitted = task.submitted
        ? '<em class="file-flag submitted">已提交</em>'
        : "";
      let detail = STATUS_LABEL[task.status] || task.status;
      if (task.status === "succeeded") {
        detail = `${task.house_bill_count} 分单 · ${
          task.inconsistent_count > 0 ? `${task.inconsistent_count} 差异` : "无字段冲突"
        }`;
      }
      return `<button class="file-item${active}" data-task-id="${esc(task.task_id)}">
        <span class="file-type">${fileTypeTag(task.source_name)}</span>
        <span class="file-copy">
          <strong title="${esc(task.source_name)}">${esc(task.source_name)}</strong>
          <small>${esc(detail)}</small>
          ${submitted}
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
  } else {
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
    dom.selectedType.textContent = "—";
    dom.selectedName.textContent = hasTask ? "加载中…" : "请选择左侧任务";
    dom.selectedTaskId.textContent = "任务 ID 未加载";
    dom.progressBar.style.width = "0%";
    dom.progressPercent.textContent = "0%";
    dom.progressLabel.textContent = "等待运行";
    return;
  }

  dom.selectedStatus.className = `status-pill ${STATUS_CLASS[snapshot.status] || "idle"}`;
  dom.selectedStatus.textContent = snapshot.submitted
    ? "已提交"
    : STATUS_LABEL[snapshot.status] || snapshot.status;
  dom.selectedType.textContent = fileTypeTag(snapshot.source_name);
  dom.selectedName.textContent = snapshot.source_name;
  dom.selectedTaskId.textContent = `${snapshot.task_id} · ${fmtDateTime(
    snapshot.created_at
  )}`;
  const progress = snapshot.status === "succeeded" ? 100 : Number(snapshot.progress || 0);
  dom.progressBar.style.width = `${Math.max(0, Math.min(progress, 100))}%`;
  dom.progressPercent.textContent = `${progress}%`;
  dom.progressLabel.textContent = snapshot.step || STATUS_LABEL[snapshot.status];
}

function nodeRows() {
  const byStep = new Map();
  for (const event of state.events) {
    if (!byStep.has(event.step)) byStep.set(event.step, []);
    byStep.get(event.step).push(event);
  }
  const succeeded = state.snapshot?.status === "succeeded";
  const failed = state.snapshot?.status === "failed";
  const failedStep = state.snapshot?.step;

  return NODES.map((label, index) => {
    const events = byStep.get(label) || [];
    const last = events[events.length - 1];
    // 节点耗时由节点完成事件（phase=succeeded/failed）带出
    const finished = [...events].reverse().find((event) => event.duration_ms != null);
    let status = "idle";
    if (last) {
      const nextStarted = NODES.slice(index + 1).some((name) => byStep.has(name));
      if (last.level === "error" || (failed && failedStep === label)) {
        status = "failed";
      } else if (succeeded || nextStarted || finished?.phase === "succeeded") {
        status = "succeeded";
      } else {
        status = "started";
      }
    } else if (failed) {
      status = "skipped";
    }
    let message = last ? last.message : "";
    let duration = finished ? finished.duration_ms : null;
    // 暂停/取消后不会再有完成事件：未跑完的节点退回"等待执行"
    const taskStatus = state.snapshot?.status;
    if ((taskStatus === "paused" || taskStatus === "cancelled") && status === "started") {
      status = "idle";
      message = "";
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
  dom.eventList.innerHTML = nodeRows()
    .map(
      (row) => `<div class="event-row">
        <span class="event-marker ${row.status}"></span>
        <div class="event-copy">
          <strong>${esc(row.label)}</strong>
          <small class="${row.warning ? "warning-text" : ""}">${esc(
            row.message || (row.status === "idle" ? "等待执行" : "—")
          )}</small>
        </div>
        <span class="event-value" title="${row.at ? esc(fmtTime(row.at)) : ""}">${
          row.duration != null
            ? formatDuration(row.duration)
            : row.status === "skipped"
              ? "已跳过"
              : row.status === "started"
                ? "进行中"
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
      hawb_no: bill.fields?.hawb_no,
      page_indexes: bill.page_indexes,
      confidence: bill.confidence,
      fields: bill.fields,
    })),
    manifest: result.manifest
      ? {
          page_indexes: result.manifest.page_indexes,
          entries: (result.manifest.entries || []).map((entry) => ({
            line_no: entry.line_no,
            fields: entry.fields,
          })),
        }
      : null,
    summary: result.summary,
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
  const pills = [
    `<span class="summary-pill">分单 <b>${summary.total_house_bills ?? 0}</b></span>`,
    `<span class="summary-pill">舱单 <b>${summary.total_manifest_entries ?? 0}</b> 行</span>`,
    `<span class="summary-pill">主单跳过 <b>${(result.master_bills || []).length}</b></span>`,
    `<span class="summary-pill ready">一致 <b>${summary.consistent ?? 0}</b></span>`,
    `<span class="summary-pill ${
      summary.inconsistent > 0 ? "failed" : ""
    }">差异 <b>${summary.inconsistent ?? 0}</b></span>`,
    `<span class="summary-pill ${
      (result.review_items || []).filter((item) => item.reason !== "mismatch").length > 0
        ? "review"
        : ""
    }">待审核 <b>${
      (result.review_items || []).filter((item) => item.reason !== "mismatch").length
    }</b></span>`,
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
  dom.prepareButton.disabled = false;
  dom.prepareButton.textContent = snapshot.submitted ? "已提交 · 查看" : "开始提交";
}

function renderReviewList(result) {
  const items = result.review_items || [];
  const diffs = items.filter((item) => item.reason === "mismatch");
  const pending = items.filter((item) => item.reason !== "mismatch");
  dom.reviewArea.innerHTML = `
    ${renderReviewBlock("差异", diffs, "failed")}
    ${renderReviewBlock("待审核", pending, "review")}
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
      const hawb = tab?.fields?.hawb_no || (item.tab_id === "manifest" ? "舱单" : item.tab_id);
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
    onSubmitted(response) {
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
  const maxBytes = 50 * 1024 * 1024;
  const tooLarge = files.find((file) => file.size > maxBytes);
  if (tooLarge) {
    showToast(`${tooLarge.name} 超过 50MB 限制`, "error");
    return;
  }
  showToast(files.length > 1 ? `正在上传 ${files.length} 个文件…` : `正在上传 ${files[0].name}…`);
  try {
    const snapshot = await api.createTask(files);
    showToast("任务已创建，开始识别");
    await refreshTasks();
    selectTask(snapshot.task_id);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    dom.fileInput.value = "";
  }
}

function subscribeTask(taskId) {
  stopSubscription();
  state.unsubscribe = api.subscribe(taskId, {
    onEvent(event) {
      if (taskId !== state.selectedId) return;
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
      loadTaskDetail(taskId);
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
