// Confirmation for destructive actions and Chart.js rendering from embedded JSON (no inline scripts: CSP).
document.addEventListener("submit", (e) => {
  const msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const gbp = (p) => "£" + (p / 100).toLocaleString("en-GB", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
if (window.Chart) {
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--line");
  Chart.defaults.font.family = css("--font");
  Chart.defaults.font.size = 11;
}
const palette = ["--cyan", "--purple", "--emerald", "--teal", "--amber", "--red"].map(css);
document.querySelectorAll("canvas[data-chart]").forEach((el) => {
  const spec = JSON.parse(document.getElementById(el.dataset.chart).textContent);
  if (!spec.labels.length) {
    el.parentElement.innerHTML = '<div class="empty">No records yet</div>';
    return;
  }
  const datasets = spec.series.map((s, i) => ({
    label: s.label, data: s.data, borderColor: palette[s.color ?? i], backgroundColor: spec.type === "line" ? palette[s.color ?? i] + "22" : spec.type === "doughnut" ? palette : palette[s.color ?? i] + "cc",
    fill: spec.type === "line", tension: 0.25, pointRadius: 2, borderWidth: spec.type === "doughnut" ? 0 : 2, borderRadius: 3,
  }));
  new Chart(el, {
    type: spec.type, data: { labels: spec.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
      plugins: { legend: { display: spec.series.length > 1 || spec.type === "doughnut", position: "bottom", labels: { boxWidth: 10 } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${gbp(c.parsed.y ?? c.parsed)}` } } },
      scales: spec.type === "doughnut" ? {} : { y: { ticks: { callback: (v) => gbp(v) }, grid: { color: css("--line") } }, x: { grid: { display: false } } },
    },
  });
});
