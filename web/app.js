import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);
const chart = echarts.init(document.querySelector("#chart"));
const colors = { community: "#8797a5", aiak: "#3d8d66" };
const names = { community: "Community SGLang", aiak: "AIAK SGLang" };
let report = { schema_version: 1, points: [], series: [], failures: [] };
let phase = "P";
let visiblePoints = [];
const controls = Object.fromEntries(["model", "context", "profile", "comparison"].map(id => [id, document.getElementById(id)]));
const format = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

function fillOptions(select, values, labels = values) {
  const previous = select.value;
  select.replaceChildren(...values.map((value, index) => new Option(labels[index], value)));
  if (values.includes(previous)) select.value = previous;
}

function refreshFilters() {
  const points = report.points.filter(point => point.phase === phase);
  fillOptions(controls.model, [...new Set(points.map(point => point.model))]);
  const contexts = [...new Set(points.filter(point => point.model === controls.model.value).map(point => String(point.input_length)))];
  fillOptions(controls.context, contexts, contexts.map(value => `${format.format(Number(value))} tokens`));
  const groups = [...new Set(points.filter(point =>
    point.model === controls.model.value && String(point.input_length) === controls.context.value &&
    point.profile === controls.profile.value
  ).map(point => point.comparison_key))];
  fillOptions(controls.comparison, groups, groups.map(value => `${value.slice(0, 8)} · 同条件组`));
  render();
}

