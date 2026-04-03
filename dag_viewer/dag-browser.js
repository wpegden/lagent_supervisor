// dag-browser.js -- Theorem Frontier DAG Browser
// Vanilla JS, no build step. Canvas-based rendering for large DAGs.

"use strict";

(function () {

// ========== State ==========
var state = {
  repos: [],
  currentRepo: null,
  frontier: null,          // current full frontier.json
  history: [],             // frontier-history.jsonl entries, sorted by cycle
  historyIndex: -1,        // which history entry we're viewing (-1 = latest)
  reconstructed: null,     // reconstructed DAG at current cycle
  layout: null,            // {positions, layers, width, height, nodeWidth, nodeHeight}
  selectedNodeId: null,
  searchQuery: "",
  hiddenStatuses: new Set(["refuted", "replaced"]),
  // Camera
  camX: 0,
  camY: 0,
  zoom: 1,
  dragging: false,
  dragStartX: 0,
  dragStartY: 0,
  // Animation
  playInterval: null,
};

// ========== DOM refs ==========
var $canvas, $ctx, $miniCanvas, $miniCtx;
var $projectList, $projectLabel, $phaseBadge;
var $cycleLabel, $cyclePrev, $cycleNext, $cyclePlay;
var $branchSelect, $metricsBar;
var $scrubberWrap, $scrubber, $scrubberTicks;
var $directivePanel, $directiveContent;
var $detailPanel, $detailContent, $detailClose;
var $canvasWrap, $searchInput, $statusFilters;

// ========== Constants ==========
var NODE_COLORS = {
  closed:   { fill: "#238636", border: "#2ea043", text: "#fff" },
  open:     { fill: "#21262d", border: "#484f58", text: "#c9d1d9" },
  active:   { fill: "#9e6a03", border: "#d29922", text: "#fff" },
  refuted:  { fill: "#3d1214", border: "#da3633", text: "#f85149" },
  replaced: { fill: "#3d1214", border: "#8b2c2c", text: "#f8514980" },
  proposed: { fill: "#0d2240", border: "#1f6feb", text: "#58a6ff" },
  frozen:   { fill: "#1c1c1c", border: "#6e7681", text: "#8b949e" },
};
var KIND_BORDER_WIDTH = { paper: 3, paper_faithful_reformulation: 3 };
var EDGE_COLORS = {
  proved:   "#3fb950",
  frontier: "#d29922",
  dead:     "#f8514960",
  unresolved: "#30363d",
  active:   "#d29922",
};
var DPR = window.devicePixelRatio || 1;
var NODE_W = 180, NODE_H = 54;
var EDGE_STUB_Y = 16;
var EDGE_LANE_X = 10;

// ========== Init ==========
function init() {
  $canvas = document.getElementById("dag-canvas");
  $ctx = $canvas.getContext("2d");
  $miniCanvas = document.getElementById("minimap-canvas");
  $miniCtx = $miniCanvas.getContext("2d");
  $projectList = document.getElementById("project-list");
  $projectLabel = document.getElementById("project-label");
  $phaseBadge = document.getElementById("phase-badge");
  $cycleLabel = document.getElementById("cycle-label");
  $cyclePrev = document.getElementById("cycle-prev");
  $cycleNext = document.getElementById("cycle-next");
  $cyclePlay = document.getElementById("cycle-play");
  $branchSelect = document.getElementById("branch-select");
  $metricsBar = document.getElementById("metrics-bar");
  $scrubberWrap = document.getElementById("scrubber-wrap");
  $scrubber = document.getElementById("scrubber");
  $scrubberTicks = document.getElementById("scrubber-ticks");
  $directivePanel = document.getElementById("directive-panel");
  $directiveContent = document.getElementById("directive-content");
  $detailPanel = document.getElementById("detail-panel");
  $detailContent = document.getElementById("detail-content");
  $detailClose = document.getElementById("detail-close");
  $canvasWrap = document.getElementById("canvas-wrap");
  $searchInput = document.getElementById("search-input");
  $statusFilters = document.getElementById("status-filters");

  bindEvents();
  resizeCanvas();
  window.addEventListener("resize", resizeCanvas);
  loadRepos();
}

// ========== Data loading ==========
function cacheBusted(url) {
  var sep = url.indexOf("?") >= 0 ? "&" : "?";
  return url + sep + "_t=" + Date.now();
}

function loadRepos() {
  fetch(cacheBusted("repos.json"))
    .then(function (r) { return r.json(); })
    .then(function (data) {
      state.repos = data.repos || [];
      renderProjectList();
      // Auto-select from hash
      var hash = location.hash.replace("#", "");
      if (hash) selectProject(hash);
    })
    .catch(function () { state.repos = []; renderProjectList(); });
}

function selectProject(repoName) {
  var repo = state.repos.find(function (r) { return r.repo_name === repoName; });
  if (!repo) return;
  state.currentRepo = repo;
  state.selectedNodeId = null;
  state.history = [];
  state.historyIndex = -1;
  state.frontier = null;
  state.reconstructed = null;
  state.layout = null;
  location.hash = repoName;
  $projectLabel.textContent = repo.project_name || repo.repo_name;
  $phaseBadge.textContent = repo.current_phase || "";
  $phaseBadge.classList.toggle("hidden", !repo.current_phase);
  hideDetail();

  // Load frontier + history in parallel
  var frontierP = fetch(cacheBusted(repoName + "/frontier.json"))
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; });
  var historyP = fetch(cacheBusted(repoName + "/frontier-history.jsonl"))
    .then(function (r) { return r.ok ? r.text() : ""; })
    .catch(function () { return ""; });

  Promise.all([frontierP, historyP]).then(function (results) {
    state.frontier = results[0];
    state.history = parseJsonl(results[1]);
    state.history.sort(function (a, b) { return (a.cycle || 0) - (b.cycle || 0); });
    state.historyIndex = state.history.length - 1;
    setupScrubber();
    reconstructAndRender();
  });
  renderProjectList();
}

