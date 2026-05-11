"use strict";

const STATUS_ICON = {
    pending: "·",
    running: "⏳",
    ok: "✅",
    error: "❌",
};

const els = {
    health: document.getElementById("health"),
    healthLabel: document.querySelector("#health .label"),
    groupList: document.getElementById("group-list"),
    groupListEmpty: document.getElementById("group-list-empty"),
    ruleEnvsList: document.getElementById("rule-envs-list"),
    ruleEnvsEmpty: document.getElementById("rule-envs-empty"),
    currentError: document.getElementById("current-error"),
    reloadCurrent: document.getElementById("reload-current"),
    availableError: document.getElementById("available-error"),
    customName: document.getElementById("custom-name"),
    ruleKind: document.getElementById("rule-kind"),
    ruleCategory: document.getElementById("rule-category"),
    categoryLabel: document.getElementById("category-label"),
    ruleValue: document.getElementById("rule-value"),
    addForm: document.getElementById("add-form"),
    addSubmit: document.getElementById("add-submit"),
    progressModal: document.getElementById("progress-modal"),
    progressTitle: document.getElementById("progress-title"),
    progressSummary: document.getElementById("progress-summary"),
    stepList: document.getElementById("step-list"),
    progressClose: document.getElementById("progress-close"),
};

// Cache of rule categories per kind so flipping between GEOSITE and GEOIP in
// the rule-kind dropdown doesn't refetch from the backend (which would in turn
// hit GitHub if the per-app cache had expired). Populated lazily on first use
// and on explicit user-triggered reload.
const ruleCategoriesCache = Object.create(null);
const CATEGORY_KINDS = new Set(["GEOSITE", "GEOIP"]);

function showError(node, message) {
    if (!message) {
        node.hidden = true;
        node.textContent = "";
        return;
    }
    node.hidden = false;
    node.textContent = message;
}

async function fetchJSON(url, opts) {
    const resp = await fetch(url, opts);
    let body = null;
    const text = await resp.text();
    if (text) {
        try {
            body = JSON.parse(text);
        } catch {
            body = { raw: text };
        }
    }
    if (!resp.ok) {
        const detail = (body && (body.error || body.detail)) || resp.statusText;
        const err = new Error(`${resp.status} ${detail}`);
        err.status = resp.status;
        err.body = body;
        throw err;
    }
    return body || {};
}

async function loadHealth() {
    els.health.dataset.state = "warn";
    els.healthLabel.textContent = "проверка…";
    try {
        const data = await fetchJSON("/api/health");
        if (data && data.ok) {
            els.health.dataset.state = "ok";
            const ident =
                (data.identity && (data.identity.name || data.identity.identity)) ||
                "MikroTik";
            els.healthLabel.textContent = `подключено: ${ident}`;
        } else {
            els.health.dataset.state = "error";
            els.healthLabel.textContent =
                (data && data.error) || "MikroTik недоступен";
        }
    } catch (exc) {
        els.health.dataset.state = "error";
        els.healthLabel.textContent = exc.message;
    }
}

async function loadCurrent() {
    showError(els.currentError, "");
    els.groupList.innerHTML = "";
    els.ruleEnvsList.innerHTML = "";
    els.groupListEmpty.hidden = true;
    els.ruleEnvsEmpty.hidden = true;
    try {
        const data = await fetchJSON("/api/groups/current");
        renderGroups(data.groups || []);
        renderRuleEnvs(data.rule_envs || []);
    } catch (exc) {
        showError(els.currentError, `Не удалось загрузить группы: ${exc.message}`);
    }
}

function renderGroups(groups) {
    els.groupList.innerHTML = "";
    if (!groups.length) {
        els.groupListEmpty.hidden = false;
        return;
    }
    els.groupListEmpty.hidden = true;
    for (const name of groups) {
        const li = document.createElement("li");
        const span = document.createElement("span");
        span.className = "name";
        span.textContent = name;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn-danger";
        btn.textContent = "Удалить";
        btn.addEventListener("click", () => onRemove(name));
        li.appendChild(span);
        li.appendChild(btn);
        els.groupList.appendChild(li);
    }
}

function renderRuleEnvs(rows) {
    els.ruleEnvsList.innerHTML = "";
    if (!rows.length) {
        els.ruleEnvsEmpty.hidden = false;
        return;
    }
    els.ruleEnvsEmpty.hidden = true;
    for (const row of rows) {
        const li = document.createElement("li");
        const k = document.createElement("span");
        k.className = "key";
        k.textContent = row.key;
        li.appendChild(k);
        li.append(` = ${row.value}`);
        els.ruleEnvsList.appendChild(li);
    }
}

