const state = {
  tags: [],
  selectedTag: null,
  eventRows: [],
  lastChartRefresh: 0,
  chartData: new Map(),
};

const palette = ["#ff4438", "#147d64", "#2964b8", "#9b5de5", "#d58a12", "#1b8d9b"];

function el(id) {
  return document.getElementById(id);
}

async function getJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
}

function formatNumber(value, maximumFractionDigits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("en-AU", { maximumFractionDigits }).format(Number(value));
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-AU", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-AU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function humanBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${formatNumber(value / 1024, 1)} KB`;
  return `${formatNumber(value / 1024 / 1024, 1)} MB`;
}

function setConnection(status, text) {
  const pill = el("connectionPill");
  pill.classList.remove("status-pending", "status-ok", "status-error");
  pill.classList.add(status === "ok" ? "status-ok" : "status-error");
  el("connectionText").textContent = text;
}

async function refreshHealth() {
  try {
    const data = await getJSON("/api/health");
    setConnection(
      "ok",
      `Redis connected · ${data.raw_series} series · ${formatNumber(data.redis_latency_ms, 3)} ms`,
    );
  } catch (_error) {
    setConnection("error", "Redis unavailable");
  }
}

function metricDisplayName(tag) {
  const metric = tag.metric || "value";
  return metric.replaceAll("_", " ").replace(/\b\w/g, character => character.toUpperCase());
}

function renderLatestCards() {
  const container = el("latestCards");
  if (!state.tags.length) {
    container.innerHTML = '<div class="loading-card">Waiting for PI samples...</div>';
    return;
  }
  container.innerHTML = state.tags.map(item => `
    <article class="metric-card" data-tag="${item.tag}">
      <div class="metric-top">
        <div>
          <div class="metric-name">${metricDisplayName(item)}</div>
          <div class="metric-location">${item.site} · ${item.asset}</div>
        </div>
        <span class="quality-badge ${item.good ? "quality-good" : "quality-bad"}">
          ${item.good ? "GOOD" : "BAD"}
        </span>
      </div>
      <div class="metric-value">
        <b data-role="value">${formatNumber(item.value)}</b>
        <span data-role="units">${item.units || ""}</span>
      </div>
      <div class="metric-time" data-role="time">Updated ${formatDateTime(item.ts_ms || item.ts)}</div>
    </article>
  `).join("");
}

function populateSelect(select, options, preferred) {
  const current = preferred || select.value;
  select.innerHTML = options
    .map(option => `<option value="${option.value}">${option.label}</option>`)
    .join("");
  if (options.some(option => option.value === current)) select.value = current;
}

async function loadTags() {
  const data = await getJSON("/api/tags");
  state.tags = data.items;
  renderLatestCards();

  const tagOptions = state.tags.map(item => ({
    value: item.tag,
    label: `${item.site} / ${item.asset} / ${metricDisplayName(item)}`,
  }));
  const preferred = state.selectedTag
    || tagOptions.find(item => item.value.toLowerCase().includes("temperature"))?.value;
  populateSelect(el("tagSelect"), tagOptions, preferred);
  state.selectedTag = el("tagSelect").value;

  const metrics = [...new Set(state.tags.map(item => item.metric).filter(Boolean))].sort();
  const currentFleetMetric = el("fleetMetricSelect").value;
  const preferredFleetMetric = metrics.includes(currentFleetMetric)
    ? currentFleetMetric
    : (metrics.includes("temperature") ? "temperature" : metrics[0]);
  populateSelect(
    el("fleetMetricSelect"),
    metrics.map(metric => ({
      value: metric,
      label: metric.replaceAll("_", " ").replace(/\b\w/g, character => character.toUpperCase()),
    })),
    preferredFleetMetric,
  );
}

