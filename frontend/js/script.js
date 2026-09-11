/* ============================================================
   REPO AUTOPSY — progressive architecture frontend

   Navigation model
   ----------------
   repository -> folder -> folder -> file -> dependencies

   Every view is one entry on state.history, so Back always means
   "one level up" and never guesses whether the previous entry was a
   folder or a file.

   Rendering model
   ---------------
   One renderer draws every graph. A view supplies nodes, edges and a
   layout function; the renderer owns the SVG, the camera and the
   selection highlighting. There is exactly one set of window listeners
   for panning, created once at startup.
============================================================ */


/* ============================================================
   CONFIG
============================================================ */

/* Empty means "same origin". The FastAPI app serves this page and the
   API together, so there is no host or port to keep in sync and no
   CORS preflight. Point this at another host only if you split them. */
const API_BASE = "http://127.0.0.1:8000";

/* How many nodes a level draws before collapsing the rest into a single
   "show the rest" node. Nothing is discarded — the sidebar always lists
   the complete level. */
const VISIBLE_NODE_LIMIT = 48;

const MIN_SCALE = 0.25;
const MAX_SCALE = 2.5;


/* ============================================================
   STATE

   One object. Declared before anything that reads it, which is what
   the old "Cannot access 'dependencyData' before initialization" was
   really complaining about.
============================================================ */

const state = {

  repo: {
    owner: "",
    repo: "",
    branch: "",
    fullName: "",
    totalFiles: 0,
    totalDirectories: 0
  },

  /* "architecture" | "file" */
  view: "architecture",

  /* Current location: folder path, or file path in file view. */
  path: "",

  /* [{ view, path }] — one entry per level the user came through. */
  history: [],

  graph: {
    nodes: [],
    edges: [],
    positions: new Map()
  },

  /* Full repository tree, fetched once, expanded locally. */
  tree: {
    files: [],
    directories: [],
    expanded: new Set(),
    filter: ""
  },

  selectedId: null,
  showAllNodes: false,

  /* Free-text filter applied to the current level (the "Filter modules"
     box in the Dependencies view). */
  graphFilter: "",

  /* Which workspace tab is showing. Each graph canvas has zero size
     until its tab is active, so fitting is deferred to activateTab. */
  tab: "overview",

  /* Where the structural graph was when the user left it. */
  lastArchitecturePath: "",

  /* Narrows the Dependencies file list to one folder. Set by clicking
     a folder in the explorer while on that tab. */
  pickerFolder: "",

  camera: { x: 0, y: 0, scale: 1 },

  /* Aborts the previous in-flight request when the user clicks ahead. */
  pending: null,

  /* Whether "imported by" scans siblings or the whole repository. */
  reverseScope: "directory"
};


/* ============================================================
   DOM HELPERS
============================================================ */

function $(selector) {
  return document.querySelector(selector);
}

function on(target, event, handler) {

  const element =
    typeof target === "string" ? document.querySelector(target) : target;

  if (!element) {
    console.warn(`Repo Autopsy: no element for "${target}" — skipping.`);
    return null;
  }

  element.addEventListener(event, handler);
  return element;
}

function setText(selector, value) {
  const element = $(selector);
  if (element) element.textContent = value;
}

function setHtml(selector, value) {
  const element = $(selector);
  if (element) element.innerHTML = value;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function svgEl(name, attributes = {}) {

  const element =
    document.createElementNS("http://www.w3.org/2000/svg", name);

  for (const [key, value] of Object.entries(attributes)) {
    element.setAttribute(key, value);
  }

  return element;
}

function plural(count, singular, pluralForm) {
  return `${count} ${count === 1 ? singular : pluralForm || singular + "s"}`;
}

/* "../shared/ReactSymbols.js" says far more about a dependency than
   "ReactSymbols.js" does — where a file lives is most of what tells
   you what it is. */
function relativeLabel(fromPath, toPath) {

  const from = String(fromPath).split("/").slice(0, -1);
  const to = String(toPath).split("/");
  const name = to.pop();

  let shared = 0;

  while (shared < from.length && shared < to.length &&
         from[shared] === to[shared]) {
    shared += 1;
  }

  const up = from.length - shared;
  const down = to.slice(shared);

  if (up === 0 && !down.length) return name;

  const prefix = up > 0 ? "../".repeat(up) : "./";

  return prefix + [...down, name].join("/");
}

function fileName(path) {
  return String(path).split("/").filter(Boolean).pop() || path;
}

function parentPath(path) {
  const parts = String(path).split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}


/* ============================================================
   API
============================================================ */

function repoQuery(extra = {}) {

  return new URLSearchParams({
    owner: state.repo.owner,
    repo: state.repo.repo,
    branch: state.repo.branch,
    ...extra
  });
}

async function api(path, options = {}) {

  /* One request at a time per user action. Clicking three folders fast
     used to race, and whichever response landed last won. */
  if (state.pending) state.pending.abort();

  const controller = new AbortController();
  state.pending = controller;

  let response;

  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: controller.signal
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error(
      "The analysis backend is not responding. Is uvicorn still running?"
    );
  } finally {
    if (state.pending === controller) state.pending = null;
  }

  let data = {};

  try {
    data = await response.json();
  } catch {
    data = {};
  }

  if (response.status === 429) {
    const retry = Number(response.headers.get("Retry-After")) || 30;
    throw new Error(
      data.detail || `Too many requests. Try again in ${retry} seconds.`
    );
  }

  if (!response.ok) {
    throw new Error(data.detail || `Request failed (HTTP ${response.status}).`);
  }

  return data;
}


/* ============================================================
   ANALYZER SHELL
============================================================ */

const analyzer = $("#analyzer");

function openAnalyzer() {
  if (analyzer) analyzer.hidden = false;
  document.body.classList.add("workspace-open");
}

function closeAnalyzer() {
  if (analyzer) analyzer.hidden = true;
  document.body.classList.remove("workspace-open");
}

on("#open", "click", event => {
  event.preventDefault();
  openAnalyzer();
});

on("#close", "click", event => {
  event.preventDefault();
  closeAnalyzer();
});

on("#homeLink", "click", event => {
  event.preventDefault();
  closeAnalyzer();
  window.scrollTo({ top: 0, behavior: "smooth" });
});


/* ============================================================
   LOADING AND ERROR STATES
============================================================ */

function showGraphLoading(title, message) {

  /* Draw into the pane that is about to render. This was pinned to
     the Dependencies canvas, so opening a folder left its spinner
     running there for ever while the graph appeared in Architecture. */
  setHtml(activePane().canvas, `
    <div class="loading-card">
      <div class="loading-title">${escapeHtml(title)}</div>
      <div class="loading-message">${escapeHtml(message)}</div>
      <div class="loading-bar"></div>
    </div>
  `);
}

function showGraphError(message) {

  setHtml(activePane().canvas, `
    <div class="graph-empty">
      <p class="graph-empty-title">This view could not load.</p>
      <p class="graph-empty-message">${escapeHtml(message)}</p>
    </div>
  `);

  setText("#depNodeCount", "0");
  setText("#depEdgeCount", "0");
}

function showTreeMessage(message) {
  setHtml("#tree", `<div class="tree-loading">${escapeHtml(message)}</div>`);
}


/* ============================================================
   REPOSITORY SUBMISSION
============================================================ */