function openModal(title) {
    els.progressTitle.textContent = title;
    els.progressSummary.textContent = "в процессе…";
    els.progressSummary.dataset.state = "running";
    els.stepList.innerHTML = "";
    els.progressClose.disabled = true;
    els.progressModal.hidden = false;
}

function closeModal() {
    els.progressModal.hidden = true;
}

function renderInitSteps(steps) {
    els.stepList.innerHTML = "";
    for (const step of steps) {
        const li = document.createElement("li");
        li.dataset.stepId = step.id;
        li.dataset.status = step.status;
        const icon = document.createElement("span");
        icon.className = "icon";
        icon.textContent = STATUS_ICON[step.status] || "•";
        const body = document.createElement("div");
        body.className = "body";
        const title = document.createElement("div");
        title.className = "title";
        title.textContent = step.title;
        const message = document.createElement("div");
        message.className = "message";
        message.textContent = step.message || "";
        body.appendChild(title);
        body.appendChild(message);
        li.appendChild(icon);
        li.appendChild(body);
        els.stepList.appendChild(li);
    }
}

function updateStep(step) {
    const li = els.stepList.querySelector(`li[data-step-id="${step.id}"]`);
    if (!li) {
        return;
    }
    li.dataset.status = step.status;
    li.querySelector(".icon").textContent = STATUS_ICON[step.status] || "•";
    li.querySelector(".message").textContent = step.message || "";
}

function finish(ok, failedStep) {
    if (ok) {
        els.progressSummary.dataset.state = "ok";
        els.progressSummary.textContent = "готово";
    } else {
        els.progressSummary.dataset.state = "error";
        els.progressSummary.textContent = failedStep
            ? `ошибка на шаге: ${failedStep}`
            : "ошибка";
    }
    els.progressClose.disabled = false;
}