function renderStats(data) {
  const units = data.units || "";
  const items = [
    ["POINTS RETURNED", data.stats.count],
    ["MIN", data.stats.min === null ? "—" : `${formatNumber(data.stats.min)} ${units}`],
    ["AVERAGE", data.stats.avg === null ? "—" : `${formatNumber(data.stats.avg)} ${units}`],
    ["MAX", data.stats.max === null ? "—" : `${formatNumber(data.stats.max)} ${units}`],
    ["LAST", data.stats.last === null ? "—" : `${formatNumber(data.stats.last)} ${units}`],
  ];
  el("seriesStats").innerHTML = items.map(([label, value]) => `
    <div class="stat-chip"><span>${label}</span><strong>${value}</strong></div>
  `).join("");
}

function niceBounds(minimum, maximum) {
  if (minimum === maximum) {
    const padding = Math.abs(minimum || 1) * 0.1;
    return [minimum - padding, maximum + padding];
  }
  const padding = (maximum - minimum) * 0.12;
  return [minimum - padding, maximum + padding];
}

function drawChart(canvas, datasets, options = {}) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssHeight = canvas.closest(".small") ? 250 : 330;
  canvas.width = Math.max(320, rect.width) * dpr;
  canvas.height = cssHeight * dpr;
  const context = canvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  const width = canvas.width / dpr;
  const height = cssHeight;
  context.clearRect(0, 0, width, height);

  const allPoints = datasets.flatMap(dataset => dataset.points || []);
  if (!allPoints.length) {
    context.fillStyle = "#60706c";
    context.font = "13px system-ui";
    context.textAlign = "center";
    context.fillText("No samples in the selected time window", width / 2, height / 2);
    return;
  }

  const margin = { left: 58, right: 18, top: 22, bottom: 38 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const timestamps = allPoints.map(point => Number(point.ts));
  const values = allPoints.map(point => Number(point.value));
  const minTimestamp = Math.min(...timestamps);
  const maxTimestamp = Math.max(...timestamps);
  const [minValue, maxValue] = niceBounds(Math.min(...values), Math.max(...values));
  const x = timestamp => margin.left
    + ((Number(timestamp) - minTimestamp) / Math.max(maxTimestamp - minTimestamp, 1)) * plotWidth;
  const y = value => margin.top
    + (1 - (Number(value) - minValue) / Math.max(maxValue - minValue, 1e-9)) * plotHeight;

  context.strokeStyle = "#e2e9e7";
  context.lineWidth = 1;
  context.fillStyle = "#71807c";
  context.font = "10px system-ui";
  for (let index = 0; index <= 4; index += 1) {
    const positionY = margin.top + (plotHeight / 4) * index;
    const labelValue = maxValue - ((maxValue - minValue) / 4) * index;
    context.beginPath();
    context.moveTo(margin.left, positionY);
    context.lineTo(width - margin.right, positionY);
    context.stroke();
    context.textAlign = "right";
    context.fillText(formatNumber(labelValue, 2), margin.left - 8, positionY + 3);
  }

  for (let index = 0; index <= 4; index += 1) {
    const positionX = margin.left + (plotWidth / 4) * index;
    const timestamp = minTimestamp + ((maxTimestamp - minTimestamp) / 4) * index;
    context.textAlign = index === 0 ? "left" : index === 4 ? "right" : "center";
    context.fillText(formatTime(timestamp), positionX, height - 14);
  }

  datasets.forEach((dataset, datasetIndex) => {
    const points = dataset.points || [];
    if (!points.length) return;
    context.strokeStyle = dataset.color || palette[datasetIndex % palette.length];
    context.lineWidth = datasets.length > 1 ? 2 : 2.5;
    context.lineJoin = "round";
    context.lineCap = "round";
    context.beginPath();
    points.forEach((point, pointIndex) => {
      const positionX = x(point.ts);
      const positionY = y(point.value);
      if (pointIndex === 0) context.moveTo(positionX, positionY);
      else context.lineTo(positionX, positionY);
    });
    context.stroke();

    if (points.length <= 30) {
      context.fillStyle = dataset.color || palette[datasetIndex % palette.length];
      points.forEach(point => {
        context.beginPath();
        context.arc(x(point.ts), y(point.value), 2.4, 0, Math.PI * 2);
        context.fill();
      });
    }
  });

  if (options.units) {
    context.save();
    context.translate(14, margin.top + plotHeight / 2);
    context.rotate(-Math.PI / 2);
    context.textAlign = "center";
    context.fillStyle = "#71807c";
    context.font = "10px system-ui";
    context.fillText(options.units, 0, 0);
    context.restore();
  }
}

