const state = {
    authenticated: false,
    meta: null,
    projects: [],
    robots: [],
    assignments: [],
    stages: new Map(),
    queues: new Map(),
    gemini: null,
    effectsCatalog: null,
    selectedEffectKey: null,
    activePage: "calls",
    activeProjectId: null,
    selectedRobotId: null,
    queuePollTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
    const isFormData = options.body instanceof FormData;
    const headers = { ...(options.headers || {}) };
    if (!isFormData && !headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, {
        credentials: "same-origin",
        ...options,
        headers,
    });
    let body = null;
    try { body = await response.json(); } catch (_) { body = {}; }
    if (!response.ok) {
        const error = new Error(body?.detail || `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
    }
    return body;
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function showToast(message, error = false) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.className = `toast ${error ? "error" : ""}`;
    setTimeout(() => toast.classList.add("hidden"), 4200);
}

async function bootstrap() {
    bindEvents();
    try {
        state.meta = await api("/api/meta");
        renderRuntimeBadge();
        const auth = await api("/api/auth/status");
        if (auth.authenticated) {
            await enterApplication();
        } else {
            showLogin();
        }
    } catch (error) {
        showLogin();
        $("#loginMessage").textContent = error.message;
    }
}

function bindEvents() {
    $("#loginForm").addEventListener("submit", handleLogin);
    $("#logoutButton").addEventListener("click", handleLogout);
    $("#createRobotButton").addEventListener("click", createRobot);
    $("#robotEditor").addEventListener("submit", saveRobot);
    $("#deleteRobotButton").addEventListener("click", deleteRobot);
    $("#robotTemperature").addEventListener("input", renderTemperature);
    $("#openAssignmentModal").addEventListener("click", openAssignmentModal);
    $$('[data-open-assignment]').forEach((button) => button.addEventListener("click", openAssignmentModal));
    $$('[data-close-modal]').forEach((button) => button.addEventListener("click", closeAssignmentModal));
    $$('[data-close-preview]').forEach((button) => button.addEventListener("click", closeLeadPreview));
    $$('[data-close-queue]').forEach((button) => button.addEventListener("click", closeQueue));
    $("#assignmentForm").addEventListener("submit", createAssignment);
    $("#geminiSettingsForm").addEventListener("submit", saveGeminiSettings);
    $("#testGeminiButton").addEventListener("click", testGeminiSettings);
    $("#enableAllEffects").addEventListener("click", () => setAllEffects(true));
    $("#disableAllEffects").addEventListener("click", () => setAllEffects(false));
    $$(".nav-button").forEach((button) => {
        button.addEventListener("click", () => switchPage(button.dataset.page));
    });
    $$(".editor-tab").forEach((button) => {
        button.addEventListener("click", () => switchEditorTab(button.dataset.tab));
    });
}

async function handleLogin(event) {
    event.preventDefault();
    const button = $("#loginButton");
    const message = $("#loginMessage");
    button.disabled = true;
    button.textContent = "Входим…";
    message.textContent = "";
    try {
        await api("/api/auth/login", {
            method: "POST",
            body: JSON.stringify({
                login: $("#loginInput").value.trim(),
                password: $("#passwordInput").value,
            }),
        });
        $("#passwordInput").value = "";
        await enterApplication();
    } catch (error) {
        message.textContent = error.message;
    } finally {
        button.disabled = false;
        button.textContent = "Войти";
    }
}

async function handleLogout() {
    try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
    state.authenticated = false;
    if (state.queuePollTimer) clearInterval(state.queuePollTimer);
    state.queuePollTimer = null;
    state.projects = [];
    state.robots = [];
    state.assignments = [];
    state.queues.clear();
    state.gemini = null;
    showLogin();
}

function showLogin() {
    $("#authScreen").classList.remove("hidden");
    $("#appShell").classList.add("hidden");
}

async function enterApplication() {
    state.authenticated = true;
    $("#authScreen").classList.add("hidden");
    $("#appShell").classList.remove("hidden");
    await refreshAll();
    startQueuePolling();
}

function startQueuePolling() {
    if (state.queuePollTimer) clearInterval(state.queuePollTimer);
    state.queuePollTimer = setInterval(() => {
        if (!state.authenticated || state.activePage !== "calls") return;
        $$("#assignmentGrid .assignment-card").forEach((card) => {
            void refreshQueueCard(card.dataset.assignmentId, card);
        });
    }, 2000);
}

async function refreshAll() {
    try {
        const [meta, projects, robots, dashboard, gemini, effectsCatalog] = await Promise.all([
            api("/api/meta"),
            api("/api/projects"),
            api("/api/robots"),
            api("/api/dashboard"),
            api("/api/settings/gemini"),
            api("/api/robots/effects/catalog"),
        ]);
        state.meta = meta;
        state.projects = projects.items || [];
        state.robots = robots.items || [];
        state.assignments = dashboard.items || [];
        state.gemini = gemini;
        state.effectsCatalog = effectsCatalog;
        if (!state.activeProjectId && state.assignments.length) {
            state.activeProjectId = state.assignments[0].project_id;
        }
        if (!state.selectedRobotId && state.robots.length) {
            state.selectedRobotId = state.robots[0].id;
        }
        renderAll();
    } catch (error) {
        if (error.status === 401) {
            showLogin();
            $("#loginMessage").textContent = "Сессия LPTracker истекла. Войдите повторно.";
            return;
        }
        showToast(error.message, true);
    }
}

function renderAll() {
    renderRuntimeBadge();
    renderProjectList();
    void renderCalls();
    renderRobotList();
    renderRobotEditor();
    renderGeminiSettings();
}

function renderRuntimeBadge() {
    if (!state.meta) return;
    const mode = state.meta.environment === "production" ? "PRODUCTION" : "LOCAL";
    const media = state.meta.media_ready ? "MEDIA READY" : "MEDIA OFF";
    $("#runtimeBadge").textContent = `${mode} · ${media} · v${state.meta.version}`;
}

function switchPage(page) {
    state.activePage = page;
    $$(".nav-button").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
    $$(".page").forEach((section) => section.classList.toggle("active", section.id === `${page}Page`));
    if (page === "settings") {
        void reloadGeminiSettings();
    }
}

function renderProjectList() {
    const grouped = new Map();
    for (const item of state.assignments) {
        if (!grouped.has(item.project_id)) {
            grouped.set(item.project_id, { name: item.project_name, count: 0 });
        }
        grouped.get(item.project_id).count += 1;
    }
    const container = $("#projectList");
    if (!grouped.size) {
        container.innerHTML = '<div class="muted sidebar-placeholder">Проекты ещё не добавлены</div>';
        return;
    }
    container.innerHTML = [...grouped.entries()].map(([id, item]) => `
        <button class="project-item ${Number(id) === Number(state.activeProjectId) ? "active" : ""}" data-project-id="${id}">
            <strong>${escapeHtml(item.name)}</strong>
            <span>${item.count} ИИ-робот${item.count === 1 ? "" : "а"}</span>
        </button>
    `).join("");
    container.querySelectorAll("[data-project-id]").forEach((button) => {
        button.addEventListener("click", async () => {
            state.activeProjectId = Number(button.dataset.projectId);
            await ensureStages(state.activeProjectId);
            renderProjectList();
            await renderCalls();
            switchPage("calls");
        });
    });
}

async function ensureStages(projectId) {
    if (!projectId || state.stages.has(projectId)) return;
    try {
        const result = await api(`/api/projects/${projectId}/stages`);
        state.stages.set(projectId, result.items || []);
    } catch (error) {
        showToast(error.message, true);
        state.stages.set(projectId, []);
    }
}

function queueStatusLabel(status) {
    const labels = {
        QUEUE_READY: "Очередь готова",
        RUNNING: "Обзвон запущен",
        CALL_REQUESTING: "Запрос звонка",
        WAITING_FOR_MEDIA: "Ожидаем Asterisk",
        IN_CALL: "Разговор",
        STOPPING: "Остановка",
        STOPPED: "Остановлено",
        COMPLETED: "Завершено",
        FAILED: "Ошибка",
    };
    return labels[status] || status || "Очередь не создана";
}

function stageOptions(stages, selectedId) {
    return ['<option value="">Не выбрана</option>', ...stages.map((stage) => `
        <option value="${stage.id}" ${Number(stage.id) === Number(selectedId) ? "selected" : ""}>${escapeHtml(stage.name)}</option>
    `)].join("");
}

function stageField(label, cssClass, idField, nameField, stages, item) {
    return `
        <label class="stage-field">
            <span>${label}</span>
            <select class="${cssClass}" data-id-field="${idField}" data-name-field="${nameField}">
                ${stageOptions(stages, item[idField])}
            </select>
        </label>
    `;
}

async function updateAssignmentValue(id, item, payload, message = "Настройки сохранены") {
    const result = await api(`/api/dashboard/assignments/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
    });
    Object.assign(item, result.item);
    showToast(message);
    return result.item;
}

