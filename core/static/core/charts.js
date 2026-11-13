(() => {
  const NS = "http://www.w3.org/2000/svg";
  const WIDTH = 920;
  const HEIGHT = 360;
  const MARGIN = { top: 58, right: 24, bottom: 66, left: 72 };

  function element(name, attributes = {}) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  }

  function label(svg, x, y, value, anchor = "middle") {
    const node = element("text", { x, y, "text-anchor": anchor, class: "chart-label" });
    node.textContent = value;
    svg.appendChild(node);
    return node;
  }

  function begin(svg, data) {
    svg.replaceChildren();
    svg.setAttribute("viewBox", `0 0 ${WIDTH} ${HEIGHT}`);
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    if (!data || !data.labels?.length || !data.series?.length) {
      label(svg, WIDTH / 2, HEIGHT / 2, "No hay datos para mostrar");
      return null;
    }
    const plot = {
      x: MARGIN.left,
      y: MARGIN.top,
      width: WIDTH - MARGIN.left - MARGIN.right,
      height: HEIGHT - MARGIN.top - MARGIN.bottom,
    };
    const maxValue = Math.max(1, ...data.series.flatMap(series => series.values.filter(Number.isFinite)));
    const scaleMax = Math.ceil(maxValue / 4) * 4 || 4;
    for (let tick = 0; tick <= 4; tick++) {
      const value = (scaleMax / 4) * tick;
      const y = plot.y + plot.height - (value / scaleMax) * plot.height;
      svg.appendChild(element("line", { x1: plot.x, y1: y, x2: plot.x + plot.width, y2: y, class: "chart-gridline" }));
      label(svg, plot.x - 10, y + 4, value.toLocaleString("es-BO", { maximumFractionDigits: 1 }), "end");
    }
    if (data.unit) label(svg, plot.x, 20, data.unit, "start");
    const legendY = 26;
    let legendX = plot.x + Math.min(170, plot.width * 0.38);
    for (const series of data.series) {
      svg.appendChild(element("line", { x1: legendX, y1: legendY - 4, x2: legendX + 18, y2: legendY - 4, stroke: series.color, "stroke-width": 4, "stroke-linecap": "round" }));
      label(svg, legendX + 25, legendY, series.label, "start");
      legendX += Math.max(150, series.label.length * 7 + 52);
    }
    return { plot, scaleMax };
  }

  function drawBars(svg, data) {
    const axes = begin(svg, data);
    if (!axes) return;
    const { plot, scaleMax } = axes;
    const count = data.labels.length;
    const groupWidth = plot.width / count;
    const barWidth = Math.min(34, groupWidth * 0.29 / data.series.length);
    data.labels.forEach((text, index) => {
      const center = plot.x + groupWidth * (index + 0.5);
      data.series.forEach((series, seriesIndex) => {
        const value = Number(series.values[index] || 0);
        const barHeight = Math.max(0, (value / scaleMax) * plot.height);
        const x = center + (seriesIndex - (data.series.length - 1) / 2) * (barWidth + 3) - barWidth / 2;
        const y = plot.y + plot.height - barHeight;
        const rect = element("rect", { x, y, width: barWidth, height: barHeight, rx: 4, fill: series.color, class: "chart-bar" });
        const title = element("title");
        title.textContent = `${text}: ${series.label} ${value.toLocaleString("es-BO")} ${data.unit || ""}`;
        rect.appendChild(title);
        svg.appendChild(rect);
      });
      const tick = label(svg, center, plot.y + plot.height + 22, text);
      if (count > 8) tick.setAttribute("font-size", "10");
    });
  }

  function drawLines(svg, data) {
    const axes = begin(svg, data);
    if (!axes) return;
    const { plot, scaleMax } = axes;
    const count = data.labels.length;
    const xAt = index => plot.x + (count === 1 ? plot.width / 2 : (plot.width * index) / (count - 1));
    const yAt = value => plot.y + plot.height - (value / scaleMax) * plot.height;
    data.labels.forEach((text, index) => {
      const tick = label(svg, xAt(index), plot.y + plot.height + 22, text);
      if (count > 8) tick.setAttribute("font-size", "10");
    });
    for (const series of data.series) {
      let segment = [];
      const drawSegment = () => {
        if (segment.length > 1) {
          svg.appendChild(element("polyline", { points: segment.map(point => `${point.x},${point.y}`).join(" "), fill: "none", stroke: series.color, "stroke-width": 3, "stroke-linecap": "round", "stroke-linejoin": "round" }));
        }
        for (const point of segment) {
          const dot = element("circle", { cx: point.x, cy: point.y, r: 4, fill: series.color, class: "chart-dot" });
          const title = element("title");
          title.textContent = `${data.labels[point.index]}: ${series.label} ${point.value.toLocaleString("es-BO")} ${data.unit || ""}`;
          dot.appendChild(title);
          svg.appendChild(dot);
        }
        segment = [];
      };
      series.values.forEach((raw, index) => {
        const value = Number(raw);
        if (raw == null || !Number.isFinite(value)) drawSegment();
        else segment.push({ x: xAt(index), y: yAt(value), value, index });
      });
      drawSegment();
    }
  }

  function readData(id) {
    const node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : null;
  }

  function init() {
    const sales = readData("sales-chart-data");
    const salesSvg = document.getElementById("sales-chart");
    if (sales && salesSvg) drawBars(salesSvg, sales);

    const dashboardSales = readData("dashboard-sales-chart-data");
    const dashboardSalesSvg = document.getElementById("dashboard-sales-chart");
    if (dashboardSales && dashboardSalesSvg) drawLines(dashboardSalesSvg, dashboardSales);

    const forecastData = readData("forecast-chart-data");
    const forecastSvg = document.getElementById("forecast-chart");
    const forecastSelect = document.getElementById("forecast-product-select");
    if (forecastData && forecastSvg && forecastSelect) {
      const update = () => {
        const selected = forecastData[forecastSelect.value];
        drawLines(forecastSvg, selected);
        const summary = selected?.history_summary;
        if (!summary) return;
        const setText = (id, value) => {
          const node = document.getElementById(id);
          if (node) node.textContent = value;
        };
        setText("forecast-months-with-sales", `${summary.months_with_sales} de 12`);
        setText("forecast-months-without-sales", `${summary.months_without_sales} de 12`);
        setText("forecast-average-sales", `${Number(summary.average_monthly_sales).toLocaleString("es-BO")} ${selected.unit || "unidades"}`);
        setText("forecast-last-sale-month", summary.last_month_with_sales);
        setText("forecast-history-guidance", summary.months_with_sales <= 3
          ? "Hay pocos meses con ventas registradas en el período. Por eso la proyección puede ser baja o cero; revisa que las ventas históricas estén completas antes de usarla para planificar."
          : summary.months_without_sales >= 6
            ? "El historial tiene varios meses sin ventas registradas. El pronóstico considera esos meses como cero, así que puede reducir la estimación."
            : "El resumen ayuda a distinguir una demanda baja de un historial con meses sin ventas registradas.");
      };
      forecastSelect.addEventListener("change", update);
      update();
    }

    const inventoryData = readData("inventory-chart-data");
    const inventorySvg = document.getElementById("inventory-chart");
    const inventorySelect = document.getElementById("inventory-unit-select");
    if (inventoryData && inventorySvg && inventorySelect) {
      const update = () => drawBars(inventorySvg, inventoryData[inventorySelect.value]);
      inventorySelect.addEventListener("change", update);
      update();
    }

    const productionData = readData("production-chart-data");
    const productionSvg = document.getElementById("production-chart");
    const productionSelect = document.getElementById("production-unit-select");
    if (productionData && productionSvg && productionSelect) {
      const update = () => drawBars(productionSvg, productionData[productionSelect.value]);
      productionSelect.addEventListener("change", update);
      update();
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
