import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);
const chart = echarts.init(document.querySelector("#chart"));
const colors = { community: "#37352f", aiak: "#a7a5a0" };
const names = { community: "Community SGLang", aiak: "AIAK SGLang" };
let report = { schema_version: 1, points: [], series: [], failures: [] };
let phase = "P";
let visiblePoints = [];
let launchConfig = {};
const controls = Object.fromEntries(["model", "context", "profile", "comparison", "run"].map(id => [id, document.getElementById(id)]));
const format = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

function fillOptions(select, values, labels = values) {
  const previous = select.value;
  select.replaceChildren(...values.map((value, index) => new Option(labels[index], value)));
  if (values.includes(previous)) select.value = previous;
}

function refreshFilters() {
  const points = report.points.filter(point => point.phase === phase && point.profile === controls.profile.value)
    .sort((a, b) => b.run_id.localeCompare(a.run_id));
  fillOptions(controls.model, [...new Set(points.map(point => point.model))]);
  const contexts = [...new Set(points.filter(point => point.model === controls.model.value).map(point => String(point.input_length)))];
  fillOptions(controls.context, contexts, contexts.map(value => `${format.format(Number(value))} tokens`));
  const groups = [...new Set(points.filter(point =>
    point.model === controls.model.value && String(point.input_length) === controls.context.value &&
    point.profile === controls.profile.value
  ).map(point => point.comparison_key))];
  fillOptions(controls.comparison, groups, groups.map(value => {
    const sample = points.find(point => point.comparison_key === value);
    return `${sample.precision.toUpperCase()} · ${sample.gpu_count} GPU · ${sample.model_revision}`;
  }));
  const runIds = [...new Set(points.filter(point => point.comparison_key === controls.comparison.value).map(point => point.run_id))].sort().reverse();
  fillOptions(controls.run, runIds, runIds.map(value => {
    const sample = points.find(point => point.run_id === value);
    return new Date(sample.timestamp).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }));
  render();
}

