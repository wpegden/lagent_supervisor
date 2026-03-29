function normalizeViewerVersion(value) {
  const normalized = String(value || "").trim();
  if (!normalized || /^__.*__$/.test(normalized)) {
    return null;
  }
  return normalized;
}

const state = {
  repos: [],
  currentProjectName: null,
  currentMeta: null,
  currentTimelineRepoName: null,
  currentEvents: [],
  currentProjectRepos: [],
  codexBudgetStatus: null,
  loadingOlderHistory: false,
  viewerVersion: normalizeViewerVersion(document.documentElement.dataset.viewerVersion),
};

const elements = {
  repoSearch: document.querySelector("#repo-search"),
  repoList: document.querySelector("#repo-list"),
  repoKicker: document.querySelector("#repo-kicker"),
  repoTitle: document.querySelector("#repo-title"),
  repoMeta: document.querySelector("#repo-meta"),
  repoDocPanel: document.querySelector("#repo-doc-panel"),
  repoDocLinks: document.querySelector("#repo-doc-links"),
  filters: document.querySelector("#filters"),
  cycleFilter: document.querySelector("#cycle-filter"),
  kindFilter: document.querySelector("#kind-filter"),
  branchPanel: document.querySelector("#branch-panel"),
  branchTitle: document.querySelector("#branch-title"),
  branchMeta: document.querySelector("#branch-meta"),
  branchCurrentPath: document.querySelector("#branch-current-path"),
  branchEpisodes: document.querySelector("#branch-episodes"),
  branchBoard: document.querySelector("#branch-board"),
  branchBoardTitle: document.querySelector("#branch-board-title"),
  branchBoardMeta: document.querySelector("#branch-board-meta"),
  branchBoardGrid: document.querySelector("#branch-board-grid"),
  emptyState: document.querySelector("#empty-state"),
  transcript: document.querySelector("#transcript"),
  refreshButton: document.querySelector("#refresh-button"),
  codexBudgetIndicator: document.querySelector("#codex-budget-indicator"),
  codexBudgetLabel: document.querySelector("#codex-budget-label"),
  codexBudgetValue: document.querySelector("#codex-budget-value"),
  codexBudgetReset: document.querySelector("#codex-budget-reset"),
  historyControls: document.querySelector("#history-controls"),
  historyLoadOlder: document.querySelector("#history-load-older"),
  historyMeta: document.querySelector("#history-meta"),
};

function cacheBusted(path) {
  const stamp = Date.now();
  return `${path}${path.includes("?") ? "&" : "?"}t=${stamp}`;
}

async function fetchJson(path, fallback) {
  try {
    const response = await fetch(cacheBusted(path), { cache: "no-store" });
    if (!response.ok) {
      return fallback;
    }
    return await response.json();
  } catch (_error) {
    return fallback;
  }
}

async function ensureViewerCurrent() {
  if (!state.viewerVersion) {
    return true;
  }
  const payload = await fetchJson("_assets/viewer-version.json", null);
  const latestVersion = normalizeViewerVersion(payload?.version);
  if (!latestVersion || latestVersion === state.viewerVersion) {
    return true;
  }
  window.location.reload();
  return false;
}

async function fetchLegacyEvents(repoName) {
  try {
    const response = await fetch(cacheBusted(`${repoName}/events.jsonl`), { cache: "no-store" });
    if (!response.ok) {
      return [];
    }
    const text = await response.text();
    return text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  } catch (_error) {
    return [];
  }
}

async function fetchEventsManifest(repoName) {
  const fallback = { chunk_size_cycles: 0, chunks: [] };
  const payload = await fetchJson(`${repoName}/events-manifest.json`, fallback);
  if (!payload || !Array.isArray(payload.chunks)) {
    return fallback;
  }
  const chunks = payload.chunks
    .filter((chunk) => chunk && typeof chunk.file === "string")
    .map((chunk) => ({
      file: String(chunk.file),
      start_cycle: Number(chunk.start_cycle) || 0,
      end_cycle: Number(chunk.end_cycle) || 0,
      event_count: Number(chunk.event_count) || 0,
      updated_at: chunk.updated_at || null,
    }))
    .sort((left, right) => Number(right.start_cycle) - Number(left.start_cycle));
  return {
    chunk_size_cycles: Number(payload.chunk_size_cycles) || 0,
    chunks,
  };
}

async function fetchEventsChunk(repoName, filePath) {
  try {
    const response = await fetch(cacheBusted(`${repoName}/${filePath}`), { cache: "no-store" });
    if (!response.ok) {
      return [];
    }
    const text = await response.text();
    return text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  } catch (_error) {
    return [];
  }
}

function manifestLooksStale(meta, eventsManifest) {
  const chunks = Array.isArray(eventsManifest?.chunks) ? eventsManifest.chunks : [];
  if (!chunks.length) {
    return false;
  }
  const newestChunk = chunks[0];
  const metaCycle = Number(meta?.current_cycle) || 0;
  if (metaCycle > (Number(newestChunk?.end_cycle) || 0)) {
    return true;
  }
  return timestampValue(meta?.updated_at) > timestampValue(newestChunk?.updated_at);
}

function eventTitle(event) {
  const titles = {
    worker_prompt: "Supervisor -> worker",
    worker_handoff: "Worker -> supervisor",
    validation_summary: "Supervisor validation",
    reviewer_prompt: "Supervisor -> reviewer",
    reviewer_decision: "Reviewer -> supervisor",
    input_request: "Supervisor -> human",
    human_input: "Human -> supervisor",
    phase_transition: "Phase transition",
    branch_strategy_prompt: "Supervisor -> branch strategist",
    branch_strategy_decision: "Branch strategy decision",
    branch_selection_prompt: "Supervisor -> branch selector",
    branch_selection_decision: "Branch selection decision",
    branch_replacement_prompt: "Supervisor -> branch frontier selector",
    branch_replacement_decision: "Branch frontier decision",
    cleanup_revert: "Cleanup rollback",
  };
  return titles[event.kind] || event.kind.replaceAll("_", " ");
}

function formatPhaseLabel(phase) {
  const value = String(phase || "").trim();
  if (!value) {
    return "unknown phase";
  }
  if (value === "proof_complete_style_cleanup") {
    return "PROOF COMPLETE - style cleanup";
  }
  return value.replaceAll("_", " ");
}

