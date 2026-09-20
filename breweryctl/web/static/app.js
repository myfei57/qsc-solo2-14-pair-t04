const STATE = {
  batches: [],
  tanks: [],
};

async function apiGet(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  return parseResponse(response);
}

async function apiPost(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  return parseResponse(response);
}

async function parseResponse(response) {
  let body;
  try {
    body = await response.json();
  } catch (error) {
    body = { error: "invalid_response", message: String(error) };
  }
  if (!response.ok) {
    const message = body && body.message ? body.message : response.statusText;
    throw new Error(body.error + ": " + message);
  }
  return body;
}

function element(id) {
  return document.getElementById(id);
}

function write(id, value) {
  const target = element(id);
  if (target) {
    target.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }
}

function addLog(id, message, payload) {
  const target = element(id);
  if (!target) {
    return;
  }
  const entry = document.createElement("div");
  entry.className = "log-entry";
  const stamp = new Date().toLocaleTimeString();
  entry.textContent = "[" + stamp + "] " + message + (payload ? " " + JSON.stringify(payload) : "");
  target.prepend(entry);
}

function value(id) {
  const target = element(id);
  return target ? target.value.trim() : "";
}

function numberValue(id) {
  return Number(value(id));
}

async function run(action, id, onSuccess) {
  try {
    const payload = await action();
    if (onSuccess) {
      onSuccess(payload);
    }
    return payload;
  } catch (error) {
    addLog(id, "失败：" + error.message);
    write(id + "-result", { error: error.message });
    return null;
  }
}

async function loadBanner() {
  const overview = await apiGet("/api/state");
  const banner = overview.banner;
  write("banner-batches", banner.active_batches);
  write("banner-tanks", banner.busy_tanks);
  write("banner-alarms", banner.active_alarms);
  write("banner-latches", banner.latching_alarms);
  write("banner-store", overview.store.collections.batches || 0);
  return overview;
}

async function loadBatches(selectId) {
  const payload = await apiGet("/api/batches");
  STATE.batches = payload.batches;
  const select = element(selectId);
  if (select) {
    select.innerHTML = "";
    STATE.batches.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.code + " · " + item.stage;
      select.appendChild(option);
    });
  }
  return STATE.batches;
}

async function loadTanks(selectId) {
  const payload = await apiGet("/api/control/tanks");
  STATE.tanks = payload.tanks;
  const select = element(selectId);
  if (select) {
    select.innerHTML = "";
    STATE.tanks.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.code + " · " + item.stage;
      select.appendChild(option);
    });
  }
  return STATE.tanks;
}

async function refreshBatch() {
  const batchId = value("batch-select");
  if (!batchId) {
    return;
  }
  const view = await apiGet("/api/batches/" + batchId);
  write("batch-view", view);
  return view;
}

async function refreshTankSnapshot() {
  const tankId = value("tank-select");
  if (!tankId) {
    return;
  }
  const snapshot = await apiGet("/api/control/tanks/" + tankId);
  write("tank-view", snapshot);
  return snapshot;
}

async function initMashPage() {
  const overview = await loadBanner();
  write("store-view", overview.store);
  await loadBatches("batch-select");
  await refreshBatch();
  const recipes = await apiGet("/api/recipes");
  const published = recipes.recipes.filter((item) => item.status === "published");
  const select = element("recipe-select");
  published.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name + " · v" + item.version;
    select.appendChild(option);
  });
  write("recipe-view", published);
}

async function initFermentPage() {
  await loadBanner();
  await loadBatches("batch-select");
  await loadTanks("tank-select");
  await refreshBatch();
  await refreshTankSnapshot();
}

async function initCipPage() {
  await loadBanner();
  await loadTanks("tank-select");
  await refreshCertificate();
}

async function initAlarmsPage() {
  await loadBanner();
  await loadAlarms();
  await loadBatches("audit-batch-select");
}

async function loadAlarms() {
  const status = value("alarm-status") || "active";
  const payload = await apiGet("/api/alarms?status=" + encodeURIComponent(status));
  const tbody = element("alarm-rows");
  tbody.innerHTML = "";
  payload.alarms.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML =
      "<td>" +
      item.severity +
      "</td><td>" +
      item.source +
      "</td><td>" +
      item.message +
      "</td><td>" +
      item.status +
      "</td><td>" +
      (item.latching ? "是" : "否") +
      "</td>";
    const actions = document.createElement("td");
    const ack = document.createElement("button");
    ack.textContent = "确认";
    ack.onclick = () =>
      run(
        () => apiPost("/api/alarms/" + item.id + "/ack", { operator: value("operator") || "console" }),
        "alarm-log",
        loadAlarms
      );
    const resolve = document.createElement("button");
    resolve.textContent = "解除";
    resolve.className = "secondary";
    resolve.onclick = () =>
      run(
        () =>
          apiPost("/api/alarms/" + item.id + "/resolve", {
            operator: value("operator") || "console",
            note: "控制台手动解除",
          }),
        "alarm-log",
        loadAlarms
      );
    actions.appendChild(ack);
    actions.appendChild(resolve);
    row.appendChild(actions);
    tbody.appendChild(row);
  });
  write("alarm-summary", payload.summary);
  return payload;
}

