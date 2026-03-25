const state = {
  repos: [],
  currentRepoName: null,
  currentMeta: null,
  currentEvents: [],
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
  cycleFilter: document.querySelector("#cycle-filter"),
  kindFilter: document.querySelector("#kind-filter"),
  branchPanel: document.querySelector("#branch-panel"),
  branchTitle: document.querySelector("#branch-title"),
  branchMeta: document.querySelector("#branch-meta"),
  branchCurrentPath: document.querySelector("#branch-current-path"),
  branchEpisodes: document.querySelector("#branch-episodes"),
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

function formatTimestamp(value) {
  if (!value) {
    return "No activity yet";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
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

function filteredRepos() {
  const query = elements.repoSearch.value.trim().toLowerCase();
  if (!query) {
    return state.repos;
  }
  return state.repos.filter((repo) => {
    const haystack = [repo.repo_name, repo.repo_display_name, repo.repo_path].join(" ").toLowerCase();
    return haystack.includes(query);
  });
}

function renderRepoList() {
  const repos = filteredRepos();
  elements.repoList.replaceChildren();
  if (!repos.length) {
    const note = document.createElement("p");
    note.className = "subtle";
    note.textContent = "No matching repos.";
    elements.repoList.append(note);
    return;
  }
  repos.forEach((repo) => {
    const link = document.createElement("a");
    link.href = `#${repo.repo_name}`;
    link.className = "repo-link";
    if (repo.repo_name === state.currentRepoName) {
      link.classList.add("active");
    }
    link.innerHTML = `
      <h3>${repo.repo_display_name || repo.repo_name}</h3>
      <p>${repo.current_phase || "No phase yet"} · ${formatTimestamp(repo.updated_at)}</p>
      <p>${repo.last_summary || "No transcript events yet."}</p>
    `;
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      await selectRepo(repo.repo_name, true);
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

function renderEvents() {
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
    const firstEvent = events[0];
    const lastEvent = events[events.length - 1];
    const summary = cycleSummaryData(events);

    const cycleCard = document.createElement("article");
    cycleCard.className = "cycle-card";

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
      badges.append(createBadge(String(summary.reviewerDecision.content.decision), `decision-${String(summary.reviewerDecision.content.decision).toLowerCase()}`));
    }
    if (summary.workerHandoff?.content?.status) {
      badges.append(createBadge(String(summary.workerHandoff.content.status), `status-${String(summary.workerHandoff.content.status).toLowerCase()}`));
    }
    header.append(heading, badges);

    const headline = document.createElement("p");
    headline.className = "cycle-summary";
    headline.textContent = summary.headline;

    const detailList = document.createElement("div");
    detailList.className = "cycle-detail-list";
    summary.detailLines.forEach((line) => {
      const item = document.createElement("p");
      item.className = "cycle-detail";
      item.textContent = line;
      detailList.append(item);
    });

    cycleCard.append(header, headline);
    if (summary.detailLines.length) {
      cycleCard.append(detailList);
    }
    elements.transcript.append(cycleCard);
  });
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
  const overview = state.currentMeta?.branch_overview;
  if (!overview?.has_branching) {
    elements.branchPanel.hidden = true;
    elements.branchCurrentPath.replaceChildren();
    elements.branchEpisodes.replaceChildren();
    return;
  }

  elements.branchPanel.hidden = false;
  elements.branchTitle.textContent = "Whole branch tree";
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

function renderHeader() {
  if (!state.currentMeta) {
    elements.repoKicker.textContent = "Transcript";
    elements.repoTitle.textContent = "No repository selected";
    elements.repoMeta.textContent = "Choose a repo from the left.";
    elements.repoDocLinks.replaceChildren();
    elements.repoDocPanel.hidden = true;
    renderBranchOverview();
    return;
  }
  elements.repoKicker.textContent = state.currentMeta.current_phase || "Transcript";
  elements.repoTitle.textContent = state.currentMeta.repo_display_name || state.currentMeta.repo_name;
  elements.repoMeta.textContent =
    `${state.currentMeta.repo_path} · last update ${formatTimestamp(state.currentMeta.updated_at)} · ` +
    `${state.currentMeta.event_count || 0} events`;

  const files = Array.isArray(state.currentMeta.markdown_files) ? state.currentMeta.markdown_files : [];
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
  renderBranchOverview();
}

async function selectRepo(repoName, updateHash = false) {
  state.currentRepoName = repoName;
  if (updateHash) {
    window.location.hash = repoName;
  }
  const [meta, events] = await Promise.all([
    fetchJson(`${repoName}/meta.json`, null),
    fetchEvents(repoName),
  ]);
  state.currentMeta = meta;
  state.currentEvents = events;
  populateFilters(events);
  renderRepoList();
  renderHeader();
  renderEvents();
}

async function refreshRepos() {
  const payload = await fetchJson("repos.json", { repos: [] });
  state.repos = Array.isArray(payload.repos) ? payload.repos : [];
  renderRepoList();

  const hashRepo = window.location.hash.replace(/^#/, "");
  const desiredRepo =
    (hashRepo && state.repos.some((repo) => repo.repo_name === hashRepo) && hashRepo) ||
    (state.currentRepoName && state.repos.some((repo) => repo.repo_name === state.currentRepoName) && state.currentRepoName) ||
    (state.repos[0] && state.repos[0].repo_name);

  if (!desiredRepo) {
    state.currentRepoName = null;
    state.currentMeta = null;
    state.currentEvents = [];
    renderHeader();
    renderEvents();
    return;
  }
  await selectRepo(desiredRepo, false);
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