function timestampValue(value) {
  if (!value) {
    return 0;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function formatTimestamp(value) {
  if (!value) {
    return "No activity yet";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatPercent(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "unknown";
  }
  return `${numeric % 1 === 0 ? numeric.toFixed(0) : numeric.toFixed(1)}%`;
}

function formatBudgetReset(value) {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) {
    return new Date(numeric * 1000).toLocaleString();
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return parsed.toLocaleString();
}

function sanitizeRepoName(value) {
  const cleaned = String(value || "").trim().replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "");
  return cleaned || "repo";
}

function markdownViewerHref(repoName, file) {
  const params = new URLSearchParams({
    repo: repoName,
    path: file.href || "",
    label: file.label || "",
    source: file.path || "",
  });
  return `_assets/markdown-viewer.html?${params.toString()}`;
}

function branchRepoName(projectRepoName, episode, branch) {
  const explicit = String(branch?.repo_name || "").trim();
  if (explicit) {
    return explicit;
  }
  return sanitizeRepoName(`${projectRepoName}-${episode?.id || ""}-${branch?.name || ""}`);
}

function inferredProjectName(repo, repos, seen = new Set()) {
  const repoName = String(repo?.repo_name || "").trim();
  if (!repoName) {
    return "";
  }
  if (seen.has(repoName)) {
    return String(repo?.project_name || repoName).trim() || repoName;
  }

  const nextSeen = new Set(seen);
  nextSeen.add(repoName);

  const explicit = String(repo?.project_name || "").trim();
  const branchPath = repo?.branch_overview?.current_path_newest_to_oldest;
  const looksLikeBranchRepo = Array.isArray(branchPath) && branchPath.length > 1;
  if (explicit && looksLikeBranchRepo && explicit !== repoName) {
    const explicitParentRepo = repos.find((candidate) => String(candidate?.repo_name || "").trim() === explicit);
    if (explicitParentRepo) {
      return inferredProjectName(explicitParentRepo, repos, nextSeen);
    }
  }
  if (explicit && (!looksLikeBranchRepo || explicit !== repoName)) {
    return explicit;
  }

  for (const candidate of repos) {
    const candidateName = String(candidate?.repo_name || "").trim();
    if (!candidate || candidateName === repoName) {
      continue;
    }
    const overview = candidate.branch_overview;
    if (!overview?.has_branching) {
      continue;
    }
    for (const episode of overview.episodes || []) {
      for (const branch of episode.branches || []) {
        if (branchRepoName(candidateName, episode, branch) === repoName) {
          return inferredProjectName(candidate, repos, nextSeen);
        }
      }
    }
  }

  return explicit || repoName;
}

function timelinePath(meta) {
  const path = meta?.branch_overview?.current_path_newest_to_oldest;
  if (Array.isArray(path) && path.length) {
    return path;
  }
  return ["mainline"];
}

function timelineDepth(meta) {
  return timelinePath(meta).length;
}

function pathEndsWith(path, suffix) {
  if (!Array.isArray(path) || !Array.isArray(suffix) || suffix.length > path.length) {
    return false;
  }
  const offset = path.length - suffix.length;
  return suffix.every((part, index) => path[offset + index] === part);
}

function projectLeafRepos(project) {
  return (project?.repos || []).filter((repo) => {
    const candidatePath = timelinePath(repo);
    return !(project.repos || []).some((other) => {
      if (!other || other.repo_name === repo.repo_name) {
        return false;
      }
      const otherPath = timelinePath(other);
      return otherPath.length > candidatePath.length && pathEndsWith(otherPath, candidatePath);
    });
  });
}

function projectGroups() {
  const groups = new Map();
  state.repos.forEach((repo) => {
    const projectName = inferredProjectName(repo, state.repos);
    if (!groups.has(projectName)) {
      groups.set(projectName, {
        projectName,
        repos: [],
      });
    }
    groups.get(projectName).repos.push(repo);
  });

  return [...groups.values()]
    .map((group) => {
      const repos = [...group.repos].sort((left, right) => {
        const timeDiff = timestampValue(right.updated_at) - timestampValue(left.updated_at);
        if (timeDiff !== 0) {
          return timeDiff;
        }
        return String(left.repo_name || "").localeCompare(String(right.repo_name || ""));
      });
      const primaryRepo =
        repos.find((repo) => repo.repo_name === group.projectName) ||
        [...repos].sort((left, right) => {
          const depthDiff = timelineDepth(left) - timelineDepth(right);
          if (depthDiff !== 0) {
            return depthDiff;
          }
          return String(left.repo_name || "").localeCompare(String(right.repo_name || ""));
        })[0];
      const newestRepo = repos[0] || primaryRepo;
      const hasBranching =
        repos.length > 1 || repos.some((repo) => Boolean(repo?.branch_overview?.has_branching));
      return {
        ...group,
        repos,
        primaryRepo,
        newestRepo,
        hasBranching,
        displayName: primaryRepo?.repo_display_name || group.projectName,
      };
    })
    .sort((left, right) => {
      const timeDiff = timestampValue(right.newestRepo?.updated_at) - timestampValue(left.newestRepo?.updated_at);
      if (timeDiff !== 0) {
        return timeDiff;
      }
      return String(left.projectName).localeCompare(String(right.projectName));
    });
}

function currentProjectGroup() {
  return projectGroups().find((group) => group.projectName === state.currentProjectName) || null;
}

function resolveProjectSelection(token) {
  if (!token) {
    return null;
  }
  const groups = projectGroups();
  if (groups.some((group) => group.projectName === token)) {
    return token;
  }
  const repo = state.repos.find((entry) => entry.repo_name === token);
  return repo ? inferredProjectName(repo, state.repos) : null;
}

function filteredProjects() {
  const query = elements.repoSearch.value.trim().toLowerCase();
  const projects = projectGroups();
  if (!query) {
    return projects;
  }
  return projects.filter((project) => {
    const haystack = [
      project.projectName,
      project.displayName,
      ...project.repos.map((repo) => [repo.repo_name, repo.repo_display_name, repo.repo_path].join(" ")),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

function repoRunStatus(meta) {
  return String(meta?.run_status || "").trim().toLowerCase();
}

function repoIsPaused(meta) {
  return repoRunStatus(meta) === "paused";
}

function projectIsPaused(project) {
  return repoIsPaused(project?.newestRepo);
}

function projectSummary(project) {
  const newest = project.newestRepo || {};
  const leafCount = projectLeafRepos(project).length || project.repos.length;
  const prefix = project.hasBranching ? `${leafCount} leaf timeline${leafCount === 1 ? "" : "s"} · ` : "";
  const pausedPrefix = projectIsPaused(project) ? "paused · " : "";
  return `${pausedPrefix}${newest.current_phase || "No phase yet"} · ${prefix}${formatTimestamp(newest.updated_at)}`;
}

function renderRepoList() {
  const projects = filteredProjects();
  elements.repoList.replaceChildren();
  if (!projects.length) {
    const note = document.createElement("p");
    note.className = "subtle";
    note.textContent = "No matching projects.";
    elements.repoList.append(note);
    return;
  }

  projects.forEach((project) => {
    const link = document.createElement("a");
    link.href = `#${project.projectName}`;
    link.className = "repo-link";
    if (project.projectName === state.currentProjectName) {
      link.classList.add("active");
    }
    const newestLabel =
      project.newestRepo?.repo_name && project.newestRepo.repo_name !== project.projectName
        ? `Latest timeline: ${timelinePath(project.newestRepo).join(" ← ")}. `
        : "";
    link.innerHTML = `
      <h3>${project.displayName}</h3>
      <p>${projectSummary(project)}</p>
      <p>${newestLabel}${project.newestRepo?.last_summary || "No transcript events yet."}</p>
    `;
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      await selectProject(project.projectName, true);
    });
    elements.repoList.append(link);
  });
}

function populateFilters(events) {
  const cycles = [...new Set(events.map((event) => String(event.cycle)))].sort((left, right) => Number(right) - Number(left));
  const kinds = [...new Set(events.map((event) => event.kind))];

  const cycleValue = elements.cycleFilter.value;
  const kindValue = elements.kindFilter.value;

  elements.cycleFilter.innerHTML = '<option value="all">All cycles</option>';
  cycles.forEach((cycle) => {
    const option = document.createElement("option");
    option.value = cycle;
    option.textContent = `Cycle ${cycle}`;
    elements.cycleFilter.append(option);
  });

  elements.kindFilter.innerHTML = '<option value="all">All events</option>';
  kinds.forEach((kind) => {
    const option = document.createElement("option");
    option.value = kind;
    option.textContent = kind.replaceAll("_", " ");
    elements.kindFilter.append(option);
  });

  if ([...elements.cycleFilter.options].some((option) => option.value === cycleValue)) {
    elements.cycleFilter.value = cycleValue;
  }
  if ([...elements.kindFilter.options].some((option) => option.value === kindValue)) {
    elements.kindFilter.value = kindValue;
  }
}

function visibleEvents() {
  const cycleFilter = elements.cycleFilter.value;
  const kindFilter = elements.kindFilter.value;
  return state.currentEvents.filter((event) => {
    if (cycleFilter !== "all" && String(event.cycle) !== cycleFilter) {
      return false;
    }
    if (kindFilter !== "all" && event.kind !== kindFilter) {
      return false;
    }
    return true;
  });
}

function cycleGroups(events) {
  const groups = new Map();
  events.forEach((event) => {
    const key = String(event.cycle);
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(event);
  });
  return [...groups.entries()]
    .sort((left, right) => Number(right[0]) - Number(left[0]))
    .map(([cycle, cycleEvents]) => ({ cycle, events: cycleEvents }));
}

function createBadge(text, className = "") {
  const badge = document.createElement("span");
  badge.className = `badge ${className}`.trim();
  badge.textContent = text;
  return badge;
}

function normalizedText(value) {
  return String(value || "").trim().replace(/\s+/g, " ");
}

function truncateText(value, limit = 280) {
  const text = normalizedText(value);
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

function truncationData(value, limit = 280) {
  const fullText = normalizedText(value);
  const collapsedText = truncateText(fullText, limit);
  return {
    fullText,
    collapsedText,
    isTruncated: fullText.length > limit,
  };
}

function labeledTruncation(label, value, limit) {
  const summary = truncationData(value, limit);
  return {
    fullText: `${label}: ${summary.fullText}`,
    collapsedText: `${label}: ${summary.collapsedText}`,
    isTruncated: summary.isTruncated,
  };
}

function setExpandableText(element, summary) {
  if (!summary || !summary.isTruncated) {
    element.textContent = summary?.collapsedText || "";
    return;
  }

  let expanded = false;
  const render = () => {
    element.textContent = expanded ? summary.fullText : summary.collapsedText;
    element.classList.toggle("is-expanded", expanded);
    element.classList.toggle("is-collapsed", !expanded);
    element.setAttribute("aria-expanded", expanded ? "true" : "false");
    element.title = expanded ? "Click to collapse" : "Click to expand";
  };

  element.classList.add("expandable-text", "is-collapsed");
  element.tabIndex = 0;
  element.setAttribute("role", "button");
  element.addEventListener("click", () => {
    expanded = !expanded;
    render();
  });
  element.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    event.preventDefault();
    expanded = !expanded;
    render();
  });
  render();
}

function cycleSummaryData(events) {
  const byKind = new Map();
  events.forEach((event) => {
    byKind.set(event.kind, event);
  });

  const reviewerDecision = byKind.get("reviewer_decision");
  const branchSelection = byKind.get("branch_selection_decision");
  const branchStrategy = byKind.get("branch_strategy_decision");
  const workerHandoff = byKind.get("worker_handoff");
  const validation = byKind.get("validation_summary");
  const phaseTransition = byKind.get("phase_transition");
  const inputRequest = byKind.get("input_request");
  const humanInput = byKind.get("human_input");
  const promptFallback = events[events.length - 1];

  const headlineEvent =
    reviewerDecision ||
    branchSelection ||
    branchStrategy ||
    workerHandoff ||
    phaseTransition ||
    inputRequest ||
    humanInput ||
    validation ||
    promptFallback;

  const lines = [];
  if (workerHandoff && workerHandoff !== headlineEvent) {
    lines.push(labeledTruncation("Worker", workerHandoff.summary || eventTitle(workerHandoff), 210));
  }
  if (validation && validation !== headlineEvent) {
    lines.push(labeledTruncation("Validation", validation.summary || eventTitle(validation), 120));
  }
  if (reviewerDecision && reviewerDecision !== headlineEvent) {
    lines.push(labeledTruncation("Review", reviewerDecision.summary || eventTitle(reviewerDecision), 220));
  }
  if (branchStrategy && branchStrategy !== headlineEvent) {
    lines.push(labeledTruncation("Branch strategy", branchStrategy.summary || eventTitle(branchStrategy), 180));
  }
  if (branchSelection && branchSelection !== headlineEvent) {
    lines.push(labeledTruncation("Branch selection", branchSelection.summary || eventTitle(branchSelection), 180));
  }
  if (phaseTransition && phaseTransition !== headlineEvent) {
    lines.push(labeledTruncation("Transition", phaseTransition.summary || eventTitle(phaseTransition), 120));
  }
  if (inputRequest && inputRequest !== headlineEvent) {
    lines.push(labeledTruncation("Input", inputRequest.summary || eventTitle(inputRequest), 120));
  }
  if (humanInput && humanInput !== headlineEvent) {
    lines.push(labeledTruncation("Human", humanInput.summary || eventTitle(humanInput), 120));
  }

  return {
    headline: truncationData(headlineEvent ? (headlineEvent.summary || eventTitle(headlineEvent)) : "No summary yet.", 320),
    detailLines: lines.slice(0, 4),
    reviewerDecision,
    workerHandoff,
  };
}

function cycleLeanDelta(projectRepo, cycle) {
  const cycles = projectRepo?.leanCycleStats?.cycles;
  if (!cycles || typeof cycles !== "object") {
    return null;
  }
  const entry = cycles[String(cycle)] || cycles[cycle];
  if (!entry) {
    return null;
  }
  const added = Number(entry.lean_added);
  const removed = Number(entry.lean_removed);
  const filesTouched = Number(entry.lean_files_touched);
  if (!Number.isFinite(added) || !Number.isFinite(removed)) {
    return null;
  }
  return {
    added,
    removed,
    filesTouched: Number.isFinite(filesTouched) ? filesTouched : 0,
  };
}

function timelineLeanTotals(projectRepo) {
  const cycles = projectRepo?.leanCycleStats?.cycles;
  if (!cycles || typeof cycles !== "object") {
    return null;
  }

  let added = 0;
  let removed = 0;
  let fileTouchCount = 0;
  let cycleCount = 0;

  Object.values(cycles).forEach((entry) => {
    const cycleAdded = Number(entry?.lean_added);
    const cycleRemoved = Number(entry?.lean_removed);
    const cycleFiles = Number(entry?.lean_files_touched);
    if (!Number.isFinite(cycleAdded) || !Number.isFinite(cycleRemoved)) {
      return;
    }
    added += cycleAdded;
    removed += cycleRemoved;
    if (Number.isFinite(cycleFiles) && cycleFiles > 0) {
      fileTouchCount += cycleFiles;
    }
    cycleCount += 1;
  });

  if (cycleCount === 0) {
    return null;
  }

  return {
    added,
    removed,
    fileTouchCount,
    cycleCount,
  };
}

function accumulateLeanTotals(left, right) {
  if (!right) {
    return left;
  }
  return {
    added: (left?.added || 0) + right.added,
    removed: (left?.removed || 0) + right.removed,
    fileTouchCount: (left?.fileTouchCount || 0) + (right?.fileTouchCount || 0),
    cycleCount: (left?.cycleCount || 0) + right.cycleCount,
  };
}

function lineageLeanTotals(projectRepo, nodes = null) {
  if (!projectRepo?.meta) {
    return null;
  }
  const timelineNodes = nodes || projectTimelineNodes();
  const node = timelineNodes.get(timelineKeyFromMeta(projectRepo.meta));
  if (!node) {
    return timelineLeanTotals(projectRepo);
  }
  const chain = ancestorChainFromLeaf(node, timelineNodes).reverse();
  let totals = null;
  chain.forEach((ancestor) => {
    totals = accumulateLeanTotals(totals, timelineLeanTotals(ancestor.projectRepo));
  });
  return totals;
}

function formatLeanDelta(delta) {
  if (!delta) {
    return "";
  }
  const filesSuffix =
    delta.filesTouched > 0 ? ` · ${delta.filesTouched} file${delta.filesTouched === 1 ? "" : "s"}` : "";
  return `Lean +${delta.added} / -${delta.removed}${filesSuffix}`;
}

function buildLeanDeltaElement(delta) {
  if (!delta) {
    return null;
  }
  const line = document.createElement("p");
  line.className = "subtle cycle-delta";

  const label = document.createElement("span");
  label.className = "cycle-delta-label";
  label.textContent = "Lean ";

  const added = document.createElement("span");
  added.className = "cycle-delta-added";
  added.textContent = `+${delta.added}`;

  const separator = document.createElement("span");
  separator.className = "cycle-delta-separator";
  separator.textContent = " / ";

  const removed = document.createElement("span");
  removed.className = "cycle-delta-removed";
  removed.textContent = `-${delta.removed}`;

  line.append(label, added, separator, removed);
  if (delta.filesTouched > 0) {
    const files = document.createElement("span");
    files.className = "cycle-delta-files";
    files.textContent = ` · ${delta.filesTouched} file${delta.filesTouched === 1 ? "" : "s"}`;
    line.append(files);
  }
  line.title = formatLeanDelta(delta);
  return line;
}

function formatLeanTotals(totals) {
  if (!totals) {
    return "";
  }
  const cycleSuffix =
    totals.cycleCount > 0
      ? ` · ${totals.cycleCount} validated cycle${totals.cycleCount === 1 ? "" : "s"}`
      : "";
  return `Lean total +${totals.added} / -${totals.removed}${cycleSuffix}`;
}

function buildLeanTotalsElement(totals, labelText = "Lean total ") {
  if (!totals) {
    return null;
  }

  const line = document.createElement("p");
  line.className = "subtle timeline-total-delta";

  const label = document.createElement("span");
  label.className = "cycle-delta-label";
  label.textContent = labelText;

  const added = document.createElement("span");
  added.className = "cycle-delta-added";
  added.textContent = `+${totals.added}`;

  const separator = document.createElement("span");
  separator.className = "cycle-delta-separator";
  separator.textContent = " / ";

  const removed = document.createElement("span");
  removed.className = "cycle-delta-removed";
  removed.textContent = `-${totals.removed}`;

  line.append(label, added, separator, removed);
  if (totals.cycleCount > 0) {
    const cycles = document.createElement("span");
    cycles.className = "cycle-delta-files";
    cycles.textContent = ` · ${totals.cycleCount} validated cycle${totals.cycleCount === 1 ? "" : "s"}`;
    line.append(cycles);
  }
  line.title = formatLeanTotals(totals);
  return line;
}

function buildCycleCard(cycle, events, extraClass = "", projectRepo = null) {
  const firstEvent = events[0];
  const lastEvent = events[events.length - 1];
  const summary = cycleSummaryData(events);
  const leanDelta = cycleLeanDelta(projectRepo, cycle);

  const cycleCard = document.createElement("article");
  cycleCard.className = `cycle-card ${extraClass}`.trim();

  const header = document.createElement("div");
  header.className = "cycle-card-header";

  const heading = document.createElement("div");
  heading.className = "cycle-card-heading";
  const title = document.createElement("h3");
  title.className = "cycle-heading";
  title.textContent = `Cycle ${cycle}`;
  const meta = document.createElement("p");
  meta.className = "subtle cycle-meta";
  meta.textContent = `${formatPhaseLabel(firstEvent?.phase)} · ${events.length} event${events.length === 1 ? "" : "s"} · ${formatTimestamp(lastEvent?.timestamp)}`;
  heading.append(title, meta);
  if (leanDelta) {
    const delta = buildLeanDeltaElement(leanDelta);
    if (delta) {
      heading.append(delta);
    }
  }

  const badges = document.createElement("div");
  badges.className = "cycle-status-badges";
  badges.append(createBadge(formatPhaseLabel(firstEvent?.phase), String(firstEvent?.phase || "unknown")));
  if (summary.reviewerDecision?.content?.decision) {
    badges.append(
      createBadge(
        String(summary.reviewerDecision.content.decision),
        `decision-${String(summary.reviewerDecision.content.decision).toLowerCase()}`,
      ),
    );
  }
  if (summary.workerHandoff?.content?.status) {
    badges.append(
      createBadge(
        String(summary.workerHandoff.content.status),
        `status-${String(summary.workerHandoff.content.status).toLowerCase()}`,
      ),
    );
  }
  header.append(heading, badges);

  const headline = document.createElement("p");
  headline.className = "cycle-summary";
  setExpandableText(headline, summary.headline);

  cycleCard.append(header, headline);
  if (summary.detailLines.length) {
    const detailList = document.createElement("div");
    detailList.className = "cycle-detail-list";
    summary.detailLines.forEach((line) => {
      const item = document.createElement("p");
      item.className = "cycle-detail";
      setExpandableText(item, line);
      detailList.append(item);
    });
    cycleCard.append(detailList);
  }

  return cycleCard;
}

function projectIsBranched(projectRepos = state.currentProjectRepos) {
  return projectRepos.some((projectRepo) => Boolean(projectRepo?.meta?.branch_overview?.has_branching)) || projectRepos.length > 1;
}

function currentRootRepoName(projectRepos = state.currentProjectRepos) {
  const project = currentProjectGroup();
  return project?.primaryRepo?.repo_name || state.currentProjectName || projectRepos[0]?.meta?.repo_name || "";
}

function currentTranscriptProjectRepo(projectRepos = state.currentProjectRepos) {
  if (!projectRepos.length) {
    return null;
  }
  if (state.currentTimelineRepoName) {
    const explicit = projectRepos.find((repo) => repo.meta.repo_name === state.currentTimelineRepoName);
    if (explicit) {
      return explicit;
    }
  }
  if (!projectIsBranched(projectRepos)) {
    const primaryRepoName = currentRootRepoName(projectRepos);
    return projectRepos.find((repo) => repo.meta.repo_name === primaryRepoName) || projectRepos[0] || null;
  }

  const nodes = projectTimelineNodes(projectRepos);
  const liveLeaves = liveLeafNodes(nodes).sort(leafSort);
  if (liveLeaves.length) {
    return liveLeaves[0].projectRepo;
  }

  const primaryRepoName = currentRootRepoName(projectRepos);
  return projectRepos.find((repo) => repo.meta.repo_name === primaryRepoName) || projectRepos[0] || null;
}

function shouldShowTranscript() {
  return state.currentProjectRepos.length > 0;
}

function projectBranchOverview() {
  const nodes = projectTimelineNodes();
  if (!nodes.size) {
    return null;
  }
  const episodesByContext = projectEpisodesByContext(nodes);
  const episodes = [...episodesByContext.values()]
    .flat()
    .sort((left, right) => Number(right.trigger_cycle || 0) - Number(left.trigger_cycle || 0))
    .map(({ sourceRepo: _sourceRepo, sourceScore: _sourceScore, contextKey, contextNode, branchEvent, selectionEvent, branchTimestamp, selectionTimestamp, ...episode }) => episode);
  if (!episodes.length) {
    return null;
  }

  const liveLeaves = liveLeafNodes(nodes).sort(leafSort);
  const derivedPath = liveLeaves.length === 1 ? liveLeaves[0].pathNewToOld : null;
  return {
    has_branching: true,
    episodes,
    current_path_newest_to_oldest: derivedPath || null,
    current_path_status: liveLeaves.length === 1 ? "alive" : null,
    active_leaf_paths: liveLeaves.map((leaf) => leaf.pathNewToOld),
    active_leaf_count: liveLeaves.length,
  };
}

function statusLabel(branch) {
  if (branch.is_current_path && branch.status === "dead") {
    return "dead current path";
  }
  if (branch.is_current_path && branch.status === "selected") {
    return "current winner";
  }
  if (branch.is_current_path) {
    return "current path";
  }
  return branch.status;
}

function renderBranchOverview() {
  const overview = projectBranchOverview();
  if (!overview?.has_branching) {
    elements.branchPanel.hidden = true;
    elements.branchCurrentPath.replaceChildren();
    elements.branchEpisodes.replaceChildren();
    return;
  }

  elements.branchPanel.hidden = false;
  elements.branchTitle.textContent = "Branch history so far";
  elements.branchMeta.textContent =
    `${overview.episodes?.length || 0} branch episode${(overview.episodes?.length || 0) === 1 ? "" : "s"} · ` +
    `${overview.active_leaf_count || 0} live timeline${(overview.active_leaf_count || 0) === 1 ? "" : "s"}`;

  elements.branchCurrentPath.replaceChildren();
  const transcriptRepo = currentTranscriptProjectRepo();
  const nodes = projectTimelineNodes();
  const transcriptPath = transcriptRepo ? timelinePath(transcriptRepo.meta) : null;
  if (transcriptPath?.length) {
    const transcriptLabel = document.createElement("p");
    transcriptLabel.className = "branch-current-label";
    transcriptLabel.textContent = "Transcript shown below";
    const transcriptValue = document.createElement("p");
    transcriptValue.className = "branch-current-value";
    transcriptValue.textContent = transcriptPath.join(" ← ");
    const transcriptTotals = buildLeanTotalsElement(lineageLeanTotals(transcriptRepo, nodes));
    elements.branchCurrentPath.append(transcriptLabel, transcriptValue);
    if (transcriptTotals) {
      elements.branchCurrentPath.append(transcriptTotals);
    }
  }

  elements.branchEpisodes.replaceChildren();
  (overview.episodes || []).forEach((episode) => {
    const card = document.createElement("article");
    card.className = "branch-episode-card";

    const header = document.createElement("div");
    header.className = "branch-episode-header";
    const title = document.createElement("h4");
    title.className = "branch-episode-title";
    title.textContent = `${episode.id} · cycle ${episode.trigger_cycle || "?"}`;
    const meta = document.createElement("p");
    meta.className = "subtle branch-episode-meta";
    const contextPath = episode.lineage_newest_to_oldest?.length ? episode.lineage_newest_to_oldest.join(" ← ") : "mainline";
    meta.textContent = `${formatPhaseLabel(episode.phase)} · ${episode.status} · context ${contextPath}`;
    header.append(title, meta);

    const summaries = document.createElement("div");
    summaries.className = "branch-episode-summaries";
    if (episode.branchSummary) {
      const branchSummary = document.createElement("p");
      branchSummary.className = "branch-item-summary";
      branchSummary.textContent = `BRANCH: ${strippedBoundarySummary("BRANCH", episode.branchSummary)}`;
      summaries.append(branchSummary);
    }
    if (episode.selectionSummary) {
      const selectionSummary = document.createElement("p");
      selectionSummary.className = "branch-item-summary";
      selectionSummary.textContent = `SELECT_BRANCH: ${strippedBoundarySummary("SELECT_BRANCH", episode.selectionSummary)}`;
      summaries.append(selectionSummary);
    }

    const branchList = document.createElement("div");
    branchList.className = "branch-list";
    const branches = [...(episode.branches || [])].sort((left, right) => {
      const leftLive = left.status === "active" || left.status === "selected";
      const rightLive = right.status === "active" || right.status === "selected";
      if (leftLive !== rightLive) {
        return Number(rightLive) - Number(leftLive);
      }
      const rank = { active: 3, selected: 2, dead: 1 };
      return (rank[right.status] || 0) - (rank[left.status] || 0);
    });

    branches.forEach((branch) => {
      const item = document.createElement("article");
      item.className = `branch-item status-${branch.status}${branch.is_current_path ? " is-current-path" : ""}`;

      const top = document.createElement("div");
      top.className = "branch-item-top";
      const name = document.createElement("h5");
      name.className = "branch-item-name";
      name.textContent = branch.name;
      const status = document.createElement("span");
      status.className = `branch-status status-${branch.status}`;
      status.textContent = statusLabel(branch);
      top.append(name, status);

      const summary = document.createElement("p");
      summary.className = "branch-item-summary";
      summary.textContent = branch.summary || "No summary recorded.";

      const path = document.createElement("p");
      path.className = "branch-item-path subtle";
      path.textContent = (branch.path_newest_to_oldest || []).join(" ← ");

      item.append(top, summary, path);
      branchList.append(item);
    });

    card.append(header);
    if (summaries.childElementCount) {
      card.append(summaries);
    }
    card.append(branchList);
    elements.branchEpisodes.append(card);
  });
}

function branchStatusMap() {
  const map = new Map();
  for (const projectRepo of state.currentProjectRepos) {
    const overview = projectRepo.meta?.branch_overview;
    const rootRepoName = projectRepo.meta?.repo_name || "";
    for (const episode of overview?.episodes || []) {
      for (const branch of episode.branches || []) {
        map.set(branchRepoName(rootRepoName, episode, branch), branch.status || "active");
      }
    }
  }
  return map;
}

function timelineStatus(projectRepo) {
  const rootRepoName = currentRootRepoName();
  if (projectRepo.meta.repo_name === rootRepoName) {
    return "mainline";
  }
  const explicit = branchStatusMap().get(projectRepo.meta.repo_name);
  if (explicit) {
    return explicit;
  }
  return projectRepo.meta?.branch_overview?.current_path_status === "dead" ? "dead" : "active";
}

function timelineName(projectRepo) {
  const rootRepoName = currentRootRepoName();
  if (projectRepo.meta.repo_name === rootRepoName) {
    return "mainline";
  }
  return timelinePath(projectRepo.meta)[0] || projectRepo.meta.repo_display_name || projectRepo.meta.repo_name;
}

function projectTimelineNodes(projectRepos = state.currentProjectRepos) {
  const nodes = new Map();
  [...projectRepos]
    .filter((projectRepo) => projectRepo?.meta)
    .forEach((projectRepo) => {
      const pathNewToOld = timelinePath(projectRepo.meta);
      const pathOldToNew = [...pathNewToOld].reverse();
      const key = pathOldToNew.join("::");
      nodes.set(key, {
        key,
        name: timelineName(projectRepo),
        status: timelineStatus(projectRepo),
        pathNewToOld,
        pathOldToNew,
        depth: pathOldToNew.length,
        parentKey: pathOldToNew.length > 1 ? pathOldToNew.slice(0, -1).join("::") : null,
        projectRepo,
        children: [],
      });
    });

  nodes.forEach((node) => {
    if (node.parentKey && nodes.has(node.parentKey)) {
      nodes.get(node.parentKey).children.push(node.key);
    }
  });
  return nodes;
}

function liveLeafNodes(nodes) {
  return [...nodes.values()].filter((node) => node.children.length === 0 && node.status !== "dead");
}

function derivedCurrentProjectPath() {
  const nodes = projectTimelineNodes();
  const liveLeaves = liveLeafNodes(nodes).sort(leafSort);
  if (liveLeaves.length !== 1) {
    return null;
  }
  return liveLeaves[0].pathNewToOld;
}

function leafSort(left, right) {
  const statusRank = { selected: 4, active: 3, dead: 2, mainline: 1 };
  const timeDiff = timestampValue(right.projectRepo.meta.updated_at) - timestampValue(left.projectRepo.meta.updated_at);
  if (timeDiff !== 0) {
    return timeDiff;
  }
  const branchDiff = (statusRank[right.status] || 0) - (statusRank[left.status] || 0);
  if (branchDiff !== 0) {
    return branchDiff;
  }
  return String(left.projectRepo.meta.repo_name || "").localeCompare(String(right.projectRepo.meta.repo_name || ""));
}

function ancestorChainFromLeaf(node, nodes) {
  const chain = [node];
  let current = node;
  while (current.parentKey && nodes.has(current.parentKey)) {
    current = nodes.get(current.parentKey);
    chain.push(current);
  }
  return chain;
}

function paddedLeafChain(chain, targetLength) {
  const padded = [...chain];
  while (padded.length < targetLength) {
    const repeated = padded[Math.max(0, padded.length - 2)] || padded[padded.length - 1];
    padded.splice(Math.max(0, padded.length - 1), 0, repeated);
  }
  return padded;
}

function timelineKeyFromPath(pathNewToOld) {
  if (!Array.isArray(pathNewToOld) || !pathNewToOld.length) {
    return "mainline";
  }
  return [...pathNewToOld].reverse().join("::");
}

function timelineKeyFromMeta(meta) {
  return timelineKeyFromPath(timelinePath(meta));
}

function findEpisodeEvent(projectRepo, cycle, kind) {
  const matches = (projectRepo?.events || []).filter(
    (event) => String(event.kind || "") === kind && Number(event.cycle) === Number(cycle),
  );
  return matches[matches.length - 1] || null;
}

function projectEpisodesByContext(nodes) {
  const episodes = new Map();

  for (const projectRepo of state.currentProjectRepos) {
    if (!projectRepo?.meta) {
      continue;
    }
    const repoKey = timelineKeyFromMeta(projectRepo.meta);
    for (const episode of projectRepo.meta?.branch_overview?.episodes || []) {
      const contextPath = Array.isArray(episode.lineage_newest_to_oldest) && episode.lineage_newest_to_oldest.length
        ? episode.lineage_newest_to_oldest
        : ["mainline"];
      const contextKey = timelineKeyFromPath(contextPath);
      const dedupeKey = `${contextKey}|${episode.id}`;
      const score = repoKey === contextKey ? 0 : 1 + Math.abs(timelineDepth(projectRepo.meta) - contextPath.length);
      const existing = episodes.get(dedupeKey);
      if (!existing || score < existing.sourceScore) {
        episodes.set(dedupeKey, {
          ...episode,
          contextKey,
          sourceRepo: projectRepo,
          sourceScore: score,
        });
      }
    }
  }

  const byContext = new Map();
  for (const episode of episodes.values()) {
    const branchEvent = findEpisodeEvent(episode.sourceRepo, episode.trigger_cycle, "branch_strategy_decision");
    const selectionEvent = findEpisodeEvent(episode.sourceRepo, episode.trigger_cycle, "branch_selection_decision");
    const enriched = {
      ...episode,
      branchEvent,
      selectionEvent,
      branchTimestamp: branchEvent ? timestampValue(branchEvent.timestamp) : null,
      selectionTimestamp: selectionEvent ? timestampValue(selectionEvent.timestamp) : null,
      branchSummary: branchEvent?.summary || "",
      selectionSummary: selectionEvent?.summary || "",
      contextNode: nodes.get(episode.contextKey) || null,
    };
    if (!byContext.has(episode.contextKey)) {
      byContext.set(episode.contextKey, []);
    }
    byContext.get(episode.contextKey).push(enriched);
  }

  byContext.forEach((items) => {
    items.sort((left, right) => Number(right.trigger_cycle || 0) - Number(left.trigger_cycle || 0));
  });

  return byContext;
}

function contextEpisode(episodesByContext, nodeKey) {
  const items = episodesByContext.get(nodeKey) || [];
  return items[0] || null;
}

function childNodeForBranch(node, branchName, nodes) {
  if (!node || !branchName) {
    return null;
  }
  const directKey = `${node.key}::${branchName}`;
  if (nodes.has(directKey)) {
    return nodes.get(directKey);
  }
  return [...node.children]
    .map((key) => nodes.get(key))
    .find((child) => child && String(child.pathNewToOld[0] || "") === String(branchName)) || null;
}

function orderedEpisodeChildren(node, episode, nodes) {
  const children = (episode?.branches || [])
    .map((branch) => {
      const child = childNodeForBranch(node, branch.name, nodes);
      if (!child) {
        return null;
      }
      return { child, branch };
    })
    .filter(Boolean);

  children.sort((left, right) => {
    const leftIsSelected = String(left.branch.name || "") === String(episode?.selected_branch || "");
    const rightIsSelected = String(right.branch.name || "") === String(episode?.selected_branch || "");
    if (leftIsSelected !== rightIsSelected) {
      return Number(rightIsSelected) - Number(leftIsSelected);
    }
    if (left.branch.status !== right.branch.status) {
      const rank = { selected: 4, active: 3, dead: 1 };
      return (rank[right.branch.status] || 0) - (rank[left.branch.status] || 0);
    }
    return leafSort(left.child, right.child);
  });

  return children;
}

function segmentEvents(events, startExclusive = null, endExclusive = null) {
  const excludedKinds = new Set([
    "branch_strategy_prompt",
    "branch_strategy_decision",
    "branch_selection_prompt",
    "branch_selection_decision",
  ]);
  return events.filter((event) => {
    if (excludedKinds.has(String(event.kind || ""))) {
      return false;
    }
    const stamp = timestampValue(event.timestamp);
    if (startExclusive !== null && stamp <= startExclusive) {
      return false;
    }
    if (endExclusive !== null && stamp >= endExclusive) {
      return false;
    }
    return true;
  });
}

function segmentSummary(events, fallback) {
  const grouped = cycleGroups(events);
  if (!grouped.length) {
    return fallback;
  }
  return cycleSummaryData(grouped[0].events).headline.collapsedText;
}

function strippedBoundarySummary(label, summary) {
  const text = String(summary || "").trim();
  const prefix = `${label}:`;
  if (text.startsWith(prefix)) {
    return text.slice(prefix.length).trim();
  }
  return text;
}

function buildCardsRow(cards) {
  return { type: "cards", cards };
}

function buildBoundaryRow(label, summary, tone) {
  const text = strippedBoundarySummary(label, summary);
  if (!text) {
    return null;
  }
  return {
    type: "boundary",
    label,
    tone,
    summary: text,
  };
}

function buildProjectRowsForNode(node, nodes, episodesByContext, startExclusive = null) {
  if (!node) {
    return [];
  }

  const rows = [];
  const episode = contextEpisode(episodesByContext, node.key);
  if (episode?.status === "selected") {
    const selectedChild = childNodeForBranch(node, episode.selected_branch, nodes);
    rows.push(...buildProjectRowsForNode(selectedChild, nodes, episodesByContext, episode.selectionTimestamp));

    const selectionRow = buildBoundaryRow("SELECT_BRANCH", episode.selectionSummary, "selection");
    if (selectionRow) {
      rows.push(selectionRow);
    }

    const splitCards = orderedEpisodeChildren(node, episode, nodes).map(({ child, branch }) => ({
      node: child,
      branch,
      events: segmentEvents(child.projectRepo.events || [], episode.branchTimestamp, episode.selectionTimestamp),
    }));
    if (splitCards.length) {
      rows.push(buildCardsRow(splitCards));
    }

    const branchRow = buildBoundaryRow("BRANCH", episode.branchSummary, "branch");
    if (branchRow) {
      rows.push(branchRow);
    }

    const nodeEvents = segmentEvents(node.projectRepo.events || [], startExclusive, episode.branchTimestamp);
    if (nodeEvents.length || !node.parentKey) {
      rows.push(buildCardsRow([{ node, events: nodeEvents }]));
    }
    return rows;
  }

  if (episode?.status === "active") {
    const activeCards = orderedEpisodeChildren(node, episode, nodes)
      .filter(({ branch }) => String(branch.status || "") !== "dead")
      .map(({ child, branch }) => ({
        node: child,
        branch,
        events: segmentEvents(child.projectRepo.events || [], episode.branchTimestamp, null),
      }));
    if (activeCards.length) {
      rows.push(buildCardsRow(activeCards));
    }

    const branchRow = buildBoundaryRow("BRANCH", episode.branchSummary, "branch");
    if (branchRow) {
      rows.push(branchRow);
    }

    const nodeEvents = segmentEvents(node.projectRepo.events || [], startExclusive, episode.branchTimestamp);
    if (nodeEvents.length || !node.parentKey) {
      rows.push(buildCardsRow([{ node, events: nodeEvents }]));
    }
    return rows;
  }

  const nodeEvents = segmentEvents(node.projectRepo.events || [], startExclusive, null);
  if (nodeEvents.length || !node.parentKey) {
    rows.push(buildCardsRow([{ node, events: nodeEvents }]));
  }
  return rows;
}

function distributedGridSlots(columnCount, itemCount) {
  const slots = [];
  let start = 1;
  let remainingColumns = Math.max(1, columnCount);
  let remainingItems = Math.max(1, itemCount);
  for (let index = 0; index < itemCount; index += 1) {
    const span = Math.max(1, Math.floor(remainingColumns / remainingItems) + (remainingColumns % remainingItems > 0 ? 1 : 0));
    slots.push({ start, span });
    start += span;
    remainingColumns -= span;
    remainingItems -= 1;
  }
  return slots;
}

function buildCardsRowElement(cards, columnCount) {
  const rowElement = document.createElement("section");
  rowElement.className = "branch-tree-row";
  rowElement.style.gridTemplateColumns = `repeat(${columnCount}, minmax(18rem, 1fr))`;

  const slots = distributedGridSlots(columnCount, cards.length);
  cards.forEach((card, index) => {
    const slot = slots[index];
    const content = buildTimelineNodeCard(card.node, card.events, {
      summary: segmentSummary(card.events, card.node.projectRepo.meta.last_summary || "No summary recorded yet."),
      selectable: true,
    });
    content.style.gridColumn = `${slot.start} / span ${slot.span}`;
    rowElement.append(content);
  });
  return rowElement;
}

function buildBoundaryRowElement(row, columnCount) {
  const rowElement = document.createElement("section");
  rowElement.className = "branch-tree-row branch-boundary-row";
  rowElement.style.gridTemplateColumns = `repeat(${columnCount}, minmax(18rem, 1fr))`;

  const note = document.createElement("article");
  note.className = `branch-boundary-note tone-${row.tone || "branch"}`;
  note.style.gridColumn = `1 / span ${columnCount}`;

  const label = document.createElement("p");
  label.className = "branch-boundary-label";
  label.textContent = row.label.replaceAll("_", " ");

  const summary = document.createElement("p");
  summary.className = "branch-boundary-summary";
  summary.textContent = row.summary;

  note.append(label, summary);
  rowElement.append(note);
  return rowElement;
}

function buildTimelineNodeCard(node, events, options = {}) {
  const projectRepo = node.projectRepo;
  const branchLeanTotals = timelineLeanTotals(projectRepo);
  const nodeCard = document.createElement("article");
  const selected = projectRepo.meta.repo_name === state.currentTimelineRepoName;
  nodeCard.className =
    `timeline-column branch-tree-node status-${node.status}` +
    `${selected ? " is-selected-transcript" : ""}` +
    `${options.extraClass ? ` ${options.extraClass}` : ""}`;

  const header = document.createElement("div");
  header.className = "timeline-column-header";

  const headingRow = document.createElement("div");
  headingRow.className = "timeline-column-heading-row";
  const title = document.createElement("h4");
  title.className = "timeline-column-title";
  title.textContent = options.title || node.name;
  const badges = document.createElement("div");
  badges.className = "timeline-column-badges";
  if (repoIsPaused(projectRepo.meta)) {
    badges.append(createBadge("paused", "status-paused"));
  }
  badges.append(createBadge(projectRepo.meta.current_phase || "unknown"));
  badges.append(createBadge(node.status, `status-${node.status}`));
  if (selected) {
    badges.append(createBadge("transcript", "status-selected"));
  }
  headingRow.append(title, badges);

  const path = document.createElement("p");
  path.className = "timeline-column-path subtle";
  path.textContent = options.pathText || node.pathNewToOld.join(" ← ");

  const summary = document.createElement("p");
  summary.className = "timeline-column-summary";
  summary.textContent = options.summary || segmentSummary(events, projectRepo.meta.last_summary || "No summary recorded yet.");
  const totalsLine = buildLeanTotalsElement(branchLeanTotals, "Lean branch ");

  const docs = document.createElement("div");
  docs.className = "timeline-column-docs";
  appendTimelineDocLinks(docs, projectRepo);

  header.append(headingRow, path, summary);
  if (totalsLine) {
    header.append(totalsLine);
  }
  if (docs.childElementCount) {
    header.append(docs);
  }

  if (options.selectable) {
    nodeCard.tabIndex = 0;
    nodeCard.role = "button";
    nodeCard.setAttribute("aria-pressed", selected ? "true" : "false");
    const activate = () => updateTranscriptSelection(projectRepo.meta.repo_name);
    nodeCard.addEventListener("click", activate);
    nodeCard.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate();
      }
    });
  }

  const cycles = document.createElement("div");
  cycles.className = "timeline-column-cycles";
  const grouped = cycleGroups(events);
  if (!grouped.length) {
    const empty = document.createElement("p");
    empty.className = "subtle timeline-empty";
    empty.textContent = "No exported events yet for this timeline.";
    cycles.append(empty);
  } else {
    grouped.forEach(({ cycle, events: cycleEvents }) => {
      cycles.append(buildCycleCard(cycle, cycleEvents, "branch-cycle-card", projectRepo));
    });
  }

  nodeCard.append(header, cycles);
  return nodeCard;
}