async function refreshCertificate() {
  const tankId = value("tank-select");
  if (!tankId) {
    return;
  }
  const report = await apiGet("/api/maintenance/tanks/" + tankId + "/certificate");
  write("certificate-view", report);
  return report;
}

async function loadAudit() {
  const batchId = value("audit-batch-select");
  const payload = await apiGet("/api/audit?batch_id=" + encodeURIComponent(batchId));
  write("audit-view", payload);
  return payload;
}

async function submitReading() {
  const batchId = value("ferment-batch-select") || value("batch-select");
  const payload = await apiPost("/api/telemetry/readings", {
    probe_id: value("probe-select"),
    value_c: numberValue("reading-value"),
    batch_id: batchId || null,
    actor: value("operator") || "console",
  });
  write("reading-result", payload);
  await loadTrend();
  return payload;
}

async function loadTrend() {
  const batchId = value("batch-select") || value("ferment-batch-select");
  if (!batchId) {
    return null;
  }
  const trend = await apiGet("/api/telemetry/batches/" + batchId + "/trend");
  write("trend-view", trend);
  return trend;
}

document.addEventListener("DOMContentLoaded", () => {
  const operator = element("operator");
  if (operator && !operator.value) {
    operator.value = "console";
  }
});

const STEP_LABELS = {
  fuel_cut: "切断燃料",
  burner_stop: "停燃烧器",
  feedwater_close: "关给水阀",
  feedwater_open: "开给水阀",
  blowdown_open: "开排污/泄压",
  blowdown_close: "关排污阀",
  steam_isolate: "关主蒸汽阀",
  steam_restore: "恢复主蒸汽阀",
  burner_load: "降负荷监视",
  tag: "现场挂牌确认",
  inspect: "查明原因后复位",
};

async function initSteamPage() {
  await loadBanner();
  await loadOverview();
  await loadConsumers();
  await loadBoilers();
  await loadCases();
}

async function loadOverview() {
  const overview = await apiGet("/api/steam/overview");
  write("overview-view", overview);
  return overview;
}

async function afterDispatch() {
  await loadOverview();
  await loadBoilers();
  await loadCases();
}

async function loadConsumers() {
  const payload = await apiGet("/api/steam/consumers");
  const tbody = element("consumer-rows");
  tbody.innerHTML = "";
  payload.consumers.forEach((item) => {
    const row = document.createElement("tr");
    const status = item.active ? "在用" : "空闲";
    row.innerHTML =
      "<td>" + item.code + "</td><td>" + item.name + "</td><td>" + item.demand_kgh +
      "</td><td>" + status + "</td><td>" + (item.batch_id || "-") + "</td>";
    const actions = document.createElement("td");
    if (item.active) {
      const btn = document.createElement("button");
      btn.textContent = "退汽";
      btn.className = "secondary";
      btn.onclick = () =>
        run(() => apiPost("/api/steam/consumers/" + item.id + "/release", { actor: value("operator") || "console" }),
          "consumer-log", async () => { await loadConsumers(); await loadOverview(); });
      actions.appendChild(btn);
    } else {
      const btn = document.createElement("button");
      btn.textContent = "投用";
      btn.onclick = () =>
        run(() => apiPost("/api/steam/consumers/" + item.id + "/claim", {
          actor: value("operator") || "console",
          batch_id: value("demand-batch") || null,
        }), "consumer-log", async () => { await loadConsumers(); await loadOverview(); });
      actions.appendChild(btn);
    }
    row.appendChild(actions);
    tbody.appendChild(row);
  });
  return payload;
}

function boolText(v) {
  return v ? "是" : "否";
}

