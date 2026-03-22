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
  cycleFilter: document.querySelector("#cycle-filter"),
  kindFilter: document.querySelector("#kind-filter"),
  emptyState: document.querySelector("#empty-state"),
  transcript: document.querySelector("#transcript"),
  refreshButton: document.querySelector("#refresh-button"),
  autoRefresh: document.querySelector("#auto-refresh"),
  eventTemplate: document.querySelector("#event-template"),
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
  };
  return titles[event.kind] || event.kind.replaceAll("_", " ");
}

function eventBadges(event) {
  return [
    `cycle ${event.cycle}`,
    event.phase,
    `${event.actor} -> ${event.target}`,
  ];
}

function prettyContent(event) {
  if (event.content_type === "json") {
    return JSON.stringify(event.content, null, 2);
  }
  return String(event.content ?? "");
}

function shouldOpen(event) {
  return ["worker_handoff", "reviewer_decision", "input_request", "phase_transition", "human_input"].includes(event.kind);
}

function formatTimestamp(value) {
  if (!value) {
    return "No activity yet";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
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
  const cycles = [...new Set(events.map((event) => String(event.cycle)))];
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

function renderEvents() {
  const cycleFilter = elements.cycleFilter.value;
  const kindFilter = elements.kindFilter.value;
  const visibleEvents = state.currentEvents.filter((event) => {
    if (cycleFilter !== "all" && String(event.cycle) !== cycleFilter) {
      return false;
    }
    if (kindFilter !== "all" && event.kind !== kindFilter) {
      return false;
    }
    return true;
  });

  elements.transcript.replaceChildren();
  if (!visibleEvents.length) {
    elements.emptyState.hidden = false;
    elements.transcript.hidden = true;
    return;
  }

  elements.emptyState.hidden = true;
  elements.transcript.hidden = false;

  let currentCycle = null;
  let cycleBlock = null;
  visibleEvents.forEach((event) => {
    if (event.cycle !== currentCycle) {
      currentCycle = event.cycle;
      cycleBlock = document.createElement("section");
      cycleBlock.className = "cycle-block";
      const heading = document.createElement("h3");
      heading.className = "cycle-heading";
      heading.textContent = `Cycle ${currentCycle}`;
      cycleBlock.append(heading);
      elements.transcript.append(cycleBlock);
    }

    const fragment = elements.eventTemplate.content.cloneNode(true);
    const card = fragment.querySelector(".event-card");
    const badges = fragment.querySelector(".event-badges");
    const title = fragment.querySelector(".event-title");
    const time = fragment.querySelector(".event-time");
    const details = fragment.querySelector(".event-details");
    const summary = fragment.querySelector("summary");
    const content = fragment.querySelector(".event-content");

    eventBadges(event).forEach((badgeText) => {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = badgeText;
      badges.append(badge);
    });
    title.textContent = event.summary || eventTitle(event);
    time.textContent = formatTimestamp(event.timestamp);
    summary.textContent = shouldOpen(event) ? "Hide content" : "Show content";
    details.open = shouldOpen(event);
    details.addEventListener("toggle", () => {
      summary.textContent = details.open ? "Hide content" : "Show content";
    });
    content.textContent = prettyContent(event);
    cycleBlock.append(card);
  });
}

function renderHeader() {
  if (!state.currentMeta) {
    elements.repoKicker.textContent = "Transcript";
    elements.repoTitle.textContent = "No repository selected";
    elements.repoMeta.textContent = "Choose a repo from the left.";
    return;
  }
  elements.repoKicker.textContent = state.currentMeta.current_phase || "Transcript";
  elements.repoTitle.textContent = state.currentMeta.repo_display_name || state.currentMeta.repo_name;
  elements.repoMeta.textContent =
    `${state.currentMeta.repo_path} · last update ${formatTimestamp(state.currentMeta.updated_at)} · ` +
    `${state.currentMeta.event_count || 0} events`;
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
