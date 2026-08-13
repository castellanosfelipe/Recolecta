(() => {
  "use strict";

  const state = {
    connections: [],
    runs: [],
    files: [],
    alerts: [],
    progress: { active: false, active_runs: 0, runs: [] },
    chart: null,
    pollTimer: null,
    connectionValidationRevision: 0,
    connectionValidatedRevision: -1,
    connectionValidatedFingerprint: null,
    connectionValidationRequest: 0,
    connectionSaving: false,
  };

  const titles = {
    inicio: "Resumen",
    "en-vivo": "Corridas en vivo",
    historial: "Historial",
    archivos: "Archivos",
    conexiones: "Conexiones",
    ajustes: "Ajustes",
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  async function api(path, options = {}) {
    const request = { ...options, headers: { ...(options.headers || {}) } };
    if (request.body && typeof request.body !== "string") {
      request.headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(request.body);
    }
    const response = await fetch(path, request);
    if (response.status === 204) return null;
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      const message = payload?.detail || `La solicitud falló (${response.status}).`;
      throw new Error(message);
    }
    return payload;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatBytes(value) {
    if (value === null || value === undefined) return "—";
    const bytes = Number(value);
    if (!Number.isFinite(bytes)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = Math.abs(bytes);
    let index = 0;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${bytes < 0 ? "-" : ""}${size.toLocaleString("es-CO", { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const total = Math.max(0, Math.round(Number(seconds)));
    if (total < 60) return `${total} s`;
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return `${minutes} min ${rest} s`;
  }

  function formatDate(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString("es-CO", {
      dateStyle: "medium",
      timeStyle: "short",
    });
  }

  const runStatusLabels = {
    no_files: "Archivos no existentes",
    no_changes: "Sin archivos nuevos",
    completed: "Descarga completada",
    ok: "Ejecución completada",
    partial: "Completada con incidencias",
    failed: "Ejecución fallida",
    running: "En ejecución",
    cancelled: "Cancelada por el usuario",
    interrupted: "Ejecución interrumpida",
  };

  const fileStatusLabels = {
    pending: "Pendiente de descarga",
    downloading: "Descargando",
    ok: "Descargado y verificado",
    skipped: "Omitido por configuración",
    duplicate: "Ya descargado",
    failed: "No se pudo descargar",
    cancelled: "Descarga cancelada",
  };

  const alertStatusLabels = {
    sent: "Enviada",
    pending: "Pendiente de envío",
    failed: "No enviada",
  };

  const connectionStatusLabels = {
    enabled: "Activa y programada",
    disabled: "En pausa",
    never_run: "Sin ejecuciones",
  };

  const planStatusLabels = {
    no_files: "Archivos no existentes",
    no_changes: "Sin archivos nuevos",
    files_ready: "Archivos listos para descargar",
    planned: "Listo para descargar",
    duplicate: "Ya descargado",
    outside_window: "Fuera del período",
    quiet_period: "Todavía en escritura",
    timestamp_missing: "Sin fecha remota",
    include_filter: "No coincide con la inclusión",
    exclude_filter: "Excluido por configuración",
    size_filter: "Fuera del tamaño permitido",
    symlink: "Enlace simbólico omitido",
    local_present: "Ya coincide en local",
    local_missing: "Ausente en local",
    local_different: "Diferente en local",
    path_invalid: "Ruta de destino no válida",
  };

  function safeStatusClass(value) {
    return /^[a-z_-]+$/i.test(value || "") ? value : "unknown";
  }

  function statusBadge(value, label) {
    return `<span class="status ${safeStatusClass(value)}">${escapeHtml(label || value || "Sin estado")}</span>`;
  }

  function runResultStatus(run) {
    return run?.result_status || run?.status || "no_runs";
  }

  function runStatusLabel(run) {
    const status = runResultStatus(run);
    return run?.status_label || runStatusLabels[status] || status;
  }

  function runStatusBadge(run) {
    return statusBadge(runResultStatus(run), runStatusLabel(run));
  }

  function fileStatusLabel(file) {
    const status = typeof file === "string" ? file : file?.status;
    return (typeof file === "object" && file?.status_label)
      || fileStatusLabels[status]
      || status
      || "Sin estado";
  }

  function fileStatusBadge(file) {
    const status = typeof file === "string" ? file : file?.status;
    return statusBadge(status, fileStatusLabel(file));
  }

  function alertStatusLabel(alert) {
    const status = typeof alert === "string" ? alert : alert?.status;
    return (typeof alert === "object" && alert?.status_label)
      || alertStatusLabels[status]
      || status
      || "Sin estado";
  }

  function alertStatusBadge(alert) {
    const status = typeof alert === "string" ? alert : alert?.status;
    return statusBadge(status, alertStatusLabel(alert));
  }

  function connectionStatusBadge(connection, includeLastRun = false) {
    if (!connection?.enabled) {
      return statusBadge("disabled", connectionStatusLabels.disabled);
    }
    if (includeLastRun && connection.last_status) {
      return runStatusBadge({
        status: connection.last_status,
        result_status: connection.last_result_status,
        status_label: connection.last_status_label,
      });
    }
    if (includeLastRun) {
      return statusBadge("never_run", connectionStatusLabels.never_run);
    }
    return statusBadge("enabled", connectionStatusLabels.enabled);
  }

  function planResultLabel(result) {
    return result?.result_label
      || planStatusLabels[result?.result_status]
      || result?.result_status
      || "Simulación completada";
  }

  function toast(message, error = false) {
    const node = document.createElement("div");
    node.className = `toast${error ? " error" : ""}`;
    node.setAttribute("role", error ? "alert" : "status");
    node.setAttribute("aria-live", error ? "assertive" : "polite");
    node.setAttribute("aria-atomic", "true");
    const text = document.createElement("span");
    text.className = "toast-message";
    text.textContent = message;
    const close = document.createElement("button");
    close.className = "toast-close";
    close.type = "button";
    close.setAttribute("aria-label", "Cerrar notificación");
    close.textContent = "×";
    node.append(text, close);
    $("#toast-region").append(node);
    const lifetime = error ? 12000 : 7000;
    let timer = null;
    let startedAt = 0;
    let remaining = lifetime;
    const pauseReasons = new Set();
    const pause = (reason) => {
      pauseReasons.add(reason);
      if (!timer) return;
      window.clearTimeout(timer);
      timer = null;
      remaining = Math.max(1000, remaining - (Date.now() - startedAt));
    };
    const resume = (reason = null) => {
      if (reason) pauseReasons.delete(reason);
      if (pauseReasons.size || timer || !node.isConnected) return;
      startedAt = Date.now();
      timer = window.setTimeout(() => node.remove(), remaining);
    };
    close.addEventListener("click", () => node.remove());
    node.addEventListener("pointerenter", () => pause("pointer"));
    node.addEventListener("pointerleave", () => resume("pointer"));
    node.addEventListener("focusin", () => pause("focus"));
    node.addEventListener("focusout", () => resume("focus"));
    resume();
    return node;
  }

  function emptyState(title, text) {
    return `<div class="empty-state"><b>${escapeHtml(title)}</b><p>${escapeHtml(text)}</p></div>`;
  }

  function setView(name, { focusMain = true } = {}) {
    const view = titles[name] ? name : "inicio";
    $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
    $$("#primary-nav a").forEach((link) => {
      const active = link.dataset.view === view;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    $("#page-title").textContent = titles[view];
    if (window.location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (focusMain) $("#main").focus({ preventScroll: true });
  }

  async function loadConnections() {
    const data = await api("/api/connections");
    state.connections = data.items;
    renderConnectionOptions();
    renderConnections();
  }

  async function loadDashboard() {
    const data = await api("/api/dashboard");
    state.progress = data.progress;
    renderSummary(data.connections);
    renderConnectionCards(data.connections);
    renderProgress();
  }

  async function loadRuns(query = "") {
    state.runs = (await api(`/api/runs${query}`)).items;
    renderHistory();
    renderChart();
  }

  async function loadFiles(query = "") {
    state.files = (await api(`/api/files${query}`)).items;
    renderFiles();
  }

  async function loadSettings() {
    const values = (await api("/api/settings")).values;
    const form = $("#settings-form");
    Object.entries(values).forEach(([key, value]) => {
      const field = form.elements.namedItem(key);
      if (!field) return;
      if (field.type === "checkbox") field.checked = Boolean(value);
      else field.value = value;
    });
  }

  async function loadAlerts() {
    state.alerts = (await api("/api/alerts?limit=50")).items;
    renderAlerts();
  }

  function renderSummary(connections) {
    const enabled = connections.filter((item) => item.enabled).length;
    const ok = connections.filter((item) => item.last_status === "ok").length;
    const attention = connections.filter((item) => ["failed", "partial"].includes(item.last_status)).length;
    const bytes = connections.reduce((sum, item) => sum + Number(item.last_bytes_downloaded || 0), 0);
    $("#hero-active").textContent = state.progress.active_runs || 0;
    $("#summary-stats").innerHTML = [
      ["Conexiones activas", enabled, `${connections.length} configuradas`],
      ["Últimas ejecuciones sin error", ok, "orígenes al día"],
      ["Atención requerida", attention, attention ? "revisar incidencias" : "sin incidencias recientes"],
      ["Volumen reciente", formatBytes(bytes), "última corrida por origen"],
    ].map(([label, value, detail]) => `<article class="stat-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`).join("");
  }

  function renderConnectionCards(items) {
    const root = $("#connection-cards");
    if (!items.length) {
      root.innerHTML = emptyState("Aún no hay conexiones", "Crea el primer origen remoto para comenzar.");
      return;
    }
    root.innerHTML = items.map((item) => `
      <article class="connection-card">
        <div class="card-top">
          <div><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.client || `${item.protocol} · origen remoto`)}</p></div>
          ${connectionStatusBadge(item, true)}
        </div>
        <div class="card-metrics">
          <div><span>Última corrida</span><strong>${escapeHtml(formatDate(item.last_started_at))}</strong></div>
          <div><span>Próxima corrida</span><strong>${item.enabled ? escapeHtml(formatDate(item.next_run_at)) : "En pausa"}</strong></div>
          <div><span>Archivos</span><strong>${Number(item.last_files_downloaded || 0).toLocaleString("es-CO")}</strong></div>
        </div>
        <div class="card-actions">
          <button class="btn primary small" data-run="${item.id}" ${item.enabled ? "" : "disabled"}>Ejecutar ahora</button>
          <button class="btn secondary small" data-edit="${item.id}">Configurar</button>
        </div>
      </article>`).join("");
  }

  function renderProgress() {
    const root = $("#live-runs");
    $("#live-count").hidden = !state.progress.active_runs;
    $("#live-count").textContent = state.progress.active_runs || 0;
    if (!state.progress.runs.length) {
      root.innerHTML = emptyState("No hay corridas activas", "Cuando comience una descarga, el progreso aparecerá aquí.");
      return;
    }
    root.innerHTML = state.progress.runs.map((run) => {
      const files = run.files.map((file) => `
        <tr>
          <td class="path-cell mono" title="${escapeHtml(file.remote_path)}">${escapeHtml(file.remote_path)}</td>
          <td>${fileStatusBadge(file)}</td>
          <td>
            <div class="progress-meta"><span>${escapeHtml(formatBytes(file.bytes_done))} / ${escapeHtml(formatBytes(file.size_bytes))}</span><span>${file.percent === null ? "—" : `${file.percent}%`}</span></div>
            <div class="progress-track" role="progressbar" aria-label="Progreso de ${escapeHtml(file.remote_path)}" ${file.percent === null ? `aria-valuetext="Progreso indeterminado; ${escapeHtml(formatBytes(file.bytes_done))} transferidos"` : `aria-valuenow="${file.percent}" aria-valuemin="0" aria-valuemax="100" aria-valuetext="${file.percent}%"`}><span class="${file.percent === null ? "indeterminate" : ""}" style="width:${file.percent ?? 100}%"></span></div>
          </td>
          <td>${escapeHtml(formatBytes(file.average_bps))}/s</td>
          <td>${escapeHtml(formatDuration(file.eta_s))}</td>
        </tr>`).join("");
      return `
        <article class="live-run">
          <div class="live-head">
            <div><p class="eyebrow">CORRIDA #${run.run_id}</p><h3>${escapeHtml(run.connection_name)}</h3><p>${run.files_completed} de ${run.files_total} archivos · ${escapeHtml(formatBytes(run.average_bps))}/s · ETA ${escapeHtml(formatDuration(run.eta_s))}</p></div>
            <button class="btn danger small" data-cancel="${run.run_id}" ${run.cancel_requested ? "disabled" : ""}>${run.cancel_requested ? "Cancelación solicitada" : "Cancelar corrida"}</button>
          </div>
          <div class="progress-meta"><span>Progreso global</span><strong>${run.percent === null ? "Tamaño desconocido" : `${run.percent}%`}</strong></div>
          <div class="progress-track" role="progressbar" aria-label="Progreso global de ${escapeHtml(run.connection_name)}" ${run.percent === null ? `aria-valuetext="Progreso indeterminado; ${run.files_completed} de ${run.files_total} archivos completados"` : `aria-valuenow="${run.percent}" aria-valuemin="0" aria-valuemax="100" aria-valuetext="${run.percent}%"`}><span class="${run.percent === null ? "indeterminate" : ""}" style="width:${run.percent ?? 100}%"></span></div>
          <div class="table-panel live-files"><table><caption class="sr-only">Archivos de la corrida ${run.run_id}</caption><thead><tr><th>Archivo</th><th>Estado</th><th>Progreso</th><th>Velocidad</th><th>ETA</th></tr></thead><tbody>${files || `<tr><td colspan="5">Preparando archivos…</td></tr>`}</tbody></table></div>
        </article>`;
    }).join("");
  }

  function renderHistory() {
    $("#history-body").innerHTML = state.runs.length ? state.runs.map((run) => `
      <tr>
        <td class="mono">#${run.id}</td>
        <td>${escapeHtml(run.connection_name)}</td>
        <td>${escapeHtml(formatDate(run.started_at))}</td>
        <td>${runStatusBadge(run)}</td>
        <td>${Number(run.files_downloaded || 0).toLocaleString("es-CO")}</td>
        <td>${Number(run.files_failed || 0).toLocaleString("es-CO")}</td>
        <td>${escapeHtml(formatBytes(run.bytes_downloaded))}</td>
        <td><button class="btn secondary small" data-run-detail="${run.id}">Ver detalle</button></td>
      </tr>`).join("") : `<tr><td colspan="8">No hay corridas para estos filtros.</td></tr>`;
  }

  function renderFiles() {
    $("#files-body").innerHTML = state.files.length ? state.files.map((file) => `
      <tr>
        <td class="path-cell mono" title="${escapeHtml(file.remote_path)}">${escapeHtml(file.remote_path)}</td>
        <td>${escapeHtml(file.connection_name)}</td>
        <td>${escapeHtml(formatDate(file.run_started_at))}</td>
        <td>${fileStatusBadge(file)}</td>
        <td>${escapeHtml(formatBytes(file.size_bytes))}</td>
        <td>${escapeHtml(formatDuration(file.duration_s))}</td>
        <td>${file.average_bps === null ? "—" : `${escapeHtml(formatBytes(file.average_bps))}/s`}</td>
      </tr>`).join("") : `<tr><td colspan="7">No hay archivos para estos filtros.</td></tr>`;
  }

  function renderConnections() {
    $("#connections-body").innerHTML = state.connections.length ? state.connections.map((item) => `
      <tr>
        <td><strong>${escapeHtml(item.name)}</strong><br><span class="muted">${escapeHtml(item.client || "Sin cliente")}</span></td>
        <td>${escapeHtml(item.protocol)}</td>
        <td class="mono">${escapeHtml(item.host)}:${item.port}</td>
        <td>${escapeHtml(item.schedule_time || "Global")}</td>
        <td>${item.has_secret ? "Configurada" : "Sin secreto"}</td>
        <td>${connectionStatusBadge(item)}</td>
        <td><div class="actions">
          <label class="reconciliation-action" title="Compara todo el árbol remoto con la carpeta local, repara los archivos ausentes o diferentes y no elimina archivos locales extra. Puede tardar en orígenes grandes.">
            <input type="checkbox" data-full-local-reconciliation="${item.id}" ${item.full_local_reconciliation ? "checked" : ""}>
            <span>Comparar todo el árbol<small>Repara ausentes o diferentes; conserva extras locales</small></span>
          </label>
          <button class="btn secondary small" data-edit="${item.id}">Editar</button>
          <button class="btn secondary small" data-test="${item.id}">Simular corrida</button>
          <button class="btn secondary small" data-duplicate="${item.id}">Duplicar</button>
          <button class="btn danger small" data-delete="${item.id}">Eliminar</button>
        </div></td>
      </tr>`).join("") : `<tr><td colspan="7">No hay conexiones configuradas.</td></tr>`;
  }

  function renderAlerts() {
    $("#alerts-body").innerHTML = state.alerts.length ? state.alerts.map((alert) => `
      <tr>
        <td>${escapeHtml(formatDate(alert.created_at))}</td>
        <td>${escapeHtml(alert.connection_name || "Sistema")}</td>
        <td class="mono">${escapeHtml(alert.cause)}</td>
        <td>${escapeHtml(alert.channel)}</td>
        <td>${alertStatusBadge(alert)}</td>
        <td class="path-cell" title="${escapeHtml(alert.message)}">${escapeHtml(alert.message)}</td>
      </tr>`).join("") : `<tr><td colspan="6">No hay alertas registradas.</td></tr>`;
  }

  function renderConnectionOptions() {
    $$("[data-connections]").forEach((select) => {
      const current = select.value;
      select.innerHTML = `<option value="">Todas</option>${state.connections.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
      select.value = current;
    });
  }

  function renderChart() {
    const canvas = $("#runs-chart");
    const ordered = [...state.runs].slice(0, 12).reverse();
    const summary = $("#runs-chart-summary");
    if (summary) {
      summary.textContent = ordered.length
        ? `Últimas corridas: ${ordered.map((run) => `corrida ${run.id}, ${run.files_downloaded || 0} archivos descargados`).join("; ")}.`
        : "No hay corridas para representar.";
    }
    if (!window.Chart || !canvas) return;
    if (state.chart) state.chart.destroy();
    state.chart = new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: ordered.map((run) => `#${run.id}`),
        datasets: [{
          data: ordered.map((run) => run.files_downloaded || 0),
          backgroundColor: "#61e6b5",
          borderRadius: 4,
          maxBarThickness: 34,
        }],
      },
      options: {
        maintainAspectRatio: false,
        animation: { duration: 250 },
        plugins: { legend: { display: false }, tooltip: { displayColors: false } },
        scales: {
          x: { grid: { display: false }, ticks: { color: "#8797ab" } },
          y: { beginAtZero: true, grid: { color: "#263349" }, ticks: { color: "#8797ab", precision: 0 } },
        },
      },
    });
  }

  function updateTlsModeField() {
    const form = $("#connection-form");
    const field = $("#ssl-mode-field");
    const select = form.elements.namedItem("ssl_mode");
    const protocol = String(form.elements.namedItem("protocol").value).toUpperCase();
    const usesTls = protocol === "FTPS" || protocol === "WEBDAVS";
    field.hidden = !usesTls;
    select.disabled = !usesTls;
    if (!usesTls || !["required", "insecure"].includes(select.value)) {
      select.value = "required";
    }
  }

  function openConnection(id = null) {
    if (state.connectionSaving) return;
    setConnectionDialogBusy(false);
    const form = $("#connection-form");
    form.reset();
    form.elements.enabled.checked = true;
    form.elements.timezone.value = "America/Bogota";
    form.elements.dest_root.value = "downloads";
    form.elements.window_hours.value = "24";
    form.elements.recursive.checked = false;
    form.elements.max_depth.value = "3";
    form.elements.full_local_reconciliation.checked = false;
    form.elements.max_parallel_files.value = "2";
    form.elements.retries.value = "3";
    form.elements.id.value = "";
    $("#connection-dialog-title").textContent = "Nueva conexión";
    if (id !== null) {
      const item = state.connections.find((candidate) => candidate.id === Number(id));
      if (!item) return;
      $("#connection-dialog-title").textContent = `Editar ${item.name}`;
      Object.entries(item).forEach(([key, value]) => {
        const field = form.elements.namedItem(key);
        if (!field || value === null || ["secret", "has_secret"].includes(key)) return;
        if (field.type === "checkbox") field.checked = Boolean(value);
        else if (key === "remote_paths") field.value = value.join("\n");
        else field.value = value;
      });
      form.elements.id.value = item.id;
    }
    updateTlsModeField();
    invalidateConnectionValidation();
    const dialog = $("#connection-dialog");
    dialog.showModal();
    dialog.scrollTop = 0;
    window.requestAnimationFrame(() => {
      form.elements.namedItem("name").focus({ preventScroll: true });
      dialog.scrollTop = 0;
    });
  }

  function connectionPayload(form) {
    const data = new FormData(form);
    const numeric = ["port", "window_hours", "max_depth", "max_parallel_files", "retries"];
    const payload = {};
    for (const [key, value] of data.entries()) {
      if (key === "id") continue;
      if (numeric.includes(key)) payload[key] = value === "" ? null : Number(value);
      else if (key === "schedule_time") payload[key] = value || null;
      else if (key === "remote_paths") payload[key] = value.split(/\r?\n/).map((part) => part.trim()).filter(Boolean);
      else payload[key] = value;
    }
    payload.enabled = form.elements.enabled.checked;
    payload.recursive = form.elements.recursive.checked;
    payload.full_local_reconciliation = (
      form.elements.full_local_reconciliation.checked
    );
    payload.ssl_mode = form.elements.namedItem("ssl_mode").value || "required";
    if (!payload.secret) delete payload.secret;
    return payload;
  }

  async function connectionPayloadFingerprint(id, payload) {
    const serialized = JSON.stringify([String(id || ""), payload]);
    if (!window.crypto?.subtle) {
      let left = 0x811c9dc5;
      let right = 0x9e3779b9;
      for (let index = 0; index < serialized.length; index += 1) {
        const code = serialized.charCodeAt(index);
        left = Math.imul(left ^ code, 0x01000193);
        right = Math.imul(right ^ code, 0x85ebca6b);
      }
      return `${(left >>> 0).toString(16)}${(right >>> 0).toString(16)}`;
    }
    const bytes = new TextEncoder().encode(serialized);
    const digest = await window.crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)]
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
  }

  function setConnectionDialogBusy(busy) {
    state.connectionSaving = busy;
    const form = $("#connection-form");
    form.setAttribute("aria-busy", String(busy));
    $$("input, select, textarea, button", form).forEach((control) => {
      control.disabled = busy;
    });
    $$('[data-close-dialog="connection-dialog"]').forEach((control) => {
      control.disabled = busy;
    });
    const saveButton = $("#connection-save-button");
    saveButton.textContent = busy ? "Guardando…" : "Guardar conexión";
    if (!busy) {
      $("#connection-test-button").disabled = false;
      saveButton.disabled = (
        state.connectionValidatedRevision
        !== state.connectionValidationRevision
        || !state.connectionValidatedFingerprint
      );
    }
  }

  function setConnectionValidationStatus(message, status = "") {
    const element = $("#connection-validation-status");
    element.textContent = message;
    element.className = `validation-status${status ? ` ${status}` : ""}`;
  }

  function invalidateConnectionValidation(
    message = "Prueba la conexión y sus rutas antes de guardar.",
  ) {
    state.connectionValidationRevision += 1;
    state.connectionValidationRequest += 1;
    state.connectionValidatedRevision = -1;
    state.connectionValidatedFingerprint = null;
    $("#connection-save-button").disabled = true;
    const testButton = $("#connection-test-button");
    testButton.removeAttribute("aria-busy");
    testButton.disabled = state.connectionSaving;
    testButton.textContent = "Probar conexión y rutas";
    $("#connection-form").elements.remote_paths.setCustomValidity("");
    setConnectionValidationStatus(message);
  }

  async function testConnectionDraft() {
    const form = $("#connection-form");
    const remotePaths = form.elements.remote_paths;
    remotePaths.setCustomValidity("");
    const payload = connectionPayload(form);
    if (!payload.remote_paths.length) {
      remotePaths.setCustomValidity("Ingresa al menos una ruta remota.");
    }
    if (!form.reportValidity()) {
      invalidateConnectionValidation(
        "Completa los campos obligatorios antes de probar.",
      );
      setConnectionValidationStatus(
        "Completa los campos obligatorios antes de probar.",
        "invalid",
      );
      return;
    }

    const id = form.elements.id.value;
    const revision = state.connectionValidationRevision;
    const requestId = ++state.connectionValidationRequest;
    let fingerprint;
    try {
      fingerprint = await connectionPayloadFingerprint(id, payload);
    } catch (error) {
      if (requestId !== state.connectionValidationRequest) return;
      invalidateConnectionValidation(
        "No fue posible identificar el borrador para validarlo.",
      );
      setConnectionValidationStatus(error.message, "invalid");
      toast(error.message, true);
      return;
    }
    if (
      requestId !== state.connectionValidationRequest
      || revision !== state.connectionValidationRevision
    ) {
      return;
    }
    const testButton = $("#connection-test-button");
    testButton.disabled = true;
    testButton.setAttribute("aria-busy", "true");
    testButton.textContent = "Probando…";
    $("#connection-save-button").disabled = true;
    setConnectionValidationStatus(
      "Validando credencial, rutas remotas y escritura local…",
      "testing",
    );
    try {
      const query = id ? `?connection_id=${encodeURIComponent(id)}` : "";
      const result = await api(`/api/connections/validate${query}`, {
        method: "POST",
        body: payload,
      });
      if (
        requestId !== state.connectionValidationRequest
        || revision !== state.connectionValidationRevision
      ) {
        return;
      }
      if (!result.valid) {
        throw new Error("La conexión o sus rutas no pudieron validarse.");
      }
      const currentFingerprint = await connectionPayloadFingerprint(
        form.elements.id.value,
        connectionPayload(form),
      );
      if (
        requestId !== state.connectionValidationRequest
        || revision !== state.connectionValidationRevision
      ) {
        return;
      }
      if (currentFingerprint !== fingerprint) {
        invalidateConnectionValidation(
          "Los datos cambiaron. Vuelve a probar la conexión y sus rutas.",
        );
        return;
      }
      state.connectionValidatedRevision = revision;
      state.connectionValidatedFingerprint = fingerprint;
      $("#connection-save-button").disabled = false;
      const warning = result.warnings?.length
        ? ` Advertencias: ${result.warnings.join(" ")}`
        : "";
      setConnectionValidationStatus(
        `Validación correcta: ${result.remote_paths.length} ruta(s) remota(s) accesible(s) y destino local escribible.${warning}`,
        "valid",
      );
      toast("Conexión y rutas validadas. Ya puedes guardar.");
    } catch (error) {
      if (requestId !== state.connectionValidationRequest) return;
      state.connectionValidatedRevision = -1;
      state.connectionValidatedFingerprint = null;
      $("#connection-save-button").disabled = true;
      setConnectionValidationStatus(error.message, "invalid");
      toast(error.message, true);
    } finally {
      if (requestId === state.connectionValidationRequest) {
        testButton.removeAttribute("aria-busy");
        testButton.disabled = false;
        testButton.textContent = "Probar conexión y rutas";
      }
    }
  }

  async function submitConnection(event) {
    event.preventDefault();
    if (state.connectionSaving) return;
    const form = event.currentTarget;
    const id = form.elements.id.value;
    const payload = connectionPayload(form);
    setConnectionDialogBusy(true);
    let fingerprint;
    try {
      fingerprint = await connectionPayloadFingerprint(id, payload);
    } catch (error) {
      setConnectionDialogBusy(false);
      invalidateConnectionValidation(
        "No fue posible confirmar el borrador validado.",
      );
      toast(error.message, true);
      return;
    }
    if (
      state.connectionValidatedRevision
      !== state.connectionValidationRevision
      || fingerprint !== state.connectionValidatedFingerprint
    ) {
      setConnectionDialogBusy(false);
      invalidateConnectionValidation();
      toast(
        "Debes probar correctamente la conexión y sus rutas antes de guardar.",
        true,
      );
      return;
    }
    setConnectionValidationStatus(
      "Guardando y revalidando la conexión y sus rutas…",
      "testing",
    );
    try {
      await api(id ? `/api/connections/${id}` : "/api/connections", {
        method: id ? "PATCH" : "POST",
        body: payload,
      });
    } catch (error) {
      setConnectionDialogBusy(false);
      invalidateConnectionValidation(
        "La conexión debe volver a probarse antes de guardar.",
      );
      setConnectionValidationStatus(error.message, "invalid");
      toast(error.message, true);
      return;
    }
    setConnectionDialogBusy(false);
    $("#connection-dialog").close();
    toast(id ? "Conexión actualizada." : "Conexión creada.");
    try {
      await Promise.all([loadConnections(), loadDashboard()]);
    } catch (error) {
      toast(
        `La conexión se guardó, pero no se pudo actualizar la vista: ${error.message}`,
        true,
      );
    }
  }

  async function importConnectionBackup(event) {
    const input = event.currentTarget;
    const file = input.files?.[0];
    if (!file) return;
    try {
      const backup = JSON.parse(await file.text());
      const total = Array.isArray(backup.connections) ? backup.connections.length : 0;
      if (!window.confirm(`Se revisarán ${total} conexión(es). Los protocolos no compatibles se omitirán y todas las conexiones quedarán en pausa hasta validar sus rutas. ¿Continuar?`)) return;
      const result = await api("/api/import/connections", {
        method: "POST",
        body: backup,
      });
      const summary = `${result.created_count} importada(s), ${result.skipped_count} omitida(s) y ${result.error_count} con error.`;
      showImportResult(result);
      toast(summary, result.error_count > 0);
      try {
        await Promise.all([loadConnections(), loadDashboard()]);
      } catch (refreshError) {
        toast(`La importación se completó, pero no se pudo actualizar la vista: ${refreshError.message}`, true);
      }
    } catch (error) {
      toast(`No se pudo importar el JSON: ${error.message}`, true);
    } finally {
      input.value = "";
    }
  }

  function showImportResult(result) {
    const issueSection = (title, items) => items.length ? `
      <h3>${escapeHtml(title)}</h3>
      <ul>${items.map((item) => `<li><strong>${escapeHtml(item.name)}</strong> · ${escapeHtml(item.protocol)}: ${escapeHtml(item.reason)}</li>`).join("")}</ul>
    ` : "";
    $("#import-result-content").innerHTML = `
      <div class="detail-grid">
        <div><span>Revisadas</span><strong>${result.total}</strong></div>
        <div><span>Importadas</span><strong>${result.created_count}</strong></div>
        <div><span>Omitidas</span><strong>${result.skipped_count}</strong></div>
        <div><span>Con error</span><strong>${result.error_count}</strong></div>
      </div>
      <p>Las conexiones importadas se guardaron en pausa hasta validar sus credenciales y rutas.</p>
      ${issueSection("Entradas omitidas", result.skipped)}
      ${issueSection("Entradas con error", result.errors)}
    `;
    $("#import-result-dialog").showModal();
  }

  async function startRun(id) {
    try {
      await api(`/api/connections/${id}/run`, { method: "POST" });
      toast("Corrida aceptada. Preparando el listado remoto.");
      setView("en-vivo");
      await pollProgress();
    } catch (error) {
      toast(error.message, true);
    }
  }

  function showSimulationResult(result) {
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    const counters = Object.entries(result.counters || {})
      .filter(([, count]) => Number(count) > 0);
    const items = Array.isArray(result.items) ? result.items : [];
    const detail = result.result_status === "no_files"
      ? "No se encontraron archivos en las rutas remotas configuradas."
      : result.result_status === "no_changes"
        ? "Los archivos encontrados no requieren una nueva descarga."
        : `${result.files_to_download} archivo(s) están listos para descargar.`;
    const counterContent = counters.length
      ? counters.map(([status, count]) => `
          <div><dt>${escapeHtml(planStatusLabels[status] || status)}</dt><dd>${Number(count).toLocaleString("es-CO")}</dd></div>`).join("")
      : "<div><dt>Sin resultados por estado</dt><dd>0</dd></div>";
    const warningContent = warnings.length
      ? `<section class="simulation-section simulation-warnings" aria-labelledby="simulation-warnings-title">
          <h3 id="simulation-warnings-title">Advertencias (${warnings.length})</h3>
          <ul>${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>
        </section>`
      : `<p class="simulation-ok">La simulación no generó advertencias.</p>`;
    const itemContent = items.length
      ? `<div class="table-panel simulation-table"><table>
          <caption>Muestra de ${items.length} elemento(s) evaluado(s)</caption>
          <thead><tr><th>Archivo remoto</th><th>Estado</th><th>Tamaño</th><th>Fecha remota</th><th>Detalle</th></tr></thead>
          <tbody>${items.map((item) => `<tr>
            <td class="path-cell mono" title="${escapeHtml(item.remote_path)}">${escapeHtml(item.remote_path)}</td>
            <td>${statusBadge(item.status, planStatusLabels[item.status] || item.status)}</td>
            <td>${escapeHtml(formatBytes(item.size_bytes))}</td>
            <td>${escapeHtml(formatDate(item.mtime_utc))}</td>
            <td>${escapeHtml(item.reason || "—")}</td>
          </tr>`).join("")}</tbody>
        </table></div>`
      : emptyState("Sin elementos en la muestra", "No hubo archivos para mostrar en el plan.");
    $("#simulation-content").innerHTML = `
      <div class="detail-grid simulation-summary">
        <div><span>Encontrados</span><strong>${Number(result.files_found || 0).toLocaleString("es-CO")}</strong></div>
        <div><span>Por descargar</span><strong>${Number(result.files_to_download || 0).toLocaleString("es-CO")}</strong></div>
        <div><span>Volumen previsto</span><strong>${escapeHtml(formatBytes(result.planned_bytes || 0))}</strong></div>
        <div><span>Modo</span><strong>${result.scan_mode === "full_local_reconciliation" ? "Comparación completa" : "Ventana programada"}</strong></div>
      </div>
      <div class="detail-message ${safeStatusClass(result.result_status)}">
        <span>Resultado</span><strong>${escapeHtml(planResultLabel(result))}</strong><p>${escapeHtml(detail)}</p>
      </div>
      ${warningContent}
      <section class="simulation-section" aria-labelledby="simulation-counters-title">
        <h3 id="simulation-counters-title">Contadores del plan</h3>
        <dl class="counter-grid">${counterContent}</dl>
      </section>
      <section class="simulation-section" aria-labelledby="simulation-items-title">
        <h3 id="simulation-items-title">Muestra del plan</h3>
        ${result.items_truncated ? `<p>Se muestra una muestra acotada; el total evaluado fue ${Number(result.files_found || 0).toLocaleString("es-CO")}.</p>` : ""}
        ${itemContent}
      </section>`;
    const dialog = $("#simulation-dialog");
    if (dialog.open) dialog.close();
    dialog.showModal();
    dialog.scrollTop = 0;
  }

  async function testConnection(id) {
    const pendingToast = toast("Simulando la corrida y calculando el plan…");
    try {
      const result = await api(`/api/connections/${id}/test`, { method: "POST" });
      pendingToast.remove();
      showSimulationResult(result);
    } catch (error) {
      pendingToast.remove();
      toast(error.message, true);
    }
  }

  async function toggleFullLocalReconciliation(input) {
    const id = Number(input.dataset.fullLocalReconciliation);
    const previous = !input.checked;
    const enabled = input.checked;
    input.disabled = true;
    try {
      const updated = await api(`/api/connections/${id}`, {
        method: "PATCH",
        body: { full_local_reconciliation: enabled },
      });
      const current = state.connections.find(
        (candidate) => candidate.id === id,
      );
      if (current) {
        current.full_local_reconciliation = Boolean(
          updated.full_local_reconciliation,
        );
      }
      toast(
        enabled
          ? "Comparación completa habilitada."
          : "Comparación completa deshabilitada.",
      );
      try {
        await Promise.all([loadConnections(), loadDashboard()]);
      } catch (error) {
        toast(
          `La opción se guardó, pero no se pudo actualizar la vista: ${error.message}`,
          true,
        );
      }
      if (input.isConnected) {
        input.checked = Boolean(updated.full_local_reconciliation);
        input.disabled = false;
      }
    } catch (error) {
      input.checked = previous;
      input.disabled = false;
      toast(
        `No se pudo cambiar la comparación completa: ${error.message}`,
        true,
      );
    }
  }

  async function cancelRun(id) {
    try {
      const result = await api(`/api/runs/${id}/cancel`, { method: "POST" });
      toast(result.cancelled ? "Cancelación solicitada; se conservarán los parciales." : "La corrida ya no está activa.", !result.cancelled);
      await pollProgress();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function showRunDetail(id) {
    try {
      const run = await api(`/api/runs/${id}`);
      const resultStatus = runResultStatus(run);
      const resultLabel = runStatusLabel(run);
      const causeTitle = run.error_msg || run.error_type ? "Causa reportada" : "Resultado";
      const causeMessage = run.error_msg
        || run.status_detail
        || (resultStatus === "no_files"
          ? "No se encontraron archivos en las rutas remotas configuradas durante esta ejecución."
          : resultStatus === "no_changes"
            ? "Se encontraron archivos, pero ninguno requería una nueva descarga."
            : resultStatus === "completed"
              ? "Los archivos previstos se descargaron y verificaron correctamente."
              : resultStatus === "partial"
                ? "La ejecución terminó, pero uno o más archivos presentaron incidencias."
                : resultStatus === "cancelled"
                  ? "La ejecución fue cancelada antes de completar el procesamiento."
                  : resultStatus === "failed"
                    ? "La ejecución terminó con un error antes de completar el procesamiento."
                    : "Consulta los archivos y métricas registrados para esta ejecución.");
      const emptyFilesMessage = resultStatus === "no_files"
        ? "No se encontraron archivos en las rutas remotas configuradas."
        : resultStatus === "no_changes"
          ? "No hubo archivos que requirieran una nueva descarga."
          : resultStatus === "failed"
            ? "La ejecución terminó antes de registrar archivos. Revisa la causa indicada arriba."
            : resultStatus === "cancelled"
              ? "La ejecución se canceló antes de registrar archivos."
              : "No hay registros de archivos para esta ejecución.";
      $("#detail-content").innerHTML = `
        <div class="detail-grid">
          <div><span>Conexión</span><strong>${escapeHtml(run.connection_name)}</strong></div>
          <div><span>Estado</span><strong>${escapeHtml(resultLabel)}</strong></div>
          <div><span>Inicio</span><strong>${escapeHtml(formatDate(run.started_at))}</strong></div>
          <div><span>Encontrados</span><strong>${run.files_found || 0}</strong></div>
          <div><span>Descargados</span><strong>${run.files_downloaded || 0}</strong></div>
          <div><span>Fallidos</span><strong>${run.files_failed || 0}</strong></div>
          <div><span>Volumen</span><strong>${escapeHtml(formatBytes(run.bytes_downloaded))}</strong></div>
        </div>
        <div class="detail-message ${safeStatusClass(resultStatus)}">
          <span>${escapeHtml(causeTitle)}</span>
          <strong>${escapeHtml(resultLabel)}</strong>
          <p>${escapeHtml(causeMessage)}</p>
        </div>
        <p><a class="btn secondary small" href="/api/runs/${run.id}/log.jsonl">Descargar log JSONL</a></p>
        <div class="table-panel"><table><caption class="sr-only">Archivos registrados en la corrida ${run.id}</caption><thead><tr><th>Archivo</th><th>Estado</th><th>Tamaño</th><th>Detalle</th></tr></thead><tbody>
          ${run.files.length ? run.files.map((file) => `<tr><td class="path-cell mono">${escapeHtml(file.remote_path)}</td><td>${fileStatusBadge(file)}</td><td>${escapeHtml(formatBytes(file.size_bytes))}</td><td>${escapeHtml(file.error_msg || file.reason || "—")}</td></tr>`).join("") : `<tr><td colspan="4">${escapeHtml(emptyFilesMessage)}</td></tr>`}
        </tbody></table></div>`;
      $("#detail-dialog").showModal();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function handleAction(event) {
    const target = event.target.closest("button, [data-go]");
    if (!target) return;
    if (target.dataset.closeDialog) {
      document.getElementById(target.dataset.closeDialog)?.close();
      return;
    }
    if (target.dataset.go) setView(target.dataset.go);
    if (target.dataset.newConnection !== undefined) openConnection();
    if (target.dataset.edit) openConnection(target.dataset.edit);
    if (target.dataset.run) await startRun(target.dataset.run);
    if (target.dataset.test) await testConnection(target.dataset.test);
    if (target.dataset.cancel) await cancelRun(target.dataset.cancel);
    if (target.dataset.runDetail) await showRunDetail(target.dataset.runDetail);
    if (target.dataset.duplicate) {
      try {
        await api(`/api/connections/${target.dataset.duplicate}/duplicate`, { method: "POST" });
        toast("Copia creada en pausa y sin credencial.");
        await Promise.all([loadConnections(), loadDashboard()]);
      } catch (error) { toast(error.message, true); }
    }
    if (target.dataset.delete) {
      const item = state.connections.find((candidate) => candidate.id === Number(target.dataset.delete));
      if (!window.confirm(`¿Eliminar la conexión “${item?.name || target.dataset.delete}” y su historial?`)) return;
      try {
        await api(`/api/connections/${target.dataset.delete}`, { method: "DELETE" });
        toast("Conexión e historial eliminados.");
        await Promise.all([loadConnections(), loadDashboard()]);
      } catch (error) { toast(error.message, true); }
    }
  }

  async function submitFilters(event, kind) {
    event.preventDefault();
    const query = new URLSearchParams();
    new FormData(event.currentTarget).forEach((value, key) => {
      if (value) query.set(key, value);
    });
    try {
      if (kind === "runs") await loadRuns(query.size ? `?${query}` : "");
      else await loadFiles(query.size ? `?${query}` : "");
    } catch (error) { toast(error.message, true); }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = {};
    new FormData(form).forEach((value, key) => {
      const field = form.elements.namedItem(key);
      values[key] = field.type === "number" ? Number(value) : value;
    });
    $$('input[type="checkbox"]', form).forEach((field) => { values[field.name] = field.checked; });
    try {
      await api("/api/settings", { method: "PUT", body: { values } });
      toast("Ajustes guardados y agenda recargada.");
    } catch (error) { toast(error.message, true); }
  }

  async function runRetention() {
    if (!window.confirm("Se eliminará historial, logs JSONL y exports anteriores al periodo configurado. Las descargas no se tocarán. ¿Continuar?")) return;
    try {
      const result = await api("/api/retention/run", { method: "POST" });
      toast(`Retención completada: ${result.runs_deleted} corrida(s) y ${result.logs_deleted} log(s) eliminados.`);
      await Promise.all([loadRuns(), loadFiles(), loadAlerts()]);
    } catch (error) { toast(error.message, true); }
  }

  async function pollProgress() {
    window.clearTimeout(state.pollTimer);
    try {
      state.progress = await api("/api/progress");
      renderProgress();
      $("#hero-active").textContent = state.progress.active_runs || 0;
      $("#api-dot").className = "health-dot ok";
      $("#api-status").textContent = "Operativo";
      $("#last-refresh").textContent = `Actualizado ${new Date().toLocaleTimeString("es-CO", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    } catch (error) {
      $("#api-dot").className = "health-dot error";
      $("#api-status").textContent = "Sin conexión";
    }
    state.pollTimer = window.setTimeout(pollProgress, state.progress.active ? 1000 : 10000);
  }

  async function refreshAll() {
    try {
      await Promise.all([loadConnections(), loadDashboard(), loadRuns(), loadFiles(), loadSettings(), loadAlerts()]);
      $("#api-dot").className = "health-dot ok";
      $("#api-status").textContent = "Operativo";
    } catch (error) {
      $("#api-dot").className = "health-dot error";
      $("#api-status").textContent = "Sin conexión";
      toast(error.message, true);
    }
  }

  function bindEvents() {
    window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
    document.addEventListener("click", handleAction);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const openDialogs = $$("dialog[open]");
      const dialog = openDialogs.at(-1);
      if (!dialog) return;
      if (dialog.id === "connection-dialog" && state.connectionSaving) return;
      event.preventDefault();
      dialog.close();
    });
    $("#new-connection-button").addEventListener("click", () => openConnection());
    $("#refresh-button").addEventListener("click", refreshAll);
    $("#connection-form").addEventListener("submit", submitConnection);
    $("#connection-form").addEventListener("input", () => {
      invalidateConnectionValidation(
        "Los datos cambiaron. Vuelve a probar la conexión y sus rutas.",
      );
    });
    $("#connection-form").addEventListener("change", (event) => {
      if (event.target.name === "protocol") updateTlsModeField();
      invalidateConnectionValidation(
        "Los datos cambiaron. Vuelve a probar la conexión y sus rutas.",
      );
    });
    document.addEventListener("change", (event) => {
      const input = event.target.closest(
        "input[data-full-local-reconciliation]",
      );
      if (input) toggleFullLocalReconciliation(input);
    });
    $("#connection-test-button").addEventListener(
      "click",
      testConnectionDraft,
    );
    $("#connection-dialog").addEventListener(
      "close",
      () => invalidateConnectionValidation(),
    );
    $("#connection-dialog").addEventListener("cancel", (event) => {
      if (state.connectionSaving) event.preventDefault();
    });
    $("#connection-import-button").addEventListener("click", () => {
      $("#connection-import-file").value = "";
      $("#connection-import-file").click();
    });
    $("#connection-import-file").addEventListener("change", importConnectionBackup);
    $("#history-filters").addEventListener("submit", (event) => submitFilters(event, "runs"));
    $("#file-filters").addEventListener("submit", (event) => submitFilters(event, "files"));
    $("#settings-form").addEventListener("submit", saveSettings);
    $("#retention-button").addEventListener("click", runRetention);
  }

  async function init() {
    bindEvents();
    setView(location.hash.slice(1) || "inicio", { focusMain: false });
    await refreshAll();
    await pollProgress();
  }

  init();
})();