function updateTranscriptSelection(repoName) {
  state.currentTimelineRepoName = repoName;
  const transcriptRepo = currentTranscriptProjectRepo();
  state.currentEvents = transcriptRepo?.events || [];
  populateFilters(state.currentEvents);
  renderHeader();
  renderFilters();
  renderBranchOverview();
  renderBranchBoard();
  renderEvents();
}

function buildTimelineSelectorCard(node) {
  const projectRepo = node.projectRepo;
  const selected = projectRepo.meta.repo_name === state.currentTimelineRepoName;

  const card = document.createElement("article");
  card.className = `timeline-selector-card status-${node.status}${selected ? " is-selected-transcript" : ""}`;
  card.tabIndex = 0;
  card.role = "button";
  card.setAttribute("aria-pressed", selected ? "true" : "false");

  const headingRow = document.createElement("div");
  headingRow.className = "timeline-column-heading-row";
  const title = document.createElement("h4");
  title.className = "timeline-column-title";
  title.textContent = node.name;
  const badges = document.createElement("div");
  badges.className = "timeline-column-badges";
  if (repoIsPaused(projectRepo.meta)) {
    badges.append(createBadge("paused", "status-paused"));
  }
  badges.append(createBadge(projectRepo.meta.current_phase || "unknown"));
  badges.append(createBadge(node.status, `status-${node.status}`));
  if (selected) {
    badges.append(createBadge("transcript", "status-selected"));
  }
  headingRow.append(title, badges);

  const path = document.createElement("p");
  path.className = "timeline-column-path subtle";
  path.textContent = node.pathNewToOld.join(" ← ");

  const summary = document.createElement("p");
  summary.className = "timeline-column-summary";
  summary.textContent = projectRepo.meta.last_summary || "No summary recorded yet.";

  const meta = document.createElement("p");
  meta.className = "timeline-empty";
  meta.textContent = `cycle ${projectRepo.meta.current_cycle || "?"} · last update ${formatTimestamp(projectRepo.meta.updated_at)}`;

  const activate = () => updateTranscriptSelection(projectRepo.meta.repo_name);
  card.addEventListener("click", activate);
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      activate();
    }
  });

  card.append(headingRow, path, summary, meta);
  return card;
}