function render() {
  document.querySelectorAll("[data-phase]").forEach(button => button.classList.toggle("selected", button.dataset.phase === phase));
  const latency = phase === "D";
  const metricUnit = latency ? "ms" : "tok/s";
  document.querySelector("#chart-title").textContent = latency ? "平均 TPOT (ms) · 越低越好" : "输入吞吐 (tok/s)";
  document.querySelector("#throughput-heading").textContent = latency ? "端到端输出 (tok/s)" : "输入吞吐 (tok/s)";
  document.querySelector("#metrics").hidden = controls.profile.value === "smoke";
  visiblePoints = report.points.filter(point => point.phase === phase &&
    point.model === controls.model.value && String(point.input_length) === controls.context.value &&
    point.profile === controls.profile.value && point.comparison_key === controls.comparison.value &&
    point.run_id === controls.run.value);
  const sample = visiblePoints[0];
  document.querySelector("#strategy-mode").textContent = sample?.strategy === "high-throughput" ? "高吞吐" : sample?.strategy === "low-latency" ? "低延迟" : "—";
  document.querySelector("#kv-mode").textContent = sample ? (sample.fake_kv ? "合成" : "真实") : "—";
  document.querySelector("#speculation-mode").textContent = sample ? (sample.speculative_algorithm ? `${sample.speculative_algorithm} · ${sample.speculative_block_size}` : "关闭") : "—";
  document.querySelector("#cache-mode").textContent = sample?.cache_mode === "off" ? "关闭" : sample?.cache_mode || "—";
  document.querySelector("#dataset-mode").textContent = sample?.dataset === "sharegpt-repeat-truncate" ? "ShareGPT 定长重采样" : sample?.dataset || "—";
  document.querySelector("#measurement-mode").textContent = visiblePoints[0]?.measurement_basis === "native-full-run"
    ? "完整计时" : visiblePoints.length ? "连续稳态" : "—";
  const latestSeries = report.series.filter(series => series.comparison_key === controls.comparison.value && series.run_id === controls.run.value);
  const versionRows = document.querySelector("#version-rows");
  versionRows.replaceChildren();
  const versionLabels = {};
  for (const engine of Object.keys(names)) {
    const point = visiblePoints.find(point => point.engine === engine);
    if (!point) continue;
    const nightly = point.engine_version_relation === "release-branch-diverged";
    versionLabels[engine] = point.engine_version
      ? `${point.engine_version}${nightly ? " 分支基线 · nightly" : ""}`
      : "发行版本待核对";
    const relation = !point.engine_version_relation ? "待核对" : nightly
      ? `分支点后 ${point.engine_release_ahead} commits；release 独有 ${point.engine_release_behind} commits`
      : point.engine_variant !== "unmodified"
        ? `${point.engine_variant} · 基础 ${point.engine_base_sha?.slice(0, 10) || "待核对"}`
        : "原始发行镜像";
    const row = document.createElement("tr");
    for (const value of [names[engine], versionLabels[engine], point.engine_sha.slice(0, 10), relation]) {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    }
    versionRows.append(row);
  }
  const aiakVariant = visiblePoints.find(point => point.engine === "aiak")?.engine_variant;
  const aiakLabel = aiakVariant && aiakVariant !== "unmodified" ? "AIAK Hotfix" : "AIAK";
  const bestLabel = latency ? "最低合格 TPOT" : "合格峰值";
  document.querySelector("#community-title").textContent = `Community · ${bestLabel}`;
  document.querySelector("#aiak-title").textContent = `${aiakLabel} · ${bestLabel}`;
  document.querySelector("#ratio-title").textContent = `${aiakLabel} 相对 Community 速度`;
  const peaks = {};
  for (const engine of Object.keys(names)) {
    peaks[engine] = latestSeries.filter(series => series.engine === engine && series.stable)
      .sort((a, b) => latency ? a.metric_value - b.metric_value : b.metric_value - a.metric_value)[0];
    const value = document.getElementById(`${engine}-value`);
    value.replaceChildren(document.createTextNode(peaks[engine] ? format.format(peaks[engine].metric_value) : "—"));
    if (peaks[engine]) {
      const unit = document.createElement("span"); unit.className = "unit"; unit.textContent = metricUnit; value.append(unit);
    }
  }
  const canCompare = peaks.community && peaks.aiak;
  document.querySelector("#ratio-value").textContent = canCompare ? `${(latency ? peaks.community.metric_value / peaks.aiak.metric_value : peaks.aiak.metric_value / peaks.community.metric_value).toFixed(2)}×` : "—";
  document.querySelector("#empty").hidden = latestSeries.length > 0;
  const chartSeries = Object.keys(names).flatMap(engine => [true, false].map(stable => ({
    name: (engine === "aiak" ? aiakLabel : names[engine]) + (stable ? "" : " · 未验证"),
    type: "line", symbol: "circle", symbolSize: 7, connectNulls: false,
    itemStyle: { color: colors[engine] }, lineStyle: { color: colors[engine], width: 2, type: stable ? "solid" : "dashed" },
    data: latestSeries.filter(series => series.engine === engine).sort((a, b) => a.concurrency - b.concurrency)
      .map(series => [series.concurrency, series.stable === stable ? series.metric_value : null]),
  })));
  chart.setOption({
    animation: false, textStyle: { fontFamily: "system-ui" },
    grid: { left: 76, right: 35, top: 32, bottom: 48 },
    tooltip: { trigger: "axis", valueFormatter: value => value == null ? "—" : `${format.format(value)} ${metricUnit}` },
    xAxis: { type: "log", logBase: 2, min: 1, max: Math.max(2, ...latestSeries.map(s => s.concurrency)), name: "并发", nameLocation: "middle", nameGap: 32, nameTextStyle: { color: "#787774", fontSize: 14 }, axisLabel: { color: "#787774", fontSize: 14 }, axisTick: { show: false }, axisLine: { lineStyle: { color: "#e9e9e7" } }, splitLine: { show: false } },
    yAxis: { type: "value", min: 0, axisLabel: { color: "#787774", fontSize: 14, formatter: value => value >= 1000 ? `${value / 1000}k` : value }, splitLine: { lineStyle: { color: "#eeedeb" } } },
    series: chartSeries,
  }, true);
  const rows = document.querySelector("#rows");
  rows.replaceChildren();
  for (const point of [...visiblePoints].reverse()) {
    const row = document.createElement("tr");
    const engineCell = document.createElement("td");
    engineCell.textContent = names[point.engine] || point.engine;
    row.append(engineCell);
    const version = document.createElement("td"); version.className = "version";
    version.textContent = `${point.engine_version || "版本待核对"}${point.engine_version_relation === "release-branch-diverged" ? " nightly" : ""} + ${point.engine_sha.slice(0, 10)}`;
    if (point.engine_variant && point.engine_variant !== "unmodified") {
      version.textContent += ` · ${point.engine_variant}`;
    }
    row.append(version);
    for (const value of [point.concurrency, point.num_prompts, point.throughput, point.duration, point.ttft_ms, point.tpot_ms, point.accept_length]) {
      const cell = document.createElement("td"); cell.textContent = value == null ? "—" : format.format(value); row.append(cell);
    }
    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    status.className = `status ${point.status}`;
    status.textContent = point.status !== "ok" ? point.status : point.stable ? "合格容量" : point.profile === "smoke" ? "Smoke" : "待验证";
    status.title = point.error || ""; statusCell.append(status); row.append(statusCell);
    const runCell = document.createElement("td"); runCell.textContent = `#${point.repetition}`;
    row.append(runCell);
    rows.append(row);
  }
  renderLaunch();
  document.querySelector("#point-count").textContent = visiblePoints.length;
  document.querySelector("#table-empty").hidden = visiblePoints.length > 0;
  const failures = report.failures.filter(failure => failure.phase === phase);
  document.querySelector("#failure-panel").hidden = failures.length === 0;
  const failureRows = document.querySelector("#failure-rows");
  failureRows.replaceChildren();
  for (const failure of failures) {
    const row = document.createElement("tr");
    for (const value of [failure.run_id, names[failure.engine], failure.phase]) {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    }
    failureRows.append(row);
  }
}

