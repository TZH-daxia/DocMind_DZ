// 后端接口封装。BASE 与后端 api_prefix 保持一致（默认 /docmind_dz）。
const BASE = "/docmind_dz/analysis";

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, options);
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const detail = payload && payload.detail;
    const message =
      (detail && detail.message) ||
      (typeof detail === "string" ? detail : null) ||
      `请求失败（HTTP ${response.status}）`;
    const error = new Error(message);
    error.code = detail && detail.code;
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export const api = {
  listTasks: () => request("/tasks"),

  getTask: (taskId) => request(`/tasks/${encodeURIComponent(taskId)}`),

  getResult: (taskId) => request(`/tasks/${encodeURIComponent(taskId)}/result`),

  getFields: () => request("/fields"),

  searchPorts: (keyword) =>
    request(`/ports?keyword=${encodeURIComponent(keyword || "")}`),

  getEvents: (taskId) =>
    request(`/tasks/${encodeURIComponent(taskId)}/events/history`),

  // 一个文件一个任务：后端只接受单个 file 字段
  createTask(file) {
    const form = new FormData();
    form.append("file", file);
    return request("/tasks", { method: "POST", body: form });
  },

  pauseTask: (taskId) =>
    request(`/tasks/${encodeURIComponent(taskId)}/pause`, { method: "POST" }),

  resumeTask: (taskId) =>
    request(`/tasks/${encodeURIComponent(taskId)}/resume`, { method: "POST" }),

  cancelTask: (taskId) =>
    request(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }),

  getSubmission: (taskId) =>
    request(`/tasks/${encodeURIComponent(taskId)}/submission`),

  subscribe(taskId, { onEvent, onDone, onError }) {
    const source = new EventSource(
      `${BASE}/tasks/${encodeURIComponent(taskId)}/events`
    );
    source.onmessage = (event) => {
      try {
        onEvent(JSON.parse(event.data));
      } catch {
        /* 忽略无法解析的事件 */
      }
    };
    source.addEventListener("done", (event) => {
      source.close();
      try {
        onDone(JSON.parse(event.data));
      } catch {
        onDone(null);
      }
    });
    source.onerror = () => {
      source.close();
      onError && onError();
    };
    return () => source.close();
  },

  pageImageUrl: (taskId, index) =>
    `${BASE}/tasks/${encodeURIComponent(taskId)}/pages/${index}`,
};