function appendTimelineDocLinks(container, projectRepo) {
  const files = Array.isArray(projectRepo.meta?.markdown_files) ? projectRepo.meta.markdown_files : [];
  container.replaceChildren();
  files.forEach((file) => {
    const link = document.createElement("a");
    link.className = "doc-link";
    link.href = markdownViewerHref(projectRepo.meta.repo_name, file);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = file.label || file.path || "Markdown file";
    link.title = file.path || file.label || "Markdown file";
    container.append(link);
  });
}

function renderBranchBoard() {
  if (!projectIsBranched()) {
    elements.branchBoard.hidden = true;
    elements.branchBoardGrid.replaceChildren();
    return;
  }

  const nodes = projectTimelineNodes();
  const rootNode = [...nodes.values()].find((node) => !node.parentKey) || null;
  if (!rootNode) {
    elements.branchBoard.hidden = true;
    elements.branchBoardGrid.replaceChildren();
    return;
  }
  const episodesByContext = projectEpisodesByContext(nodes);
  const rows = buildProjectRowsForNode(rootNode, nodes, episodesByContext);
  const columnCount = Math.max(
    1,
    ...rows
      .filter((row) => row.type === "cards")
      .map((row) => row.cards.length),
  );

  elements.branchBoard.hidden = false;
  elements.branchBoardTitle.textContent = "Branch timelines";
  elements.branchBoardMeta.textContent =
    "Recent branch activity appears above earlier shared history. Click any timeline column to focus that branch and update the project links above.";
  elements.branchBoardGrid.replaceChildren();
  rows.forEach((row) => {
    if (row.type === "boundary") {
      elements.branchBoardGrid.append(buildBoundaryRowElement(row, columnCount));
      return;
    }
    elements.branchBoardGrid.append(buildCardsRowElement(row.cards, columnCount));
  });
}