async function streamSSE(url, body, onEvent) {
    const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) {
        const text = await resp.text().catch(() => "");
        let detail = text;
        try {
            const parsed = JSON.parse(text);
            detail = parsed.detail || parsed.error || text;
        } catch {
            // keep raw text
        }
        throw new Error(`${resp.status} ${detail || resp.statusText}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
        const { value, done } = await reader.read();
        if (done) {
            break;
        }
        buffer += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const dataLines = frame
                .split("\n")
                .filter((l) => l.startsWith("data:"))
                .map((l) => l.slice(5).trimStart());
            if (!dataLines.length) {
                continue;
            }
            const payload = dataLines.join("\n");
            try {
                onEvent(JSON.parse(payload));
            } catch (exc) {
                console.warn("bad SSE frame", payload, exc);
            }
        }
    }
}

async function runWorkflow(url, body, title) {
    openModal(title);
    let sawDone = false;
    try {
        await streamSSE(url, body, (event) => {
            if (event.type === "init") {
                renderInitSteps(event.steps || []);
            } else if (event.type === "step") {
                updateStep(event.step);
            } else if (event.type === "done") {
                sawDone = true;
                finish(Boolean(event.ok), event.failed_step);
            }
        });
        if (!sawDone) {
            finish(false, "stream ended without final event");
        }
    } catch (exc) {
        if (!els.stepList.children.length) {
            const li = document.createElement("li");
            li.dataset.status = "error";
            li.innerHTML =
                '<span class="icon">❌</span><div class="body">' +
                '<div class="title">Запрос не выполнен</div>' +
                '<div class="message"></div></div>';
            li.querySelector(".message").textContent = exc.message;
            els.stepList.appendChild(li);
        }
        finish(false, "request");
    } finally {
        await loadCurrent();
    }
}

async function loadRuleCategories(kind) {
    if (!CATEGORY_KINDS.has(kind)) {
        return [];
    }
    if (ruleCategoriesCache[kind]) {
        return ruleCategoriesCache[kind];
    }
    const data = await fetchJSON(
        `/api/rules/categories?kind=${encodeURIComponent(kind)}`,
    );
    const cats = Array.isArray(data.categories) ? data.categories : [];
    ruleCategoriesCache[kind] = cats;
    return cats;
}

function setCategoryPlaceholder(label) {
    els.ruleCategory.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = label;
    els.ruleCategory.appendChild(opt);
}

async function syncCategoryDropdown() {
    const kind = els.ruleKind.value;
    if (!CATEGORY_KINDS.has(kind)) {
        els.categoryLabel.hidden = true;
        els.ruleCategory.disabled = true;
        return;
    }
    els.categoryLabel.hidden = false;
    els.ruleCategory.disabled = true;
    const previous = els.ruleCategory.value;
    setCategoryPlaceholder("— загрузка… —");
    try {
        const cats = await loadRuleCategories(kind);
        setCategoryPlaceholder(`— выбрать категорию (${cats.length}) —`);
        for (const name of cats) {
            const opt = document.createElement("option");
            opt.value = name;
            opt.textContent = name;
            els.ruleCategory.appendChild(opt);
        }
        if (previous && cats.includes(previous)) {
            els.ruleCategory.value = previous;
        }
        els.ruleCategory.disabled = false;
    } catch (exc) {
        setCategoryPlaceholder("— ошибка загрузки —");
        showError(
            els.availableError,
            `Не удалось загрузить категории ${kind}: ${exc.message}.`,
        );
    }
}

async function onAdd(event) {
    event.preventDefault();
    const custom = els.customName.value.trim();
    const ruleKind = els.ruleKind.value;
    // Only honor the category dropdown when the current rule-kind actually
    // uses categories. Otherwise a leftover selection from a prior GEOSITE
    // pick would override the group-name fallback after switching to
    // DOMAIN/SUFFIX/KEYWORD (the dropdown is hidden but the value persists).
    const selectedCategory = CATEGORY_KINDS.has(ruleKind)
        ? (els.ruleCategory.value || "").trim()
        : "";
    const NAME_RE = /^[A-Za-z0-9_-]+$/;
    // meta-rules-dat ships categories like `category-ai-!cn` and `alibaba@!cn`
    // that contain `!` / `@` — perfectly valid as rule values, but they can't
    // double as a default group name (env-name lookup, YAML proxy-group key).
    // Only auto-default to the category when it makes a valid name; otherwise
    // require the operator to type one and keep the original as rule_value.
    const categoryUsableAsName =
        selectedCategory && NAME_RE.test(selectedCategory);
    const name = custom || (categoryUsableAsName ? selectedCategory : "");
    if (!name) {
        if (selectedCategory && !categoryUsableAsName) {
            showError(
                els.availableError,
                `Категория '${selectedCategory}' содержит спецсимволы и не ` +
                    "может быть именем группы — введите своё имя группы ниже.",
            );
        } else {
            showError(
                els.availableError,
                "Выберите категорию или введите имя группы.",
            );
        }
        return;
    }
    if (!NAME_RE.test(name)) {
        showError(
            els.availableError,
            "Имя группы может содержать только буквы, цифры, '_' и '-'.",
        );
        return;
    }
    showError(els.availableError, "");
    const explicitValue = els.ruleValue.value.trim();
    // Precedence: user-entered rule-value > picked category > group name. The
    // explicit input wins so a user can override the dropdown without having
    // to clear it first.
    const ruleValue = explicitValue || selectedCategory || name;
    if (CATEGORY_KINDS.has(ruleKind)) {
        const cats = ruleCategoriesCache[ruleKind];
        if (Array.isArray(cats) && cats.length) {
            // rule_value can be CSV with optional `!` negation prefix per token
            // (e.g. `category-ai-!cn,openai,google-gemini` shipped as the AI_GEOSITE
            // default in script21.rsc). Validate each token individually instead
            // of doing an exact-match on the whole string, which would always fail
            // for valid CSV/negated values and train operators to dismiss the dialog.
            const missing = ruleValue
                .split(",")
                .map((t) => t.trim().replace(/^!/, ""))
                .filter((t) => t.length > 0)
                .filter((t) => !cats.includes(t));
            if (missing.length) {
                const label = missing.length === 1
                    ? `Категория '${missing[0]}'`
                    : `Категории ${missing.map((t) => `'${t}'`).join(", ")}`;
                const ok = confirm(
                    `${label} не найдена в meta-rules-dat (${ruleKind}).\n` +
                    "Правило не сработает на стороне mihomo. Продолжить?",
                );
                if (!ok) {
                    return;
                }
            }
        }
    }
    els.addSubmit.disabled = true;
    try {
        await runWorkflow(
            "/api/groups/add",
            { name, rule_kind: ruleKind, rule_value: ruleValue },
            `Добавление группы '${name}'`,
        );
    } finally {
        els.addSubmit.disabled = false;
    }
}

async function onRemove(name) {
    if (!confirm(`Удалить группу '${name}'?`)) {
        return;
    }
    await runWorkflow(
        "/api/groups/remove",
        { name },
        `Удаление группы '${name}'`,
    );
}

function bind() {
    els.addForm.addEventListener("submit", onAdd);
    els.reloadCurrent.addEventListener("click", loadCurrent);
    els.ruleKind.addEventListener("change", syncCategoryDropdown);
    els.progressClose.addEventListener("click", closeModal);
}

bind();
loadHealth();
loadCurrent();
syncCategoryDropdown();
