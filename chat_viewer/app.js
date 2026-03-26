const state = {
  repos: [],
  currentProjectName: null,
  currentMeta: null,
  currentEvents: [],
  currentProjectRepos: [],
  refreshHandle: null,
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
  autoRefresh: document.querySelector("#auto-refresh"),
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

async function fetchEvents(repoName) {
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
  };
  return titles[event.kind] || event.kind.replaceAll("_", " ");
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

function inferredProjectName(repo, repos) {
  const explicit = String(repo?.project_name || "").trim();
  const branchPath = repo?.branch_overview?.current_path_newest_to_oldest;
  const looksLikeBranchRepo = Array.isArray(branchPath) && branchPath.length > 1;
  if (explicit && (!looksLikeBranchRepo || explicit !== repo.repo_name)) {
    return explicit;
  }
  for (const candidate of repos) {
    if (!candidate || candidate.repo_name === repo.repo_name) {
      continue;
    }
    const overview = candidate.branch_overview;
    if (!overview?.has_branching) {
      continue;
    }
    for (const episode of overview.episodes || []) {
      for (const branch of episode.branches || []) {
        if (branchRepoName(candidate.repo_name, episode, branch) === repo.repo_name) {
          return String(candidate.project_name || candidate.repo_name || repo.repo_name);
        }
      }
    }
  }
  return explicit || repo.repo_name;
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

function projectSummary(project) {
  const newest = project.newestRepo || {};
  const leafCount = projectLeafRepos(project).length || project.repos.length;
  const prefix = project.hasBranching ? `${leafCount} leaf timeline${leafCount === 1 ? "" : "s"} · ` : "";
  return `${newest.current_phase || "No phase yet"} · ${prefix}${formatTimestamp(newest.updated_at)}`;
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

function truncateText(value, limit = 280) {
  const text = String(value || "").trim().replace(/\s+/g, " ");
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
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
    lines.push(`Worker: ${truncateText(workerHandoff.summary || eventTitle(workerHandoff), 210)}`);
  }
  if (validation && validation !== headlineEvent) {
    lines.push(`Validation: ${truncateText(validation.summary || eventTitle(validation), 120)}`);
  }
  if (reviewerDecision && reviewerDecision !== headlineEvent) {
    lines.push(`Review: ${truncateText(reviewerDecision.summary || eventTitle(reviewerDecision), 220)}`);
  }
  if (branchStrategy && branchStrategy !== headlineEvent) {
    lines.push(`Branch strategy: ${truncateText(branchStrategy.summary || eventTitle(branchStrategy), 180)}`);
  }
  if (branchSelection && branchSelection !== headlineEvent) {
    lines.push(`Branch selection: ${truncateText(branchSelection.summary || eventTitle(branchSelection), 180)}`);
  }
  if (phaseTransition && phaseTransition !== headlineEvent) {
    lines.push(`Transition: ${truncateText(phaseTransition.summary || eventTitle(phaseTransition), 120)}`);
  }
  if (inputRequest && inputRequest !== headlineEvent) {
    lines.push(`Input: ${truncateText(inputRequest.summary || eventTitle(inputRequest), 120)}`);
  }
  if (humanInput && humanInput !== headlineEvent) {
    lines.push(`Human: ${truncateText(humanInput.summary || eventTitle(humanInput), 120)}`);
  }

  return {
    headline: truncateText(headlineEvent ? (headlineEvent.summary || eventTitle(headlineEvent)) : "No summary yet.", 320),
    detailLines: lines.slice(0, 4),
    reviewerDecision,
    workerHandoff,
  };
}

function buildCycleCard(cycle, events, extraClass = "") {
  const firstEvent = events[0];
  const lastEvent = events[events.length - 1];
  const summary = cycleSummaryData(events);

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
  meta.textContent = `${firstEvent?.phase || "unknown phase"} · ${events.length} event${events.length === 1 ? "" : "s"} · ${formatTimestamp(lastEvent?.timestamp)}`;
  heading.append(title, meta);

  const badges = document.createElement("div");
  badges.className = "cycle-status-badges";
  badges.append(createBadge(firstEvent?.phase || "unknown"));
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
  headline.textContent = summary.headline;

  cycleCard.append(header, headline);
  if (summary.detailLines.length) {
    const detailList = document.createElement("div");
    detailList.className = "cycle-detail-list";
    summary.detailLines.forEach((line) => {
      const item = document.createElement("p");
      item.className = "cycle-detail";
      item.textContent = line;
      detailList.append(item);
    });
    cycleCard.append(detailList);
  }

  return cycleCard;
}

function projectIsBranched() {
  return Boolean(state.currentMeta?.branch_overview?.has_branching) || state.currentProjectRepos.length > 1;
}

function projectBranchOverview() {
  const overview = state.currentMeta?.branch_overview;
  if (!overview?.has_branching) {
    return null;
  }
  const derivedPath = derivedCurrentProjectPath();
  return {
    ...overview,
    current_path_newest_to_oldest: derivedPath || overview.current_path_newest_to_oldest,
    current_path_status: derivedPath ? "alive" : overview.current_path_status,
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
  elements.branchTitle.textContent = "Project tree so far";
  elements.branchMeta.textContent =
    `${overview.episodes?.length || 0} branch episode${(overview.episodes?.length || 0) === 1 ? "" : "s"} · ` +
    `${overview.current_path_status || "alive"} current path`;

  elements.branchCurrentPath.replaceChildren();
  const currentPathLabel = document.createElement("p");
  currentPathLabel.className = "branch-current-label";
  currentPathLabel.textContent = "Current path";
  const currentPathValue = document.createElement("p");
  currentPathValue.className = "branch-current-value";
  currentPathValue.textContent = (overview.current_path_newest_to_oldest || []).join(" ← ");
  elements.branchCurrentPath.append(currentPathLabel, currentPathValue);

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
    meta.textContent = `${episode.phase || "unknown phase"} · ${episode.status} · context ${contextPath}`;
    header.append(title, meta);

    const branchList = document.createElement("div");
    branchList.className = "branch-list";
    const branches = [...(episode.branches || [])].sort((left, right) => {
      if (Boolean(right.is_current_path) !== Boolean(left.is_current_path)) {
        return Number(Boolean(right.is_current_path)) - Number(Boolean(left.is_current_path));
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

    card.append(header, branchList);
    elements.branchEpisodes.append(card);
  });
}

function branchStatusMap() {
  const map = new Map();
  const overview = state.currentMeta?.branch_overview;
  const rootRepoName = state.currentMeta?.repo_name || state.currentProjectName || "";
  for (const episode of overview?.episodes || []) {
    for (const branch of episode.branches || []) {
      map.set(branchRepoName(rootRepoName, episode, branch), branch.status || "active");
    }
  }
  return map;
}

function timelineStatus(projectRepo) {
  const rootRepoName = state.currentMeta?.repo_name || state.currentProjectName || "";
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
  const rootRepoName = state.currentMeta?.repo_name || state.currentProjectName || "";
  if (projectRepo.meta.repo_name === rootRepoName) {
    return "mainline";
  }
  return timelinePath(projectRepo.meta)[0] || projectRepo.meta.repo_display_name || projectRepo.meta.repo_name;
}

function projectTimelineNodes() {
  const nodes = new Map();
  [...state.currentProjectRepos]
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

function eventsAfterTimestamp(events, cutoffTimestamp) {
  return events.filter((event) => timestampValue(event.timestamp) > cutoffTimestamp);
}

function eventsUpToTimestamp(events, cutoffTimestamp) {
  return events.filter((event) => timestampValue(event.timestamp) <= cutoffTimestamp);
}

function buildSelectedContinuationRows(nodes) {
  const liveLeaves = liveLeafNodes(nodes).sort(leafSort);
  if (liveLeaves.length !== 1) {
    return { rows: [], cutoffByNodeKey: new Map() };
  }

  const rows = [];
  const cutoffByNodeKey = new Map();
  let current = liveLeaves[0];
  while (current.parentKey && nodes.has(current.parentKey)) {
    const parent = nodes.get(current.parentKey);
    const cutoffTimestamp = timestampValue(parent.projectRepo.meta.updated_at);
    const continuationEvents = eventsAfterTimestamp(current.projectRepo.events || [], cutoffTimestamp);
    if (continuationEvents.length) {
      rows.push({ node: current, continuationEvents });
      cutoffByNodeKey.set(current.key, cutoffTimestamp);
    }
    current = parent;
  }

  return { rows, cutoffByNodeKey };
}

function buildBranchTreeRows(nodes) {
  const leaves = [...nodes.values()].filter((node) => node.children.length === 0).sort(leafSort);
  if (!leaves.length) {
    return { leaves: [], rows: [] };
  }

  const chains = leaves.map((leaf) => ancestorChainFromLeaf(leaf, nodes));
  const maxLength = Math.max(...chains.map((chain) => chain.length));
  const paddedChains = chains.map((chain) => paddedLeafChain(chain, maxLength));

  const rows = [];
  for (let rowIndex = 0; rowIndex < maxLength; rowIndex += 1) {
    const segments = [];
    let column = 0;
    while (column < leaves.length) {
      const node = paddedChains[column][rowIndex];
      const key = node.key;
      let span = 1;
      const continuedFlags = [
        rowIndex > 0 && paddedChains[column][rowIndex - 1].key === key,
      ];
      while (column + span < leaves.length && paddedChains[column + span][rowIndex].key === key) {
        continuedFlags.push(rowIndex > 0 && paddedChains[column + span][rowIndex - 1].key === key);
        span += 1;
      }
      segments.push({
        node,
        start: column + 1,
        span,
        continuationOnly: continuedFlags.every(Boolean),
      });
      column += span;
    }
    rows.push({ index: rowIndex, segments });
  }

  return { leaves, rows };
}

function buildTimelineNodeCard(node, events, options = {}) {
  const projectRepo = node.projectRepo;
  const nodeCard = document.createElement("article");
  nodeCard.className = `timeline-column branch-tree-node status-${node.status}${options.extraClass ? ` ${options.extraClass}` : ""}`;

  const header = document.createElement("div");
  header.className = "timeline-column-header";

  const headingRow = document.createElement("div");
  headingRow.className = "timeline-column-heading-row";
  const title = document.createElement("h4");
  title.className = "timeline-column-title";
  title.textContent = options.title || node.name;
  const badges = document.createElement("div");
  badges.className = "timeline-column-badges";
  badges.append(createBadge(projectRepo.meta.current_phase || "unknown"));
  badges.append(createBadge(node.status, `status-${node.status}`));
  headingRow.append(title, badges);

  const path = document.createElement("p");
  path.className = "timeline-column-path subtle";
  path.textContent = options.pathText || node.pathNewToOld.join(" ← ");

  const summary = document.createElement("p");
  summary.className = "timeline-column-summary";
  summary.textContent = options.summary || projectRepo.meta.last_summary || "No summary recorded yet.";

  const docs = document.createElement("div");
  docs.className = "timeline-column-docs";
  appendTimelineDocLinks(docs, projectRepo);

  header.append(headingRow, path, summary);
  if (docs.childElementCount) {
    header.append(docs);
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
      cycles.append(buildCycleCard(cycle, cycleEvents, "branch-cycle-card"));
    });
  }

  nodeCard.append(header, cycles);
  return nodeCard;
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
  const tree = buildBranchTreeRows(nodes);
  if (!tree.leaves.length || !tree.rows.length) {
    elements.branchBoard.hidden = true;
    elements.branchBoardGrid.replaceChildren();
    return;
  }
  const continuation = buildSelectedContinuationRows(nodes);

  elements.branchBoard.hidden = false;
  elements.branchBoardTitle.textContent = `${tree.leaves.length} leaf timeline${tree.leaves.length === 1 ? "" : "s"} above shared history`;
  elements.branchBoardMeta.textContent =
    "Leaf branches stay side by side until one is pruned and the surviving route continues as the main branch above.";
  elements.branchBoardGrid.replaceChildren();

  continuation.rows.forEach(({ node, continuationEvents }) => {
    const rowElement = document.createElement("section");
    rowElement.className = "branch-tree-row";
    rowElement.style.gridTemplateColumns = `repeat(${tree.leaves.length}, minmax(18rem, 1fr))`;

    const nodeCard = buildTimelineNodeCard(node, continuationEvents, {
      title: "main branch",
      pathText: `current route: ${node.pathNewToOld.join(" ← ")}`,
      extraClass: "main-branch-continuation",
    });
    nodeCard.style.gridColumn = `1 / span ${tree.leaves.length}`;
    rowElement.append(nodeCard);
    elements.branchBoardGrid.append(rowElement);
  });

  tree.rows.forEach((row, rowIndex) => {
    const rowElement = document.createElement("section");
    rowElement.className = "branch-tree-row";
    rowElement.style.gridTemplateColumns = `repeat(${tree.leaves.length}, minmax(18rem, 1fr))`;

    row.segments.forEach((segment) => {
      const nodeCard = document.createElement("article");
      nodeCard.className = `timeline-column branch-tree-node status-${segment.node.status}${segment.continuationOnly ? " continuation-only" : ""}`;
      nodeCard.style.gridColumn = `${segment.start} / span ${segment.span}`;

      if (segment.continuationOnly) {
        const continuation = document.createElement("p");
        continuation.className = "branch-tree-continuation";
        continuation.textContent = `${segment.node.name} continues`;
        nodeCard.append(continuation);
        rowElement.append(nodeCard);
        return;
      }

      const cutoffTimestamp = continuation.cutoffByNodeKey.get(segment.node.key);
      const events =
        cutoffTimestamp === undefined
          ? segment.node.projectRepo.events || []
          : eventsUpToTimestamp(segment.node.projectRepo.events || [], cutoffTimestamp);
      const content = buildTimelineNodeCard(segment.node, events);
      nodeCard.replaceChildren(...content.childNodes);
      rowElement.append(nodeCard);
    });

    elements.branchBoardGrid.append(rowElement);
  });
}

function renderFilters() {
  elements.filters.hidden = projectIsBranched();
}

function renderHeader() {
  const project = currentProjectGroup();
  if (!state.currentMeta || !project) {
    elements.repoKicker.textContent = "Transcript";
    elements.repoTitle.textContent = "No project selected";
    elements.repoMeta.textContent = "Choose a project from the left.";
    elements.repoDocLinks.replaceChildren();
    elements.repoDocPanel.hidden = true;
    return;
  }

  elements.repoKicker.textContent = projectIsBranched() ? "Project" : state.currentMeta.current_phase || "Transcript";
  elements.repoTitle.textContent = project.displayName;
  const timelineLabel = `${project.repos.length} timeline${project.repos.length === 1 ? "" : "s"}`;
  elements.repoMeta.textContent =
    `${project.primaryRepo?.repo_path || state.currentMeta.repo_path} · ${timelineLabel} · ` +
    `last update ${formatTimestamp(project.newestRepo?.updated_at || state.currentMeta.updated_at)}`;

  const files =
    projectIsBranched() || !Array.isArray(state.currentMeta.markdown_files) ? [] : state.currentMeta.markdown_files;
  elements.repoDocLinks.replaceChildren();
  elements.repoDocPanel.hidden = !files.length;
  files.forEach((file) => {
    const link = document.createElement("a");
    link.className = "doc-link";
    link.href = markdownViewerHref(state.currentMeta.repo_name, file);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = file.label || file.path || "Markdown file";
    link.title = file.path || file.label || "Markdown file";
    elements.repoDocLinks.append(link);
  });
}

function renderEvents() {
  if (projectIsBranched()) {
    elements.transcript.replaceChildren();
    elements.transcript.hidden = true;
    elements.emptyState.hidden = state.currentProjectRepos.length > 0;
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
  groupedCycles.forEach(({ cycle, events }) => {
    elements.transcript.append(buildCycleCard(cycle, events));
  });
}

async function selectProject(projectName, updateHash = false) {
  state.currentProjectName = projectName;
  if (updateHash) {
    window.location.hash = projectName;
  }

  const project = currentProjectGroup();
  if (!project) {
    state.currentMeta = null;
    state.currentEvents = [];
    state.currentProjectRepos = [];
    renderRepoList();
    renderHeader();
    renderFilters();
    renderBranchOverview();
    renderBranchBoard();
    renderEvents();
    return;
  }

  const fetchedRepos = await Promise.all(
    project.repos.map(async (repo) => {
      const [meta, events] = await Promise.all([
        fetchJson(`${repo.repo_name}/meta.json`, repo),
        fetchEvents(repo.repo_name),
      ]);
      return {
        meta: meta || repo,
        events,
      };
    }),
  );

  const primary = fetchedRepos.find((repo) => repo.meta.repo_name === project.primaryRepo.repo_name) || fetchedRepos[0] || null;
  state.currentProjectRepos = fetchedRepos;
  state.currentMeta = primary?.meta || null;
  state.currentEvents = primary?.events || [];
  populateFilters(state.currentEvents);
  renderRepoList();
  renderHeader();
  renderFilters();
  renderBranchOverview();
  renderBranchBoard();
  renderEvents();
}

async function refreshRepos() {
  const payload = await fetchJson("repos.json", { repos: [] });
  state.repos = Array.isArray(payload.repos) ? payload.repos : [];
  renderRepoList();

  const hashValue = window.location.hash.replace(/^#/, "");
  const desiredProject =
    resolveProjectSelection(hashValue) ||
    (state.currentProjectName && projectGroups().some((group) => group.projectName === state.currentProjectName) && state.currentProjectName) ||
    (projectGroups()[0] && projectGroups()[0].projectName);

  if (!desiredProject) {
    state.currentProjectName = null;
    state.currentMeta = null;
    state.currentEvents = [];
    state.currentProjectRepos = [];
    renderHeader();
    renderFilters();
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

function scheduleRefresh() {
  window.clearInterval(state.refreshHandle);
  if (!elements.autoRefresh.checked) {
    return;
  }
  state.refreshHandle = window.setInterval(() => {
    refreshRepos();
  }, 15000);
}

elements.repoSearch.addEventListener("input", () => renderRepoList());
elements.cycleFilter.addEventListener("change", () => renderEvents());
elements.kindFilter.addEventListener("change", () => renderEvents());
elements.refreshButton.addEventListener("click", () => refreshRepos());
elements.autoRefresh.addEventListener("change", () => scheduleRefresh());
window.addEventListener("hashchange", () => refreshRepos());

refreshRepos();
scheduleRefresh();