function renderFilters() {
  elements.filters.hidden = projectIsBranched() || !(state.currentProjectRepos.length && state.currentEvents.length);
}

function renderHeader() {
  renderCodexBudgetStatus();
  const project = currentProjectGroup();
  if (!state.currentMeta || !project) {
    elements.repoKicker.textContent = "Transcript";
    elements.repoTitle.textContent = "No project selected";
    elements.repoMeta.textContent = "Choose a project from the left.";
    elements.repoDocLinks.replaceChildren();
    elements.repoDocPanel.hidden = true;
    return;
  }

  const paused = projectIsPaused(project);
  elements.repoKicker.textContent = paused
    ? projectIsBranched()
      ? "Project paused"
      : `${state.currentMeta.current_phase || "Transcript"} paused`
    : projectIsBranched()
      ? "Project"
      : state.currentMeta.current_phase || "Transcript";
  elements.repoTitle.textContent = paused ? `${project.displayName} (paused)` : project.displayName;
  const timelineLabel = `${project.repos.length} timeline${project.repos.length === 1 ? "" : "s"}`;
  const metaParts = [
    project.primaryRepo?.repo_path || state.currentMeta.repo_path,
    timelineLabel,
  ];
  if (paused) {
    metaParts.push(`paused ${formatTimestamp(project.newestRepo?.paused_at || project.newestRepo?.updated_at)}`);
  }
  metaParts.push(`last update ${formatTimestamp(project.newestRepo?.updated_at || state.currentMeta.updated_at)}`);
  elements.repoMeta.textContent = metaParts.join(" · ");

  const transcriptRepo = currentTranscriptProjectRepo();
  const files = Array.isArray(transcriptRepo?.meta?.markdown_files) ? transcriptRepo.meta.markdown_files : [];
  elements.repoDocLinks.replaceChildren();
  elements.repoDocPanel.hidden = !files.length;
  files.forEach((file) => {
    const link = document.createElement("a");
    link.className = "doc-link";
    link.href = markdownViewerHref(transcriptRepo.meta.repo_name, file);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = file.label || file.path || "Markdown file";
    link.title = file.path || file.label || "Markdown file";
    elements.repoDocLinks.append(link);
  });
}