function parseJsonl(text) {
  if (!text) return [];
  return text.trim().split("\n").map(function (line) {
    try { return JSON.parse(line); } catch (e) { return null; }
  }).filter(Boolean);
}

// ========== DAG reconstruction from history ==========
function reconstructAtIndex(index) {
  if (state.history.length === 0 && state.frontier) {
    // No history, use frontier.json directly
    return {
      nodes: state.frontier.nodes || {},
      edges: state.frontier.edges || [],
      activeEdgeId: state.frontier.active_edge_id,
      activeLeafId: state.frontier.active_leaf_id,
      metrics: state.frontier.metrics || {},
      escalation: state.frontier.escalation || {},
      directive: "",
      cycle: state.frontier.current ? state.frontier.current.cycle : 0,
      outcome: "",
    };
  }
  if (state.history.length === 0) return null;
  var target = Math.max(0, Math.min(index, state.history.length - 1));

  var nodes = {};
  var edges = [];
  var activeEdgeId = null;
  var activeLeafId = null;
  var metrics = {};
  var escalation = {};
  var directive = "";
  var cycle = 0;
  var outcome = "";

  for (var i = 0; i <= target; i++) {
    var entry = state.history[i];
    cycle = entry.cycle || cycle;
    if (entry.type === "seed") {
      nodes = deepCopy(entry.nodes || {});
      edges = deepCopy(entry.edges || []);
      activeEdgeId = entry.active_edge_id;
      activeLeafId = entry.active_leaf_id;
      metrics = deepCopy(entry.metrics || {});
      escalation = {};
      directive = "";
      outcome = "seed";
    } else if (entry.type === "review") {
      // Merge new nodes
      var added = entry.nodes_added || {};
      for (var nid in added) {
        if (added.hasOwnProperty(nid)) {
          nodes[nid] = deepCopy(added[nid]);
        }
      }
      // Update statuses
      var statuses = entry.node_statuses || {};
      for (var sid in statuses) {
        if (statuses.hasOwnProperty(sid) && nodes[sid]) {
          nodes[sid].status = statuses[sid];
        }
      }
      // Add edges
      if (entry.edges_added) {
        for (var ei = 0; ei < entry.edges_added.length; ei++) {
          upsertEdge(edges, entry.edges_added[ei]);
        }
      }
      applyEdgeStatuses(edges, entry.edge_statuses || {});
      activeEdgeId = entry.active_edge_id;
      activeLeafId = entry.active_leaf_id;
      metrics = deepCopy(entry.metrics || metrics);
      escalation = deepCopy(entry.escalation || escalation);
      directive = entry.worker_directive || "";
      outcome = entry.outcome || "";
    }
  }

  // If we're viewing the latest entry and have a full frontier, prefer its full node data
  if (target === state.history.length - 1 && state.frontier && state.frontier.nodes) {
    var fNodes = state.frontier.nodes;
    for (var fid in fNodes) {
      if (fNodes.hasOwnProperty(fid)) {
        // Keep the reconstruction's status but fill in missing fields from frontier
        var existing = nodes[fid];
        if (existing) {
          for (var key in fNodes[fid]) {
            if (fNodes[fid].hasOwnProperty(key) && !existing.hasOwnProperty(key)) {
              existing[key] = fNodes[fid][key];
            }
          }
        } else {
          nodes[fid] = deepCopy(fNodes[fid]);
        }
      }
    }
    // Use frontier edges at latest
    if (state.frontier.edges) edges = deepCopy(state.frontier.edges);
    activeEdgeId = state.frontier.active_edge_id || activeEdgeId;
    activeLeafId = state.frontier.active_leaf_id || activeLeafId;
    metrics = state.frontier.metrics || metrics;
    escalation = state.frontier.escalation || escalation;
  }

  applyEffectiveNodeStatuses(nodes, edges);

  return { nodes: nodes, edges: edges, activeEdgeId: activeEdgeId, activeLeafId: activeLeafId,
           metrics: metrics, escalation: escalation,
           directive: directive, cycle: cycle, outcome: outcome };
}

function canonicalEdgeId(edge) {
  if (!edge) return "";
  if (edge.edge_id) return String(edge.edge_id);
  if (!edge.parent || !edge.child || !edge.edge_type) return "";
  return String(edge.parent) + "|" + String(edge.edge_type) + "|" + String(edge.child);
}

