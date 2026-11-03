const bridge = window.AstrBotPluginPage;

const state = {
  revision: 0,
  settings: null,
  deliveryStatus: [],
  alertOptions: [],
  levelOptions: [],
  provinces: [],
  provinceCities: {},
  sourceState: {},
  dirty: false,
};

const dom = {
  connectionState: document.getElementById("connection-state"),
  refreshButton: document.getElementById("refresh-button"),
  saveButton: document.getElementById("save-button"),
  pageError: document.getElementById("page-error"),
  serviceState: document.getElementById("service-state"),
  subscriptionCount: document.getElementById("subscription-count"),
  enabledCount: document.getElementById("enabled-count"),
  lastPoll: document.getElementById("last-poll"),
  lastSummary: document.getElementById("last-summary"),
  revisionLabel: document.getElementById("revision-label"),
  enabledInput: document.getElementById("enabled-input"),
  pollIntervalInput: document.getElementById("poll-interval-input"),
  contentLimitInput: document.getElementById("content-limit-input"),
  addRuleButton: document.getElementById("add-rule-button"),
  rulesContainer: document.getElementById("rules-container"),
  rulesSummary: document.getElementById("rules-summary"),
  rulesEmpty: document.getElementById("rules-empty"),
  sourceDetail: document.getElementById("source-detail"),
  sourceTestButton: document.getElementById("source-test-button"),
  sourceTestResult: document.getElementById("source-test-result"),
  previewAlertButton: document.getElementById("preview-alert-button"),
  previewForecastButton: document.getElementById("preview-forecast-button"),
  previewResult: document.getElementById("preview-result"),
  deliveryBody: document.getElementById("delivery-body"),
  deliverySummary: document.getElementById("delivery-summary"),
  confirmDialog: document.getElementById("confirm-dialog"),
  confirmTitle: document.getElementById("confirm-title"),
  confirmMessage: document.getElementById("confirm-message"),
  confirmAccept: document.getElementById("confirm-accept"),
  confirmCancel: document.getElementById("confirm-cancel"),
  toast: document.getElementById("toast"),
};

// --------------------------------------------------------------------------
// 工具
// --------------------------------------------------------------------------
function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function unwrapPayload(value) {
  if (value && value.status === "error") {
    throw new Error(String(value.message || "请求失败"));
  }
  if (value && value.status === "ok" && Object.hasOwn(value, "data")) {
    return value.data;
  }
  return value ?? {};
}

async function apiGet(endpoint, params = undefined) {
  return unwrapPayload(await bridge.apiGet(endpoint, params));
}

async function apiPost(endpoint, body = {}) {
  return unwrapPayload(await bridge.apiPost(endpoint, body));
}

function makeId() {
  return `rule-${Math.random().toString(36).slice(2, 10)}`;
}

function showError(message) {
  if (!message) {
    dom.pageError.hidden = true;
    dom.pageError.textContent = "";
    return;
  }
  dom.pageError.hidden = false;
  dom.pageError.textContent = message;
}

let toastTimer = null;
function toast(message) {
  dom.toast.hidden = false;
  dom.toast.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    dom.toast.hidden = true;
  }, 2600);
}

function setDirty(next) {
  state.dirty = next;
  dom.saveButton.textContent = next ? "保存设置（未保存）" : "保存设置";
  dom.saveButton.classList.toggle("button-primary", !next);
  dom.saveButton.classList.toggle("button-warning", next);
}

function confirmAction(title, message) {
  dom.confirmTitle.textContent = title;
  dom.confirmMessage.textContent = message;
  dom.confirmDialog.showModal();
  return new Promise((resolve) => {
    const done = (value) => {
      dom.confirmDialog.close();
      dom.confirmAccept.removeEventListener("click", onAccept);
      dom.confirmCancel.removeEventListener("click", onCancel);
      resolve(value);
    };
    const onAccept = () => done(true);
    const onCancel = () => done(false);
    dom.confirmAccept.addEventListener("click", onAccept);
    dom.confirmCancel.addEventListener("click", onCancel);
  });
}

function formatTime(value) {
  if (!value) {
    return "--";
  }
  const text = String(value);
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (match) {
    return `${match[2]}-${match[3]} ${match[4]}:${match[5]}`;
  }
  return text;
}