async function loadSeries() {
  if (!el("tagSelect").value) return;
  state.selectedTag = el("tagSelect").value;
  const parameters = new URLSearchParams({
    window_minutes: el("windowSelect").value,
    aggregation: el("aggregationSelect").value,
    bucket_ms: el("bucketSelect").value,
    count: "1000",
  });
  if (el("valueFilterEnabled").checked) {
    parameters.set("min_value", el("minValue").value);
    parameters.set("max_value", el("maxValue").value);
  }

  try {
    const data = await getJSON(`/api/series/${encodeURIComponent(state.selectedTag)}?${parameters}`);
    state.chartData.set("series", data);
    renderStats(data);
    drawChart(el("seriesChart"), [{ name: data.tag, points: data.points }], { units: data.units });
    el("seriesCommand").textContent = data.command;
    el("seriesExplanation").textContent = data.explanation;
    el("seriesLatency").textContent = `${formatNumber(data.query_latency_ms, 3)} ms`;
    await loadRollup();
  } catch (error) {
    el("seriesExplanation").textContent = error.message;
    drawChart(el("seriesChart"), []);
  }
}

function renderLegend(rows) {
  el("fleetLegend").innerHTML = rows.map((row, index) => {
    const name = row.key.includes("=") ? row.key.split("=").slice(1).join("=") : row.key;
    return `
      <span class="legend-item">
        <span class="legend-swatch" style="background:${palette[index % palette.length]}"></span>
        ${name}
      </span>
    `;
  }).join("");
}

async function loadFleet() {
  if (!el("fleetMetricSelect").value) return;
  const parameters = new URLSearchParams({
    metric: el("fleetMetricSelect").value,
    group_by: el("groupBySelect").value,
    reducer: el("reducerSelect").value,
    window_minutes: el("windowSelect").value,
    aggregation: "avg",
    bucket_ms: el("bucketSelect").value,
  });

  try {
    const data = await getJSON(`/api/multi?${parameters}`);
    state.chartData.set("fleet", data);
    const datasets = data.series.map((row, index) => ({
      name: row.key,
      points: row.points,
      color: palette[index % palette.length],
    }));
    renderLegend(data.series);
    drawChart(el("fleetChart"), datasets);
    el("fleetCommand").textContent = data.command;
    el("fleetExplanation").textContent = data.explanation;
    el("fleetLatency").textContent = `${formatNumber(data.query_latency_ms, 3)} ms`;
  } catch (error) {
    el("fleetExplanation").textContent = error.message;
    drawChart(el("fleetChart"), []);
  }
}

async function loadRollup() {
  if (!state.selectedTag) return;
  try {
    const data = await getJSON(
      `/api/rollup/${encodeURIComponent(state.selectedTag)}?window_minutes=${el("windowSelect").value}`,
    );
    state.chartData.set("rollup", data);
    el("rawSamples").textContent = `${formatNumber(data.raw.total_samples, 0)} samples`;
    el("rawMemory").textContent = `${humanBytes(data.raw.memory_bytes)} · ${data.raw.chunk_type}`;
    el("rollupSamples").textContent = `${formatNumber(data.rollup.total_samples, 0)} samples`;
    el("rollupMemory").textContent = `${humanBytes(data.rollup.memory_bytes)} · ${data.rollup.chunk_type}`;
    el("rollupRule").textContent = data.rule;
    el("rollupCommand").textContent = data.command;
    el("rollupExplanation").textContent = data.explanation;
    drawChart(el("rollupChart"), [{ name: data.rollup.name, points: data.points }]);
  } catch (error) {
    el("rollupExplanation").textContent = error.message;
    drawChart(el("rollupChart"), []);
  }
}