async function loadBoilers() {
  const payload = await apiGet("/api/steam/boilers");
  const tbody = element("boiler-rows");
  tbody.innerHTML = "";
  const select = element("reading-boiler");
  if (select) {
    select.innerHTML = "";
  }
  payload.boilers.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML =
      "<td>" + item.code + "</td><td>" + item.rating_kgh + "</td><td>" + item.priority +
      "</td><td>" + item.state + "</td><td>" + (item.water_pct == null ? "-" : item.water_pct) +
      "</td><td>" + (item.steam_bar == null ? "-" : item.steam_bar) +
      "</td><td>" + boolText(item.burner_on) + "</td><td>" + boolText(item.feedwater_open) +
      "</td><td>" + boolText(item.blowdown_open) + "</td><td>" + (item.steam_isolated ? "已隔离" : "连通") +
      "</td><td>" + boolText(item.tagged) + "</td>";
    const actions = document.createElement("td");
    const start = document.createElement("button");
    start.textContent = "点火";
    start.onclick = () =>
      run(() => apiPost("/api/steam/boilers/" + item.id + "/start", { operator: value("operator") || "console" }),
        "boiler-log", afterReading);
    const stop = document.createElement("button");
    stop.textContent = "停炉";
    stop.className = "secondary";
    stop.onclick = () =>
      run(() => apiPost("/api/steam/boilers/" + item.id + "/stop", { operator: value("operator") || "console" }),
        "boiler-log", afterReading);
    actions.appendChild(start);
    actions.appendChild(stop);
    row.appendChild(actions);
    tbody.appendChild(row);
    if (select) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.code;
      select.appendChild(option);
    }
  });
  return payload;
}

async function submitBoilerReading() {
  const boilerId = value("reading-boiler");
  if (!boilerId) {
    throw new Error("请选择锅炉");
  }
  const waterRaw = value("reading-water");
  const steamRaw = value("reading-steam");
  const payload = { operator: value("operator") || "console" };
  if (waterRaw !== "") payload.water_pct = Number(waterRaw);
  if (steamRaw !== "") payload.steam_bar = Number(steamRaw);
  const result = await apiPost("/api/steam/boilers/" + boilerId + "/readings", payload);
  if (result.interlock) {
    addLog("boiler-log", "越线处置单 " + result.interlock.state + "：" + result.interlock.reason);
  }
  return result;
}

async function afterReading() {
  await loadOverview();
  await loadBoilers();
  await loadCases();
}

const CASE_KIND_LABELS = {
  low_water: "低水位",
  low_low_water: "低低水位",
  high_water: "高水位",
  high_high_water: "高高水位",
  steam_high: "汽压高高压",
};

async function loadCases() {
  const payload = await apiGet("/api/steam/cases");
  const tbody = element("case-rows");
  tbody.innerHTML = "";
  payload.cases.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML =
      "<td>" + item.id + "</td><td>" + item.boiler_id.slice(-6) + "</td><td>" +
      (CASE_KIND_LABELS[item.kind] || item.kind) + "</td><td>" + boolText(item.tripping) +
      "</td><td>" + item.trigger_value + " / " + item.threshold +
      "</td><td>" + item.state + "</td><td>" + item.raised_at + "</td>";
    const actions = document.createElement("td");
    const view = document.createElement("button");
    view.textContent = "查看";
    view.className = "secondary";
    view.onclick = () => run(() => viewCase(item.id), "case-log");
    actions.appendChild(view);
    const pendingManual = (item.steps || []).filter((s) => !s.automatic && s.status === "pending");
    if (item.state === "locked" && pendingManual.length > 0) {
      const confirm = document.createElement("button");
      confirm.textContent = "确认人工步";
      confirm.onclick = () =>
        run(() => apiPost("/api/steam/cases/" + item.id + "/steps/" + pendingManual[0].key,
          { operator: value("operator") || "console" }), "case-log", async () => { await loadCases(); await viewCase(item.id); });
      actions.appendChild(confirm);
    }
    if (item.state === "locked" && pendingManual.length === 0) {
      const reset = document.createElement("button");
      reset.textContent = "复位摘牌";
      reset.onclick = () =>
        run(() => apiPost("/api/steam/cases/" + item.id + "/reset",
          { operator: value("operator") || "console" }), "case-log", async () => { await loadCases(); await loadBoilers(); await loadOverview(); });
      actions.appendChild(reset);
    }
    row.appendChild(actions);
    tbody.appendChild(row);
  });
  return payload;
}

async function viewCase(caseId) {
  const payload = await apiGet("/api/steam/cases/" + caseId);
  const item = payload.case;
  const lines = [
    "处置单 " + item.id + "（" + (CASE_KIND_LABELS[item.kind] || item.kind) + "） " + item.state,
    "原因：" + item.reason,
    "触发值 " + item.trigger_value + " / 阈值 " + item.threshold + "，跳闸=" + boolText(item.tripping),
    "步骤：",
  ];
  (item.steps || []).forEach((s) => {
    lines.push(
      "  " + s.order + ". [" + (s.automatic ? "自动" : "人工") + "] " +
      (STEP_LABELS[s.key] || s.label) + " — " + s.status +
      (s.actor ? "（" + s.actor + "）" : "")
    );
  });
  write("case-view", lines.join("\n"));
  return payload;
}