function upsertEdge(edges, edge) {
  var copy = deepCopy(edge);
  var edgeId = canonicalEdgeId(copy);
  if (edgeId) copy.edge_id = edgeId;
  for (var i = 0; i < edges.length; i++) {
    if (canonicalEdgeId(edges[i]) === edgeId && edgeId) {
      edges[i] = copy;
      return;
    }
  }
  edges.push(copy);
}

function applyEdgeStatuses(edges, edgeStatuses) {
  if (!edgeStatuses) return;
  for (var i = 0; i < edges.length; i++) {
    var edgeId = canonicalEdgeId(edges[i]);
    if (edgeId && Object.prototype.hasOwnProperty.call(edgeStatuses, edgeId)) {
      edges[i].status = edgeStatuses[edgeId];
    }
  }
}

function outgoingDependencyEdges(nodes, edges, nodeId) {
  var result = [];
  for (var i = 0; i < edges.length; i++) {
    var edge = edges[i];
    if (!edge || edge.parent !== nodeId) continue;
    if (edge.edge_type === "replacement" || edge.status === "replaced") continue;
    if (!nodes[edge.child]) continue;
    result.push(edge);
  }
  return result;
}

function effectiveNodeStatus(nodes, edges, nodeId, memo, visiting) {
  if (!nodes[nodeId]) return "open";
  if (Object.prototype.hasOwnProperty.call(memo, nodeId)) return memo[nodeId];
  var raw = String(nodes[nodeId].status || "open");
  if (raw === "refuted" || raw === "replaced") {
    memo[nodeId] = raw;
    return raw;
  }
  if (visiting[nodeId]) {
    return raw === "active" || raw === "frozen" || raw === "proposed" ? raw : "open";
  }
  visiting[nodeId] = true;
  var deps = outgoingDependencyEdges(nodes, edges, nodeId);
  var closedDeps = [];
  for (var i = 0; i < deps.length; i++) {
    var edge = deps[i];
    if (edge.status === "closed" && effectiveNodeStatus(nodes, edges, edge.child, memo, visiting) === "closed") {
      closedDeps.push(edge);
    }
  }
  var closureMode = nodes[nodeId].closure_mode;
  var proved = false;
  if (closureMode === "leaf") {
    proved = deps.length === 0 && raw === "closed";
  } else if (closureMode === "all_children" || closureMode === "all_cases") {
    proved = deps.length > 0 && closedDeps.length === deps.length;
  } else if (closureMode === "any_child") {
    proved = closedDeps.length > 0;
  }
  delete visiting[nodeId];
  var effective = proved ? "closed" : ((raw === "active" || raw === "frozen" || raw === "proposed") ? raw : "open");
  memo[nodeId] = effective;
  return effective;
}

function applyEffectiveNodeStatuses(nodes, edges) {
  var memo = {};
  var visiting = {};
  var ids = Object.keys(nodes || {});
  for (var i = 0; i < ids.length; i++) {
    var node = nodes[ids[i]];
    if (!node) continue;
    node.effective_status = effectiveNodeStatus(nodes, edges, ids[i], memo, visiting);
  }
}

function deepCopy(obj) {
  return JSON.parse(JSON.stringify(obj));
}

// ========== Layout ==========
var layoutWorker = null;
function getLayoutWorker() {
  if (!layoutWorker) {
    layoutWorker = new Worker("_assets/dag-layout-worker.js");
  }
  return layoutWorker;
}

function computeLayout(dag) {
  return new Promise(function (resolve) {
    var worker = getLayoutWorker();
    worker.onmessage = function (e) { resolve(e.data); };
    worker.postMessage({
      nodes: dag.nodes,
      edges: dag.edges,
      options: { nodeWidth: NODE_W, nodeHeight: NODE_H },
    });
  });
}

// ========== Reconstruct & render pipeline ==========
function reconstructAndRender() {
  var dag = reconstructAtIndex(state.historyIndex);
  state.reconstructed = dag;
  if (!dag || Object.keys(dag.nodes).length === 0) {
    state.layout = null;
    renderEmpty();
    updateCycleLabel();
    updateDirective();
    updateMetrics();
    return;
  }
  computeLayout(dag).then(function (layout) {
    state.layout = layout;
    fitView();
    renderDAG();
    renderMinimap();
    updateCycleLabel();
    updateDirective();
    updateMetrics();
    if (state.selectedNodeId && dag.nodes[state.selectedNodeId]) {
      showDetail(state.selectedNodeId);
    } else {
      hideDetail();
    }
  });
}

// ========== Canvas rendering ==========
function resizeCanvas() {
  var rect = $canvasWrap.getBoundingClientRect();
  $canvas.width = rect.width * DPR;
  $canvas.height = rect.height * DPR;
  $canvas.style.width = rect.width + "px";
  $canvas.style.height = rect.height + "px";
  if (state.layout) renderDAG();
}

function renderEmpty() {
  var w = $canvas.width / DPR;
  var h = $canvas.height / DPR;
  $ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  $ctx.clearRect(0, 0, w, h);
  $ctx.fillStyle = "#484f58";
  $ctx.font = "15px sans-serif";
  $ctx.textAlign = "center";
  $ctx.fillText("No frontier data for this project", w / 2, h / 2);
}

