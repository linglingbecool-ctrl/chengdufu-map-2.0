const statusClass = {
  "存续点": "status-existing",
  "变迁点": "status-changed",
  "不确定点": "status-uncertain",
  existing: "status-existing",
  changed: "status-changed",
  uncertain: "status-uncertain"
};

const statusLabel = {
  existing: "存续点",
  changed: "变迁点",
  uncertain: "不确定点"
};

const citywalkOrder = [
  "jiuyanqiao",
  "wuhouci",
  "wenshuyuan",
  "qingyanggong",
  "mancheng",
  "hongpailou"
];

const markersEl = document.querySelector("#mapMarkers");
const detailEl = document.querySelector("#pointDetail");
const routeListEl = document.querySelector("#routeList");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function getStatusClass(point) {
  return statusClass[point.status] || "status-uncertain";
}

function getStatusLabel(point) {
  return statusLabel[point.status] || point.status || "待校核";
}

function renderOptionalRow(label, value) {
  if (!value) return "";
  return `
    <strong>${escapeHtml(label)}</strong>
    <span>${escapeHtml(value)}</span>
  `;
}

function renderDetail(point) {
  if (!detailEl) return;

  detailEl.innerHTML = `
    <article class="point-card">
      <span class="type-pill">${escapeHtml(point.type)}</span>

      <div>
        <p class="detail-kicker">Point Detail</p>
        <h3>${escapeHtml(point.nameModern)}</h3>
      </div>

      <div class="meta-grid">
        <strong>古图名</strong>
        <span>${escapeHtml(point.nameAncient)}</span>

        <strong>今名</strong>
        <span>${escapeHtml(point.nameModern)}</span>

        <strong>状态</strong>
        <span>${escapeHtml(getStatusLabel(point))}</span>

        ${renderOptionalRow("可信度", point.confidence)}
        ${renderOptionalRow("判断依据", point.evidence)}
        ${renderOptionalRow("校勘备注", point.note)}
      </div>

      <p>${escapeHtml(point.quick)}</p>
      <p>${escapeHtml(point.extended)}</p>

      <div class="point-media">
        <figure>
          <img src="${escapeHtml(point.oldImage)}" alt="${escapeHtml(point.nameAncient)}古图局部图">
          <figcaption>古图局部图</figcaption>
        </figure>
        <figure>
          <img src="${escapeHtml(point.currentImage)}" alt="${escapeHtml(point.nameModern)}今景图">
          <figcaption>今景图</figcaption>
        </figure>
      </div>

      <p class="source">来源：${escapeHtml(point.source)}</p>
    </article>
  `;
}

function renderMarkers(points) {
  if (!markersEl || !detailEl) return;

  markersEl.innerHTML = "";

  points.forEach((point, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `map-marker ${getStatusClass(point)}`;
    button.style.left = `${point.x}%`;
    button.style.top = `${point.y}%`;
    button.setAttribute("aria-label", point.nameModern);
    button.setAttribute("title", `${point.nameModern}｜${getStatusLabel(point)}`);

    button.addEventListener("click", () => {
      document
        .querySelectorAll(".map-marker")
        .forEach((marker) => marker.classList.remove("active"));

      button.classList.add("active");
      renderDetail(point);
    });

    markersEl.appendChild(button);

    if (index === 0) {
      button.classList.add("active");
      renderDetail(point);
    }
  });
}

function renderRoute(points) {
  if (!routeListEl) return;

  const pointMap = new Map(points.map((point) => [point.id, point]));

  routeListEl.innerHTML = citywalkOrder
    .map((id) => pointMap.get(id))
    .filter(Boolean)
    .map((point) => `
      <li class="route-card">
        <h3>${escapeHtml(point.nameModern)}</h3>
        <p>${escapeHtml(point.routeNote)}</p>
      </li>
    `)
    .join("");
}

async function init() {
  if (!markersEl && !routeListEl) return;

  try {
    const response = await fetch("./points.json");

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const points = await response.json();

    renderMarkers(points);
    renderRoute(points);
  } catch (error) {
    if (detailEl) {
      detailEl.innerHTML = `
        <p class="empty-state">
          点位数据暂时无法加载。请检查 points.json 是否位于仓库根目录，并确认 GitHub Pages 已完成部署。
        </p>
      `;
    }
    if (routeListEl) {
      routeListEl.innerHTML = "";
    }
    console.error("Failed to load points.json", error);
  }
}

init();