function parseGitHubUrl(value) {

  const match = String(value)
    .trim()
    .match(/^(?:https?:\/\/)?(?:www\.)?github\.com\/([^/\s]+)\/([^/\s#?]+)/i);

  if (!match) {
    throw new Error("Enter a GitHub repository URL, like github.com/owner/repo.");
  }

  return { owner: match[1], repo: match[2].replace(/\.git$/, "") };
}

on("#repoForm", "submit", async event => {

  event.preventDefault();
  event.stopPropagation();

  const input = $("#repoUrl");
  const value = input ? input.value.trim() : "";

  if (!value) return;

  const button = $("#analyzeButton");
  const originalLabel = button ? button.textContent : "";

  /* Open the workspace immediately so the page never looks like it
     navigated or refreshed while the request is in flight. */
  openAnalyzer();
  showGraphLoading("Opening repository", "Reading repository details…");
  showTreeMessage("Loading repository structure…");

  if (button) {
    button.disabled = true;
    button.textContent = "Reading…";
  }

  try {

    const parsed = parseGitHubUrl(value);

    setText("#repoName", `${parsed.owner}/${parsed.repo}`);
    setText("#project", parsed.repo);

    const data = await api("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: value })
    });

    state.repo.owner = data.owner || parsed.owner;
    state.repo.repo = data.repo || parsed.repo;
    state.repo.branch = data.default_branch || "main";
    state.repo.fullName =
      data.full_name || `${state.repo.owner}/${state.repo.repo}`;

    setText("#repoName", state.repo.fullName);
    setText("#project", data.name || state.repo.repo);
    setText("#branchName", state.repo.branch);
    setText(
      "#projectDescription",
      data.description || "Click a folder to open it. Click a file to see what it touches."
    );

    state.history = [];
    state.selectedId = null;

    /* Start on the structural map; dependencies follow from a file. */
    activateTab("architecture");

    await Promise.all([
      loadArchitecture("", { record: false }),
      loadRepositoryTree()
    ]);

    /* Deliberately not awaited: the graph is already usable. */
    loadRepositorySummary();

  } catch (error) {

    if (error.name === "AbortError") return;

    console.error("Repository analysis failed:", error);
    showGraphError(error.message);
    showTreeMessage("Could not load this repository.");
    setText("#projectDescription", error.message);

  } finally {

    if (button) {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }
});


/* ============================================================
   ARCHITECTURE LEVELS
============================================================ */

function normalizeGraphData(data, defaultEdgeType) {

  const rawNodes = Array.isArray(data.nodes) ? data.nodes : [];
  const rawEdges = Array.isArray(data.edges) ? data.edges : [];

  const nodes = rawNodes.map((node, index) => ({
    id: String(node.id ?? node.path ?? node.label ?? index),
    path: String(node.path ?? node.id ?? ""),
    label: String(node.label ?? node.path ?? node.id ?? index),
    type: node.type || "file",
    role: node.role || "",
    expandable: Boolean(node.expandable),
    important: Boolean(node.important),
    isParent: Boolean(node.isParent),
    language: node.language || "",
    count: Number(
      node.count ??
      node.descendantFileCount ??
      node.fileCount ??
      0
    ),
    fileCount: Number(node.descendantFileCount ?? 0),
    directoryCount: Number(node.descendantDirectoryCount ?? 0)
  }));

  const knownIds = new Set(nodes.map(node => node.id));

  const edges = rawEdges
    .map(edge => ({
      source: String(edge.source ?? ""),
      target: String(edge.target ?? ""),
      type: edge.type || defaultEdgeType
    }))
    .filter(edge => knownIds.has(edge.source) && knownIds.has(edge.target));

  return { nodes, edges, meta: data.meta || {} };
}

function normalizeArchitectureData(data) {
  return normalizeGraphData(data, "contains");
}

/* The function whose absence broke every file click. */
function normalizeDependencyData(data) {

  const normalized = normalizeGraphData(data, "dependency");

  normalized.path = String(data.path || "");
  normalized.structure = data.structure || { functions: [], classes: [] };

  return normalized;
}

async function loadArchitecture(path, options = {}) {

  const { record = true } = options;

  if (record) {
    state.history.push({ view: state.view, path: state.path });
  }

  state.view = "architecture";
  state.path = path;
  state.lastArchitecturePath = path;

  /* Folders are a structural question, so show them in that tab
     wherever the click came from — graph, file list or explorer. */
  activateTab("architecture");
  state.showAllNodes = false;
  state.selectedId = null;

  updateNavigationControls();

  showGraphLoading(
    path ? "Opening folder" : "Building architecture",
    path ? `Reading ${path}…` : "Reading the repository boundary…"
  );

  try {

    const endpoint = path
      ? `/api/repository/architecture/expand?${repoQuery({ path })}`
      : `/api/repository/architecture?${repoQuery()}`;

    const data = normalizeArchitectureData(await api(endpoint));

    /* Repository totals come from the root response and stay put. The
       old code re-read them from every response, so opening a folder
       reset the file count to zero. */
    if (data.meta.totalFiles !== undefined) {
      state.repo.totalFiles = Number(data.meta.totalFiles);
      state.repo.totalDirectories = Number(data.meta.totalDirectories || 0);
    }

    state.graph = { ...data, positions: new Map() };

    renderGraph();
    renderRepositoryFacts();
    renderImportantModules();
    syncTreeToPath(path);
    clearInspector(path);

  } catch (error) {

    if (error.name === "AbortError") return;

    console.error("Architecture load failed:", error);

    /* Roll the history back so Back still points somewhere real. */
    if (record) {
      const previous = state.history.pop();
      if (previous) {
        state.view = previous.view;
        state.path = previous.path;
      }
    }

    updateNavigationControls();
    showGraphError(error.message);
  }
}


/* ============================================================
   FILE DEPENDENCIES
============================================================ */

async function loadFileDependencies(path, options = {}) {

  const { record = true } = options;

  if (record) {
    state.history.push({ view: state.view, path: state.path });
  }

  state.view = "file";
  state.path = path;
  state.selectedId = path;

  activateTab("dependencies");
  state.showAllNodes = false;

  updateNavigationControls();

  showGraphLoading("Reading file", `Parsing ${fileName(path)}…`);

  try {

    const data = normalizeDependencyData(
      await api(
        `/api/repository/file/dependencies?${repoQuery({
          path,
          scope: state.reverseScope
        })}`
      )
    );

    state.graph = { ...data, positions: new Map() };

    renderGraph();
    renderRepositoryFacts();
    syncTreeToPath(path);
    renderFileInspector(path, data);

    /* Neither of these blocks the graph. */
    loadFileSource(path);
    loadFileSummary(path);

  } catch (error) {

    if (error.name === "AbortError") return;

    console.error("File dependency load failed:", error);

    if (record) {
      const previous = state.history.pop();
      if (previous) {
        state.view = previous.view;
        state.path = previous.path;
      }
    }

    updateNavigationControls();
    showGraphError(error.message);
  }
}

async function loadFileSource(path) {

  setText("#codePath", path);
  setText("#code", "Loading…");

  try {

    const response = await fetch(
      `${API_BASE}/api/repository/file?${repoQuery({ path })}`
    );

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || `HTTP ${response.status}`);
    }

    setText("#codePath", `${data.path} · ${data.language || "Unknown"}`);
    setText("#code", data.content || "This file is empty.");

  } catch (error) {
    setText("#codePath", path);
    setText("#code", `This file could not be read.\n\n${error.message}`);
  }
}


/* ============================================================
   NAVIGATION
============================================================ */

function goBack() {

  const previous = state.history.pop();

  if (!previous) return;

  updateNavigationControls();

  if (previous.view === "file") {
    loadFileDependencies(previous.path, { record: false });
  } else {
    loadArchitecture(previous.path, { record: false });
  }
}

function resetGraph() {
  state.history = [];
  loadArchitecture("", { record: false });
}

function openNode(node) {

  if (!node) return;

  if (node.type === "more" || node.type === "filegroup") {
    state.showAllNodes = true;
    renderGraph();
    return;
  }

  if (node.type === "external") return;

  if (node.isParent) {
    goBack();
    return;
  }

  if (node.type === "directory" || node.type === "repository" || node.expandable) {
    loadArchitecture(node.path || node.id);
    return;
  }

  loadFileDependencies(node.path || node.id);
}

function updateNavigationControls() {

  const back = $("#graphBack");
  if (back) back.disabled = state.history.length === 0;

  renderBreadcrumb();
}

function renderBreadcrumb() {

  const container = $("#graphBreadcrumb");
  if (!container) return;

  const segments = state.path ? state.path.split("/") : [];

  const crumbs = [{ label: state.repo.repo || "Repository", path: "" }];

  segments.forEach((segment, index) => {
    crumbs.push({
      label: segment,
      path: segments.slice(0, index + 1).join("/")
    });
  });

  container.innerHTML = "";

  crumbs.forEach((crumb, index) => {

    if (index > 0) {
      const separator = document.createElement("span");
      separator.className = "path-separator";
      separator.textContent = "/";
      container.appendChild(separator);
    }

    const isLast = index === crumbs.length - 1;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "path-crumb" + (isLast ? " path-crumb-current" : "");
    button.textContent = crumb.label;

    if (!isLast) {
      button.addEventListener("click", event => {
        event.preventDefault();
        loadArchitecture(crumb.path);
      });
    }

    container.appendChild(button);
  });
}


/* ============================================================
   NODE SIZING AND LABELS

   Nodes are sized to their text rather than the other way round.
   Clipping every label at a fixed width made four different
   @typescript-eslint packages render as the same node.
============================================================ */

const CHAR_WIDTH = 6.4;          /* DM Mono at 11px */
const NODE_MIN_WIDTH = 118;
const NODE_MAX_WIDTH = 232;
const NODE_PADDING = 26;

const SINGLE_LINE_MAX = 32;      /* fits NODE_MAX_WIDTH on one line */

function splitLabel(label) {

  const slash = Math.max(label.lastIndexOf("/"), label.lastIndexOf("\\"));

  /* Scoped packages read naturally as scope over name, so wrap them
     there even when they would fit on one line — it keeps a column of
     @typescript-eslint/* nodes the same height. */
  if (slash > 0 && slash < label.length - 1 && label.length > 20) {
    return [label.slice(0, slash + 1), label.slice(slash + 1)];
  }

  if (label.length <= SINGLE_LINE_MAX) return [label];

  /* Otherwise break at the separator closest to the middle. Splitting
     mid-word turned ESLintRuleExhaustiveDeps.js into "ESLintRuleExha"
     and "ustiveDeps.js". */
  const middle = Math.floor(label.length / 2);

  let best = -1;

  for (let index = 0; index < label.length - 1; index += 1) {
    if (!"-_.".includes(label[index])) continue;
    if (best === -1 || Math.abs(index - middle) < Math.abs(best - middle)) {
      best = index;
    }
  }

  if (best > 3 && best < label.length - 3) {
    return [label.slice(0, best + 1), label.slice(best + 1)];
  }

  return [label.slice(0, middle), label.slice(middle)];
}

function clip(text, max) {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

function displayLabel(node) {

  if (state.view !== "file") return node.label;
  if (node.type === "external") return node.label;
  if (!node.path || node.path === state.path) return node.label;

  return relativeLabel(state.path, node.path);
}

function nodeLines(node) {
  return splitLabel(displayLabel(node)).map(line => clip(line, 32));
}

function nodeSize(node) {

  const lines = nodeLines(node);

  const widest = lines.reduce(
    (max, line) => Math.max(max, line.length),
    0
  );

  const subtitle = nodeSubtitle(node);

  const width = Math.min(
    NODE_MAX_WIDTH,
    Math.max(
      NODE_MIN_WIDTH,
      Math.max(widest, subtitle.length * 0.82) * CHAR_WIDTH + NODE_PADDING
    )
  );

  const height = (lines.length > 1 ? 26 : 0) + (subtitle ? 54 : 42);

  return { w: Math.round(width), h: height, lines, subtitle };
}


/* ============================================================
   LAYOUT

   Each entry in the positions map carries its own size, so edges
   can stop at the node's boundary instead of running under it.
============================================================ */

function measureAll(nodes) {

  const sizes = new Map();

  nodes.forEach(node => sizes.set(node.id, nodeSize(node)));

  return sizes;
}

function layoutHierarchy(nodes, edges, sizes) {

  const positions = new Map();

  const parent = nodes.find(
    node => node.isParent || node.type === "repository"
  );

  const children = nodes.filter(node => node !== parent);

  const put = (node, x, y) => {
    const size = sizes.get(node.id);
    positions.set(node.id, { x, y, w: size.w, h: size.h });
  };

  if (parent) put(parent, 0, 0);

  if (!children.length) return positions;

  /* Up to five children read best as one horizontal row — the shape
     of the mockup. Beyond that, a grid biased wider than tall, since
     the canvas is landscape. */
  const columns =
    children.length <= 5
      ? children.length
      : Math.max(1, Math.min(6, Math.ceil(Math.sqrt(children.length * 1.6))));

  const widest = children.reduce(
    (max, node) => Math.max(max, sizes.get(node.id).w),
    0
  );

  const tallest = children.reduce(
    (max, node) => Math.max(max, sizes.get(node.id).h),
    0
  );

  const columnGap = widest + 76;
  const rowGap = tallest + 84;

  const startY = parent ? rowGap * 1.15 : 0;

  children.forEach((node, index) => {

    const column = index % columns;
    const row = Math.floor(index / columns);

    const itemsInRow = Math.min(columns, children.length - row * columns);
    const rowStart = -((itemsInRow - 1) * columnGap) / 2;

    put(node, rowStart + column * columnGap, startY + row * rowGap);
  });

  return positions;
}

function layoutFocus(nodes, edges, centerId, sizes) {

  const positions = new Map();

  const byId = new Map(nodes.map(node => [node.id, node]));

  const put = (id, x, y) => {
    const size = sizes.get(id);
    if (size) positions.set(id, { x, y, w: size.w, h: size.h });
  };

  const importers = edges
    .filter(edge => edge.target === centerId)
    .map(edge => edge.source);

  const outgoing = edges
    .filter(edge => edge.source === centerId)
    .map(edge => edge.target);

  /* Repository files and external packages get their own columns.
     Mixing them made a dozen identical dashed boxes fan out from one
     point with nothing to tell them apart. */
  const internal = outgoing.filter(
    id => (byId.get(id) || {}).type !== "external"
  );

  const external = outgoing.filter(
    id => (byId.get(id) || {}).type === "external"
  );

  const columnWidth = id => (sizes.get(id) || { w: NODE_MIN_WIDTH }).w;

  const widest = ids =>
    ids.reduce((max, id) => Math.max(max, columnWidth(id)), NODE_MIN_WIDTH);

  const centerSize = sizes.get(centerId) || { w: NODE_MIN_WIDTH, h: 56 };

  put(centerId, 0, 0);

  /* A single tall column of a dozen packages forces the fit-to-screen
     scale down to about 0.45, which is what made the labels
     unreadable. Wrapping into columns keeps the block roughly square. */
  const MAX_PER_COLUMN = 7;

  const stack = (ids, x, direction) => {

    if (!ids.length) return 0;

    const gap = ids.reduce(
      (max, id) => Math.max(max, (sizes.get(id) || { h: 48 }).h),
      0
    ) + 18;

    const columnCount = Math.ceil(ids.length / MAX_PER_COLUMN);
    const perColumn = Math.ceil(ids.length / columnCount);
    const columnGap = widest(ids) + 48;

    ids.forEach((id, index) => {

      const column = Math.floor(index / perColumn);
      const row = index % perColumn;
      const inColumn = Math.min(perColumn, ids.length - column * perColumn);
      const total = (inColumn - 1) * gap;

      put(
        id,
        x + direction * column * columnGap,
        -total / 2 + row * gap
      );
    });

    return columnCount;
  };

  const leftX = -(centerSize.w / 2 + widest(importers) / 2 + 130);

  const internalX = centerSize.w / 2 + widest(internal) / 2 + 130;

  const externalX = internal.length
    ? internalX + widest(internal) / 2 + widest(external) / 2 + 90
    : centerSize.w / 2 + widest(external) / 2 + 130;

  const importerColumns = stack(importers, leftX, -1);
  const internalColumns = stack(internal, internalX, 1);

  /* Push the external column clear of however many columns the
     internal imports needed. */
  stack(
    external,
    externalX + (internalColumns > 1 ? (internalColumns - 1) * (widest(internal) + 48) : 0),
    1
  );

  /* Anything unconnected still gets a home rather than vanishing. */
  let stray = 0;

  nodes.forEach(node => {
    if (!positions.has(node.id)) {
      put(node.id, 0, 160 + stray * 70);
      stray += 1;
    }
  });

  return positions;
}


/* ============================================================
   EDGE GEOMETRY

   An edge starts and ends on the boundary of its node, not at its
   centre — otherwise the arrowhead is drawn on top of the label,
   which is what made the focus view unreadable.
============================================================ */

function boundaryPoint(box, towards, inset) {

  const dx = towards.x - box.x;
  const dy = towards.y - box.y;

  if (dx === 0 && dy === 0) return { x: box.x, y: box.y };

  const halfWidth = box.w / 2 + inset;
  const halfHeight = box.h / 2 + inset;

  const scale = Math.min(
    halfWidth / (Math.abs(dx) || 0.0001),
    halfHeight / (Math.abs(dy) || 0.0001)
  );

  return { x: box.x + dx * scale, y: box.y + dy * scale };
}

/* Containment is drawn as right-angle connectors on a shared bus —
   the shape of the mockup on the landing page. A curve implies flow;
   a bus implies "these belong to that". */
function orthogonalPath(from, to, spineX) {

  const startY = from.y + from.h / 2;
  const endY = to.y - to.h / 2;
  const busY = endY - 26;

  /* Directly beneath the parent: one straight drop. */
  if (Math.abs(to.x - from.x) < 1.5 && spineX === null) {
    return `M ${from.x} ${startY} L ${to.x} ${endY}`;
  }

  /* Single row: symmetrical bus straight under the parent. */
  if (spineX === null) {
    return `M ${from.x} ${startY} ` +
           `L ${from.x} ${busY} ` +
           `L ${to.x} ${busY} ` +
           `L ${to.x} ${endY}`;
  }

  /* Several rows: run a spine down the left margin so the drops to
     lower rows never pass through the nodes above them. */
  return `M ${from.x} ${startY} ` +
         `L ${from.x} ${startY + 22} ` +
         `L ${spineX} ${startY + 22} ` +
         `L ${spineX} ${busY} ` +
         `L ${to.x} ${busY} ` +
         `L ${to.x} ${endY}`;
}

function edgePath(from, to) {

  const start = boundaryPoint(from, to, 2);
  const end = boundaryPoint(to, from, 6);

  const dx = end.x - start.x;
  const dy = end.y - start.y;

  /* Curve along the dominant axis. Straight radial lines from one
     point produce a fan of crossing diagonals; a gentle curve reads
     as separate paths. */
  if (Math.abs(dx) >= Math.abs(dy)) {
    const bend = dx * 0.42;
    return `M ${start.x} ${start.y} ` +
           `C ${start.x + bend} ${start.y}, ` +
           `${end.x - bend} ${end.y}, ${end.x} ${end.y}`;
  }

  const bend = dy * 0.42;

  return `M ${start.x} ${start.y} ` +
         `C ${start.x} ${start.y + bend}, ` +
         `${end.x} ${end.y - bend}, ${end.x} ${end.y}`;
}


/* ============================================================
   GRAPH RENDERING
============================================================ */

let activeSvg = null;
let activeViewport = null;

/* ============================================================
   STRUCTURAL FILTER

   A repository root is mostly config files — react's has six folders
   and twenty-seven dotfiles. Drawing all of them as equal boxes
   buries the structure, so the architecture graph shows folders and
   landmarks, and the rest collapse into one node. Every file is still
   listed in the FILES panel beside it.
============================================================ */

function collapseLooseFiles(nodes, edges) {

  if (state.view !== "architecture" || state.showAllNodes) {
    return { nodes, edges };
  }

  const parentId = state.path || "repository";

  const kept = [];
  const collapsed = [];

  nodes.forEach(node => {

    const isStructural =
      node.isParent ||
      node.type === "repository" ||
      node.type === "directory" ||
      node.important;

    if (isStructural) {
      kept.push(node);
    } else {
      collapsed.push(node);
    }
  });

  /* Not worth a summary node for a couple of files. */
  if (collapsed.length <= 3) return { nodes, edges };

  const keptIds = new Set(kept.map(node => node.id));

  const groupNode = {
    id: "__files__",
    path: "",
    label: plural(collapsed.length, "other file"),
    type: "filegroup",
    language: "",
    count: collapsed.length
  };

  return {
    nodes: [...kept, groupNode],
    edges: [
      ...edges.filter(
        edge => keptIds.has(edge.source) && keptIds.has(edge.target)
      ),
      { source: parentId, target: "__files__", type: "contains" }
    ]
  };
}

function visibleNodesForLevel() {

  const filter = state.graphFilter.trim().toLowerCase();

  const nodes = filter
    ? state.graph.nodes.filter(
        node =>
          node.isParent ||
          node.label.toLowerCase().includes(filter) ||
          node.path.toLowerCase().includes(filter)
      )
    : state.graph.nodes;

  if (state.view === "file" || state.showAllNodes) {
    return { nodes, hidden: 0 };
  }

  if (nodes.length <= VISIBLE_NODE_LIMIT) {
    return { nodes, hidden: 0 };
  }

  return {
    nodes: nodes.slice(0, VISIBLE_NODE_LIMIT),
    hidden: nodes.length - VISIBLE_NODE_LIMIT
  };
}

function renderGraph() {

  const canvas = $(activePane().canvas);
  if (!canvas) return;

  canvas.innerHTML = "";

  const { nodes: levelNodes, hidden } = visibleNodesForLevel();

  const nodes = levelNodes.slice();

  if (hidden > 0) {
    nodes.push({
      id: "__more__",
      path: "",
      label: `Show ${hidden} more`,
      type: "more",
      language: "",
      count: hidden
    });
  }

  if (!nodes.length) {
    canvas.innerHTML = `
      <div class="graph-empty">
        <p class="graph-empty-title">Nothing to draw here.</p>
        <p class="graph-empty-message">This folder has no readable files.</p>
      </div>
    `;
    setText("#depNodeCount", "0");
    setText("#depEdgeCount", "0");
    return;
  }

  const visibleIds = new Set(nodes.map(node => node.id));

  const rawEdges = state.graph.edges.filter(
    edge => visibleIds.has(edge.source) && visibleIds.has(edge.target)
  );

  const structural = collapseLooseFiles(nodes, rawEdges);

  const drawNodes = structural.nodes;
  const edges = structural.edges;

  const sizes = measureAll(drawNodes);

  const positions =
    state.view === "file"
      ? layoutFocus(drawNodes, edges, state.path, sizes)
      : layoutHierarchy(drawNodes, edges, sizes);

  state.graph.positions = positions;

  setText(
    "#depNodeCount",
    hidden > 0
      ? `${levelNodes.length} of ${state.graph.nodes.length}`
      : String(state.graph.nodes.length)
  );

  setText("#depEdgeCount", String(state.graph.edges.length));

  const svg = svgEl("svg", {
    class: "arch-graph",
    width: "100%",
    height: "100%"
  });

  const defs = svgEl("defs");

  ["contains", "dependency", "external"].forEach(kind => {
    const marker = svgEl("marker", {
      id: `arrow-${kind}`,
      viewBox: "0 0 10 10",
      refX: "8",
      refY: "5",
      markerWidth: "5",
      markerHeight: "5",
      orient: "auto-start-reverse"
    });
    marker.appendChild(
      svgEl("path", { d: "M 0 1 L 9 5 L 0 9 z", class: `arrowhead ${kind}` })
    );
    defs.appendChild(marker);
  });

  svg.appendChild(defs);

  const viewport = svgEl("g", { class: "viewport" });
  const edgeLayer = svgEl("g", { class: "edge-layer" });
  const nodeLayer = svgEl("g", { class: "node-layer" });

  /* --- edges --- */

  /* How many distinct rows the children occupy decides whether a
     symmetrical bus is safe or a left spine is needed. */
  const childRows = new Set();

  positions.forEach((position, id) => {
    if (id !== state.path && id !== "repository") childRows.add(Math.round(position.y));
  });

  let spineX = null;

  if (state.view === "architecture" && childRows.size > 1) {
    let leftmost = Infinity;
    positions.forEach(position => {
      leftmost = Math.min(leftmost, position.x - position.w / 2);
    });
    spineX = leftmost - 38;
  }

  edges.forEach(edge => {

    const from = positions.get(edge.source);
    const to = positions.get(edge.target);

    if (!from || !to) return;

    const isContainment =
      edge.type === "contains" && to.y > from.y + from.h / 2;

    const path = svgEl("path", {
      class: `graph-edge edge-${edge.type}`,
      d: isContainment
        ? orthogonalPath(from, to, spineX)
        : edgePath(from, to),
      fill: "none"
    });

    /* Containment needs no arrowhead — the mockup has none, and the
       direction is obvious from the layout. */
    if (!isContainment) {
      path.setAttribute("marker-end", `url(#arrow-${edge.type})`);
    }

    path.dataset.source = edge.source;
    path.dataset.target = edge.target;

    edgeLayer.appendChild(path);
  });

  /* --- nodes --- */

  drawNodes.forEach(node => {

    const position = positions.get(node.id);
    if (!position) return;

    const size = sizes.get(node.id);

    const group = svgEl("g", {
      class: `graph-node node-${node.type}`,
      transform: `translate(${position.x}, ${position.y})`,
      tabindex: "0",
      role: "button"
    });

    group.dataset.id = node.id;

    if (node.important) group.classList.add("is-important");
    if (node.isParent) group.classList.add("is-parent");
    if (node.role) group.classList.add(`role-${node.role}`);

    group.appendChild(svgEl("rect", {
      class: "node-body",
      x: -size.w / 2,
      y: -size.h / 2,
      width: size.w,
      height: size.h,
      rx: state.view === "architecture" ? 2 : 7
    }));

    /* Title, one or two lines, centred as a block. */
    const hasSubtitle = Boolean(size.subtitle);
    const lineHeight = 15;
    const titleBlock = (size.lines.length - 1) * lineHeight;
    const titleTop = hasSubtitle
      ? -titleBlock / 2 - 3
      : -titleBlock / 2 + 4;

    size.lines.forEach((line, index) => {

      const text = svgEl("text", {
        class: "graph-node-title",
        x: 0,
        y: titleTop + index * lineHeight,
        "text-anchor": "middle"
      });

      text.textContent = line;
      group.appendChild(text);
    });

    if (hasSubtitle) {

      const subtitle = svgEl("text", {
        class: "graph-node-subtitle",
        x: 0,
        y: titleTop + titleBlock + 17,
        "text-anchor": "middle"
      });

      subtitle.textContent = size.subtitle;
      group.appendChild(subtitle);
    }

    const tooltip = svgEl("title");
    tooltip.textContent = node.path || node.label;
    group.appendChild(tooltip);

    group.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();

      if (node.type === "more" || node.type === "filegroup") {
        openNode(node);
        return;
      }

      selectNode(node.id);
    });

    group.addEventListener("dblclick", event => {
      event.preventDefault();
      event.stopPropagation();
      openNode(node);
    });

    group.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openNode(node);
      }
    });

    nodeLayer.appendChild(group);
  });

  viewport.appendChild(edgeLayer);
  viewport.appendChild(nodeLayer);
  svg.appendChild(viewport);
  canvas.appendChild(svg);

  activeSvg = svg;
  activeViewport = viewport;

  attachCameraTo(canvas);

  fitToContent();
  renderLevelFiles();
  updateNavigationControls();

  if (state.selectedId) applySelectionHighlight();
}