function fitView() {
  if (!state.layout) return;
  var cw = $canvas.width / DPR;
  var ch = $canvas.height / DPR;
  var lw = state.layout.width + NODE_W;
  var lh = state.layout.height + NODE_H;
  if (lw === 0 || lh === 0) return;
  var zx = cw / lw;
  var zy = ch / lh;
  state.zoom = Math.min(zx, zy, 1.5) * 0.9;
  state.camX = (cw - lw * state.zoom) / 2;
  state.camY = (ch - lh * state.zoom) / 2 + 20;
}

function renderDAG() {
  if (!state.layout || !state.reconstructed) return;
  var dag = state.reconstructed;
  var positions = state.layout.positions;
  var cw = $canvas.width / DPR;
  var ch = $canvas.height / DPR;
  var z = state.zoom;
  var cx = state.camX;
  var cy = state.camY;

  $ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  $ctx.clearRect(0, 0, cw, ch);
  $ctx.save();
  $ctx.translate(cx, cy);
  $ctx.scale(z, z);

  var searchLower = state.searchQuery.toLowerCase();
  var edges = dag.edges || [];
  var outgoingOrder = {};
  var incomingOrder = {};
  var outgoingCount = {};
  var incomingCount = {};

  edges.forEach(function (edge) {
    if (!edge || !edge.parent || !edge.child) return;
    if (!outgoingOrder[edge.parent]) outgoingOrder[edge.parent] = [];
    if (!incomingOrder[edge.child]) incomingOrder[edge.child] = [];
    outgoingOrder[edge.parent].push(edge.child);
    incomingOrder[edge.child].push(edge.parent);
  });
  Object.keys(outgoingOrder).forEach(function (parentId) {
    outgoingOrder[parentId].sort(function (a, b) {
      var ax = positions[a] ? positions[a].x : 0;
      var bx = positions[b] ? positions[b].x : 0;
      return ax - bx;
    });
    outgoingCount[parentId] = outgoingOrder[parentId].length;
  });
  Object.keys(incomingOrder).forEach(function (childId) {
    incomingOrder[childId].sort(function (a, b) {
      var ax = positions[a] ? positions[a].x : 0;
      var bx = positions[b] ? positions[b].x : 0;
      return ax - bx;
    });
    incomingCount[childId] = incomingOrder[childId].length;
  });

  // Draw edges
  for (var i = 0; i < edges.length; i++) {
    var edge = edges[i];
    var from = positions[edge.parent];
    var to = positions[edge.child];
    if (!from || !to) continue;
    var parentNode = dag.nodes[edge.parent];
    var childNode = dag.nodes[edge.child];
    if (!parentNode || !childNode) continue;
    if (isNodeHidden(parentNode) && isNodeHidden(childNode)) continue;

    var edgeColor = classifyEdgeColor(edge, parentNode, childNode);
    var outIdx = outgoingOrder[edge.parent] ? outgoingOrder[edge.parent].indexOf(edge.child) : 0;
    var inIdx = incomingOrder[edge.child] ? incomingOrder[edge.child].indexOf(edge.parent) : 0;
    var fromLane = centeredLaneOffset(outIdx, outgoingCount[edge.parent] || 1);
    var toLane = centeredLaneOffset(inIdx, incomingCount[edge.child] || 1);
    var startX = from.x + fromLane * EDGE_LANE_X;
    var endX = to.x + toLane * EDGE_LANE_X;
    var startY = from.y + NODE_H / 2;
    var endY = to.y - NODE_H / 2;
    var bendY = Math.max(startY + EDGE_STUB_Y, (startY + endY) / 2);

    $ctx.beginPath();
    $ctx.moveTo(startX, startY);
    $ctx.lineTo(startX, bendY);
    $ctx.lineTo(endX, bendY);
    $ctx.lineTo(endX, endY);
    $ctx.strokeStyle = edgeColor;
    $ctx.lineWidth = edgeColor === EDGE_COLORS.proved ? 2.5 : 1.5;
    if (parentNode.status === "refuted" || parentNode.status === "replaced" ||
        childNode.status === "refuted" || childNode.status === "replaced") {
      $ctx.setLineDash([4, 3]);
    } else {
      $ctx.setLineDash([]);
    }
    $ctx.stroke();
    $ctx.setLineDash([]);

    // Arrow head
    drawArrowHead($ctx, endX, endY, edgeColor);
  }

  // Draw nodes
  var ids = Object.keys(dag.nodes);
  for (var ni = 0; ni < ids.length; ni++) {
    var nid = ids[ni];
    var node = dag.nodes[nid];
    var pos = positions[nid];
    if (!pos || isNodeHidden(node)) continue;

    var displayStatus = nodeDisplayStatus(node);
    var colors = NODE_COLORS[displayStatus] || NODE_COLORS.open;
    var bw = KIND_BORDER_WIDTH[node.kind] || 1.5;
    var isActive = nid === dag.activeLeafId;
    var isSelected = nid === state.selectedNodeId;
    var matchesSearch = searchLower &&
      ((node.node_id || "").toLowerCase().indexOf(searchLower) >= 0 ||
       (node.natural_language_statement || "").toLowerCase().indexOf(searchLower) >= 0 ||
       (node.lean_anchor || "").toLowerCase().indexOf(searchLower) >= 0 ||
       (node.lean_statement || "").toLowerCase().indexOf(searchLower) >= 0);
    var dimmed = searchLower && !matchesSearch;

    var x = pos.x - NODE_W / 2;
    var y = pos.y - NODE_H / 2;

    // Glow for active node
    if (isActive) {
      $ctx.save();
      $ctx.shadowColor = "#d29922";
      $ctx.shadowBlur = 12;
      $ctx.fillStyle = colors.fill;
      roundRect($ctx, x - 2, y - 2, NODE_W + 4, NODE_H + 4, 8);
      $ctx.fill();
      $ctx.restore();
    }

    // Node rect
    $ctx.globalAlpha = dimmed ? 0.25 : 1;
    $ctx.fillStyle = colors.fill;
    roundRect($ctx, x, y, NODE_W, NODE_H, 6);
    $ctx.fill();
    $ctx.strokeStyle = isSelected ? "#58a6ff" : colors.border;
    $ctx.lineWidth = isSelected ? 2.5 : bw;
    $ctx.stroke();

    // Label: truncated node_id or lean_anchor
    var label = node.display_label || node.node_id || nid;
    if (label.length > 26) label = label.substring(0, 24) + "...";
    $ctx.fillStyle = colors.text;
    $ctx.font = "bold 11px sans-serif";
    $ctx.textAlign = "center";
    $ctx.textBaseline = "middle";
    $ctx.fillText(label, pos.x, pos.y - 8);

    // Kind badge
    var kindLabel = (node.kind || "").replace(/_/g, " ");
    if (kindLabel.length > 18) kindLabel = kindLabel.substring(0, 16) + "..";
    $ctx.font = "10px sans-serif";
    $ctx.fillStyle = colors.text + "99";
    $ctx.fillText(kindLabel, pos.x, pos.y + 8);

    // Status dot
    var dotColor = (NODE_COLORS[displayStatus] || NODE_COLORS.open).border;
    $ctx.beginPath();
    $ctx.arc(x + NODE_W - 10, y + 10, 4, 0, Math.PI * 2);
    $ctx.fillStyle = dotColor;
    $ctx.fill();

    $ctx.globalAlpha = 1;
  }

  $ctx.restore();
}

