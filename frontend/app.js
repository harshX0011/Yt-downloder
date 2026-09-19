/* Authorized Media Downloader - front end.
 *
 * Everything rendered here comes from the API. Text is written with
 * textContent and links are built as DOM nodes, so no response field is ever
 * interpreted as markup.
 */

"use strict";

const el = (id) => document.getElementById(id);

const dom = {
  form: el("inspect-form"),
  url: el("url"),
  checkButton: el("check-button"),
  alert: el("alert"),
  result: el("result"),
  embedHolder: el("embed-holder"),
  thumbnail: el("thumbnail"),
  thumbFallback: el("thumb-fallback"),
  sourceBadge: el("source-badge"),
  title: el("title"),
  meta: el("meta"),
  description: el("description"),
  metadataSource: el("metadata-source"),
  downloadBlock: el("download-block"),
  format: el("format"),
  downloadButton: el("download-button"),
  cancelButton: el("cancel-button"),
  progressBlock: el("progress-block"),
  progressState: el("progress-state"),
  progressDetail: el("progress-detail"),
  progressBar: el("progress-bar"),
  progressFill: el("progress-fill"),
  progressLive: el("progress-live"),
  readyBlock: el("ready-block"),
  readyDetail: el("ready-detail"),
  saveLink: el("save-link"),
  blockedBlock: el("blocked-block"),
  blockedReason: el("blocked-reason"),
  alternatives: el("alternatives"),
  policySources: el("policy-sources"),
  policyLimits: el("policy-limits"),
  policyYoutube: el("policy-youtube"),
};

const state = {
  inspected: null,
  jobId: null,
  eventSource: null,
  inFlight: false,
};

const STATE_LABELS = {
  queued: "Queued",
  preparing: "Preparing",
  downloading: "Downloading",
  processing: "Converting to MP4",
  completed: "Complete",
  failed: "Failed",
  cancelled: "Cancelled",
};

/* ---------- formatting helpers ---------- */

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return null;
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return null;
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

function formatCount(value) {
  if (value === null || value === undefined) return null;
  return value.toLocaleString();
}

