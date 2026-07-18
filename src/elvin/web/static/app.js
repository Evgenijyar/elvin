const state = {
    authenticated: false,
    meta: null,
    projects: [],
    robots: [],
    assignments: [],
    stages: new Map(),
    queues: new Map(),
    gemini: null,
    activePage: "calls",
    activeProjectId: null,
    selectedRobotId: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
    const response = await fetch(path, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
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
}

async function refreshAll() {
    try {
        const [meta, projects, robots, dashboard, gemini] = await Promise.all([
            api("/api/meta"),
            api("/api/projects"),
            api("/api/robots"),
            api("/api/dashboard"),
            api("/api/settings/gemini"),
        ]);
        state.meta = meta;
        state.projects = projects.items || [];
        state.robots = robots.items || [];
        state.assignments = dashboard.items || [];
        state.gemini = gemini;
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

    $("#assignmentGrid").innerHTML = assignments.map((item) => {
        const stageOptions = ['<option value="">Выберите стадию</option>', ...stages.map((stage) => `
            <option value="${stage.id}" ${Number(stage.id) === Number(item.source_stage_id) ? "selected" : ""}>${escapeHtml(stage.name)}</option>
        `)].join("");
        return `
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
                    <label>
                        <span>Стадия, из которой забирать лиды</span>
                        <select class="stage-select">${stageOptions}</select>
                    </label>
                    <label>
                        <span>Лимит звонков в очереди</span>
                        <input class="call-limit" type="number" min="1" max="1000" value="${Number(item.call_limit || 50)}">
                    </label>
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
        `;
    }).join("");

    $("#assignmentGrid").querySelectorAll(".assignment-card").forEach((card) => {
        const id = card.dataset.assignmentId;
        const item = state.assignments.find((assignment) => assignment.id === id);
        card.querySelector(".stage-select").addEventListener("change", async (event) => {
            const stage = stages.find((candidate) => String(candidate.id) === event.target.value);
            try {
                const result = await api(`/api/dashboard/assignments/${id}`, {
                    method: "PUT",
                    body: JSON.stringify({
                        source_stage_id: stage?.id || null,
                        source_stage_name: stage?.name || "",
                    }),
                });
                Object.assign(item, result.item);
                state.queues.delete(id);
                showToast("Стадия сохранена");
                await refreshQueueCard(id, card);
            } catch (error) { showToast(error.message, true); }
        });
        card.querySelector(".call-limit").addEventListener("change", async (event) => {
            const value = Math.max(1, Math.min(1000, Number(event.target.value || 50)));
            event.target.value = value;
            try {
                const result = await api(`/api/dashboard/assignments/${id}`, {
                    method: "PUT",
                    body: JSON.stringify({ call_limit: value }),
                });
                Object.assign(item, result.item);
                state.queues.delete(id);
                showToast("Лимит сохранён");
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
        summary.querySelector("small").textContent = `${batch.completed || 0} выполнено · ${batch.failed || 0} ошибок · ${batch.total || 0} всего`;
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
        </tr>
    `).join("");
    $("#queueContent").innerHTML = `
        <div class="preview-summary">
            <div class="meta-cell"><span>Статус</span><strong>${escapeHtml(queueStatusLabel(batch.status))}</strong></div>
            <div class="meta-cell"><span>Выполнено</span><strong>${batch.completed || 0}</strong></div>
            <div class="meta-cell"><span>Ошибок</span><strong>${batch.failed || 0}</strong></div>
            <div class="meta-cell"><span>Всего</span><strong>${batch.total || 0}</strong></div>
        </div>
        <table class="preview-table">
            <thead><tr><th>№</th><th>ID</th><th>Лид</th><th>Контакт</th><th>Телефон</th><th>Статус</th></tr></thead>
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
    renderTemperature();
    const key = $("#geminiKeyStatus");
    key.textContent = state.gemini?.configured
        ? "✓ Gemini API key настроен"
        : "Gemini API key ещё не настроен. Откройте раздел «Настройки».";
    key.classList.toggle("ok", Boolean(state.gemini?.configured));
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
        active: true,
    };
    if (!payload.name) {
        $("#robotSaveMessage").textContent = "Название обязательно.";
        return;
    }
    try {
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
    $("#geminiApiKey").value = state.gemini.api_key || "";
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
            body: JSON.stringify({ api_key: $("#geminiApiKey").value.trim() }),
        });
        await reloadGeminiSettings();
        state.meta = await api("/api/meta");
        renderRuntimeBadge();
        message.textContent = "Ключ сохранён";
        message.classList.add("success");
        showToast("Gemini API key сохранён");
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
            body: JSON.stringify({ api_key: $("#geminiApiKey").value.trim() }),
        });
        message.textContent = result.message;
        message.classList.add("success");
        showToast("Gemini Live подключение подтверждено");
    } catch (error) {
        message.textContent = error.message;
        showToast(error.message, true);
    } finally {
        button.disabled = false;
        button.textContent = "Проверить подключение";
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