function nodeSubtitle(node) {

  if (node.type === "more") return "click to reveal";
  if (node.type === "filegroup") return "click to show them";

  /* A grouped node must say how much it stands for, otherwise the
     tidier graph is just a less informative one. */
  if (node.type === "external") {
    return node.count > 1 ? plural(node.count, "import") : "";
  }

  if (node.isParent) return "back to parent";

  if (node.type === "directory" || node.type === "repository") {
    if (node.fileCount || node.directoryCount) {
      return `${node.fileCount} files · ${node.directoryCount} folders`;
    }
    return "folder";
  }

  if (node.role === "selected") return "selected";
  if (node.role === "importer") return "imports this";

  if (node.role === "import" && node.count > 1) {
    return plural(node.count, "import");
  }

  return node.language || "";
}


/* ============================================================
   SELECTION AND HIGHLIGHTING
============================================================ */

function selectNode(id) {

  state.selectedId = id;
  applySelectionHighlight();

  const node = state.graph.nodes.find(item => item.id === id);
  if (node) renderNodeInspector(node);

  document.querySelectorAll(".level-file").forEach(button => {
    button.classList.toggle(
      "active",
      button.querySelector(".level-file-name").textContent ===
        (node ? node.label : null)
    );
  });
}

function applySelectionHighlight() {

  if (!activeViewport) return;

  const id = state.selectedId;

  const connected = new Set([id]);

  state.graph.edges.forEach(edge => {
    if (edge.source === id) connected.add(edge.target);
    if (edge.target === id) connected.add(edge.source);
  });

  activeViewport.querySelectorAll(".graph-node").forEach(element => {
    const nodeId = element.dataset.id;
    element.classList.toggle("is-selected", nodeId === id);
    element.classList.toggle("is-linked", nodeId !== id && connected.has(nodeId));
    element.classList.toggle("is-dimmed", Boolean(id) && !connected.has(nodeId));
  });

  activeViewport.querySelectorAll(".graph-edge").forEach(element => {
    const isLinked =
      element.dataset.source === id || element.dataset.target === id;
    element.classList.toggle("is-linked", isLinked);
    element.classList.toggle("is-dimmed", Boolean(id) && !isLinked);
  });
}