function formatEta(seconds) {
  if (!seconds || seconds <= 0) return null;
  if (seconds < 60) return `${Math.round(seconds)}s left`;
  const minutes = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${minutes}m ${String(secs).padStart(2, "0")}s left`;
}

function describeFormat(format) {
  const bits = [];
  if (format.quality_label) bits.push(format.quality_label);
  else if (format.height) bits.push(`${format.height}p`);
  if (format.container) bits.push(format.container.toUpperCase());
  if (!format.has_audio) bits.push("no audio track");
  const size = formatBytes(format.filesize_bytes);
  if (size) bits.push(size);
  return bits.join(" - ") || format.format_id;
}

/* ---------- ui state ---------- */

function showAlert(message) {
  dom.alert.textContent = message;
  dom.alert.hidden = false;
}

function clearAlert() {
  dom.alert.textContent = "";
  dom.alert.hidden = true;
}

function setBusy(button, busy) {
  button.disabled = busy;
  button.classList.toggle("is-busy", busy);
}

function resetResult() {
  closeEventSource();
  state.jobId = null;
  dom.result.hidden = true;
  dom.embedHolder.hidden = true;
  dom.embedHolder.replaceChildren();
  dom.thumbnail.hidden = true;
  dom.thumbnail.removeAttribute("src");
  dom.thumbFallback.hidden = true;
  dom.description.hidden = true;
  dom.downloadBlock.hidden = true;
  dom.progressBlock.hidden = true;
  dom.readyBlock.hidden = true;
  dom.blockedBlock.hidden = true;
  dom.cancelButton.hidden = true;
  dom.meta.replaceChildren();
  dom.alternatives.replaceChildren();
  dom.format.replaceChildren();
  setBusy(dom.downloadButton, false);
}

function addMetaRow(label, value, href) {
  if (value === null || value === undefined || value === "") return;
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  if (href) {
    const link = document.createElement("a");
    link.href = href;
    link.textContent = value;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    dd.appendChild(link);
  } else {
    dd.textContent = value;
  }
  dom.meta.append(dt, dd);
}

/* ---------- api ---------- */

async function apiCall(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error(
      "Could not reach the server. Check your connection and try again."
    );
  }

  if (response.status === 204) return null;

  let payload = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
  }

  if (!response.ok) {
    if (response.status === 429) {
      const retry = response.headers.get("retry-after");
      throw new Error(
        (payload && payload.error) ||
          `Too many requests. Try again in ${retry || "a few"} seconds.`
      );
    }
    const detail =
      (payload && (payload.error || payload.detail)) ||
      `The server returned an error (HTTP ${response.status}).`;
    throw new Error(
      typeof detail === "string" ? detail : `The request failed (HTTP ${response.status}).`
    );
  }

  return payload;
}

/* ---------- rendering ---------- */

function renderResult(data) {
  resetResult();
  state.inspected = data;

  dom.sourceBadge.textContent = data.source_name || data.source_kind;
  dom.title.textContent = data.title || "Untitled";

  addMetaRow("Duration", formatDuration(data.duration_seconds) || (
    data.source_kind === "youtube" ? "Not exposed by this endpoint" : null
  ));
  addMetaRow("Channel", data.uploader, data.uploader_url || undefined);
  addMetaRow("Published", data.upload_date);
  addMetaRow("Views", formatCount(data.view_count));
  addMetaRow("Licence", data.license_name);
  addMetaRow("Page", data.webpage_url, data.webpage_url || undefined);

  if (data.description) {
    dom.description.textContent = data.description;
    dom.description.hidden = false;
  }
  dom.metadataSource.textContent = data.metadata_source
    ? `Metadata read from: ${data.metadata_source}`
    : "";

  if (data.embed_url) {
    const frame = document.createElement("iframe");
    frame.src = data.embed_url;
    frame.title = data.title ? `YouTube player: ${data.title}` : "YouTube player";
    frame.allow = "accelerometer; encrypted-media; gyroscope; picture-in-picture";
    frame.setAttribute("allowfullscreen", "");
    frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
    frame.loading = "lazy";
    dom.embedHolder.appendChild(frame);
    dom.embedHolder.hidden = false;
  } else if (data.thumbnail_url) {
    dom.thumbnail.src = data.thumbnail_url;
    dom.thumbnail.alt = data.title ? `Thumbnail for ${data.title}` : "Video thumbnail";
    dom.thumbnail.hidden = false;
    dom.thumbnail.onerror = () => {
      dom.thumbnail.hidden = true;
      dom.thumbFallback.hidden = false;
    };
  } else {
    dom.thumbFallback.hidden = false;
  }

  if (data.downloadable && data.formats.length > 0) {
    for (const format of data.formats) {
      const option = document.createElement("option");
      option.value = format.format_id;
      option.textContent = describeFormat(format);
      if (format.format_id === data.recommended_format_id) option.selected = true;
      dom.format.appendChild(option);
    }
    dom.downloadBlock.hidden = false;
  } else {
    dom.blockedReason.textContent =
      data.download_blocked_reason ||
      "This source does not publish a file that can be downloaded here.";
    dom.alternatives.replaceChildren();
    for (const alternative of data.authorized_alternatives || []) {
      const item = document.createElement("li");
      const name = document.createElement("strong");
      if (alternative.url) {
        const link = document.createElement("a");
        link.href = alternative.url;
        link.textContent = alternative.title;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        name.appendChild(link);
      } else {
        name.textContent = alternative.title;
      }
      item.appendChild(name);
      item.appendChild(document.createTextNode(alternative.detail));
      dom.alternatives.appendChild(item);
    }
    dom.blockedBlock.hidden = false;
  }

  dom.result.hidden = false;
}

function renderProgress(status) {
  dom.progressBlock.hidden = false;
  dom.progressState.textContent = STATE_LABELS[status.state] || status.state;

  const indeterminate =
    !status.total_bytes && (status.state === "downloading" || status.state === "preparing");
  dom.progressFill.classList.toggle("indeterminate", indeterminate);

  if (indeterminate) {
    dom.progressFill.style.width = "";
    dom.progressBar.removeAttribute("aria-valuenow");
  } else {
    const percent = status.state === "completed" ? 100 : status.progress_percent || 0;
    dom.progressFill.style.width = `${percent}%`;
    dom.progressBar.setAttribute("aria-valuenow", String(Math.round(percent)));
  }

  const parts = [];
  const done = formatBytes(status.downloaded_bytes);
  const total = formatBytes(status.total_bytes);
  if (done && total) parts.push(`${done} of ${total}`);
  else if (done && status.downloaded_bytes > 0) parts.push(done);
  if (status.speed_bytes_per_second) {
    parts.push(`${formatBytes(status.speed_bytes_per_second)}/s`);
  }
  const eta = formatEta(status.eta_seconds);
  if (eta) parts.push(eta);
  dom.progressDetail.textContent = parts.join("  -  ");
  dom.progressLive.textContent = `${dom.progressState.textContent}. ${parts.join(", ")}`;
}

function renderReady(status) {
  dom.progressBlock.hidden = true;
  dom.cancelButton.hidden = true;
  setBusy(dom.downloadButton, false);

  const size = formatBytes(status.filesize_bytes);
  const expiry = status.expires_in_seconds
    ? ` It is removed from the server in about ${Math.round(status.expires_in_seconds / 60)} minutes.`
    : "";
  dom.readyDetail.textContent = `${status.filename || "file"}${size ? ` (${size})` : ""}.${expiry}`;
  dom.saveLink.href = status.download_url;
  dom.saveLink.setAttribute("download", status.filename || "download");
  dom.readyBlock.hidden = false;
  dom.saveLink.focus({ preventScroll: true });
}

/* ---------- progress streaming ---------- */

function closeEventSource() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

function handleTerminal(status) {
  closeEventSource();
  setBusy(dom.downloadButton, false);
  dom.cancelButton.hidden = true;

  if (status.state === "completed") {
    renderReady(status);
    return;
  }
  dom.progressBlock.hidden = true;
  if (status.state === "failed") {
    showAlert(status.error || "The download failed. Please try again.");
  } else if (status.state === "cancelled") {
    showAlert("The download was cancelled.");
  }
}

function watchJob(jobId) {
  closeEventSource();
  const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
  state.eventSource = source;

  source.onmessage = (event) => {
    let status;
    try {
      status = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    if (["completed", "failed", "cancelled"].includes(status.state)) {
      handleTerminal(status);
    } else {
      renderProgress(status);
    }
  };

  source.addEventListener("gone", () => {
    closeEventSource();
    setBusy(dom.downloadButton, false);
    dom.progressBlock.hidden = true;
    showAlert("That download expired on the server. Start it again.");
  });

  source.onerror = () => {
    // The stream ends normally once the job settles; only fall back while running.
    if (!state.eventSource) return;
    closeEventSource();
    pollJob(jobId);
  };
}

async function pollJob(jobId) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    if (state.jobId !== jobId) return;
    let status;
    try {
      status = await apiCall(`/api/jobs/${encodeURIComponent(jobId)}`);
    } catch (error) {
      showAlert(error.message);
      setBusy(dom.downloadButton, false);
      return;
    }
    if (["completed", "failed", "cancelled"].includes(status.state)) {
      handleTerminal(status);
      return;
    }
    renderProgress(status);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

/* ---------- actions ---------- */

async function onInspect(event) {
  event.preventDefault();
  if (state.inFlight) return;

  const url = dom.url.value.trim();
  if (!url) {
    showAlert("Please paste a video URL first.");
    dom.url.focus();
    return;
  }

  clearAlert();
  resetResult();
  state.inFlight = true;
  setBusy(dom.checkButton, true);

  try {
    const data = await apiCall("/api/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    renderResult(data);
  } catch (error) {
    showAlert(error.message);
  } finally {
    state.inFlight = false;
    setBusy(dom.checkButton, false);
  }
}

async function onDownload() {
  if (!state.inspected || !state.inspected.downloadable) return;

  clearAlert();
  dom.readyBlock.hidden = true;
  setBusy(dom.downloadButton, true);
  renderProgress({ state: "queued", downloaded_bytes: 0, progress_percent: 0 });

  try {
    const accepted = await apiCall("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: state.inspected.url,
        format_id: dom.format.value || null,
      }),
    });
    state.jobId = accepted.job_id;
    dom.cancelButton.hidden = false;
    watchJob(accepted.job_id);
  } catch (error) {
    dom.progressBlock.hidden = true;
    setBusy(dom.downloadButton, false);
    showAlert(error.message);
  }
}

async function onCancel() {
  if (!state.jobId) return;
  const jobId = state.jobId;
  state.jobId = null;
  closeEventSource();
  dom.cancelButton.hidden = true;
  dom.progressBlock.hidden = true;
  setBusy(dom.downloadButton, false);
  try {
    await apiCall(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    showAlert("The download was cancelled.");
  } catch (error) {
    showAlert(error.message);
  }
}

/* ---------- policy panel ---------- */

async function loadPolicy() {
  let policy;
  try {
    policy = await apiCall("/api/policy");
  } catch (error) {
    dom.policyLimits.textContent =
      "The source policy could not be loaded from the server.";
    return;
  }

  dom.policySources.replaceChildren();
  for (const source of policy.allowed_sources) {
    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = source.name;
    const host = document.createElement("code");
    host.textContent = source.host;
    const detail = document.createElement("span");
    detail.textContent = source.description;
    item.append(name, host, detail);
    dom.policySources.appendChild(item);
  }

  const sizeLimit = formatBytes(policy.max_download_bytes);
  const minutes = Math.round(policy.max_duration_seconds / 60);
  dom.policyLimits.textContent =
    `Server limits: up to ${sizeLimit} per file and ${minutes} minutes of runtime. ` +
    `YouTube metadata comes from ${policy.youtube_metadata_source}.`;
  dom.policyYoutube.textContent = policy.youtube_policy_note;
}

/* ---------- wiring ---------- */

dom.form.addEventListener("submit", onInspect);
dom.downloadButton.addEventListener("click", onDownload);
dom.cancelButton.addEventListener("click", onCancel);
dom.url.addEventListener("input", clearAlert);
window.addEventListener("beforeunload", closeEventSource);

loadPolicy();