function sessionLabel(umo) {
  const parts = String(umo || "").split(":");
  if (parts.length < 3) {
    return String(umo || "未知会话");
  }
  const scene = parts[1] === "GroupMessage" ? "群聊" : "私聊";
  const tail = parts.slice(2).join(":");
  return `${scene} ${tail.length > 12 ? `${tail.slice(0, 6)}…${tail.slice(-4)}` : tail}`;
}

// --------------------------------------------------------------------------
// 渲染
// --------------------------------------------------------------------------
function normalizeSettings(raw) {
  const value = raw && typeof raw === "object" ? raw : {};
  return {
    revision: Number(value.revision || 0),
    enabled: value.enabled !== false,
    poll_interval_seconds: Number(value.poll_interval_seconds || 180),
    content_limit: Number(value.content_limit || 600),
    subscriptions: asArray(value.subscriptions).map((item) => ({
      id: String(item?.id || makeId()),
      target_umo: String(item?.target_umo || ""),
      enabled: item?.enabled !== false,
      cities: asArray(item?.cities).map(String),
      alert_types: asArray(item?.alert_types).map(String),
      levels: asArray(item?.levels).map(String),
      actions: asArray(item?.actions).map(String),
      forecast_enabled: item?.forecast_enabled !== false,
      forecast_times: asArray(item?.forecast_times).map(String),
      include_detail: item?.include_detail !== false,
    })),
  };
}

function applyState(payload) {
  state.revision = Number(payload?.revision || payload?.settings?.revision || 0);
  state.settings = normalizeSettings(payload?.settings);
  state.settings.revision = state.revision;
  state.deliveryStatus = asArray(payload?.delivery_status);
  state.provinces = asArray(payload?.provinces).map(String);
  state.provinceCities =
    payload?.province_cities && typeof payload.province_cities === "object"
      ? payload.province_cities
      : {};
  state.alertOptions =
    payload?.alert_types && typeof payload.alert_types === "object"
      ? Object.entries(payload.alert_types).map(([value, label]) => [
          String(value),
          String(label),
        ])
      : [];
  state.levelOptions =
    payload?.levels && typeof payload.levels === "object"
      ? Object.entries(payload.levels).map(([value, label]) => [
          String(value),
          String(label),
        ])
      : [];
  state.sourceState = {
    started_at: payload?.started_at || "",
    last_alert_poll_at: payload?.last_alert_poll_at || "",
    last_alert_count: Number(payload?.last_alert_count || 0),
    last_loop_error: payload?.last_loop_error || "",
  };

  dom.enabledInput.checked = state.settings.enabled;
  dom.pollIntervalInput.value = String(state.settings.poll_interval_seconds);
  dom.contentLimitInput.value = String(state.settings.content_limit);

  renderStatus();
  renderSourceDetail();
  renderRules();
  renderDelivery();
  setDirty(false);
}

function renderStatus() {
  const subscriptions = state.settings?.subscriptions || [];
  const enabled = subscriptions.filter((item) => item.enabled).length;
  dom.serviceState.textContent = state.settings?.enabled ? "运行中" : "已停用";
  dom.subscriptionCount.textContent = String(subscriptions.length);
  dom.enabledCount.textContent = String(enabled);
  dom.lastPoll.textContent = formatTime(state.sourceState.last_alert_poll_at);
  dom.revisionLabel.textContent = `版本 ${state.revision}`;

  const count = state.sourceState.last_alert_count;
  const error = state.sourceState.last_loop_error;
  dom.lastSummary.textContent = error
    ? `错误：${error}`
    : `${count} 条有效预警`;
  dom.lastSummary.title = error || "";
}

