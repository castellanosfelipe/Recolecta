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

  function statusLabel(value) {
    return {
      ok: "Correcta",
      failed: "Fallida",
      partial: "Parcial",
      running: "En curso",
      downloading: "Descargando",
      pending: "Pendiente",
      skipped: "Omitido",
      duplicate: "Duplicado",
      cancelled: "Cancelado",
      enabled: "Activa",
      disabled: "En pausa",
    }[value] || value || "Sin corridas";
  }

  function statusBadge(value) {
    const safe = /^[a-z_-]+$/i.test(value || "") ? value : "disabled";
    return `<span class="status ${safe}">${escapeHtml(statusLabel(value))}</span>`;
  }

  function toast(message, error = false) {
    const node = document.createElement("div");
    node.className = `toast${error ? " error" : ""}`;
    node.textContent = message;
    $("#toast-region").append(node);
    window.setTimeout(() => node.remove(), 4500);
  }

  function emptyState(title, text) {
    return `<div class="empty-state"><b>${escapeHtml(title)}</b><p>${escapeHtml(text)}</p></div>`;
  }

  function setView(name) {
    const view = titles[name] ? name : "inicio";
    $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
    $$("#primary-nav a").forEach((link) => link.classList.toggle("active", link.dataset.view === view));
    $("#page-title").textContent = titles[view];
    if (window.location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    $("#main").focus({ preventScroll: true });
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
    const failed = connections.filter((item) => item.last_status === "failed").length;
    const bytes = connections.reduce((sum, item) => sum + Number(item.last_bytes_downloaded || 0), 0);
    $("#hero-active").textContent = state.progress.active_runs || 0;
    $("#summary-stats").innerHTML = [
      ["Conexiones activas", enabled, `${connections.length} configuradas`],
      ["Última ejecución correcta", ok, "orígenes sin novedades"],
      ["Atención requerida", failed, failed ? "revisar fallos" : "sin fallos recientes"],
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
          ${statusBadge(item.enabled ? (item.last_status || "enabled") : "disabled")}
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
      const percent = run.percent ?? 0;
      const files = run.files.map((file) => `
        <tr>
          <td class="path-cell mono" title="${escapeHtml(file.remote_path)}">${escapeHtml(file.remote_path)}</td>
          <td>${statusBadge(file.status)}</td>
          <td>
            <div class="progress-meta"><span>${escapeHtml(formatBytes(file.bytes_done))} / ${escapeHtml(formatBytes(file.size_bytes))}</span><span>${file.percent === null ? "—" : `${file.percent}%`}</span></div>
            <div class="progress-track" role="progressbar" aria-label="Progreso de ${escapeHtml(file.remote_path)}" aria-valuenow="${file.percent ?? 0}" aria-valuemin="0" aria-valuemax="100"><span style="width:${file.percent ?? 3}%"></span></div>
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
          <div class="progress-track" role="progressbar" aria-label="Progreso global" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"><span style="width:${run.percent ?? 3}%"></span></div>
          <div class="table-panel live-files"><table><thead><tr><th>Archivo</th><th>Estado</th><th>Progreso</th><th>Velocidad</th><th>ETA</th></tr></thead><tbody>${files || `<tr><td colspan="5">Preparando archivos…</td></tr>`}</tbody></table></div>
        </article>`;
    }).join("");
  }

  function renderHistory() {
    $("#history-body").innerHTML = state.runs.length ? state.runs.map((run) => `
      <tr>
        <td class="mono">#${run.id}</td>
        <td>${escapeHtml(run.connection_name)}</td>
        <td>${escapeHtml(formatDate(run.started_at))}</td>
        <td>${statusBadge(run.status)}</td>
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
        <td>${statusBadge(file.status)}</td>
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
        <td>${item.has_secret ? "Configurada" : "Sin secreto"}</td>
        <td>${statusBadge(item.enabled ? "enabled" : "disabled")}</td>
        <td><div class="actions">
          <button class="btn secondary small" data-edit="${item.id}">Editar</button>
          <button class="btn secondary small" data-test="${item.id}">Probar</button>
          <button class="btn secondary small" data-duplicate="${item.id}">Duplicar</button>
          <button class="btn danger small" data-delete="${item.id}">Eliminar</button>
        </div></td>
      </tr>`).join("") : `<tr><td colspan="6">No hay conexiones configuradas.</td></tr>`;
  }

  function renderAlerts() {
    $("#alerts-body").innerHTML = state.alerts.length ? state.alerts.map((alert) => `
      <tr>
        <td>${escapeHtml(formatDate(alert.created_at))}</td>
        <td>${escapeHtml(alert.connection_name || "Sistema")}</td>
        <td class="mono">${escapeHtml(alert.cause)}</td>
        <td>${escapeHtml(alert.channel)}</td>
        <td>${statusBadge(alert.status === "sent" ? "ok" : "failed")}</td>
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
    if (!window.Chart || !canvas) return;
    const ordered = [...state.runs].slice(0, 12).reverse();
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

  function openConnection(id = null) {
    const form = $("#connection-form");
    form.reset();
    form.elements.enabled.checked = true;
    form.elements.timezone.value = "America/Bogota";
    form.elements.dest_root.value = "downloads";
    form.elements.window_hours.value = "24";
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
    $("#connection-dialog").showModal();
  }

  function connectionPayload(form) {
    const data = new FormData(form);
    const numeric = ["port", "window_hours", "max_parallel_files", "retries"];
    const payload = {};
    for (const [key, value] of data.entries()) {
      if (key === "id") continue;
      if (numeric.includes(key)) payload[key] = value === "" ? null : Number(value);
      else if (key === "remote_paths") payload[key] = value.split(/\r?\n/).map((part) => part.trim()).filter(Boolean);
      else payload[key] = value;
    }
    payload.enabled = form.elements.enabled.checked;
    if (!payload.secret) delete payload.secret;
    return payload;
  }

  async function submitConnection(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const id = form.elements.id.value;
    try {
      await api(id ? `/api/connections/${id}` : "/api/connections", {
        method: id ? "PATCH" : "POST",
        body: connectionPayload(form),
      });
      $("#connection-dialog").close();
      toast(id ? "Conexión actualizada." : "Conexión creada.");
      await Promise.all([loadConnections(), loadDashboard()]);
    } catch (error) {
      toast(error.message, true);
    }
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

  async function testConnection(id) {
    toast("Probando conexión y calculando el plan…");
    try {
      const result = await api(`/api/connections/${id}/test`, { method: "POST" });
      const suffix = result.is_partial ? " Hay advertencias en el listado." : "";
      toast(`Conexión correcta: ${result.files_to_download} archivo(s) entrarían en la ventana.${suffix}`);
    } catch (error) {
      toast(error.message, true);
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
      $("#detail-content").innerHTML = `
        <div class="detail-grid">
          <div><span>Conexión</span><strong>${escapeHtml(run.connection_name)}</strong></div>
          <div><span>Estado</span><strong>${escapeHtml(statusLabel(run.status))}</strong></div>
          <div><span>Inicio</span><strong>${escapeHtml(formatDate(run.started_at))}</strong></div>
          <div><span>Descargados</span><strong>${run.files_downloaded || 0}</strong></div>
          <div><span>Fallidos</span><strong>${run.files_failed || 0}</strong></div>
          <div><span>Volumen</span><strong>${escapeHtml(formatBytes(run.bytes_downloaded))}</strong></div>
        </div>
        <p><a class="btn secondary small" href="/api/runs/${run.id}/log.jsonl">Descargar log JSONL</a></p>
        <div class="table-panel"><table><thead><tr><th>Archivo</th><th>Estado</th><th>Tamaño</th><th>Error</th></tr></thead><tbody>
          ${run.files.length ? run.files.map((file) => `<tr><td class="path-cell mono">${escapeHtml(file.remote_path)}</td><td>${statusBadge(file.status)}</td><td>${escapeHtml(formatBytes(file.size_bytes))}</td><td>${escapeHtml(file.error_msg || "—")}</td></tr>`).join("") : `<tr><td colspan="4">La corrida no contiene archivos.</td></tr>`}
        </tbody></table></div>`;
      $("#detail-dialog").showModal();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function handleAction(event) {
    const target = event.target.closest("button, [data-go]");
    if (!target) return;
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
    $("#new-connection-button").addEventListener("click", () => openConnection());
    $("#refresh-button").addEventListener("click", refreshAll);
    $("#connection-form").addEventListener("submit", submitConnection);
    $("#history-filters").addEventListener("submit", (event) => submitFilters(event, "runs"));
    $("#file-filters").addEventListener("submit", (event) => submitFilters(event, "files"));
    $("#settings-form").addEventListener("submit", saveSettings);
    $("#retention-button").addEventListener("click", runRetention);
    $("[data-close-detail]").addEventListener("click", () => $("#detail-dialog").close());
  }

  async function init() {
    bindEvents();
    setView(location.hash.slice(1) || "inicio");
    await refreshAll();
    await pollProgress();
  }

  init();
})();