function render() {
  document.querySelectorAll("[data-phase]").forEach(button => button.classList.toggle("selected", button.dataset.phase === phase));
  document.querySelector("#phase-note").textContent = phase === "P"
    ? "P · 集中式服务，固定输入、输出 1 token、关闭 prefix cache。"
    : "D · Synthetic KV · 保留真实专家路由；只表示合成单压容量，不代表生产精度或真实 1P1D 吞吐。";
  document.querySelector("#chart-subtitle").textContent = `${phase === "P" ? "输入" : "输出"}吞吐 · tokens / second`;
  visiblePoints = report.points.filter(point => point.phase === phase &&
    point.model === controls.model.value && String(point.input_length) === controls.context.value &&
    point.profile === controls.profile.value && point.comparison_key === controls.comparison.value);
  const selectedSeries = report.series.filter(series => series.comparison_key === controls.comparison.value);
  // Only compare engines from the same run. New incomplete runs must not inherit an older engine's peak.
  const runIds = [...new Set(selectedSeries.map(series => series.run_id))].sort();
  const latestRun = runIds.at(-1);
  const latestSeries = selectedSeries.filter(series => series.run_id === latestRun);
  const peaks = {};
  for (const engine of Object.keys(names)) {
    peaks[engine] = latestSeries.filter(series => series.engine === engine && series.stable)
      .sort((a, b) => b.throughput - a.throughput)[0];
    const value = document.getElementById(`${engine}-value`);
    const foot = document.getElementById(`${engine}-foot`);
    value.replaceChildren(document.createTextNode(peaks[engine] ? format.format(peaks[engine].throughput) : "—"));
    if (peaks[engine]) {
      const unit = document.createElement("small"); unit.textContent = "tok/s"; value.append(unit);
      foot.textContent = `并发 ${peaks[engine].concurrency} · ${peaks[engine].repeats} 次重复 · CV ${(peaks[engine].cv * 100).toFixed(1)}%`;
    } else {
      foot.textContent = controls.profile.value === "smoke" ? "Smoke 不参与容量排名" : "等待合格的容量结果";
    }
  }
  const canCompare = peaks.community && peaks.aiak;
  document.querySelector("#ratio-value").textContent = canCompare ? `${(peaks.aiak.throughput / peaks.community.throughput).toFixed(2)}×` : "—";
  document.querySelector("#ratio-foot").textContent = canCompare ? "AIAK / Community · 已扫描范围的合格峰值" : "两套引擎均通过稳定性验证后显示";
  document.querySelector("#empty").hidden = latestSeries.length > 0;
  const chartSeries = Object.keys(names).flatMap(engine => [true, false].map(stable => ({
    name: names[engine] + (stable ? "" : " · 未验证"),
    type: "line", symbol: "circle", symbolSize: 7, connectNulls: false,
    itemStyle: { color: colors[engine] }, lineStyle: { color: colors[engine], width: 2, type: stable ? "solid" : "dashed" },
    data: latestSeries.filter(series => series.engine === engine).sort((a, b) => a.concurrency - b.concurrency)
      .map(series => [series.concurrency, series.stable === stable ? series.throughput : null]),
  })));
  chart.setOption({
    animation: false, textStyle: { fontFamily: "system-ui" },
    grid: { left: 76, right: 35, top: 32, bottom: 48 },
    tooltip: { trigger: "axis", valueFormatter: value => value == null ? "—" : `${format.format(value)} tok/s` },
    xAxis: { type: "log", logBase: 2, min: 1, max: Math.max(2, ...latestSeries.map(s => s.concurrency)), name: "并发", nameLocation: "middle", nameGap: 30, nameTextStyle: { color: "#97a59c", fontSize: 10 }, axisLabel: { color: "#9ca89f", fontSize: 10 }, axisTick: { show: false }, axisLine: { lineStyle: { color: "#e8eee9" } }, splitLine: { show: false } },
    yAxis: { type: "value", min: 0, axisLabel: { color: "#9ca89f", fontSize: 10, formatter: value => value >= 1000 ? `${value / 1000}k` : value }, splitLine: { lineStyle: { color: "#eff3ef", type: "dashed" } } },
    series: chartSeries,
  }, true);
  document.querySelector("#scope").textContent = latestSeries.length
    ? `运行 ${latestRun} · 已测并发 ${Math.min(...latestSeries.map(s => s.concurrency))}–${Math.max(...latestSeries.map(s => s.concurrency))} · 不代表全局极限`
    : "扫描上限与实测范围会随结果显示";
  const rows = document.querySelector("#rows");
  rows.replaceChildren();
  for (const point of [...visiblePoints].reverse()) {
    const row = document.createElement("tr");
    const engineCell = document.createElement("td");
    engineCell.textContent = names[point.engine] || point.engine;
    const version = document.createElement("small"); version.textContent = point.engine_sha.slice(0, 10);
    engineCell.append(version); row.append(engineCell);
    for (const value of [point.concurrency, point.throughput, point.duration, point.ttft_ms, point.tpot_ms]) {
      const cell = document.createElement("td"); cell.textContent = value == null ? "—" : format.format(value); row.append(cell);
    }
    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    status.className = `status ${point.status}`;
    status.textContent = point.status !== "ok" ? point.status : point.stable ? "合格容量" : point.profile === "smoke" ? "Smoke" : "待验证";
    status.title = point.error || ""; statusCell.append(status); row.append(statusCell);
    const runCell = document.createElement("td"); runCell.textContent = `#${point.repetition}`;
    const run = document.createElement("small"); run.textContent = point.run_id; runCell.append(run); row.append(runCell);
    rows.append(row);
  }
  document.querySelector("#point-count").textContent = visiblePoints.length;
  document.querySelector("#table-empty").hidden = visiblePoints.length > 0;
  const failures = report.failures.filter(failure => failure.phase === phase);
  document.querySelector("#table-empty").textContent = failures.length ? `${failures.length} 次服务启动失败，详细日志保存在内网运行目录` : "暂无测试记录";
  document.querySelector("#update-time").textContent = report.generated_at ? `更新于 ${new Date(report.generated_at).toLocaleString("zh-CN")}` : "尚未加载结果";
}

function acceptReport(payload) {
  if (payload.schema_version !== 1 || !Array.isArray(payload.points) || !Array.isArray(payload.series) || !Array.isArray(payload.failures)) {
    throw new Error("结果文件格式不支持，需要 schema_version=1 的 summary.json");
  }
  for (const point of payload.points) {
    if (!["P", "D"].includes(point.phase) || typeof point.engine_sha !== "string" || typeof point.run_id !== "string") {
      throw new Error("结果点缺少必要的版本或阶段字段");
    }
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
    acceptReport(await response.json());
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
  const columns = ["run_id", "engine", "engine_sha", "model", "phase", "profile", "input_length", "concurrency", "repetition", "status", "stable", "throughput", "duration", "ttft_ms", "tpot_ms"];
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