function renderLaunch() {
  const grid = document.querySelector("#launch-grid");
  grid.replaceChildren();
  document.querySelector("#launch-phase").textContent = phase === "P" ? "Prefill" : "Decode";
  for (const engine of Object.keys(names)) {
    const entry = launchConfig[controls.run.value]?.[engine];
    const launch = entry?.phases?.[phase];
    if (!launch || !visiblePoints.some(point => point.engine === engine)) continue;
    const card = document.createElement("article"); card.className = "launch-card";
    const title = document.createElement("h3"); title.textContent = names[engine];
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = `${entry.gpu_count} GPU · 镜像 ${String(entry.image).replace("sha256:", "").slice(0, 12)}`;
    const commandTitle = document.createElement("h4"); commandTitle.textContent = "启动命令";
    const command = document.createElement("pre");
    const args = [];
    for (const arg of launch.args) {
      if (arg.startsWith("--") || !args.length) args.push(arg); else args[args.length - 1] += ` ${arg}`;
    }
    command.textContent = ["sglang serve --model-path <模型>", ...args].join(" \\\n  ");
    const envTitle = document.createElement("h4"); envTitle.textContent = "环境变量";
    const env = document.createElement("pre");
    const entries = Object.entries(launch.env);
    env.textContent = entries.length ? entries.map(([key, value]) => `${key}=${value}`).join("\n") : "（无额外环境变量）";
    card.append(title, meta, commandTitle, command, envTitle, env);
    grid.append(card);
  }
  document.querySelector("#launch-empty").hidden = grid.children.length > 0;
}