function centeredLaneOffset(index, count) {
  if (!count || count <= 1) return 0;
  return index - (count - 1) / 2;
}

function nodeDisplayStatus(node) {
  return (node && (node.effective_status || node.status)) || "open";
}

function classifyEdgeColor(edge, parentNode, childNode) {
  var edgeStatus = edge && edge.status ? String(edge.status) : "";
  var parentStatus = nodeDisplayStatus(parentNode);
  var childStatus = nodeDisplayStatus(childNode);
  if (edgeStatus === "refuted" || edgeStatus === "replaced" ||
      parentStatus === "refuted" || parentStatus === "replaced" ||
      childStatus === "refuted" || childStatus === "replaced") {
    return EDGE_COLORS.dead;
  }
  if (edgeStatus === "closed") {
    return EDGE_COLORS.proved;
  }
  if (edgeStatus === "active" || childStatus === "active" || parentStatus === "active") {
    return EDGE_COLORS.active;
  }
  return EDGE_COLORS.unresolved;
}

function drawArrowHead(ctx, x, y, color) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x - 4, y - 8);
  ctx.lineTo(x + 4, y - 8);
  ctx.closePath();
  ctx.fill();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function isNodeHidden(node) {
  return state.hiddenStatuses.has(nodeDisplayStatus(node));
}

// ========== Minimap ==========
function renderMinimap() {
  if (!state.layout || !state.reconstructed) return;
  var dag = state.reconstructed;
  var positions = state.layout.positions;
  var mw = 180, mh = 120;
  $miniCtx.clearRect(0, 0, mw, mh);

  var lw = state.layout.width + NODE_W;
  var lh = state.layout.height + NODE_H;
  if (lw === 0 || lh === 0) return;
  var scale = Math.min(mw / lw, mh / lh) * 0.9;
  var ox = (mw - lw * scale) / 2;
  var oy = (mh - lh * scale) / 2;

  // Nodes as dots
  var ids = Object.keys(dag.nodes);
  for (var i = 0; i < ids.length; i++) {
    var pos = positions[ids[i]];
    var node = dag.nodes[ids[i]];
    if (!pos || !node) continue;
    var colors = NODE_COLORS[nodeDisplayStatus(node)] || NODE_COLORS.open;
    $miniCtx.fillStyle = colors.border;
    $miniCtx.fillRect(
      ox + pos.x * scale - 2,
      oy + pos.y * scale - 1,
      4, 3
    );
  }

  // Viewport rectangle
  var cw = $canvas.width / DPR;
  var ch = $canvas.height / DPR;
  var vx = -state.camX / state.zoom;
  var vy = -state.camY / state.zoom;
  var vw = cw / state.zoom;
  var vh = ch / state.zoom;
  $miniCtx.strokeStyle = "#58a6ff";
  $miniCtx.lineWidth = 1;
  $miniCtx.strokeRect(
    ox + vx * scale,
    oy + vy * scale,
    vw * scale,
    vh * scale
  );
}