async function renderCalls() {
    const assignments = state.assignments.filter((item) => Number(item.project_id) === Number(state.activeProjectId));
    $("#callsEmpty").classList.toggle("hidden", assignments.length > 0);
    $("#callsWorkspace").classList.toggle("hidden", assignments.length === 0);
    if (!assignments.length) return;

    const project = assignments[0];
    $("#activeProjectName").textContent = project.project_name;
    $("#activeProjectMeta").textContent = `ID: ${project.project_id} · ИИ-роботов: ${assignments.length}`;
    await ensureStages(project.project_id);
    const stages = state.stages.get(project.project_id) || [];

    $("#assignmentGrid").innerHTML = assignments.map((item) => `
        <article class="assignment-card" data-assignment-id="${item.id}">
            <div class="assignment-head">
                <div>
                    <h3>${escapeHtml(item.robot_name)}</h3>
                    <p>${escapeHtml(item.robot_description || "Описание не заполнено")}</p>
                </div>
                <span class="status-pill">${escapeHtml(item.status || "STOPPED")}</span>
            </div>
            <div class="assignment-body">
                <div class="assignment-meta">
                    <div class="meta-cell"><span>Модель</span><strong>${escapeHtml(item.model_id)}</strong></div>
                    <div class="meta-cell"><span>Голос</span><strong>${escapeHtml(item.voice_name)}</strong></div>
                </div>

                <div class="stage-grid">
                    ${stageField("Откуда забирать лиды", "source-stage", "source_stage_id", "source_stage_name", stages, item)}
                    ${stageField("Лид", "outcome-stage", "lead_stage_id", "lead_stage_name", stages, item)}
                    ${stageField("Спецстадия", "outcome-stage", "special_stage_id", "special_stage_name", stages, item)}
                    ${stageField("Отказ", "outcome-stage", "refusal_stage_id", "refusal_stage_name", stages, item)}
                    ${stageField("Перезвонить", "outcome-stage", "callback_stage_id", "callback_stage_name", stages, item)}
                    ${stageField("Стоп-лист", "outcome-stage", "stop_list_stage_id", "stop_list_stage_name", stages, item)}
                    ${stageField("Автоответчик", "outcome-stage", "answering_machine_stage_id", "answering_machine_stage_name", stages, item)}
                    ${stageField("Недозвон", "outcome-stage", "no_answer_stage_id", "no_answer_stage_name", stages, item)}
                    <label class="checkbox-field">
                        <input class="count-special" type="checkbox" ${item.count_special_as_lead ? "checked" : ""}>
                        <span>Считать спецстадию лидом</span>
                    </label>
                </div>

                <div class="limits-grid">
                    <label><span>Limit звонков</span><input class="call-limit" type="number" min="1" max="1000" value="${Number(item.call_limit || 50)}"></label>
                    <label><span>Limit лидов (0 — без лимита)</span><input class="lead-limit" type="number" min="0" max="1000" value="${Number(item.lead_limit || 0)}"></label>
                    <div class="counter-cell"><span>Звонков совершено</span><strong class="calls-count">0</strong></div>
                    <div class="counter-cell"><span>Лидов собрано</span><strong class="leads-count">0</strong></div>
                </div>

                <div class="background-audio-row">
                    <label class="audio-file-field">
                        <span>Фоновое аудио (циклично, только клиенту)</span>
                        <input class="background-file" type="file" accept="audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus,.webm">
                    </label>
                    <div class="audio-current ${item.background_audio_filename ? "" : "empty"}">
                        <span class="audio-filename">${escapeHtml(item.background_audio_filename || "Файл не выбран")}</span>
                        <button class="audio-delete" type="button" title="Удалить фоновое аудио" ${item.background_audio_filename ? "" : "disabled"}>×</button>
                    </div>
                    <label class="volume-field">
                        <span>Громкость: <b class="volume-value">${Number(item.background_audio_volume ?? 15)}%</b></span>
                        <input class="background-volume" type="range" min="0" max="100" step="1" value="${Number(item.background_audio_volume ?? 15)}">
                    </label>
                </div>

                <div class="queue-summary" data-queue-summary>
                    <span>Очередь</span>
                    <strong>Загрузка…</strong>
                    <small></small>
                </div>
                <div class="assignment-actions multi-row">
                    <button class="flat-button preview-button" type="button">Проверить лиды</button>
                    <button class="flat-button prepare-queue" type="button">Собрать очередь</button>
                    <button class="flat-button show-queue" type="button">Открыть очередь</button>
                    <button class="primary-button start" type="button">Старт</button>
                    <button class="flat-button stop" type="button">Стоп</button>
                    <button class="flat-button danger remove-assignment" type="button">Удалить</button>
                </div>
                <div class="webhook-line ${item.webhook_registered ? "ok" : ""}">
                    ${item.webhook_registered ? "✓ Webhook LPTracker зарегистрирован" : (state.meta?.environment === "production" ? "Webhook пока не подтверждён" : "Webhook зарегистрируется после серверного деплоя")}
                </div>
            </div>
        </article>
    `).join("");

    $("#assignmentGrid").querySelectorAll(".assignment-card").forEach((card) => {
        const id = card.dataset.assignmentId;
        const item = state.assignments.find((assignment) => assignment.id === id);
        card.querySelectorAll(".source-stage, .outcome-stage").forEach((select) => {
            select.addEventListener("change", async (event) => {
                const stage = stages.find((candidate) => String(candidate.id) === event.target.value);
                const payload = {
                    [select.dataset.idField]: stage?.id || null,
                    [select.dataset.nameField]: stage?.name || "",
                };
                try {
                    await updateAssignmentValue(id, item, payload, "Стадия сохранена");
                    if (select.classList.contains("source-stage")) {
                        state.queues.delete(id);
                        await refreshQueueCard(id, card);
                    }
                } catch (error) { showToast(error.message, true); }
            });
        });
        card.querySelector(".count-special").addEventListener("change", async (event) => {
            try { await updateAssignmentValue(id, item, { count_special_as_lead: event.target.checked }); }
            catch (error) { showToast(error.message, true); }
        });
        card.querySelector(".call-limit").addEventListener("change", async (event) => {
            const value = Math.max(1, Math.min(1000, Number(event.target.value || 50)));
            event.target.value = value;
            try {
                await updateAssignmentValue(id, item, { call_limit: value }, "Лимит звонков сохранён");
                state.queues.delete(id);
            } catch (error) { showToast(error.message, true); }
        });
        card.querySelector(".lead-limit").addEventListener("change", async (event) => {
            const value = Math.max(0, Math.min(1000, Number(event.target.value || 0)));
            event.target.value = value;
            try { await updateAssignmentValue(id, item, { lead_limit: value }, "Лимит лидов сохранён"); }
            catch (error) { showToast(error.message, true); }
        });
        const volume = card.querySelector(".background-volume");
        volume.addEventListener("input", () => {
            card.querySelector(".volume-value").textContent = `${volume.value}%`;
        });
        volume.addEventListener("change", async () => {
            try { await updateAssignmentValue(id, item, { background_audio_volume: Number(volume.value) }, "Громкость сохранена"); }
            catch (error) { showToast(error.message, true); }
        });
        card.querySelector(".background-file").addEventListener("change", async (event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            const form = new FormData();
            form.append("file", file);
            event.target.disabled = true;
            try {
                const result = await api(`/api/dashboard/assignments/${id}/background-audio`, { method: "POST", body: form });
                Object.assign(item, result.item);
                showToast("Фоновое аудио загружено");
                await renderCalls();
            } catch (error) { showToast(error.message, true); }
            finally { event.target.disabled = false; }
        });
        card.querySelector(".audio-delete").addEventListener("click", async () => {
            try {
                const result = await api(`/api/dashboard/assignments/${id}/background-audio`, { method: "DELETE" });
                Object.assign(item, result.item);
                showToast("Фоновое аудио удалено");
                await renderCalls();
            } catch (error) { showToast(error.message, true); }
        });
        card.querySelector(".preview-button").addEventListener("click", () => previewLeads(id));
        card.querySelector(".prepare-queue").addEventListener("click", () => prepareQueue(id, card));
        card.querySelector(".show-queue").addEventListener("click", () => showQueue(id));
        card.querySelector(".remove-assignment").addEventListener("click", () => removeAssignment(id));
        card.querySelector(".start").addEventListener("click", () => startAssignment(id, card));
        card.querySelector(".stop").addEventListener("click", () => stopAssignment(id, card));
        void refreshQueueCard(id, card);
    });
}

