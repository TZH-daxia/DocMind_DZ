let toastTimer = null;

export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function text(value) {
  if (value === null || value === undefined || String(value).trim() === "") {
    return "<span class=\"empty-value\">—</span>";
  }
  return esc(value);
}

export function fmtTime(iso) {
  if (!iso) return "--:--:--";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

export function fmtDateTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function showToast(message, kind = "info") {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast visible ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove("visible");
  }, 3000);
}

export function statusPill(label, className) {
  return `<span class="status-pill ${className}">${esc(label)}</span>`;
}

export function fileTypeTag(name) {
  const suffix = String(name || "").split(".").pop().toUpperCase();
  if (suffix === "XLSX" || suffix === "XLS") return "XLS";
  if (suffix === "DOCX" || suffix === "DOC") return "DOC";
  if (["PNG", "JPG", "JPEG", "BMP", "TIF", "TIFF", "WEBP"].includes(suffix)) return "IMG";
  return "PDF";
}