// ========== Hit testing ==========
function nodeAtPoint(screenX, screenY) {
  if (!state.layout || !state.reconstructed) return null;
  var dag = state.reconstructed;
  var positions = state.layout.positions;
  // Convert screen to world coords
  var wx = (screenX - state.camX) / state.zoom;
  var wy = (screenY - state.camY) / state.zoom;

  var ids = Object.keys(dag.nodes);
  for (var i = 0; i < ids.length; i++) {
    var nid = ids[i];
    var pos = positions[nid];
    var node = dag.nodes[nid];
    if (!pos || !node || isNodeHidden(node)) continue;
    if (wx >= pos.x - NODE_W / 2 && wx <= pos.x + NODE_W / 2 &&
        wy >= pos.y - NODE_H / 2 && wy <= pos.y + NODE_H / 2) {
      return nid;
    }
  }
  return null;
}

// ========== Interaction ==========
function bindEvents() {
  // Canvas pan & click
  $canvas.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    state.dragging = true;
    state.dragStartX = e.clientX - state.camX;
    state.dragStartY = e.clientY - state.camY;
    $canvas.classList.add("dragging");
  });
  window.addEventListener("mousemove", function (e) {
    if (!state.dragging) return;
    state.camX = e.clientX - state.dragStartX;
    state.camY = e.clientY - state.dragStartY;
    renderDAG();
    renderMinimap();
  });
  window.addEventListener("mouseup", function (e) {
    if (!state.dragging) return;
    var moved = Math.abs(e.clientX - state.dragStartX - state.camX) > 3 ||
                Math.abs(e.clientY - state.dragStartY - state.camY) > 3;
    state.dragging = false;
    $canvas.classList.remove("dragging");
    // If barely moved, treat as click
    if (!moved) {
      var rect = $canvas.getBoundingClientRect();
      var sx = e.clientX - rect.left;
      var sy = e.clientY - rect.top;
      var hit = nodeAtPoint(sx, sy);
      if (hit) {
        state.selectedNodeId = hit;
        showDetail(hit);
      } else {
        state.selectedNodeId = null;
        hideDetail();
      }
      renderDAG();
    }
  });

  // Zoom
  $canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    var rect = $canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    var oldZoom = state.zoom;
    var factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    state.zoom = Math.max(0.05, Math.min(5, state.zoom * factor));
    // Zoom toward mouse
    state.camX = mx - (mx - state.camX) * (state.zoom / oldZoom);
    state.camY = my - (my - state.camY) * (state.zoom / oldZoom);
    renderDAG();
    renderMinimap();
  }, { passive: false });

  // Cycle navigation
  $cyclePrev.addEventListener("click", function () { navigateCycle(-1); });
  $cycleNext.addEventListener("click", function () { navigateCycle(1); });
  $cyclePlay.addEventListener("click", togglePlay);
  $scrubber.addEventListener("input", function () {
    state.historyIndex = parseInt($scrubber.value, 10);
    reconstructAndRender();
  });

  // Keyboard
  document.addEventListener("keydown", function (e) {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.key === "ArrowLeft") { navigateCycle(-1); e.preventDefault(); }
    if (e.key === "ArrowRight") { navigateCycle(1); e.preventDefault(); }
    if (e.key === "Escape") { state.selectedNodeId = null; hideDetail(); renderDAG(); }
    if (e.key === "f") { fitView(); renderDAG(); renderMinimap(); }
  });

  // Detail close
  $detailClose.addEventListener("click", function () {
    state.selectedNodeId = null;
    hideDetail();
    renderDAG();
  });

  // Search
  $searchInput.addEventListener("input", function () {
    state.searchQuery = $searchInput.value.trim();
    renderDAG();
  });

  // Status filters
  $statusFilters.addEventListener("change", function (e) {
    var cb = e.target;
    if (!cb.dataset.status) return;
    if (cb.checked) {
      state.hiddenStatuses.delete(cb.dataset.status);
    } else {
      state.hiddenStatuses.add(cb.dataset.status);
    }
    renderDAG();
    renderMinimap();
  });
}

function navigateCycle(delta) {
  if (state.history.length === 0) return;
  var newIdx = state.historyIndex + delta;
  newIdx = Math.max(0, Math.min(state.history.length - 1, newIdx));
  if (newIdx === state.historyIndex) return;
  state.historyIndex = newIdx;
  $scrubber.value = newIdx;
  reconstructAndRender();
}

function togglePlay() {
  if (state.playInterval) {
    clearInterval(state.playInterval);
    state.playInterval = null;
    $cyclePlay.innerHTML = "&#9654;";
    return;
  }
  $cyclePlay.innerHTML = "&#9646;&#9646;";
  state.playInterval = setInterval(function () {
    if (state.historyIndex >= state.history.length - 1) {
      clearInterval(state.playInterval);
      state.playInterval = null;
      $cyclePlay.innerHTML = "&#9654;";
      return;
    }
    navigateCycle(1);
  }, 1000);
}