async function refreshQueueCard(assignmentId, card) {
    try {
        const result = await api(`/api/dashboard/assignments/${assignmentId}/queue`);
        state.queues.set(assignmentId, result);
        const summary = card.querySelector("[data-queue-summary]");
        if (!result.batch) {
            summary.querySelector("strong").textContent = "Не сформирована";
            summary.querySelector("small").textContent = "Нажмите «Собрать очередь»";
            return;
        }
        const batch = result.batch;
        summary.querySelector("strong").textContent = queueStatusLabel(batch.status);
        const stopReason = batch.stop_reason === "lead_limit" ? " · остановка по лимиту лидов" : (batch.stop_reason === "call_limit" ? " · остановка по лимиту звонков" : "");
        summary.querySelector("small").textContent = `${batch.completed || 0} выполнено · ${batch.failed || 0} ошибок · ${batch.total || 0} всего${stopReason}`;
        card.querySelector(".calls-count").textContent = batch.calls_made || 0;
        card.querySelector(".leads-count").textContent = batch.leads_count || 0;
        const statusPill = card.querySelector(".status-pill");
        statusPill.textContent = batch.status;
    } catch (error) {
        card.querySelector("[data-queue-summary] strong").textContent = "Ошибка загрузки";
    }
}

async function previewLeads(assignmentId) {
    openLeadPreview();
    $("#leadPreviewContent").innerHTML = '<p class="muted">Загружаем лиды LPTracker…</p>';
    try {
        const result = await api(`/api/dashboard/assignments/${assignmentId}/lead-preview`);
        const rows = (result.items || []).map((item) => `
            <tr><td>${item.lead_id}</td><td>${escapeHtml(item.lead_name)}</td><td>${escapeHtml(item.contact_name)}</td><td>${escapeHtml(item.phone)}</td></tr>
        `).join("");
        $("#leadPreviewContent").innerHTML = `
            <div class="preview-summary">
                <div class="meta-cell"><span>Просканировано</span><strong>${result.scanned_count}</strong></div>
                <div class="meta-cell"><span>С телефоном</span><strong>${result.with_phone_count}</strong></div>
                <div class="meta-cell"><span>Подходит</span><strong>${result.matched_count}</strong></div>
            </div>
            ${rows ? `<table class="preview-table"><thead><tr><th>ID</th><th>Лид</th><th>Контакт</th><th>Телефон</th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="muted">В выбранной стадии не найдено лидов с телефоном.</p>'}
        `;
    } catch (error) {
        $("#leadPreviewContent").innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;
    }
}

async function prepareQueue(assignmentId, card) {
    const button = card.querySelector(".prepare-queue");
    button.disabled = true;
    button.textContent = "Собираем…";
    try {
        const result = await api(`/api/dashboard/assignments/${assignmentId}/queue`, { method: "POST" });
        state.queues.set(assignmentId, result);
        showToast(`Очередь собрана: ${result.batch.total} лидов`);
        await refreshQueueCard(assignmentId, card);
        renderQueueModal(result);
    } catch (error) {
        showToast(error.message, true);
    } finally {
        button.disabled = false;
        button.textContent = "Собрать очередь";
    }
}

async function showQueue(assignmentId) {
    openQueue();
    $("#queueContent").innerHTML = '<p class="muted">Загружаем очередь…</p>';
    try {
        const result = await api(`/api/dashboard/assignments/${assignmentId}/queue`);
        state.queues.set(assignmentId, result);
        renderQueueModal(result);
    } catch (error) {
        $("#queueContent").innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;
    }
}

function renderQueueModal(result) {
    openQueue();
    if (!result.batch) {
        $("#queueContent").innerHTML = '<p class="muted">Очередь ещё не сформирована.</p>';
        return;
    }
    const batch = result.batch;
    const rows = (result.items || []).map((item) => `
        <tr>
            <td>${item.position}</td>
            <td>${item.lead_id}</td>
            <td>${escapeHtml(item.lead_name)}</td>
            <td>${escapeHtml(item.contact_name)}</td>
            <td>${escapeHtml(item.phone_masked)}</td>
            <td><span class="queue-item-status">${escapeHtml(item.status)}</span></td>
            <td>${escapeHtml(item.outcome || "—")}</td>
            <td>${escapeHtml(item.destination_stage_name || "—")}</td>
        </tr>
    `).join("");
    $("#queueContent").innerHTML = `
        <div class="preview-summary">
            <div class="meta-cell"><span>Статус</span><strong>${escapeHtml(queueStatusLabel(batch.status))}</strong></div>
            <div class="meta-cell"><span>Выполнено</span><strong>${batch.completed || 0}</strong></div>
            <div class="meta-cell"><span>Ошибок</span><strong>${batch.failed || 0}</strong></div>
            <div class="meta-cell"><span>Всего</span><strong>${batch.total || 0}</strong></div>
            <div class="meta-cell"><span>Звонков</span><strong>${batch.calls_made || 0}</strong></div>
            <div class="meta-cell"><span>Лидов</span><strong>${batch.leads_count || 0}</strong></div>
        </div>
        <table class="preview-table">
            <thead><tr><th>№</th><th>ID</th><th>Лид</th><th>Контакт</th><th>Телефон</th><th>Статус</th><th>Результат</th><th>Стадия</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

async function startAssignment(assignmentId, card) {
    try {
        const result = await api(`/api/dashboard/assignments/${assignmentId}/start`, { method: "POST" });
        showToast("Последовательный обзвон запущен");
        state.queues.set(assignmentId, { batch: result.batch, items: [] });
        await refreshQueueCard(assignmentId, card);
    } catch (error) {
        showToast(error.message, true);
    }
}

async function stopAssignment(assignmentId, card) {
    try {
        await api(`/api/dashboard/assignments/${assignmentId}/stop`, { method: "POST" });
        showToast("Остановка очереди запрошена");
        await refreshQueueCard(assignmentId, card);
    } catch (error) {
        showToast(error.message, true);
    }
}

async function removeAssignment(assignmentId) {
    if (!confirm("Удалить этого робота из проекта?")) return;
    try {
        await api(`/api/dashboard/assignments/${assignmentId}`, { method: "DELETE" });
        state.assignments = state.assignments.filter((item) => item.id !== assignmentId);
        state.queues.delete(assignmentId);
        if (!state.assignments.some((item) => Number(item.project_id) === Number(state.activeProjectId))) {
            state.activeProjectId = state.assignments[0]?.project_id || null;
        }
        renderAll();
    } catch (error) { showToast(error.message, true); }
}

function renderRobotList() {
    const container = $("#robotList");
    if (!state.robots.length) {
        container.innerHTML = '<div class="muted robot-placeholder">Пока нет сохранённых роботов.</div>';
        return;
    }
    container.innerHTML = state.robots.map((robot) => `
        <button class="robot-list-item ${robot.id === state.selectedRobotId ? "active" : ""}" data-robot-id="${robot.id}">
            <strong>${escapeHtml(robot.name)}</strong>
            <span>${escapeHtml(robot.voice_name)} · ${escapeHtml(robot.model_id)}</span>
        </button>
    `).join("");
    container.querySelectorAll("[data-robot-id]").forEach((button) => {
        button.addEventListener("click", () => {
            state.selectedRobotId = button.dataset.robotId;
            renderRobotList();
            renderRobotEditor();
        });
    });
}

function selectedRobot() {
    return state.robots.find((robot) => robot.id === state.selectedRobotId) || null;
}

function renderRobotEditor() {
    const robot = selectedRobot();
    $("#robotEditorEmpty").classList.toggle("hidden", Boolean(robot));
    $("#robotEditor").classList.toggle("hidden", !robot);
    if (!robot) return;
    $("#robotName").value = robot.name || "";
    $("#robotDescription").value = robot.description || "";
    $("#robotModel").value = robot.model_id || "gemini-3.1-flash-live-preview";
    $("#robotVoice").value = robot.voice_name || "Kore";
    $("#robotTemperature").value = robot.temperature ?? 0.3;
    $("#robotRole").value = robot.role_prompt || "";
    $("#robotKnowledge").value = robot.knowledge_base || "";
    $("#robotFirstPhrase").value = robot.first_phrase || "";
    $("#robotLeadCondition").value = robot.lead_condition || "";
    $("#robotSpecialCondition").value = robot.special_condition || "";
    $("#robotRefusalCondition").value = robot.refusal_condition || "";
    $("#robotCallbackCondition").value = robot.callback_condition || "";
    $("#robotStopListCondition").value = robot.stop_list_condition || "";
    $("#robotAnsweringMachineCondition").value = robot.answering_machine_condition || "";
    $("#robotActorApiKey").value = state.gemini?.actor_api_key || state.gemini?.api_key || "";
    $("#robotDirectorApiKey").value = state.gemini?.director_api_key || "";
    ensureRobotEffects(robot);
    renderEffectsEditor();
    renderTemperature();
    const key = $("#geminiKeyStatus");
    const actorReady = Boolean(state.gemini?.actor_configured ?? state.gemini?.configured);
    const directorReady = Boolean(state.gemini?.director_configured);
    key.textContent = actorReady && directorReady
        ? "✓ Ключи «Актёр» и «Режиссёр» настроены"
        : actorReady
            ? "Ключ «Актёр» настроен. Для эффектов добавьте ключ «Режиссёр»."
            : "Ключ «Актёр» ещё не настроен. Откройте раздел «Настройки».";
    key.classList.toggle("ok", actorReady && directorReady);
}

function cloneEffectsDefaults() {
    return JSON.parse(JSON.stringify(state.effectsCatalog?.defaults || {}));
}

function ensureRobotEffects(robot) {
    const defaults = cloneEffectsDefaults();
    const source = robot.effects_config && typeof robot.effects_config === "object"
        ? robot.effects_config
        : {};
    Object.entries(defaults).forEach(([effectKey, defaultValues]) => {
        defaults[effectKey] = { ...defaultValues, ...(source[effectKey] || {}) };
    });
    robot.effects_config = defaults;
    const available = state.effectsCatalog?.effects || [];
    if (!available.some((effect) => effect.key === state.selectedEffectKey)) {
        state.selectedEffectKey = available[0]?.key || null;
    }
}

function setAllEffects(enabled) {
    const robot = selectedRobot();
    if (!robot) return;
    ensureRobotEffects(robot);
    Object.values(robot.effects_config).forEach((effect) => { effect.enabled = enabled; });
    renderEffectsEditor();
}

function renderEffectsEditor() {
    const robot = selectedRobot();
    const catalog = state.effectsCatalog?.effects || [];
    const menu = $("#effectsMenu");
    const settings = $("#effectSettings");
    if (!robot || !catalog.length) {
        menu.innerHTML = '<div class="muted">Каталог эффектов не загружен.</div>';
        settings.innerHTML = "";
        return;
    }
    ensureRobotEffects(robot);
    menu.innerHTML = catalog.map((effect) => {
        const enabled = Boolean(robot.effects_config[effect.key]?.enabled);
        const active = effect.key === state.selectedEffectKey;
        return `
            <div class="effect-menu-row ${active ? "active" : ""} ${enabled ? "enabled" : ""}" data-effect-key="${escapeHtml(effect.key)}">
                <input class="effect-enabled-toggle" type="checkbox" ${enabled ? "checked" : ""} aria-label="Включить ${escapeHtml(effect.label)}">
                <button class="effect-select-button" type="button">
                    <strong>${escapeHtml(effect.label)}</strong>
                    <small>${enabled ? "Включён" : "Выключен"}</small>
                </button>
            </div>
        `;
    }).join("");
    menu.querySelectorAll(".effect-menu-row").forEach((row) => {
        const effectKey = row.dataset.effectKey;
        row.querySelector(".effect-select-button").addEventListener("click", () => {
            state.selectedEffectKey = effectKey;
            renderEffectsEditor();
        });
        row.querySelector(".effect-enabled-toggle").addEventListener("change", (event) => {
            robot.effects_config[effectKey].enabled = event.target.checked;
            state.selectedEffectKey = effectKey;
            renderEffectsEditor();
        });
    });

    const effect = catalog.find((item) => item.key === state.selectedEffectKey) || catalog[0];
    const values = robot.effects_config[effect.key];
    settings.innerHTML = `
        <div class="effect-settings-header">
            <div>
                <div class="eyebrow">НАСТРОЙКИ ЭФФЕКТА</div>
                <h3>${escapeHtml(effect.label)}</h3>
                <p>${escapeHtml(effect.description)}</p>
            </div>
            <span class="effect-state-badge ${values.enabled ? "on" : ""}">${values.enabled ? "Включён" : "Выключен"}</span>
        </div>
        <div class="effect-fields-grid">
            ${effect.fields.map((field) => renderEffectField(effect.key, field, values[field.key])).join("")}
        </div>
        <div class="effect-safety-note">Изменения применятся после сохранения робота и только к новым звонкам. При выключенном эффекте его обработчик не участвует в аудиотракте.</div>
    `;
    settings.querySelectorAll("[data-effect-field]").forEach((input) => {
        const fieldKey = input.dataset.effectField;
        const field = effect.fields.find((item) => item.key === fieldKey);
        const update = () => {
            robot.effects_config[effect.key][fieldKey] = field.type === "number"
                ? Number(input.value)
                : input.value;
        };
        input.addEventListener(field.type === "number" ? "change" : "input", update);
    });
}

function renderEffectField(effectKey, field, value) {
    if (field.type === "text") {
        return `
            <label class="effect-field wide">
                <span>${escapeHtml(field.label)}</span>
                <textarea data-effect-field="${escapeHtml(field.key)}" rows="5">${escapeHtml(value ?? "")}</textarea>
            </label>
        `;
    }
    return `
        <label class="effect-field">
            <span>${escapeHtml(field.label)}</span>
            <input data-effect-field="${escapeHtml(field.key)}" type="number"
                   min="${field.min}" max="${field.max}" step="${field.step}" value="${Number(value ?? field.default)}">
            <small>Диапазон: ${field.min}–${field.max}, шаг ${field.step}</small>
        </label>
    `;
}

function renderTemperature() {
    $("#temperatureValue").textContent = Number($("#robotTemperature").value || 0.3).toFixed(2);
}

async function createRobot() {
    try {
        const result = await api("/api/robots", {
            method: "POST",
            body: JSON.stringify({
                name: `Новый робот ${state.robots.length + 1}`,
                description: "",
                model_id: "gemini-3.1-flash-live-preview",
                voice_name: "Kore",
                temperature: 0.3,
                role_prompt: "",
                knowledge_base: "",
                first_phrase: "",
                lead_condition: "",
                special_condition: "",
                refusal_condition: "",
                callback_condition: "",
                stop_list_condition: "",
                answering_machine_condition: "",
                effects_config: cloneEffectsDefaults(),
                active: true,
            }),
        });
        state.robots.unshift(result.item);
        state.selectedRobotId = result.item.id;
        switchPage("robots");
        renderRobotList();
        renderRobotEditor();
    } catch (error) { showToast(error.message, true); }
}

async function saveRobot(event) {
    event.preventDefault();
    const robot = selectedRobot();
    if (!robot) return;
    const payload = {
        name: $("#robotName").value.trim(),
        description: $("#robotDescription").value.trim(),
        model_id: $("#robotModel").value,
        voice_name: $("#robotVoice").value,
        temperature: Number($("#robotTemperature").value),
        role_prompt: $("#robotRole").value,
        knowledge_base: $("#robotKnowledge").value,
        first_phrase: $("#robotFirstPhrase").value,
        lead_condition: $("#robotLeadCondition").value,
        special_condition: $("#robotSpecialCondition").value,
        refusal_condition: $("#robotRefusalCondition").value,
        callback_condition: $("#robotCallbackCondition").value,
        stop_list_condition: $("#robotStopListCondition").value,
        answering_machine_condition: $("#robotAnsweringMachineCondition").value,
        effects_config: robot.effects_config || cloneEffectsDefaults(),
        active: true,
    };
    if (!payload.name) {
        $("#robotSaveMessage").textContent = "Название обязательно.";
        return;
    }
    try {
        await saveRobotGeminiKeysIfChanged();
        const result = await api(`/api/robots/${robot.id}`, {
            method: "PUT",
            body: JSON.stringify(payload),
        });
        Object.assign(robot, result.item);
        $("#robotSaveMessage").textContent = "Сохранено";
        $("#robotSaveMessage").classList.add("success");
        renderRobotList();
        setTimeout(() => { $("#robotSaveMessage").textContent = ""; }, 1800);
    } catch (error) {
        $("#robotSaveMessage").classList.remove("success");
        $("#robotSaveMessage").textContent = error.message;
    }
}

async function saveRobotGeminiKeysIfChanged() {
    const actor = $("#robotActorApiKey").value.trim();
    const director = $("#robotDirectorApiKey").value.trim();
    const currentActor = state.gemini?.actor_api_key || state.gemini?.api_key || "";
    const currentDirector = state.gemini?.director_api_key || "";
    if (actor === currentActor && director === currentDirector) return;
    await api("/api/settings/gemini", {
        method: "PUT",
        body: JSON.stringify({
            api_key: actor,
            director_api_key: director,
        }),
    });
    state.gemini = await api("/api/settings/gemini");
}

async function deleteRobot() {
    const robot = selectedRobot();
    if (!robot || !confirm(`Удалить робота «${robot.name}»? Его назначения тоже будут удалены.`)) return;
    try {
        await api(`/api/robots/${robot.id}`, { method: "DELETE" });
        state.robots = state.robots.filter((item) => item.id !== robot.id);
        state.assignments = state.assignments.filter((item) => item.robot_id !== robot.id);
        state.selectedRobotId = state.robots[0]?.id || null;
        renderAll();
    } catch (error) { showToast(error.message, true); }
}

function switchEditorTab(tab) {
    $$(".editor-tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
    $$(".tab-pane").forEach((pane) => pane.classList.toggle("active", pane.dataset.pane === tab));
}

async function reloadGeminiSettings() {
    try {
        state.gemini = await api("/api/settings/gemini");
        renderGeminiSettings();
        renderRobotEditor();
    } catch (error) {
        showToast(error.message, true);
    }
}

function renderGeminiSettings() {
    if (!state.gemini) return;
    $("#geminiApiKey").value = state.gemini.actor_api_key || state.gemini.api_key || "";
    $("#geminiDirectorApiKey").value = state.gemini.director_api_key || "";
    $("#geminiModelId").value = state.gemini.model_id || "";
    $("#geminiEndpoint").value = state.gemini.websocket_endpoint || "";
}

async function saveGeminiSettings(event) {
    event.preventDefault();
    const message = $("#geminiSettingsMessage");
    message.textContent = "Сохраняем…";
    try {
        await api("/api/settings/gemini", {
            method: "PUT",
            body: JSON.stringify({
                api_key: $("#geminiApiKey").value.trim(),
                director_api_key: $("#geminiDirectorApiKey").value.trim(),
            }),
        });
        await reloadGeminiSettings();
        state.meta = await api("/api/meta");
        renderRuntimeBadge();
        message.textContent = "Ключи сохранены";
        message.classList.add("success");
        showToast("Ключи Gemini сохранены");
    } catch (error) {
        message.classList.remove("success");
        message.textContent = error.message;
    }
}

async function testGeminiSettings() {
    const button = $("#testGeminiButton");
    const message = $("#geminiSettingsMessage");
    button.disabled = true;
    button.textContent = "Подключаемся…";
    message.textContent = "Открываем Gemini Live WebSocket…";
    message.classList.remove("success");
    try {
        const result = await api("/api/settings/gemini/test", {
            method: "POST",
            body: JSON.stringify({
                api_key: $("#geminiApiKey").value.trim(),
                director_api_key: $("#geminiDirectorApiKey").value.trim(),
                target: "both",
            }),
        });
        message.textContent = result.message;
        message.classList.add("success");
        showToast("Оба подключения Gemini Live подтверждены");
    } catch (error) {
        message.textContent = error.message;
        showToast(error.message, true);
    } finally {
        button.disabled = false;
        button.textContent = "Проверить оба подключения";
    }
}

function openAssignmentModal() {
    if (!state.robots.length) {
        switchPage("robots");
        showToast("Сначала создайте и сохраните ИИ-робота.", true);
        return;
    }
    $("#assignmentProject").innerHTML = state.projects.map((project) => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join("");
    $("#assignmentRobot").innerHTML = state.robots.map((robot) => `<option value="${robot.id}">${escapeHtml(robot.name)}</option>`).join("");
    $("#assignmentMessage").textContent = "";
    $("#assignmentModal").classList.remove("hidden");
}

function closeAssignmentModal() { $("#assignmentModal").classList.add("hidden"); }
function openLeadPreview() { $("#leadPreviewModal").classList.remove("hidden"); }
function closeLeadPreview() { $("#leadPreviewModal").classList.add("hidden"); }
function openQueue() { $("#queueModal").classList.remove("hidden"); }
function closeQueue() { $("#queueModal").classList.add("hidden"); }

async function createAssignment(event) {
    event.preventDefault();
    try {
        const result = await api("/api/dashboard/assignments", {
            method: "POST",
            body: JSON.stringify({
                project_id: Number($("#assignmentProject").value),
                robot_id: $("#assignmentRobot").value,
            }),
        });
        closeAssignmentModal();
        await refreshAll();
        state.activeProjectId = result.item.project_id;
        switchPage("calls");
        renderProjectList();
        await renderCalls();
        showToast("Робот добавлен в проект");
    } catch (error) {
        $("#assignmentMessage").textContent = error.message;
    }
}

bootstrap();