function clearSelection() {

  state.selectedId = null;

  if (!activeViewport) return;

  activeViewport
    .querySelectorAll(".graph-node, .graph-edge")
    .forEach(element => {
      element.classList.remove("is-selected", "is-linked", "is-dimmed");
    });
}


/* ============================================================
   CAMERA — pan, zoom, fit

   The transform lives on a single <g>, so panning never resizes the
   canvas or reflows the page.
============================================================ */

function applyCamera() {

  if (!activeViewport) return;

  const { x, y, scale } = state.camera;

  activeViewport.setAttribute(
    "transform",
    `translate(${x}, ${y}) scale(${scale})`
  );

  setText("#zoomLevel", `${Math.round(scale * 100)}%`);
}

function contentBounds() {

  const positions = state.graph.positions;

  if (!positions || positions.size === 0) return null;

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  positions.forEach(position => {
    const halfWidth = (position.w || NODE_MIN_WIDTH) / 2;
    const halfHeight = (position.h || 48) / 2;
    minX = Math.min(minX, position.x - halfWidth);
    maxX = Math.max(maxX, position.x + halfWidth);
    minY = Math.min(minY, position.y - halfHeight);
    maxY = Math.max(maxY, position.y + halfHeight);
  });

  return { minX, minY, maxX, maxY };
}