function updateCardFromEvent(event) {
  const card = document.querySelector(`.metric-card[data-tag="${CSS.escape(event.tag)}"]`);
  if (!card) return;
  card.querySelector('[data-role="value"]').textContent = formatNumber(event.value);
  card.querySelector('[data-role="units"]').textContent = event.units || "";
  card.querySelector('[data-role="time"]').textContent = `Updated ${formatDateTime(event.ts_ms || event.ts)}`;
}

function renderEvents() {
  const body = el("eventsBody");
  if (!state.eventRows.length) return;
  body.innerHTML = state.eventRows.map(item => `
    <tr>
      <td>${formatTime(item.ts_ms || item.ts)}</td>
      <td>${item.tag}</td>
      <td>${formatNumber(item.value)} ${item.units || ""}</td>
      <td>
        <span class="quality-badge ${item.good ? "quality-good" : "quality-bad"}">
          ${item.good ? "GOOD" : "BAD"}
        </span>
      </td>
    </tr>
  `).join("");
}

function startEvents() {
  const source = new EventSource("/api/events");
  source.addEventListener("sample", event => {
    const payload = JSON.parse(event.data);
    updateCardFromEvent(payload);
    state.eventRows.unshift(payload);
    state.eventRows = state.eventRows.slice(0, 10);
    renderEvents();

    const now = Date.now();
    if (now - state.lastChartRefresh > 3500) {
      state.lastChartRefresh = now;
      loadSeries();
      loadFleet();
    }
  });
  source.onerror = () => {
    el("liveIndicator").innerHTML = "<span></span> Reconnecting";
  };
  source.onopen = () => {
    el("liveIndicator").innerHTML = "<span></span> Live";
  };
}

function bindControls() {
  el("valueFilterEnabled").addEventListener("change", event => {
    el("minValue").disabled = !event.target.checked;
    el("maxValue").disabled = !event.target.checked;
  });
  el("runQueryButton").addEventListener("click", () => {
    loadSeries();
    loadFleet();
  });
  el("tagSelect").addEventListener("change", loadSeries);
  el("windowSelect").addEventListener("change", () => {
    loadSeries();
    loadFleet();
  });
  el("aggregationSelect").addEventListener("change", () => {
    el("bucketSelect").disabled = el("aggregationSelect").value === "raw";
    loadSeries();
  });
  el("bucketSelect").addEventListener("change", () => {
    loadSeries();
    loadFleet();
  });
  el("fleetMetricSelect").addEventListener("change", loadFleet);
  el("groupBySelect").addEventListener("change", loadFleet);
  el("reducerSelect").addEventListener("change", loadFleet);

  window.addEventListener("resize", () => {
    const series = state.chartData.get("series");
    if (series) {
      drawChart(
        el("seriesChart"),
        [{ name: series.tag, points: series.points }],
        { units: series.units },
      );
    }
    const fleet = state.chartData.get("fleet");
    if (fleet) {
      drawChart(
        el("fleetChart"),
        fleet.series.map((row, index) => ({
          name: row.key,
          points: row.points,
          color: palette[index % palette.length],
        })),
      );
    }
    const rollup = state.chartData.get("rollup");
    if (rollup) {
      drawChart(el("rollupChart"), [{ name: rollup.rollup.name, points: rollup.points }]);
    }
  });
}

async function initialise() {
  bindControls();
  await refreshHealth();
  try {
    await loadTags();
    await Promise.all([loadSeries(), loadFleet()]);
  } catch (error) {
    setConnection("error", error.message);
  }
  startEvents();
  setInterval(refreshHealth, 10000);
  setInterval(loadTags, 15000);
}

document.addEventListener("DOMContentLoaded", initialise);