// ========== Scrubber ==========
function setupScrubber() {
  if (state.history.length <= 1) {
    $scrubberWrap.classList.add("hidden");
    return;
  }
  $scrubberWrap.classList.remove("hidden");
  $scrubber.min = 0;
  $scrubber.max = state.history.length - 1;
  $scrubber.value = state.historyIndex >= 0 ? state.historyIndex : state.history.length - 1;

  // Render ticks
  $scrubberTicks.innerHTML = "";
  for (var i = 0; i < state.history.length; i++) {
    var entry = state.history[i];
    var tick = document.createElement("div");
    tick.className = "scrub-tick " + outcomeTickClass(entry);
    $scrubberTicks.appendChild(tick);
  }
}

function outcomeTickClass(entry) {
  if (entry.type === "seed") return "seed";
  var o = (entry.outcome || "").toUpperCase();
  if (o === "CLOSED") return "closed";
  if (o === "EXPANDED") return "expanded";
  if (o === "REFUTED_REPLACED") return "refuted";
  if (o === "NO_FRONTIER_PROGRESS") return "no-progress";
  return "still-open";
}

// ========== UI updates ==========
function updateCycleLabel() {
  var dag = state.reconstructed;
  if (!dag) { $cycleLabel.textContent = "--"; return; }
  var total = state.history.length || 1;
  var idx = state.historyIndex + 1;
  $cycleLabel.textContent = "cycle " + (dag.cycle || "?") + " (" + idx + "/" + total + ")";
}

function updateDirective() {
  var dag = state.reconstructed;
  if (!dag || !dag.directive) {
    $directivePanel.classList.add("hidden");
    return;
  }
  $directivePanel.classList.remove("hidden");
  $directiveContent.textContent = dag.directive;
}

function updateMetrics() {
  var dag = state.reconstructed;
  if (!dag || !dag.metrics) { $metricsBar.innerHTML = ""; return; }
  var m = dag.metrics;
  var esc = dag.escalation || {};
  var chips = [];

  var closed = m.closed_nodes_count || 0;
  var total = dag.nodes ? Object.keys(dag.nodes).length : 0;
  chips.push(chip("proved", closed + "/" + total));
  var closedEdges = m.closed_edges_count || 0;
  var totalEdges = dag.edges ? dag.edges.length : 0;
  chips.push(chip("edges", closedEdges + "/" + totalEdges));

  if (m.paper_nodes_closed != null) {
    chips.push(chip("paper", m.paper_nodes_closed + " paper"));
  }
  if (dag.activeEdgeId) {
    chips.push(chip("active edge", dag.activeEdgeId));
  }
  if (m.active_edge_age != null) {
    chips.push(chip("edge age", m.active_edge_age, m.active_edge_age >= 5 ? "warn" : ""));
  }
  if (m.active_leaf_age != null) {
    chips.push(chip("leaf age", m.active_leaf_age, m.active_leaf_age >= 5 ? "warn" : ""));
  }
  if (m.blocker_cluster_age != null) {
    chips.push(chip("blocker age", m.blocker_cluster_age, m.blocker_cluster_age >= 5 ? "danger" : ""));
  }
  if (m.cone_purity) {
    var pClass = m.cone_purity === "LOW" ? "danger" : (m.cone_purity === "MEDIUM" ? "warn" : "");
    chips.push(chip("cone", m.cone_purity, pClass));
  }
  if (esc.required) {
    chips.push(chip("ESCALATION", "!", "danger"));
  }

  $metricsBar.innerHTML = chips.join("");
}

function chip(label, value, cls) {
  return '<span class="metric-chip ' + (cls || "") + '">' + label + ': <b>' + value + '</b></span>';
}