function acceptReport(payload) {
  if (payload.schema_version !== 1 || !Array.isArray(payload.points) || !Array.isArray(payload.series) || !Array.isArray(payload.failures)) {
    throw new Error("结果文件格式不支持，需要 schema_version=1 的 summary.json");
  }
  for (const point of payload.points) {
    if (!["P", "D"].includes(point.phase) || typeof point.engine_sha !== "string" || typeof point.run_id !== "string") {
      throw new Error("结果点缺少必要的版本或阶段字段");
    }
    if (point.protocol_id !== "sglang-cookbook-v2") {
      throw new Error("旧测试口径已作废，仅接受 sglang-cookbook-v2 结果");
    }
  }
  if (payload.series.some(series => series.protocol_id !== "sglang-cookbook-v2" || !Number.isFinite(series.metric_value))) {
    throw new Error("结果序列缺少 Cookbook 协议或主指标");
  }
  report = payload;
  if (!report.points.some(point => point.profile === "capacity")) controls.profile.value = "smoke";
  document.querySelector("#error").hidden = true;
  refreshFilters();
}

async function loadReport() {
  try {
    const response = await fetch("./data/summary.json", { cache: "no-store" });
    if (response.status === 404) { render(); return; }
    if (!response.ok) throw new Error(`读取结果失败：HTTP ${response.status}`);
    const payload = await response.json();
    // A running older exporter can publish new measurements before it supports version fields.
    const versionsResponse = await fetch("./data/versions.json", { cache: "no-store" });
    if (versionsResponse.ok) {
      const versions = await versionsResponse.json();
      for (const point of [...payload.points, ...payload.series]) {
        const identity = versions[point.run_id]?.[point.engine];
        if (!identity) continue;
        if (identity.engine_sha !== point.engine_sha) throw new Error("版本信息与运行 Commit 不一致");
        for (const key of ["engine_version", "engine_package_version", "engine_version_relation",
          "engine_base_sha", "engine_release_ahead", "engine_release_behind"]) {
          point[key] = identity[key];
        }
      }
    } else if (versionsResponse.status !== 404) {
      throw new Error(`读取版本信息失败：HTTP ${versionsResponse.status}`);
    }
    const launchResponse = await fetch("./data/launch-config.json", { cache: "no-store" });
    launchConfig = launchResponse.ok ? await launchResponse.json() : {};
    acceptReport(payload);
  } catch (error) {
    document.querySelector("#error").textContent = error.message;
    document.querySelector("#error").hidden = false;
  }
}

function download(content, name, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = name; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

document.querySelectorAll("[data-phase]").forEach(button => button.addEventListener("click", () => {
  phase = button.dataset.phase; refreshFilters();
}));
Object.values(controls).forEach(control => control.addEventListener("change", refreshFilters));
document.querySelector("#refresh").addEventListener("click", loadReport);
document.querySelector("#method-button").addEventListener("click", () => document.querySelector("#method-dialog").showModal());
document.querySelector("#download").addEventListener("click", () => download(JSON.stringify(report, null, 2), "summary.json", "application/json"));
document.querySelector("#download-csv").addEventListener("click", () => {
  const columns = ["run_id", "engine", "engine_version", "engine_sha", "engine_variant", "engine_package_version", "engine_version_relation", "engine_base_sha", "engine_release_ahead", "engine_release_behind", "model", "phase", "profile", "protocol_id", "strategy", "speculative_algorithm", "dataset_sha256", "measurement_basis", "input_length", "concurrency", "num_prompts", "repetition", "status", "stable", "metric_name", "metric_value", "throughput", "duration", "ttft_ms", "tpot_ms", "p99_tpot_ms", "accept_length"];
  const csv = [columns, ...visiblePoints.map(point => columns.map(key => point[key] ?? ""))]
    .map(row => row.map(value => `"${String(value).replace(/^[=+@-]/, "'$&").replaceAll('"', '""')}"`).join(",")).join("\r\n");
  download("\uFEFF" + csv, "points.csv", "text/csv;charset=utf-8");
});
document.querySelector("#import-file").addEventListener("change", async event => {
  try {
    if (event.target.files[0]) acceptReport(JSON.parse(await event.target.files[0].text()));
  } catch (error) {
    document.querySelector("#error").textContent = error.message;
    document.querySelector("#error").hidden = false;
  }
});
new ResizeObserver(() => chart.resize()).observe(document.querySelector("#chart"));
await loadReport();
