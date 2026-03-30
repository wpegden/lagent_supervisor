// dag-layout-worker.js -- Web Worker for layered DAG layout computation
// Receives: { nodes: {id: {parent_ids, child_ids, ...}}, edges: [...] }
// Returns:  { positions: {id: {x, y, layer}}, layers: [[ids], ...], width, height }

"use strict";

self.onmessage = function (e) {
  var data = e.data;
  var result = computeLayout(data.nodes, data.edges, data.options || {});
  self.postMessage(result);
};

function computeLayout(nodes, edges, options) {
  var nodeW = options.nodeWidth || 180;
  var nodeH = options.nodeHeight || 54;
  var layerGap = options.layerGap || 80;
  var nodeGap = options.nodeGap || 30;

  var ids = Object.keys(nodes);
  if (ids.length === 0) {
    return { positions: {}, layers: [], width: 0, height: 0 };
  }

  // Build adjacency: parent -> children, child -> parents
  var childrenOf = {};
  var parentsOf = {};
  ids.forEach(function (id) {
    childrenOf[id] = [];
    parentsOf[id] = [];
  });
  ids.forEach(function (id) {
    var node = nodes[id];
    var cids = node.child_ids || [];
    for (var i = 0; i < cids.length; i++) {
      if (nodes[cids[i]]) {
        childrenOf[id].push(cids[i]);
        parentsOf[cids[i]].push(id);
      }
    }
  });

  // Layer assignment: longest path from roots
  var layer = {};
  var visited = {};

  function assignLayer(id) {
    if (visited[id]) return layer[id];
    visited[id] = true;
    var parents = parentsOf[id];
    if (parents.length === 0) {
      layer[id] = 0;
      return 0;
    }
    var maxParent = 0;
    for (var i = 0; i < parents.length; i++) {
      var pl = assignLayer(parents[i]);
      if (pl + 1 > maxParent) maxParent = pl + 1;
    }
    layer[id] = maxParent;
    return maxParent;
  }

  ids.forEach(function (id) { assignLayer(id); });

  // Group by layer
  var maxLayer = 0;
  ids.forEach(function (id) {
    if (layer[id] > maxLayer) maxLayer = layer[id];
  });
  var layers = [];
  for (var l = 0; l <= maxLayer; l++) { layers.push([]); }
  ids.forEach(function (id) { layers[layer[id]].push(id); });

  // Crossing reduction: barycenter heuristic (2 passes)
  for (var pass = 0; pass < 4; pass++) {
    for (l = 1; l <= maxLayer; l++) {
      var bary = {};
      layers[l].forEach(function (id) {
        var parents = parentsOf[id].filter(function (p) { return layer[p] === l - 1; });
        if (parents.length === 0) {
          bary[id] = layers[l].indexOf(id);
          return;
        }
        var sum = 0;
        parents.forEach(function (p) { sum += layers[l - 1].indexOf(p); });
        bary[id] = sum / parents.length;
      });
      layers[l].sort(function (a, b) { return (bary[a] || 0) - (bary[b] || 0); });
    }
    // Reverse pass
    for (l = maxLayer - 1; l >= 0; l--) {
      var bary2 = {};
      layers[l].forEach(function (id) {
        var children = childrenOf[id].filter(function (c) { return layer[c] === l + 1; });
        if (children.length === 0) {
          bary2[id] = layers[l].indexOf(id);
          return;
        }
        var sum = 0;
        children.forEach(function (c) { sum += layers[l + 1].indexOf(c); });
        bary2[id] = sum / children.length;
      });
      layers[l].sort(function (a, b) { return (bary2[a] || 0) - (bary2[b] || 0); });
    }
  }

  // Coordinate assignment
  var positions = {};
  var totalWidth = 0;
  for (l = 0; l <= maxLayer; l++) {
    var count = layers[l].length;
    var layerWidth = count * nodeW + (count - 1) * nodeGap;
    if (layerWidth > totalWidth) totalWidth = layerWidth;
  }
  for (l = 0; l <= maxLayer; l++) {
    var count = layers[l].length;
    var layerWidth = count * nodeW + (count - 1) * nodeGap;
    var startX = (totalWidth - layerWidth) / 2;
    for (var i = 0; i < count; i++) {
      var id = layers[l][i];
      positions[id] = {
        x: startX + i * (nodeW + nodeGap) + nodeW / 2,
        y: l * (nodeH + layerGap) + nodeH / 2,
        layer: l,
      };
    }
  }

  var totalHeight = (maxLayer + 1) * (nodeH + layerGap);

  return {
    positions: positions,
    layers: layers,
    width: totalWidth,
    height: totalHeight,
    nodeWidth: nodeW,
    nodeHeight: nodeH,
  };
}