function renderSourceDetail() {
  const rows = [
    ["数据来源", "中央气象台 nmc.cn"],
    ["服务启动", formatTime(state.sourceState.started_at)],
    ["最近轮询", formatTime(state.sourceState.last_alert_poll_at)],
    ["预警条数", String(state.sourceState.last_alert_count ?? 0)],
    ["最近错误", state.sourceState.last_loop_error || "无"],
  ];
  dom.sourceDetail.replaceChildren(
    ...rows.map(([key, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = key;
      const dd = document.createElement("dd");
      dd.textContent = value;
      dd.title = value;
      return Object.assign(document.createElement("div"), {
        className: "detail-row",
        children: [dt, dd],
      });
    }),
  );
}

function buildCitySelect(subscription, index) {
  const wrapper = document.createElement("div");
  wrapper.className = "city-picker";

  const tags = document.createElement("div");
  tags.className = "tag-list";
  const renderTags = () => {
    tags.replaceChildren(
      ...subscription.cities.map((city) => {
        const tag = document.createElement("span");
        tag.className = "tag";
        const label = document.createElement("span");
        label.textContent = city;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "tag-remove";
        remove.textContent = "×";
        remove.title = `移除 ${city}`;
        remove.addEventListener("click", () => {
          subscription.cities = subscription.cities.filter((item) => item !== city);
          renderTags();
          setDirty(true);
          refreshEmptyHint();
        });
        tag.append(label, remove);
        return tag;
      }),
    );
    if (!subscription.cities.length) {
      const hint = document.createElement("span");
      hint.className = "metadata-text";
      hint.textContent = "尚未选择城市";
      tags.append(hint);
    }
  };
  renderTags();

  const provinceSelect = document.createElement("select");
  provinceSelect.className = "city-province";
  const citySelect = document.createElement("select");
  citySelect.className = "city-city";
  const addButton = document.createElement("button");
  addButton.type = "button";
  addButton.className = "button button-secondary button-small";
  addButton.textContent = "添加";

  const fillCities = () => {
    const cities = asArray(state.provinceCities[provinceSelect.value]);
    citySelect.replaceChildren(
      ...cities.map((city) => {
        const option = document.createElement("option");
        option.value = city;
        option.textContent = city;
        return option;
      }),
    );
    citySelect.disabled = !cities.length;
  };

  provinceSelect.replaceChildren(
    ...state.provinces.map((province) => {
      const option = document.createElement("option");
      option.value = province;
      option.textContent = province;
      return option;
    }),
  );
  const preferred =
    state.provinces.find((province) =>
      asArray(state.provinceCities[province]).includes(subscription.cities[0]),
    ) || state.provinces[0];
  if (preferred) {
    provinceSelect.value = preferred;
  }
  provinceSelect.addEventListener("change", fillCities);
  fillCities();

  addButton.addEventListener("click", () => {
    const city = citySelect.value;
    if (!city || subscription.cities.includes(city)) {
      return;
    }
    subscription.cities = [...subscription.cities, city];
    renderTags();
    setDirty(true);
    refreshEmptyHint();
  });

  wrapper.append(tags, Object.assign(document.createElement("div"), {
    className: "city-add",
    children: [provinceSelect, citySelect, addButton],
  }));
  wrapper.dataset.ruleIndex = String(index);
  return wrapper;
}

function buildCheckboxGroup(options, selected, onChange, className) {
  const wrapper = document.createElement("div");
  wrapper.className = className || "checkbox-group";
  const set = new Set(selected);
  options.forEach(([value, label]) => {
    const labelEl = document.createElement("label");
    labelEl.className = "checkbox-item";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value;
    input.checked = set.has(value);
    input.addEventListener("change", () => {
      const next = new Set(selected);
      if (input.checked) {
        next.add(value);
      } else {
        next.delete(value);
      }
      selected.length = 0;
      selected.push(...next);
      onChange?.();
      setDirty(true);
    });
    const span = document.createElement("span");
    span.textContent = label;
    labelEl.append(input, span);
    wrapper.append(labelEl);
  });
  if (!options.length) {
    const hint = document.createElement("span");
    hint.className = "metadata-text";
    hint.textContent = "无可选项";
    wrapper.append(hint);
  }
  return wrapper;
}

function buildTimeInputs(subscription) {
  const wrapper = document.createElement("div");
  wrapper.className = "time-list";
  const render = () => {
    wrapper.replaceChildren(
      ...subscription.forecast_times.map((time, index) => {
        const row = document.createElement("div");
        row.className = "time-row";
        const input = document.createElement("input");
        input.type = "time";
        input.value = time;
        input.addEventListener("change", () => {
          const value = input.value;
          if (!value) {
            return;
          }
          subscription.forecast_times[index] = value;
          subscription.forecast_times.sort();
          setDirty(true);
        });
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "button button-secondary button-small";
        remove.textContent = "删除";
        remove.addEventListener("click", () => {
          subscription.forecast_times.splice(index, 1);
          render();
          setDirty(true);
        });
        row.append(input, remove);
        return row;
      }),
    );
    if (subscription.forecast_times.length < 4) {
      const add = document.createElement("button");
      add.type = "button";
      add.className = "button button-secondary button-small";
      add.textContent = "增加时间";
      add.addEventListener("click", () => {
        subscription.forecast_times.push("07:30");
        subscription.forecast_times.sort();
        render();
        setDirty(true);
      });
      wrapper.append(add);
    }
  };
  render();
  return wrapper;
}

function renderRules() {
  const subscriptions = state.settings?.subscriptions || [];
  dom.rulesSummary.textContent = `${subscriptions.length} 条`;
  dom.rulesContainer.replaceChildren();

  subscriptions.forEach((subscription, index) => {
    const item = document.createElement("article");
    item.className = "rule-item";

    const header = document.createElement("div");
    header.className = "rule-header";

    const indexEl = document.createElement("span");
    indexEl.className = "rule-index";
    indexEl.textContent = `#${index + 1}`;

    const targetField = document.createElement("label");
    targetField.className = "field";
    const targetLabel = document.createElement("span");
    targetLabel.textContent = "目标会话（UMO）";
    const targetInput = document.createElement("input");
    targetInput.type = "text";
    targetInput.value = subscription.target_umo;
    targetInput.placeholder = "default_xxx:GroupMessage:xxxx";
    targetInput.addEventListener("input", () => {
      subscription.target_umo = targetInput.value.trim();
      setDirty(true);
    });
    targetField.append(targetLabel, targetInput);

    const enabledField = document.createElement("label");
    enabledField.className = "field field-inline";
    const enabledLabel = document.createElement("span");
    enabledLabel.textContent = "启用";
    const enabledInput = document.createElement("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = subscription.enabled;
    enabledInput.addEventListener("change", () => {
      subscription.enabled = enabledInput.checked;
      setDirty(true);
      renderStatus();
    });
    enabledField.append(enabledLabel, enabledInput);

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "button button-quiet-danger button-small";
    deleteButton.textContent = "删除规则";
    deleteButton.addEventListener("click", async () => {
      const ok = await confirmAction(
        "删除订阅规则",
        `确认删除 ${sessionLabel(subscription.target_umo)} 的订阅规则？`,
      );
      if (!ok) {
        return;
      }
      state.settings.subscriptions = state.settings.subscriptions.filter(
        (entry) => entry.id !== subscription.id,
      );
      setDirty(true);
      renderRules();
      renderStatus();
      refreshEmptyHint();
    });

    header.append(indexEl, targetField, enabledField, deleteButton);

    const body = document.createElement("div");
    body.className = "rule-body";

    const citySection = document.createElement("div");
    citySection.className = "rule-section";
    const cityTitle = document.createElement("h3");
    cityTitle.textContent = "播报城市";
    citySection.append(cityTitle, buildCitySelect(subscription, index));

    const alertSection = document.createElement("div");
    alertSection.className = "rule-section";
    const alertTitle = document.createElement("h3");
    alertTitle.textContent = "预警类型";
    alertSection.append(
      alertTitle,
      buildCheckboxGroup(state.alertOptions, subscription.alert_types),
    );

    const levelSection = document.createElement("div");
    levelSection.className = "rule-section";
    const levelTitle = document.createElement("h3");
    levelTitle.textContent = "预警等级";
    levelSection.append(
      levelTitle,
      buildCheckboxGroup(state.levelOptions, subscription.levels),
    );

    const forecastSection = document.createElement("div");
    forecastSection.className = "rule-section";
    const forecastTitle = document.createElement("h3");
    forecastTitle.textContent = "天气播报";
    const forecastToggle = document.createElement("label");
    forecastToggle.className = "field field-inline";
    const forecastLabel = document.createElement("span");
    forecastLabel.textContent = "每日定时播报天气";
    const forecastInput = document.createElement("input");
    forecastInput.type = "checkbox";
    forecastInput.checked = subscription.forecast_enabled;
    forecastInput.addEventListener("change", () => {
      subscription.forecast_enabled = forecastInput.checked;
      setDirty(true);
    });
    forecastToggle.append(forecastLabel, forecastInput);
    const timeLabel = document.createElement("span");
    timeLabel.className = "metadata-text";
    timeLabel.textContent = "播报时间（最多 4 个）";
    forecastSection.append(forecastTitle, forecastToggle, timeLabel, buildTimeInputs(subscription));

    const detailSection = document.createElement("div");
    detailSection.className = "rule-section";
    const detailToggle = document.createElement("label");
    detailToggle.className = "field field-inline";
    const detailLabel = document.createElement("span");
    detailLabel.textContent = "附带预警正文与防御指南";
    const detailInput = document.createElement("input");
    detailInput.type = "checkbox";
    detailInput.checked = subscription.include_detail;
    detailInput.addEventListener("change", () => {
      subscription.include_detail = detailInput.checked;
      setDirty(true);
    });
    detailToggle.append(detailLabel, detailInput);
    detailSection.append(detailToggle);

    body.append(citySection, alertSection, levelSection, forecastSection, detailSection);
    item.append(header, body);
    dom.rulesContainer.append(item);
  });

  refreshEmptyHint();
}

function refreshEmptyHint() {
  const count = state.settings?.subscriptions?.length || 0;
  dom.rulesEmpty.hidden = count > 0;
}

function renderDelivery() {
  const records = state.deliveryStatus || [];
  dom.deliverySummary.textContent = records.length
    ? `${records.length} 条记录`
    : "暂无记录";
  dom.deliveryBody.replaceChildren(
    ...records.map((record) => {
      const tr = document.createElement("tr");
      const status = String(record.last_status || "");
      const cells = [
        formatTime(record.last_attempt_at),
        sessionLabel(record.target_umo),
        record.kind === "forecast" ? "天气播报" : record.kind === "test" ? "测试" : "预警",
        status === "ok" ? "成功" : "失败",
        String(record.last_error || ""),
      ];
      cells.forEach((value, cellIndex) => {
        const td = document.createElement("td");
        td.textContent = value;
        td.title = value;
        if (cellIndex === 3) {
          const pill = document.createElement("span");
          pill.className = `status-pill ${status === "ok" ? "status-success" : "status-danger"}`;
          pill.textContent = value;
          td.replaceChildren(pill);
        }
        tr.append(td);
      });
      return tr;
    }),
  );
  if (!records.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "empty-cell";
    td.textContent = "还没有投递记录。";
    tr.append(td);
    dom.deliveryBody.append(tr);
  }
}

// --------------------------------------------------------------------------
// 动作
// --------------------------------------------------------------------------
function collectSettings() {
  return {
    revision: state.revision,
    enabled: dom.enabledInput.checked,
    poll_interval_seconds: Number(dom.pollIntervalInput.value || 180),
    content_limit: Number(dom.contentLimitInput.value || 600),
    subscriptions: (state.settings?.subscriptions || []).map((item) => ({
      id: item.id,
      target_umo: item.target_umo,
      enabled: item.enabled,
      cities: item.cities,
      alert_types: item.alert_types,
      levels: item.levels,
      actions: item.actions,
      forecast_enabled: item.forecast_enabled,
      forecast_times: item.forecast_times,
      include_detail: item.include_detail,
    })),
  };
}

async function loadState() {
  try {
    dom.connectionState.textContent = "加载中";
    dom.connectionState.className = "status-pill status-neutral";
    const payload = await apiGet("state");
    applyState(payload);
    dom.connectionState.textContent = "已连接";
    dom.connectionState.className = "status-pill status-success";
    showError("");
  } catch (error) {
    dom.connectionState.textContent = "连接失败";
    dom.connectionState.className = "status-pill status-danger";
    showError(error?.message || String(error));
  }
}

async function saveSettings() {
  try {
    dom.saveButton.disabled = true;
    const payload = collectSettings();
    const result = await apiPost("settings", payload);
    state.revision = Number(result?.revision || state.revision + 1);
    toast(result?.message || "设置已保存。");
    setDirty(false);
    await loadState();
  } catch (error) {
    showError(error?.message || String(error));
    toast("保存失败");
  } finally {
    dom.saveButton.disabled = false;
  }
}

async function testSource() {
  dom.sourceTestButton.disabled = true;
  dom.sourceTestResult.hidden = false;
  dom.sourceTestResult.textContent = "正在测试…";
  try {
    const result = await apiPost("source/test", {});
    dom.sourceTestResult.textContent = result?.ok
      ? `接口可用：全国预警 ${result.alarm_count} 条，示例城市 ${result.weather_city || "--"}，耗时 ${result.latency_ms} ms。`
      : `接口异常：${result?.message || "未知错误"}`;
    dom.sourceTestResult.className = `inline-result ${result?.ok ? "is-ok" : "is-error"}`;
  } catch (error) {
    dom.sourceTestResult.textContent = `接口异常：${error?.message || error}`;
    dom.sourceTestResult.className = "inline-result is-error";
  } finally {
    dom.sourceTestButton.disabled = false;
  }
}

async function preview(kind) {
  dom.previewResult.hidden = false;
  dom.previewResult.textContent = "正在生成预览…";
  try {
    const subscription = state.settings?.subscriptions?.find((item) => item.enabled);
    const body = { kind };
    if (kind === "forecast") {
      body.cities = subscription?.cities || [];
      body.target_umo = subscription?.target_umo || "";
    }
    const result = await apiPost("preview", body);
    dom.previewResult.textContent = result?.text || result?.message || "没有内容。";
    dom.previewResult.className = `inline-result ${result?.ok ? "is-ok" : "is-error"}`;
  } catch (error) {
    dom.previewResult.textContent = `预览失败：${error?.message || error}`;
    dom.previewResult.className = "inline-result is-error";
  }
}

function addRule() {
  const subscription = {
    id: makeId(),
    target_umo: "",
    enabled: true,
    cities: [],
    alert_types: state.alertOptions.map(([value]) => value),
    levels: state.levelOptions.map(([value]) => value),
    actions: ["issue", "change", "cancel"],
    forecast_enabled: true,
    forecast_times: ["07:30", "19:00"],
    include_detail: true,
  };
  state.settings.subscriptions = [...(state.settings?.subscriptions || []), subscription];
  setDirty(true);
  renderRules();
  renderStatus();
}

function bindGlobalInputs() {
  dom.enabledInput.addEventListener("change", () => {
    setDirty(true);
    renderStatus();
  });
  dom.pollIntervalInput.addEventListener("input", () => setDirty(true));
  dom.contentLimitInput.addEventListener("input", () => setDirty(true));
}

function bindEvents() {
  dom.refreshButton.addEventListener("click", loadState);
  dom.saveButton.addEventListener("click", saveSettings);
  dom.addRuleButton.addEventListener("click", addRule);
  dom.sourceTestButton.addEventListener("click", testSource);
  dom.previewAlertButton.addEventListener("click", () => preview("alerts"));
  dom.previewForecastButton.addEventListener("click", () => preview("forecast"));
  bindGlobalInputs();
}

async function boot() {
  bindEvents();
  try {
    await bridge.ready();
  } catch (error) {
    showError(`桥接初始化失败：${error?.message || error}`);
  }
  await loadState();
  setInterval(async () => {
    if (state.dirty) {
      return;
    }
    try {
      const payload = await apiGet("state");
      const revision = Number(payload?.revision || 0);
      if (revision !== state.revision) {
        applyState(payload);
      } else {
        state.deliveryStatus = asArray(payload?.delivery_status);
        state.sourceState = {
          started_at: payload?.started_at || "",
          last_alert_poll_at: payload?.last_alert_poll_at || "",
          last_alert_count: Number(payload?.last_alert_count || 0),
          last_loop_error: payload?.last_loop_error || "",
        };
        renderStatus();
        renderSourceDetail();
        renderDelivery();
      }
    } catch (error) {
      // 轮询失败保持静默，避免打断正在编辑的用户
      console.debug("state refresh failed", error);
    }
  }, 20000);
}

boot();