function fitToContent() {

  const canvas = $(activePane().canvas);
  const bounds = contentBounds();

  if (!canvas || !bounds) return;

  const width = canvas.clientWidth;
  const height = canvas.clientHeight;

  /* The canvas is inside a tab. While that tab is hidden it measures
     zero, and fitting against zero produces a graph parked off-screen.
     Refit when the tab becomes visible instead. */
  if (!width || !height) return;

  const padding = 48;

  const contentWidth = bounds.maxX - bounds.minX;
  const contentHeight = bounds.maxY - bounds.minY;

  /* Below roughly 0.6 the 11px labels stop being readable, so stop
     shrinking and let the user pan instead of showing an unreadable
     whole. */
  const LEGIBLE_MIN = 0.6;

  const scale = Math.max(
    LEGIBLE_MIN,
    Math.min(
      1,
      (width - padding * 2) / Math.max(contentWidth, 1),
      (height - padding * 2) / Math.max(contentHeight, 1)
    )
  );

  state.camera = {
    scale,
    x: width / 2 - ((bounds.minX + bounds.maxX) / 2) * scale,
    y: height / 2 - ((bounds.minY + bounds.maxY) / 2) * scale
  };

  applyCamera();
}

function zoomBy(factor, origin) {

  const canvas = $(activePane().canvas);
  if (!canvas) return;

  const rect = canvas.getBoundingClientRect();

  const pointX = origin ? origin.x - rect.left : rect.width / 2;
  const pointY = origin ? origin.y - rect.top : rect.height / 2;

  const previous = state.camera.scale;
  const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, previous * factor));

  if (next === previous) return;

  /* Keep the point under the cursor stationary while zooming. */
  state.camera.x = pointX - ((pointX - state.camera.x) * next) / previous;
  state.camera.y = pointY - ((pointY - state.camera.y) * next) / previous;
  state.camera.scale = next;

  applyCamera();
}

/* One drag handler for the lifetime of the page. The previous version
   registered a fresh pair of window listeners on every render. */
const drag = { active: false, startX: 0, startY: 0, originX: 0, originY: 0 };

function initCameraControls() {

  document
    .querySelectorAll(".graph-canvas")
    .forEach(attachCameraTo);
}

function attachCameraTo(canvas) {

  if (!canvas || canvas.dataset.cameraReady) return;

  canvas.dataset.cameraReady = "1";

  canvas.addEventListener("mousedown", event => {

    if (event.target.closest(".graph-node")) return;

    drag.active = true;
    drag.startX = event.clientX;
    drag.startY = event.clientY;
    drag.originX = state.camera.x;
    drag.originY = state.camera.y;

    canvas.classList.add("dragging");
  });

  window.addEventListener("mousemove", event => {

    if (!drag.active) return;

    state.camera.x = drag.originX + (event.clientX - drag.startX);
    state.camera.y = drag.originY + (event.clientY - drag.startY);

    applyCamera();
  });

  window.addEventListener("mouseup", () => {
    drag.active = false;
    canvas.classList.remove("dragging");
  });

  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    zoomBy(event.deltaY < 0 ? 1.12 : 1 / 1.12, { x: event.clientX, y: event.clientY });
  }, { passive: false });

  /* Clicking empty canvas clears the highlight. */
  canvas.addEventListener("click", event => {
    if (event.target.closest(".graph-node")) return;
    clearSelection();
    clearInspector(state.path);
  });
}


/* ============================================================
   TOOLBAR

   Controls are created only if index.html does not already provide
   them, so this works with the existing markup either way.
============================================================ */

function ensureToolbar() {

  const canvas = $("#architectureGraph") || $("#dependencyGraph");
  if (!canvas) return;

  /* index.html already has a .graph-toolbar holding the breadcrumb and
     the node/edge counts. Rather than fight its layout, the controls go
     in their own strip directly above the canvas. */
  let bar = $("#graphControls");

  if (!bar) {
    bar = document.createElement("div");
    bar.id = "graphControls";
    bar.className = "graph-controls";
    canvas.parentNode.insertBefore(bar, canvas);
  }

  const left = document.createElement("div");
  left.className = "control-group";

  const right = document.createElement("div");
  right.className = "control-group";

  bar.appendChild(left);
  bar.appendChild(right);

  const addButton = (id, label, title, handler, group) => {

    /* Reuse the button index.html already provides (Back usually lives
       in .workspace-actions), otherwise create one here. */
    let button = document.getElementById(id);

    if (!button) {
      button = document.createElement("button");
      button.id = id;
      button.type = "button";
      button.className = "graph-control";
      button.textContent = label;
      group.appendChild(button);
    } else {
      button.type = "button";
    }

    button.title = title;

    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      handler();
    });

    return button;
  };

  addButton("graphBack", "\u2190 Back", "Go up one level", goBack, left);
  addButton("graphReset", "Reset", "Return to the repository view", resetGraph, left);

  addButton("zoomOut", "\u2212", "Zoom out", () => zoomBy(1 / 1.2), right);

  if (!$("#zoomLevel")) {
    const level = document.createElement("span");
    level.id = "zoomLevel";
    level.className = "zoom-level";
    level.textContent = "100%";
    right.appendChild(level);
  }

  addButton("zoomIn", "+", "Zoom in", () => zoomBy(1.2), right);
  addButton("zoomFit", "Fit", "Fit the graph to the panel", fitToContent, right);

  /* index.html already has a "Reset view" button in the graph toolbar. */
  const existingReset = document.getElementById("resetGraph");

  if (existingReset) {
    existingReset.type = "button";
    existingReset.addEventListener("click", event => {
      event.preventDefault();
      resetGraph();
    });
  }

  /* If the markup has no breadcrumb at all, add one to the strip. */
  if (!$("#graphBreadcrumb")) {
    const crumbs = document.createElement("div");
    crumbs.id = "graphBreadcrumb";
    crumbs.className = "graph-breadcrumb";
    bar.insertBefore(crumbs, right);
  }
}


/* ============================================================
   REPOSITORY TREE SIDEBAR

   Fetched once. Expanding a folder is local — no further requests.
============================================================ */

async function loadRepositoryTree() {

  showTreeMessage("Loading repository structure…");

  try {

    const response = await fetch(
      `${API_BASE}/api/repository/tree?${repoQuery()}`
    );

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || `HTTP ${response.status}`);
    }

    state.tree.files = data.files || [];
    state.tree.directories = data.directories || [];
    state.tree.expanded = new Set([""]);

    if (data.meta && data.meta.totalFiles !== undefined) {
      state.repo.totalFiles = Number(data.meta.totalFiles);
      state.repo.totalDirectories = Number(data.meta.totalDirectories);
    }

    ensureTreeFilter();
    renderTree();
    renderRepositoryFacts();

  } catch (error) {
    console.error("Tree load failed:", error);
    showTreeMessage("Repository structure could not be loaded.");
  }
}