function rebuildRepoEvents(projectRepo) {
  const loadedChunks = Array.isArray(projectRepo.loadedChunks) ? projectRepo.loadedChunks : [];
  const events = loadedChunks
    .slice()
    .sort((left, right) => Number(left.start_cycle) - Number(right.start_cycle))
    .flatMap((chunk) => chunk.events || []);
  projectRepo.events = events;
  projectRepo.loadedAllHistory =
    !projectRepo.eventsManifest || loadedChunks.length >= (projectRepo.eventsManifest.chunks || []).length;
  return projectRepo;
}

function repoHasOlderHistory(projectRepo) {
  return Boolean(projectRepo?.eventsManifest?.chunks?.length) &&
    (projectRepo.loadedChunks || []).length < (projectRepo.eventsManifest.chunks || []).length;
}

function nextOlderChunkMeta(projectRepo) {
  if (!projectRepo?.eventsManifest?.chunks?.length) {
    return null;
  }
  const loaded = new Set((projectRepo.loadedChunks || []).map((chunk) => chunk.file));
  return projectRepo.eventsManifest.chunks.find((chunk) => !loaded.has(chunk.file)) || null;
}

function renderHistoryControls() {
  if (!elements.historyControls) {
    return;
  }
  const currentProject = currentProjectGroup();
  if (!state.currentMeta || !currentProject || !state.currentProjectRepos.length) {
    elements.historyControls.hidden = true;
    return;
  }
  const reposWithOlder = state.currentProjectRepos.filter(repoHasOlderHistory);
  if (!reposWithOlder.length) {
    elements.historyControls.hidden = true;
    return;
  }
  elements.historyControls.hidden = false;
  elements.historyLoadOlder.disabled = state.loadingOlderHistory;
  elements.historyLoadOlder.textContent = state.loadingOlderHistory ? "Loading older history..." : "Load older history";

  const remainingChunks = reposWithOlder.reduce((sum, repo) => {
    const total = (repo.eventsManifest?.chunks || []).length;
    const loaded = (repo.loadedChunks || []).length;
    return sum + Math.max(0, total - loaded);
  }, 0);
  const timelineCount = reposWithOlder.length;
  elements.historyMeta.textContent =
    timelineCount === 1
      ? `${remainingChunks} older chunk${remainingChunks === 1 ? "" : "s"} available.`
      : `${remainingChunks} older chunk${remainingChunks === 1 ? "" : "s"} available across ${timelineCount} timelines. Each click loads one older chunk per timeline.`;
}