// ========== Detail panel ==========
function showDetail(nodeId) {
  var dag = state.reconstructed;
  if (!dag || !dag.nodes[nodeId]) { hideDetail(); return; }
  var node = dag.nodes[nodeId];
  var displayStatus = nodeDisplayStatus(node);
  var html = '';
  html += '<div class="detail-section">';
  html += '<span class="detail-status-badge ' + displayStatus + '">' + displayStatus + '</span>';
  html += ' <span style="color:#8b949e;font-size:11px">' + (node.kind || '').replace(/_/g, ' ') + '</span>';
  html += '</div>';
  if (node.status && node.status !== displayStatus) {
    html += section("Workflow Status", esc(node.status));
  }

  if (node.display_label) {
    html += section("Label", '<b>' + esc(node.display_label) + '</b>');
  }
  html += section("Node ID", esc(node.node_id || nodeId));
  html += section("Lean Anchor", esc(node.lean_anchor || "(none)"));

  if (node.natural_language_statement) {
    html += section("Natural Language", esc(node.natural_language_statement));
  }
  if (node.lean_statement) {
    html += '<div class="detail-section">';
    html += '<div class="detail-label">Lean Statement</div>';
    html += '<div class="detail-value lean">' + esc(node.lean_statement) + '</div>';
    html += '</div>';
  }
  if (node.blocker_cluster) {
    html += section("Blocker", esc(node.blocker_cluster));
  }
  if (node.paper_provenance) {
    html += section("Paper Provenance", esc(node.paper_provenance));
  }
  if (node.closure_mode) {
    html += section("Closure Mode", esc(node.closure_mode));
  }
  if (node.acceptance_evidence) {
    html += section("Acceptance Evidence", esc(node.acceptance_evidence));
  }
  if (node.notes) {
    html += section("Notes", esc(node.notes));
  }
  var outgoing = [];
  for (var i = 0; i < (dag.edges || []).length; i++) {
    var edge = dag.edges[i];
    if (edge && edge.parent === nodeId && edge.edge_type !== "replacement" && edge.status !== "replaced") {
      outgoing.push((edge.status || "open") + " · " + (edge.edge_type || "?") + " · " + (edge.child || "?"));
    }
  }
  if (outgoing.length > 0) {
    html += section("Outgoing Edges", esc(outgoing.join("\n")));
  }

  // Show node history from history entries
  var nodeHistory = buildNodeHistory(nodeId);
  if (nodeHistory.length > 0) {
    html += '<div class="detail-section">';
    html += '<div class="detail-label">History</div>';
    html += '<div class="detail-value">';
    for (var i = 0; i < nodeHistory.length; i++) {
      html += esc(nodeHistory[i]) + "\n";
    }
    html += '</div></div>';
  }

  $detailContent.innerHTML = html;
  $detailPanel.classList.remove("hidden");
}

function hideDetail() {
  $detailPanel.classList.add("hidden");
}

function section(label, value) {
  return '<div class="detail-section"><div class="detail-label">' + label +
         '</div><div class="detail-value">' + value + '</div></div>';
}

function esc(s) {
  var d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

function buildNodeHistory(nodeId) {
  var lines = [];
  for (var i = 0; i < state.history.length; i++) {
    var entry = state.history[i];
    if (entry.type === "seed") {
      if (entry.nodes && entry.nodes[nodeId]) {
        lines.push("c" + (entry.cycle || "?") + ": seeded as " + (entry.nodes[nodeId].status || "open"));
      }
    } else if (entry.type === "review") {
      if (entry.reviewed_node_id === nodeId) {
        lines.push("c" + (entry.cycle || "?") + ": reviewed, outcome=" + (entry.outcome || "?"));
      } else if (entry.nodes_added && entry.nodes_added[nodeId]) {
        lines.push("c" + (entry.cycle || "?") + ": added as " + (entry.nodes_added[nodeId].status || "open"));
      } else if (entry.node_statuses && entry.node_statuses[nodeId]) {
        // Only record if status changed
        var prevStatus = findPreviousStatus(nodeId, i);
        if (prevStatus !== entry.node_statuses[nodeId]) {
          lines.push("c" + (entry.cycle || "?") + ": " + prevStatus + " -> " + entry.node_statuses[nodeId]);
        }
      }
    }
  }
  return lines;
}

function findPreviousStatus(nodeId, beforeIndex) {
  for (var i = beforeIndex - 1; i >= 0; i--) {
    var entry = state.history[i];
    if (entry.node_statuses && entry.node_statuses[nodeId]) {
      return entry.node_statuses[nodeId];
    }
    if (entry.nodes_added && entry.nodes_added[nodeId]) {
      return entry.nodes_added[nodeId].status || "open";
    }
    if (entry.type === "seed" && entry.nodes && entry.nodes[nodeId]) {
      return entry.nodes[nodeId].status || "open";
    }
  }
  return "?";
}

// ========== Project list ==========
function renderProjectList() {
  var html = "";
  for (var i = 0; i < state.repos.length; i++) {
    var repo = state.repos[i];
    var selected = state.currentRepo && state.currentRepo.repo_name === repo.repo_name;
    var fs = repo.frontier_summary || {};
    var total = fs.total_nodes || 0;
    var closed = (fs.status_counts || {}).closed || 0;
    var pct = total > 0 ? Math.round(closed / total * 100) : 0;
    html += '<div class="project-item' + (selected ? " selected" : "") + '" data-repo="' + esc(repo.repo_name) + '">';
    html += '<div class="project-name">' + esc(repo.project_name || repo.repo_name) + '</div>';
    var metaText = (repo.current_phase || "") + ' c' + (repo.current_cycle || 0);
    if (total > 0) {
      metaText += ' &middot; ' + closed + '/' + total + ' proved';
    }
    html += '<div class="project-meta">' + metaText + '</div>';
    if (total > 0) {
      html += '<div class="project-frontier-bar"><div class="frontier-fill" style="width:' + pct + '%"></div></div>';
    }
    html += '</div>';
  }
  if (state.repos.length === 0) {
    html = '<div class="project-item"><div class="project-meta">No projects yet</div></div>';
  }
  $projectList.innerHTML = html;

  // Bind clicks
  var items = $projectList.querySelectorAll(".project-item[data-repo]");
  for (var j = 0; j < items.length; j++) {
    items[j].addEventListener("click", function () {
      selectProject(this.dataset.repo);
    });
  }
}

// ========== Bootstrap ==========
document.addEventListener("DOMContentLoaded", init);

})();