function buildTreeIndex() {

  /* parent path -> [{ name, path, type }] */
  const children = new Map();

  const add = (path, type) => {

    const parent = parentPath(path);

    if (!children.has(parent)) children.set(parent, []);

    children.get(parent).push({
      name: fileName(path),
      path,
      type
    });
  };

  state.tree.directories.forEach(path => add(path, "directory"));
  state.tree.files.forEach(path => add(path, "file"));

  children.forEach(list => {
    list.sort((a, b) => {
      if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
  });

  return children;
}

function renderTree() {

  const container = $("#tree");
  if (!container) return;

  container.innerHTML = "";

  const filter = state.tree.filter.trim().toLowerCase();

  if (filter) {

    const matches = state.tree.files
      .filter(path => path.toLowerCase().includes(filter))
      .slice(0, 200);

    if (!matches.length) {
      showTreeMessage(`No file matches “${filter}”.`);
      return;
    }

    matches.forEach(path => {
      container.appendChild(
        treeRow({ name: path, path, type: "file" }, 0, false)
      );
    });

    return;
  }

  const index = buildTreeIndex();

  const walk = (parent, depth) => {

    (index.get(parent) || []).forEach(entry => {

      const expanded = state.tree.expanded.has(entry.path);

      container.appendChild(treeRow(entry, depth, expanded));

      if (entry.type === "directory" && expanded) {
        walk(entry.path, depth + 1);
      }
    });
  };

  walk("", 0);

  if (!container.children.length) {
    showTreeMessage("This repository has no readable files.");
  }
}

function treeRow(entry, depth, expanded) {

  const button = document.createElement("button");

  button.type = "button";
  button.className = "tree-button";
  button.style.paddingLeft = `${8 + depth * 14}px`;
  button.dataset.path = entry.path;

  if (entry.path === state.path) button.classList.add("active");

  const arrow = document.createElement("span");
  arrow.className = "tree-arrow";
  arrow.textContent =
    entry.type === "directory" ? (expanded ? "▾" : "▸") : "·";

  const label = document.createElement("span");
  label.className = "tree-label";
  label.textContent = entry.name;

  button.appendChild(arrow);
  button.appendChild(label);

  button.addEventListener("click", event => {

    event.preventDefault();
    event.stopPropagation();

    if (entry.type === "directory") {

      if (state.tree.expanded.has(entry.path)) {
        state.tree.expanded.delete(entry.path);
      } else {
        state.tree.expanded.add(entry.path);
      }

      renderTree();

      /* On the Dependencies tab a folder is not something to open, it
         is a way to narrow the list of files you can trace. Opening
         the structural graph here would throw the user into the other
         tab, which is not what clicking a folder should do. */
      if (state.tab === "dependencies") {
        state.pickerFolder = entry.path;
        renderDependencyPicker();
        return;
      }

      loadArchitecture(entry.path);
      return;
    }

    loadFileDependencies(entry.path);
  });

  return button;
}

function syncTreeToPath(path) {

  /* Open every ancestor so the current location is visible. */
  let current = parentPath(path);

  while (current) {
    state.tree.expanded.add(current);
    current = parentPath(current);
  }

  if (path && state.tree.directories.includes(path)) {
    state.tree.expanded.add(path);
  }

  if (state.tree.files.length || state.tree.directories.length) {
    renderTree();

    const active = document.querySelector(`.tree-button[data-path="${CSS.escape(path)}"]`);
    if (active) active.scrollIntoView({ block: "nearest" });
  }
}

function ensureTreeFilter() {

  if ($("#treeFilter")) return;

  const tree = $("#tree");
  if (!tree) return;

  const input = document.createElement("input");

  input.id = "treeFilter";
  input.type = "search";
  input.className = "tree-filter";
  input.placeholder = "Filter files";
  input.autocomplete = "off";

  input.addEventListener("input", () => {
    state.tree.filter = input.value;
    renderTree();
  });

  tree.parentNode.insertBefore(input, tree);
}


/* ============================================================
   INSPECTOR
============================================================ */

function renderRepositoryFacts() {

  const facts = $("#facts");

  if (facts) {
    facts.innerHTML = `
      <span class="fact"><span class="fact-label">Files</span>
        ${state.repo.totalFiles.toLocaleString()}</span>
      <span class="fact"><span class="fact-label">Folders</span>
        ${state.repo.totalDirectories.toLocaleString()}</span>
      <span class="fact"><span class="fact-label">Viewing</span>
        ${escapeHtml(state.path || state.repo.repo || "repository")}</span>
    `;
  }

  setText("#totalFiles", state.repo.totalFiles.toLocaleString());
  setText("#metricFiles", state.repo.totalFiles.toLocaleString());
  setText("#metricDirectories", state.repo.totalDirectories.toLocaleString());
}

function clearInspector(path) {

  setText("#depTitle", path ? fileName(path) : state.repo.repo || "Repository");
  setText(
    "#depDesc",
    "Click a node to see what it connects to. Double-click to open it."
  );

  setHtml("#imports", "<li class='muted'>Nothing selected</li>");
  setHtml("#usedBy", "<li class='muted'>Nothing selected</li>");
}

function renderNodeInspector(node) {

  setText("#depTitle", node.label);

  const outgoing = state.graph.edges
    .filter(edge => edge.source === node.id)
    .map(edge => edge.target);

  const incoming = state.graph.edges
    .filter(edge => edge.target === node.id)
    .map(edge => edge.source);

  if (state.view === "architecture") {
    setText(
      "#depDesc",
      node.type === "directory"
        ? `${plural(node.fileCount, "file")} and ${plural(node.directoryCount, "folder")} inside. Double-click to open.`
        : `${node.language || "File"} · double-click to trace its dependencies.`
    );
  } else {
    setText(
      "#depDesc",
      `${plural(outgoing.length, "outgoing link")} · ${plural(incoming.length, "incoming link")}.`
    );
  }

  renderList("#imports", outgoing, "Depends on nothing in this view");
  renderList("#usedBy", incoming, "Nothing here points at this");
}

function ensureImportsLabel() {

  if ($("#importsLabel")) return;

  const list = $("#imports");
  if (!list) return;

  const label = document.createElement("div");
  label.id = "importsLabel";
  label.className = "column-count";

  list.parentNode.insertBefore(label, list);
}

function renderFileInspector(path, data) {

  ensureImportsLabel();

  const imports = data.edges
    .filter(edge => edge.source === path)
    .map(edge => edge.target);

  const importers = data.edges
    .filter(edge => edge.target === path)
    .map(edge => edge.source);

  setText("#depTitle", fileName(path));

  const meta = data.meta || {};

  if (meta.analyzable === false) {
    setText("#depDesc", meta.reason || "This file type is not parsed yet.");
  } else {
    const scanned =
      state.reverseScope === "repository"
        ? "whole repository"
        : "this folder";
    setText(
      "#depDesc",
      `Imports ${plural(imports.length, "thing")} · ` +
      `${plural(importers.length, "file")} in ${scanned} ` +
      `${importers.length === 1 ? "imports" : "import"} it.`
    );
  }

  const files = imports.filter(id => !id.startsWith("external:"));
  const packages = imports.filter(id => id.startsWith("external:"));

  renderList(
    "#imports",
    [...files, ...packages],
    "No imports resolved"
  );

  renderList("#usedBy", importers, "No callers found in this scope");

  setText(
    "#importsLabel",
    `${plural(files.length, "file")} · ${plural(packages.length, "package")}`
  );

  ensureScopeControl();
}

function renderList(selector, items, emptyMessage) {

  const container = $(selector);
  if (!container) return;

  if (!items.length) {
    container.innerHTML = `<li class="muted">${escapeHtml(emptyMessage)}</li>`;
    return;
  }

  container.innerHTML = "";

  items.forEach(item => {

    const li = document.createElement("li");
    const isExternal = item.startsWith("external:");

    if (isExternal) {
      li.className = "dep-external";
      li.textContent = item.replace(/^external:/, "");
      container.appendChild(li);
      return;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "dep-link";
    button.textContent =
      state.view === "file" ? relativeLabel(state.path, item) : item;
    button.title = item;

    button.addEventListener("click", event => {
      event.preventDefault();
      loadFileDependencies(item);
    });

    li.appendChild(button);
    container.appendChild(li);
  });
}

function ensureScopeControl() {

  if ($("#scopeToggle")) return;

  const anchor = $("#usedBy");
  if (!anchor) return;

  const button = document.createElement("button");

  button.id = "scopeToggle";
  button.type = "button";
  button.className = "scope-toggle";
  button.textContent = "Search the whole repository for callers";

  button.addEventListener("click", event => {

    event.preventDefault();

    state.reverseScope =
      state.reverseScope === "directory" ? "repository" : "directory";

    button.textContent =
      state.reverseScope === "repository"
        ? "Search only this folder for callers"
        : "Search the whole repository for callers";

    if (state.view === "file") {
      loadFileDependencies(state.path, { record: false });
    }
  });

  anchor.parentNode.insertBefore(button, anchor.nextSibling);
}


/* ============================================================
   SUMMARIES

   A one-line description of what a file or repository does, fetched
   separately so the graph never waits on it. If the backend has no
   model key, or hits its daily limit, nothing appears and nothing
   breaks.
============================================================ */

const summaryCache = new Map();

/* In-flight requests, so clicking the same file repeatedly does not
   spend the server's per-client allowance on identical questions. */
const summaryPending = new Map();

/* Set when the server says we are over a limit. Nothing is requested
   again until it passes. */
let summaryPausedUntil = 0;

async function fetchSummary(url, key) {

  if (summaryCache.has(key)) return summaryCache.get(key);

  if (Date.now() < summaryPausedUntil) return null;

  if (summaryPending.has(key)) return summaryPending.get(key);

  const request = (async () => {

    try {

      const response = await fetch(url);

      if (response.status === 429 || response.status === 503) {
        const retry = Number(response.headers.get("Retry-After")) || 60;
        summaryPausedUntil = Date.now() + retry * 1000;
        return null;
      }

      const data = await response.json();

      if (data.summary) summaryCache.set(key, data.summary);

      return data.summary || null;

    } catch {
      return null;
    } finally {
      summaryPending.delete(key);
    }
  })();

  summaryPending.set(key, request);

  return request;
}

function setSummary(selector, text, pending) {

  const host = $(selector);
  if (!host) return;

  host.textContent = text || "";
  host.classList.toggle("is-pending", Boolean(pending));
  host.hidden = !text && !pending;
}

function ensureSummarySlot(id, afterSelector) {

  if ($(`#${id}`)) return;

  const anchor = $(afterSelector);
  if (!anchor) return;

  const line = document.createElement("p");
  line.id = id;
  line.className = "summary-line";
  line.hidden = true;

  anchor.parentNode.insertBefore(line, anchor.nextSibling);
}

async function loadRepositorySummary() {

  ensureSummarySlot("repoSummary", "#project");

  const key = `repo:${state.repo.fullName}`;

  if (summaryCache.has(key)) {
    setSummary("#repoSummary", summaryCache.get(key), false);
    return;
  }

  setSummary("#repoSummary", "Reading the repository…", true);

  const summary = await fetchSummary(
    `${API_BASE}/api/repository/summary?${repoQuery()}`,
    key
  );

  setSummary("#repoSummary", summary, false);
}

async function loadFileSummary(path) {

  ensureSummarySlot("fileSummary", "#depTitle");

  if (summaryCache.has(path)) {
    setSummary("#fileSummary", summaryCache.get(path), false);
    return;
  }

  setSummary("#fileSummary", "Reading the file…", true);

  const summary = await fetchSummary(
    `${API_BASE}/api/repository/file/summary?${repoQuery({ path })}`,
    path
  );

  /* The user may have moved on while this was in flight. */
  if (state.path !== path) return;

  setSummary("#fileSummary", summary, false);
}


/* ============================================================
   PANES

   Architecture and Dependencies each get their own canvas and file
   list. One renderer draws into whichever pane the current view
   belongs to, so there is still a single graph implementation.
============================================================ */

function activePane() {

  return state.view === "architecture"
    ? { canvas: "#architectureGraph", files: "#architectureFiles" }
    : { canvas: "#dependencyGraph", files: "#levelFiles" };
}

function buildPane(host, canvasId, filesId, emptyMessage) {

  if (!host || document.getElementById(canvasId)) return;

  host.innerHTML = "";

  const layout = document.createElement("div");
  layout.className = "dep-layout";

  const filesPanel = document.createElement("section");
  filesPanel.className = "panel dep-files";

  const filesTitle = document.createElement("div");
  filesTitle.className = "panel-title";
  filesTitle.textContent = "FILES";

  const list = document.createElement("div");
  list.id = filesId;
  list.className = "level-files";

  filesPanel.appendChild(filesTitle);
  filesPanel.appendChild(list);

  const graphPanel = document.createElement("section");
  graphPanel.className = "panel dep-graph-panel";

  const canvas = document.createElement("div");
  canvas.id = canvasId;
  canvas.className = "graph-canvas";
  canvas.innerHTML =
    `<div class="graph-empty">
       <p class="graph-empty-message">${emptyMessage}</p>
     </div>`;

  graphPanel.appendChild(canvas);

  layout.appendChild(filesPanel);
  layout.appendChild(graphPanel);

  host.appendChild(layout);
}

function ensureArchitecturePane() {

  const view = $("#architecture");
  if (!view) return;

  /* The .map block is a hardcoded illustration — apps/web, auth
     service, events — with no connection to the repository being
     analysed. Replace it with the real progressive graph. */
  const map = view.querySelector(".map");
  const inspector = view.querySelector("#inspector");

  if (inspector) inspector.remove();

  const intro = view.querySelector(".view-intro p");

  if (intro) {
    intro.textContent =
      "How the repository is laid out. Click a folder to open it.";
  }

  if (map) {
    buildPane(
      map.parentNode.insertBefore(document.createElement("div"), map),
      "architectureGraph",
      "architectureFiles",
      "Analyze a repository to map its structure."
    );
    map.remove();
  }
}


/* ============================================================
   SPLIT LAYOUT

   Files on the left, graph on the right — the shape of the .canvas
   block on the landing page. The file list shows the current level,
   so it stays in step with whatever the graph is showing.
============================================================ */

function ensureSplitLayout() {

  /* Scope to the Dependencies view. A bare .dep-layout query now
     matches the Architecture pane first, which put the file list in
     the wrong tab. */
  const view = $("#dependencies") || document;

  const intro = view.querySelector(".view-intro p");

  if (intro) {
    intro.textContent =
      "What one file imports, and what imports it. Open a file to " +
      "trace it.";
  }

  const layout = view.querySelector(".dep-layout");
  const graphPanel = view.querySelector(".dep-graph-panel");

  if (!layout || !graphPanel) return;
  if ($("#levelFiles")) return;

  const panel = document.createElement("section");
  panel.className = "panel dep-files";

  const title = document.createElement("div");
  title.className = "panel-title";
  title.textContent = "FILES";

  const list = document.createElement("div");
  list.id = "levelFiles";
  list.className = "level-files";

  panel.appendChild(title);
  panel.appendChild(list);

  layout.insertBefore(panel, graphPanel);
}

/* The Dependencies pane needs its own way in. Telling the user to go
   to another tab made the whole view a dead end. */
const PICKER_LIMIT = 300;

function renderDependencyPicker() {

  const container = $("#levelFiles");
  if (!container) return;

  /* Every file in the repository, not just the folder Architecture
     happens to be showing. Tying this list to the other tab meant you
     had to leave to find anything. */
  const filter = state.graphFilter.trim().toLowerCase();
  const folder = state.pickerFolder;
  const prefix = folder ? `${folder}/` : "";

  const all = state.tree.files.filter(path => {
    if (folder && !path.startsWith(prefix)) return false;
    if (!filter) return true;
    return path.toLowerCase().includes(filter);
  });

  const files = all.slice(0, PICKER_LIMIT);

  if (!files.length) {
    container.innerHTML =
      `<div class="tree-loading">${
        filter
          ? `No file matches “${escapeHtml(filter)}”.`
          : folder
            ? `No readable files in ${escapeHtml(folder)}.`
            : "Analyze a repository to list its files."
      }</div>`;
    return;
  }

  container.innerHTML = "";

  const heading = document.createElement("div");
  heading.className = "picker-heading";

  const count =
    all.length > files.length
      ? `${files.length} of ${all.length} files`
      : plural(all.length, "file");

  if (folder) {

    heading.textContent = `${count} in ${folder}`;

    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "picker-clear";
    clear.textContent = "show all";

    clear.addEventListener("click", event => {
      event.preventDefault();
      state.pickerFolder = "";
      renderDependencyPicker();
    });

    heading.appendChild(clear);

  } else {
    heading.textContent = count;
  }

  container.appendChild(heading);

  /* Keep the open file in view even when it is far down the list. */
  if (state.path && !files.includes(state.path) && all.includes(state.path)) {
    files.unshift(state.path);
  }

  files.forEach(path => {

    const button = document.createElement("button");
    button.type = "button";
    button.className = "level-file";

    if (path === state.path) button.classList.add("active");

    const icon = document.createElement("span");
    icon.className = "level-file-icon";
    icon.textContent = path === state.path ? "▸" : "·";

    const name = document.createElement("span");
    name.className = "level-file-name";
    name.textContent = fileName(path);

    const where = document.createElement("span");
    where.className = "level-file-note";
    where.textContent = parentPath(path) || "/";

    button.title = path;

    button.appendChild(icon);
    button.appendChild(name);
    button.appendChild(where);

    button.addEventListener("click", event => {
      event.preventDefault();
      loadFileDependencies(path);
    });

    container.appendChild(button);
  });
}

function renderLevelFiles() {

  /* The Dependencies pane is always a picker. It used to switch to
     listing the current file's imports once a file was open, which
     left no way to choose a different file without going back to the
     Architecture tab. The graph already shows the imports; this list
     is for navigation. */
  renderDependencyPicker();

  if (state.view === "file") {
    const other = $("#architectureFiles");
    if (other) {
      other.innerHTML =
        `<div class="tree-loading">Shown in the Architecture tab.</div>`;
    }
    return;
  }

  const container = $("#architectureFiles");
  if (!container) return;

  const nodes = state.graph.nodes.filter(
    node => !node.isParent && node.type !== "repository"
  );

  if (!nodes.length) {
    container.innerHTML = `<div class="tree-loading">Nothing at this level.</div>`;
    return;
  }

  container.innerHTML = "";

  nodes.forEach(node => {

    const button = document.createElement("button");
    button.type = "button";
    button.className = "level-file";

    if (node.id === state.selectedId) button.classList.add("active");
    if (node.path === state.path) button.classList.add("active");

    const icon = document.createElement("span");
    icon.className = "level-file-icon";
    icon.textContent =
      node.type === "directory" ? "▸" :
      node.type === "external" ? "◆" : "·";

    const name = document.createElement("span");
    name.className = "level-file-name";
    name.textContent = node.label;

    const note = document.createElement("span");
    note.className = "level-file-note";
    note.textContent =
      node.type === "directory"
        ? String(node.count || "")
        : (node.language || "");

    button.appendChild(icon);
    button.appendChild(name);
    button.appendChild(note);

    button.addEventListener("click", event => {
      event.preventDefault();
      selectNode(node.id);
    });

    button.addEventListener("dblclick", event => {
      event.preventDefault();
      openNode(node);
    });

    container.appendChild(button);
  });
}


/* ============================================================
   WORKSPACE TABS

   index.html ships four tabs and four .view sections but nothing
   ever switched them, so every view rendered at once and the graph
   canvas sat inside a panel the user could not reach.
============================================================ */

function nextFrame(callback) {

  if (typeof window.requestAnimationFrame === "function") {
    window.requestAnimationFrame(callback);
    return;
  }

  setTimeout(callback, 16);
}

function activateTab(name) {

  state.tab = name;

  document.querySelectorAll(".tabs button[data-tab]").forEach(button => {
    button.classList.toggle("active", button.dataset.tab === name);
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
  });

  document.querySelectorAll(".workspace .view").forEach(view => {
    view.classList.toggle("active", view.id === name);
  });

  if (name === "architecture") {

    /* Architecture always shows the structural graph, never a file
       focus view. */
    if (state.view !== "architecture") {
      loadArchitecture(state.lastArchitecturePath || "", { record: false });
      return;
    }

    nextFrame(fitToContent);
    return;
  }

  if (name === "dependencies") {

    if (state.view !== "file") {
      showDependencyHint();
      return;
    }

    nextFrame(fitToContent);
  }
}

function showDependencyHint() {

  const canvas = $("#dependencyGraph");
  if (!canvas) return;

  canvas.innerHTML = `
    <div class="graph-empty">
      <p class="graph-empty-title">Pick a file to trace.</p>
      <p class="graph-empty-message">
        Choose one from the list on the left to see what it imports and
        what imports it.
      </p>
    </div>
  `;

  setText("#depNodeCount", "0");
  setText("#depEdgeCount", "0");

  setSummary("#fileSummary", null, false);
  renderDependencyPicker();
}

function initTabs() {

  const buttons = document.querySelectorAll(".tabs button[data-tab]");

  if (!buttons.length) return;

  buttons.forEach(button => {
    button.type = "button";
    button.setAttribute("role", "tab");
    button.addEventListener("click", event => {
      event.preventDefault();
      activateTab(button.dataset.tab);
    });
  });

  const active = document.querySelector(".tabs button.active");
  activateTab(active ? active.dataset.tab : "overview");
}


/* ============================================================
   LEGAL PAGES

   index.html calls showHome() from inline onclick handlers, but the
   function was never defined, so those buttons threw.
============================================================ */

function showLegal(id) {

  document.querySelectorAll(".legal-page").forEach(page => {
    page.classList.toggle("active", page.id === id);
  });

  const main = $("#mainContent");
  if (main) main.hidden = true;

  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showHome() {

  document.querySelectorAll(".legal-page").forEach(page => {
    page.classList.remove("active");
  });

  const main = $("#mainContent");
  if (main) main.hidden = false;

  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* The inline onclick attributes resolve against window. */
window.showHome = showHome;

function initLegalPages() {

  on("#privacyLink", "click", event => {
    event.preventDefault();
    showLegal("privacyPage");
  });

  on("#termsLink", "click", event => {
    event.preventDefault();
    showLegal("termsPage");
  });

  document.querySelectorAll(".back-home").forEach(button => {
    button.type = "button";
    button.addEventListener("click", event => {
      event.preventDefault();
      showHome();
    });
  });
}


/* ============================================================
   MODULE FILTER

   The "Filter modules" box in the Dependencies view filters the
   current level rather than searching the whole repository — the
   sidebar filter already does that.
============================================================ */

function initModuleFilter() {

  on("#filter", "input", event => {

    state.graphFilter = event.target.value;
    state.showAllNodes = false;

    if (state.view === "file" || state.tab === "dependencies") {
      renderDependencyPicker();
    }

    if (state.view !== "file") renderGraph();
  });
}


/* ============================================================
   IMPORTANT MODULES

   Replaces the "Repository analysis will populate this section"
   placeholder with the landmarks the backend flags plus the largest
   top-level folders.
============================================================ */

function renderImportantModules() {

  const container = $("#modules");
  if (!container) return;

  const nodes = state.graph.nodes.filter(node => !node.isParent);

  const landmarks = nodes.filter(node => node.important);

  const biggest = nodes
    .filter(node => node.type === "directory")
    .sort((a, b) => b.fileCount - a.fileCount)
    .slice(0, 6);

  const entries = [...landmarks, ...biggest];

  if (!entries.length) {
    container.innerHTML =
      `<div class="module-empty">Nothing notable at this level.</div>`;
    return;
  }

  container.innerHTML = "";

  entries.forEach(node => {

    const button = document.createElement("button");
    button.type = "button";
    button.className = "module-row";

    const name = document.createElement("span");
    name.className = "module-name";
    name.textContent = node.path || node.label;

    const note = document.createElement("span");
    note.className = "module-note";
    note.textContent =
      node.type === "directory"
        ? plural(node.fileCount, "file")
        : node.language || "file";

    button.appendChild(name);
    button.appendChild(note);

    button.addEventListener("click", event => {
      event.preventDefault();
      openNode(node);
      activateTab("dependencies");
    });

    container.appendChild(button);
  });
}


/* ============================================================
   TASK IMPACT

   A run is a dozen model calls and up to ten file reads, so it takes
   tens of seconds. The steps are shown as they are reported rather
   than hidden behind a spinner: watching what it opened is most of
   how you judge whether to trust the answer.
============================================================ */

let impactRunning = false;

function renderImpactStatus(message) {

  setHtml("#results", `
    <div class="impact-status">
      <div class="loading-title">${escapeHtml(message)}</div>
      <div class="loading-bar"></div>
      <p class="impact-note">
        Searching the repository and reading candidate files. This
        usually takes twenty to forty seconds.
      </p>
    </div>
  `);
}

const STEP_LABELS = {
  search_repository: "Searched for",
  read_file: "Read",
  get_dependencies: "Traced dependencies of",
  report_impact: "Finished"
};

function renderImpact(data) {

  const container = $("#results");
  if (!container) return;

  container.innerHTML = "";

  if (data.summary) {
    const summary = document.createElement("p");
    summary.className = "impact-summary";
    summary.textContent = data.summary;
    container.appendChild(summary);
  }

  if (data.incomplete) {
    const note = document.createElement("p");
    note.className = "impact-note";
    note.textContent = data.reason || "No conclusion was reached.";
    container.appendChild(note);
  }

  if (data.files && data.files.length) {

    const heading = document.createElement("div");
    heading.className = "detail-label";
    heading.textContent = "FILES TO LOOK AT";
    container.appendChild(heading);

    const list = document.createElement("div");
    list.className = "impact-files";

    data.files.forEach(file => {

      const row = document.createElement("button");
      row.type = "button";
      row.className = `impact-file confidence-${file.confidence}`;
      row.title = `Open ${file.path}`;

      const path = document.createElement("span");
      path.className = "impact-path";
      path.textContent = file.path;

      const badge = document.createElement("span");
      badge.className = "impact-confidence";
      badge.textContent = file.confidence;

      const reason = document.createElement("span");
      reason.className = "impact-reason";
      reason.textContent = file.reason;

      const top = document.createElement("span");
      top.className = "impact-file-top";
      top.appendChild(path);
      top.appendChild(badge);

      row.appendChild(top);
      row.appendChild(reason);

      /* Every claim is checkable: the row opens that file's graph. */
      row.addEventListener("click", event => {
        event.preventDefault();
        loadFileDependencies(file.path);
      });

      list.appendChild(row);
    });

    container.appendChild(list);
  }

  if (data.unknowns) {
    const note = document.createElement("p");
    note.className = "impact-note";
    note.textContent = `Not determined: ${data.unknowns}`;
    container.appendChild(note);
  }

  if (data.steps && data.steps.length) {

    const details = document.createElement("details");
    details.className = "impact-steps";

    const toggle = document.createElement("summary");
    toggle.textContent = `How it got there (${data.steps.length} steps, ${
      data.filesRead || 0
    } files read)`;
    details.appendChild(toggle);

    data.steps.forEach(step => {
      const line = document.createElement("div");
      line.className = "impact-step";
      line.textContent = `${STEP_LABELS[step.tool] || step.tool} ${
        step.detail || ""
      }`;
      details.appendChild(line);
    });

    container.appendChild(details);
  }

  if (data.dropped) {
    const note = document.createElement("p");
    note.className = "impact-note";
    note.textContent =
      `${plural(data.dropped, "suggested path")} did not exist in this ` +
      "repository and were left out.";
    container.appendChild(note);
  }
}

function initImpact() {

  const form = $("#impactForm");
  if (!form) return;

  form.addEventListener("submit", async event => {

    event.preventDefault();
    event.stopPropagation();

    if (impactRunning) return;

    const input = $("#task");
    const task = input ? input.value.trim() : "";

    if (!task) return;

    if (!state.repo.owner) {
      setHtml("#results",
        `<p class="impact-note">Analyze a repository first.</p>`);
      return;
    }

    const button = form.querySelector("button[type=submit]");
    const original = button ? button.textContent : "";

    impactRunning = true;

    if (button) {
      button.disabled = true;
      button.textContent = "Working…";
    }

    renderImpactStatus("Working out what this change touches");

    try {

      const response = await fetch(`${API_BASE}/api/impact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          owner: state.repo.owner,
          repo: state.repo.repo,
          branch: state.repo.branch,
          task
        })
      });

      const data = await response.json();

      if (!response.ok) {
        setHtml("#results", `<p class="impact-note">${
          escapeHtml(data.detail || `Failed (HTTP ${response.status}).`)
        }</p>`);
        return;
      }

      renderImpact(data);

    } catch (error) {
      setHtml("#results",
        `<p class="impact-note">${escapeHtml(error.message)}</p>`);
    } finally {
      impactRunning = false;
      if (button) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  });
}


/* ============================================================
   KEYBOARD
============================================================ */

document.addEventListener("keydown", event => {

  const typing = ["INPUT", "TEXTAREA"].includes(
    document.activeElement && document.activeElement.tagName
  );

  if (typing) return;

  if (event.key === "Escape") {
    if (state.selectedId) {
      clearSelection();
      clearInspector(state.path);
    } else {
      closeAnalyzer();
    }
    return;
  }

  if (analyzer && analyzer.hidden) return;

  if (event.key === "Backspace" || (event.altKey && event.key === "ArrowLeft")) {
    event.preventDefault();
    goBack();
  }

  if (event.key === "+" || event.key === "=") zoomBy(1.2);
  if (event.key === "-") zoomBy(1 / 1.2);
  if (event.key === "0") fitToContent();
});


/* ============================================================
   RESIZE
============================================================ */

let resizeTimer = null;

window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(fitToContent, 150);
});


/* ============================================================
   INIT
============================================================ */

ensureArchitecturePane();
ensureSplitLayout();
initTabs();
initLegalPages();
initModuleFilter();
initImpact();
ensureToolbar();
initCameraControls();
updateNavigationControls();
clearInspector("");

if (analyzer) analyzer.hidden = true;