function renderCodexBudgetStatus() {
  if (!elements.codexBudgetIndicator) {
    return;
  }

  elements.codexBudgetLabel.textContent = "weekly budget left";
  const status = state.codexBudgetStatus;
  const percentLeft = Number(status?.percent_left);
  const isAvailable = Boolean(status) && status.available !== false && Number.isFinite(percentLeft);
  elements.codexBudgetValue.textContent = isAvailable ? formatPercent(percentLeft) : "unknown";

  const resetText = isAvailable ? formatBudgetReset(status?.resets_at) : "";
  elements.codexBudgetReset.textContent = resetText ? `resets ${resetText}` : "";

  const checkedAt = status?.checked_at ? formatTimestamp(status.checked_at) : "";
  elements.codexBudgetIndicator.title = checkedAt ? `last checked ${checkedAt}` : "";
}

function renderEvents() {
  if (projectIsBranched()) {
    elements.emptyState.hidden = true;
    elements.transcript.hidden = true;
    elements.transcript.replaceChildren();
    return;
  }

  const groupedCycles = cycleGroups(visibleEvents());
  elements.transcript.replaceChildren();
  if (!groupedCycles.length) {
    elements.emptyState.hidden = false;
    elements.transcript.hidden = true;
    return;
  }

  elements.emptyState.hidden = true;
  elements.transcript.hidden = false;
  const transcriptRepo = currentTranscriptProjectRepo();
  groupedCycles.forEach(({ cycle, events }) => {
    elements.transcript.append(buildCycleCard(cycle, events, "", transcriptRepo));
  });
}

async function buildProjectRepoData(repo, existingProjectRepo = null) {
  const [meta, events, leanCycleStats] = await Promise.all([
    fetchJson(`${repo.repo_name}/meta.json`, repo),
    fetchLegacyEvents(repo.repo_name),
    fetchJson(`${repo.repo_name}/lean-cycle-stats.json`, null),
  ]);
  const resolvedMeta = meta || repo;
  return {
    meta: resolvedMeta,
    eventsManifest: null,
    loadedChunks: [],
    loadedAllHistory: true,
    events,
    leanCycleStats,
  };
}

async function loadOlderHistoryForProject() {
  if (state.loadingOlderHistory || !state.currentProjectRepos.length) {
    return;
  }
  state.loadingOlderHistory = true;
  renderHistoryControls();
  try {
    const updatedRepos = await Promise.all(
      state.currentProjectRepos.map(async (projectRepo) => {
        const nextChunk = nextOlderChunkMeta(projectRepo);
        if (!nextChunk) {
          return projectRepo;
        }
        const events = await fetchEventsChunk(projectRepo.meta.repo_name, nextChunk.file);
        return rebuildRepoEvents({
          ...projectRepo,
          loadedChunks: [...(projectRepo.loadedChunks || []), { ...nextChunk, events }],
        });
      }),
    );
    state.currentProjectRepos = updatedRepos;
    const primaryRepoName = state.currentMeta?.repo_name;
    const primary = updatedRepos.find((repo) => repo.meta.repo_name === primaryRepoName) || updatedRepos[0] || null;
    const transcriptRepo = currentTranscriptProjectRepo(updatedRepos);
    state.currentMeta = primary?.meta || null;
    state.currentTimelineRepoName = transcriptRepo?.meta?.repo_name || null;
    state.currentEvents = transcriptRepo?.events || [];
    populateFilters(state.currentEvents);
    renderHeader();
    renderFilters();
    renderHistoryControls();
    renderBranchOverview();
    renderBranchBoard();
    renderEvents();
  } finally {
    state.loadingOlderHistory = false;
    renderHistoryControls();
  }
}

async function selectProject(projectName, updateHash = false) {
  state.currentProjectName = projectName;
  if (updateHash) {
    window.location.hash = projectName;
  }

  const project = currentProjectGroup();
  if (!project) {
    state.currentMeta = null;
    state.currentTimelineRepoName = null;
    state.currentEvents = [];
    state.currentProjectRepos = [];
    renderRepoList();
    renderHeader();
    renderFilters();
    renderHistoryControls();
    renderBranchOverview();
    renderBranchBoard();
    renderEvents();
    return;
  }

  const existingByRepoName = new Map(
    (state.currentProjectName === project.projectName ? state.currentProjectRepos : []).map((projectRepo) => [
      projectRepo.meta?.repo_name,
      projectRepo,
    ]),
  );
  const fetchedRepos = await Promise.all(
    project.repos.map((repo) => buildProjectRepoData(repo, existingByRepoName.get(repo.repo_name))),
  );

  const primary = fetchedRepos.find((repo) => repo.meta.repo_name === project.primaryRepo.repo_name) || fetchedRepos[0] || null;
  const transcriptRepo = currentTranscriptProjectRepo(fetchedRepos);
  state.currentProjectRepos = fetchedRepos;
  state.currentMeta = primary?.meta || null;
  state.currentTimelineRepoName = transcriptRepo?.meta?.repo_name || null;
  state.currentEvents = transcriptRepo?.events || [];
  populateFilters(state.currentEvents);
  renderRepoList();
  renderHeader();
  renderFilters();
  renderHistoryControls();
  renderBranchOverview();
  renderBranchBoard();
  renderEvents();
}

async function refreshRepos() {
  if (!(await ensureViewerCurrent())) {
    return;
  }
  const [payload, budgetStatus] = await Promise.all([
    fetchJson("repos.json", { repos: [] }),
    fetchJson("codex-budget.json", null),
  ]);
  state.repos = Array.isArray(payload.repos) ? payload.repos : [];
  state.codexBudgetStatus = budgetStatus;
  renderRepoList();

  const hashValue = window.location.hash.replace(/^#/, "");
  const desiredProject =
    resolveProjectSelection(hashValue) ||
    (state.currentProjectName && projectGroups().some((group) => group.projectName === state.currentProjectName) && state.currentProjectName) ||
    (projectGroups()[0] && projectGroups()[0].projectName);

  if (!desiredProject) {
    state.currentProjectName = null;
    state.currentMeta = null;
    state.currentTimelineRepoName = null;
    state.currentEvents = [];
    state.currentProjectRepos = [];
    renderHeader();
    renderFilters();
    renderHistoryControls();
    renderBranchOverview();
    renderBranchBoard();
    renderEvents();
    return;
  }

  if (hashValue && hashValue !== desiredProject) {
    history.replaceState(null, "", `#${desiredProject}`);
  }
  await selectProject(desiredProject, false);
}

elements.repoSearch.addEventListener("input", () => renderRepoList());
elements.cycleFilter.addEventListener("change", () => renderEvents());
elements.kindFilter.addEventListener("change", () => renderEvents());
elements.refreshButton.addEventListener("click", () => refreshRepos());
elements.historyLoadOlder?.addEventListener("click", () => loadOlderHistoryForProject());
window.addEventListener("hashchange", () => refreshRepos());

refreshRepos();
