// ---------------- Shared helpers ----------------

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return null;
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden", "error");
  if (isError) el.classList.add("error");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 4500);
}

function showModal({ title, body, confirmLabel, onConfirm }) {
  const backdrop = document.getElementById("modal-backdrop");
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").textContent = body;
  const confirmBtn = document.getElementById("modal-confirm");
  const cancelBtn = document.getElementById("modal-cancel");
  confirmBtn.disabled = false;
  cancelBtn.disabled = false;
  confirmBtn.innerHTML = "";
  confirmBtn.appendChild(document.createTextNode(confirmLabel));
  backdrop.classList.remove("hidden");

  const cleanup = () => {
    backdrop.classList.add("hidden");
    confirmBtn.removeEventListener("click", onConfirmClick);
    cancelBtn.removeEventListener("click", onCancelClick);
  };
  const onConfirmClick = async () => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    confirmBtn.innerHTML = "";
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    confirmBtn.appendChild(spinner);
    confirmBtn.appendChild(document.createTextNode("Working..."));
    try {
      await onConfirm();
    } finally {
      cleanup();
    }
  };
  const onCancelClick = () => cleanup();

  confirmBtn.addEventListener("click", onConfirmClick);
  cancelBtn.addEventListener("click", onCancelClick);
}

// ---------------- Tabs ----------------

function activateTab(tab) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab === "storage" && !storageBrowser.hasLoaded) {
    storageBrowser.hasLoaded = true;
    storageBrowser.showDrives();
  }
  if (tab === "onedrive" && !oneDriveBrowser.hasLoaded) {
    initOneDrive();
  }
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

// ---------------- Generic folder browser (Storage Explorer + OneDrive tabs) ----------------

function makeBrowser({ bodyId, breadcrumbId, statusId, homeLabel, getHomePath, warningId }) {
  const elBody = document.getElementById(bodyId);
  const elBreadcrumb = document.getElementById(breadcrumbId);
  const elStatus = document.getElementById(statusId);
  const elWarning = warningId ? document.getElementById(warningId) : null;

  let entries = [];
  let mode = "home"; // "home" | "real" | "virtual"
  let currentPath = null;
  let virtualLabel = null;

  function setWarning(msg) {
    if (!elWarning) return;
    if (msg) {
      elWarning.textContent = msg;
      elWarning.classList.remove("hidden");
    } else {
      elWarning.textContent = "";
      elWarning.classList.add("hidden");
    }
  }

  function renderBreadcrumb() {
    elBreadcrumb.innerHTML = "";
    const homeBtn = document.createElement("button");
    homeBtn.textContent = homeLabel;
    homeBtn.addEventListener("click", goHome);
    elBreadcrumb.appendChild(homeBtn);

    if (mode === "virtual") {
      const sep = document.createElement("span");
      sep.className = "sep"; sep.textContent = " / ";
      elBreadcrumb.appendChild(sep);
      const b = document.createElement("button");
      b.textContent = virtualLabel;
      b.disabled = true;
      elBreadcrumb.appendChild(b);
      return;
    }
    if (mode !== "real" || !currentPath) return;

    const home = getHomePath();
    let base = "";
    if (home && currentPath.toLowerCase().startsWith(home.toLowerCase())) {
      base = home.replace(/\\$/, "");
    }
    const afterHome = base ? currentPath.slice(base.length).replace(/^\\/, "") : null;
    const segments = base
      ? (afterHome ? afterHome.split("\\") : [])
      : currentPath.replace(/\\$/, "").split("\\");

    let acc = base;
    segments.forEach((seg) => {
      const sep = document.createElement("span");
      sep.className = "sep"; sep.textContent = " / ";
      elBreadcrumb.appendChild(sep);
      acc = acc ? acc + "\\" + seg : seg + "\\";
      const b = document.createElement("button");
      b.textContent = seg;
      const target = acc;
      b.addEventListener("click", () => browse(target, { keepWarning: true }));
      elBreadcrumb.appendChild(b);
    });
  }

  function goHome() {
    const home = getHomePath();
    if (home) browse(home);
    else showDrives();
  }

  async function showDrives() {
    mode = "home";
    currentPath = null;
    elStatus.textContent = "";
    setWarning(null);
    renderBreadcrumb();
    try {
      const res = await fetch("/api/drives");
      const data = await res.json();
      elBody.innerHTML = "";
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      const grid = document.createElement("div");
      grid.className = "drive-grid";
      data.drives.forEach((d) => {
        const b = document.createElement("button");
        b.textContent = d;
        b.addEventListener("click", () => browse(d));
        grid.appendChild(b);
      });
      td.appendChild(grid);
      tr.appendChild(td);
      elBody.appendChild(tr);
    } catch (e) {
      toast("Could not list drives.", true);
    }
  }

  async function browse(path, { keepWarning = false } = {}) {
    if (!keepWarning) setWarning(null);
    elStatus.textContent = "Loading...";
    try {
      const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to open folder");
      mode = "real";
      currentPath = data.path;
      entries = data.entries;
      renderBreadcrumb();
      render();
      elStatus.textContent = `${data.entries.length} items`;
      data.entries.filter((e) => e.is_dir && e.size_bytes === null).forEach(fetchEntrySize);
    } catch (e) {
      elStatus.textContent = "";
      toast(e.message, true);
    }
  }

  async function browseVirtual(label, paths, warning = null) {
    setWarning(warning);
    elStatus.textContent = "Loading...";
    try {
      const res = await fetch(`/api/entries?paths=${encodeURIComponent(JSON.stringify(paths || []))}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to load");
      mode = "virtual";
      virtualLabel = label;
      currentPath = null;
      entries = data.entries;
      renderBreadcrumb();
      render();
      elStatus.textContent = entries.length ? `${entries.length} items` : "Nothing found here.";
    } catch (e) {
      elStatus.textContent = "";
      toast(e.message, true);
    }
  }

  async function fetchEntrySize(entry, force = false) {
    try {
      const res = await fetch(`/api/size?path=${encodeURIComponent(entry.path)}${force ? "&refresh=1" : ""}`);
      if (!res.ok) return;
      const data = await res.json();
      entry.size_bytes = data.size_bytes;
      render();
    } catch (e) { /* ignore */ }
  }

  function render() {
    elBody.innerHTML = "";
    const sorted = [...entries].sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      const av = a.size_bytes ?? -1, bv = b.size_bytes ?? -1;
      return bv - av;
    });

    for (const entry of sorted) {
      const tr = document.createElement("tr");

      const tdName = document.createElement("td");
      const wrap = document.createElement("div");
      wrap.className = "name-cell";
      const displayName = mode === "virtual" ? entry.path : entry.name;
      if (entry.is_dir) {
        const link = document.createElement("button");
        link.className = "link";
        link.textContent = "📁 " + displayName;
        link.addEventListener("click", () => browse(entry.path, { keepWarning: true }));
        wrap.appendChild(link);
      } else {
        const span = document.createElement("span");
        span.textContent = "📄 " + displayName;
        wrap.appendChild(span);
      }
      tdName.appendChild(wrap);
      tr.appendChild(tdName);

      const tdRisk = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = `badge ${entry.risk.level}`;
      badge.textContent = entry.risk.label;
      tdRisk.appendChild(badge);
      const reason = document.createElement("span");
      reason.className = "reason";
      reason.textContent = entry.risk.reason;
      tdRisk.appendChild(reason);
      tr.appendChild(tdRisk);

      const tdSize = document.createElement("td");
      tdSize.className = "col-size";
      tdSize.textContent = entry.size_bytes != null ? fmtSize(entry.size_bytes) : "…";
      tr.appendChild(tdSize);

      const tdAction = document.createElement("td");
      tdAction.className = "actions-cell";
      const isOneDrive = entry.risk.level === "onedrive" || entry.risk.level === "onedrive_personal";
      if (isOneDrive) {
        const unlinkBtn = document.createElement("button");
        unlinkBtn.className = "secondary";
        unlinkBtn.textContent = "Unlink";
        unlinkBtn.title = "Move out of OneDrive; keep the file locally";
        unlinkBtn.addEventListener("click", () => confirmUnlink(entry));
        tdAction.appendChild(unlinkBtn);
      }
      const delBtn = document.createElement("button");
      delBtn.textContent = "Delete";
      delBtn.disabled = !!entry.risk.blocked;
      delBtn.title = entry.risk.blocked ? entry.risk.reason : "Sends to Recycle Bin";
      delBtn.addEventListener("click", () => confirmDelete(entry));
      tdAction.appendChild(delBtn);
      tr.appendChild(tdAction);

      elBody.appendChild(tr);
    }
  }

  function confirmDelete(entry) {
    const label = entry.name || entry.path;
    showModal({
      title: `Move "${label}" to Recycle Bin?`,
      body: `${entry.risk.reason}\n\nIt will go to the Recycle Bin, not be permanently deleted, so you can restore it if something breaks.`,
      confirmLabel: "Move to Recycle Bin",
      onConfirm: async () => {
        try {
          const res = await fetch("/api/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: entry.path, confirmed: true }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "Failed");
          toast(`Moved "${label}" to the Recycle Bin.`);
          entries = entries.filter((e) => e.path !== entry.path);
          render();
        } catch (e) {
          toast(e.message, true);
        }
      },
    });
  }

  function confirmUnlink(entry) {
    const label = entry.name || entry.path;
    showModal({
      title: `Unlink "${label}" from OneDrive?`,
      body: `This moves it out of your OneDrive folder into "Files Unlinked from OneDrive" in your user folder. ` +
            `It stops syncing to the cloud, but nothing is deleted.`,
      confirmLabel: "Unlink",
      onConfirm: async () => {
        try {
          const res = await fetch("/api/onedrive/unlink", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: entry.path }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "Failed");
          toast(`Unlinked. Moved to ${data.new_path}`);
          entries = entries.filter((e) => e.path !== entry.path);
          render();
        } catch (e) {
          toast(e.message, true);
        }
      },
    });
  }

  function refreshVisible() {
    entries.filter((e) => e.is_dir).forEach((e) => { e.size_bytes = null; fetchEntrySize(e, true); });
    render();
  }

  return { browse, browseVirtual, showDrives, refreshVisible, goHome, hasLoaded: false };
}

const storageBrowser = makeBrowser({
  bodyId: "storage-body", breadcrumbId: "storage-breadcrumb", statusId: "storage-status",
  homeLabel: "Drives", getHomePath: () => null, warningId: "storage-warning",
});

let oneDriveRootPath = null;
const oneDriveBrowser = makeBrowser({
  bodyId: "onedrive-body", breadcrumbId: "onedrive-breadcrumb", statusId: "onedrive-status",
  homeLabel: "OneDrive", getHomePath: () => oneDriveRootPath,
});

document.getElementById("refresh-folder").addEventListener("click", () => storageBrowser.refreshVisible());
document.getElementById("refresh-onedrive").addEventListener("click", () => oneDriveBrowser.refreshVisible());

document.getElementById("pick-folder").addEventListener("click", async () => {
  const btn = document.getElementById("pick-folder");
  btn.disabled = true;
  btn.textContent = "Opening picker...";
  try {
    const res = await fetch("/api/pick-folder", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to open folder picker");
    if (data.path) {
      storageBrowser.hasLoaded = true;
      storageBrowser.browse(data.path);
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Browse folder...";
  }
});

document.getElementById("quit-app").addEventListener("click", () => {
  showModal({
    title: "Quit SafeCleanup?",
    body: "This stops the local SafeCleanup server. You can reopen it anytime from its shortcut.",
    confirmLabel: "Quit",
    onConfirm: async () => {
      try {
        await fetch("/api/shutdown", { method: "POST" });
      } catch (e) { /* the server is shutting down, an error here is expected */ }
      document.body.innerHTML =
        '<div style="padding:40px;text-align:center;font-family:sans-serif;color:#6b7280;">' +
        "SafeCleanup has stopped. You can close this tab.</div>";
    },
  });
});

async function initOneDrive() {
  oneDriveBrowser.hasLoaded = true;
  try {
    const res = await fetch("/api/onedrive/info");
    const data = await res.json();
    oneDriveRootPath = data.root;
    if (!oneDriveRootPath) {
      document.getElementById("onedrive-missing").classList.remove("hidden");
      return;
    }
    document.getElementById("onedrive-missing").classList.add("hidden");
    oneDriveBrowser.browse(oneDriveRootPath);
  } catch (e) {
    toast("Could not detect a OneDrive folder.", true);
  }
}

// ---------------- Programs tab ----------------

const appsState = { apps: [], sort: { key: "size_bytes", dir: "desc" } };
const TYPE_LABELS = { installer: "Installer", uwp: "Store app", files_only: "Files only" };

async function loadApps(refresh = false) {
  const status = document.getElementById("apps-status");
  status.textContent = "Loading...";
  try {
    const res = await fetch(`/api/apps${refresh ? "?refresh=1" : ""}`);
    const data = await res.json();
    appsState.apps = data.apps || [];
    status.textContent = `${appsState.apps.length} items found`;
    renderApps();
    appsState.apps.filter((a) => a.size_bytes === null && a.install_location).forEach(fetchAppSize);
  } catch (e) {
    status.textContent = "Failed to load apps.";
  }
}

async function fetchAppSize(app) {
  try {
    const res = await fetch(`/api/app-size?path=${encodeURIComponent(app.install_location)}`);
    if (!res.ok) return;
    const data = await res.json();
    app.size_bytes = data.size_bytes;
    renderApps();
  } catch (e) { /* ignore */ }
}

function sortedApps() {
  const { key, dir } = appsState.sort;
  const filter = document.getElementById("app-search").value.toLowerCase();
  const typeFilter = document.getElementById("app-type-filter").value;
  let list = appsState.apps.filter((a) =>
    (a.name.toLowerCase().includes(filter) || (a.publisher || "").toLowerCase().includes(filter)) &&
    (!typeFilter || a.type === typeFilter)
  );
  list.sort((a, b) => {
    let av = a[key], bv = b[key];
    if (key === "size_bytes") { av = av ?? -1; bv = bv ?? -1; }
    else { av = (av || "").toString().toLowerCase(); bv = (bv || "").toString().toLowerCase(); }
    if (av < bv) return dir === "asc" ? -1 : 1;
    if (av > bv) return dir === "asc" ? 1 : -1;
    return 0;
  });
  return list;
}

function renderApps() {
  const body = document.getElementById("apps-body");
  body.innerHTML = "";
  for (const app of sortedApps()) {
    const tr = document.createElement("tr");

    const tdName = document.createElement("td");
    tdName.textContent = app.name;
    tr.appendChild(tdName);

    const tdPub = document.createElement("td");
    tdPub.textContent = app.publisher || "—";
    tr.appendChild(tdPub);

    const tdType = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge type-${app.type}`;
    badge.textContent = TYPE_LABELS[app.type] || app.type;
    tdType.appendChild(badge);
    tr.appendChild(tdType);

    const tdSize = document.createElement("td");
    tdSize.className = "col-size";
    tdSize.textContent = app.size_bytes != null ? fmtSize(app.size_bytes) : (app.install_location ? "…" : "—");
    tr.appendChild(tdSize);

    const tdAction = document.createElement("td");
    tdAction.className = "actions-cell";
    const btn = document.createElement("button");
    btn.textContent = { installer: "Uninstall", uwp: "Remove", files_only: "Delete files" }[app.type] || "Remove";
    const canAct = app.type === "files_only"
      ? !!app.install_location
      : !!(app.uninstall_string || app.quiet_uninstall_string || app.package_full_name);
    btn.disabled = !canAct;
    btn.addEventListener("click", () => confirmUninstall(app));
    tdAction.appendChild(btn);

    if (app.type === "installer" && app.install_location) {
      const forceBtn = document.createElement("button");
      forceBtn.className = "secondary";
      forceBtn.textContent = "Force delete…";
      forceBtn.title = "Skip the uninstaller and just delete its files - only if the uninstaller is broken or missing";
      forceBtn.addEventListener("click", () => confirmForceDelete(app));
      tdAction.appendChild(forceBtn);
    }
    tr.appendChild(tdAction);

    body.appendChild(tr);
  }
}

function confirmUninstall(app) {
  let body;
  let confirmLabel;
  if (app.type === "uwp") {
    body = `This removes the Store app "${app.name}" using Windows' own Remove-AppxPackage command.`;
    confirmLabel = "Remove";
  } else if (app.type === "files_only") {
    body = `"${app.name}" has no uninstaller registered with Windows - it's just a folder of files at:\n${app.install_location}\n\n` +
           `This moves that entire folder to the Recycle Bin (not permanently deleted).`;
    confirmLabel = "Move to Recycle Bin";
  } else {
    body = `This launches ${app.name}'s own official uninstaller (the same one Windows Settings would run). Follow any prompts it shows you.`;
    confirmLabel = "Launch uninstaller";
  }
  showModal({
    title: `${app.type === "files_only" ? "Delete" : "Uninstall"} ${app.name}?`,
    body,
    confirmLabel,
    onConfirm: async () => {
      try {
        const res = await fetch("/api/uninstall", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ app_id: app.id }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Failed");
        toast(data.message);
        if (app.type !== "installer") loadApps(true);
      } catch (e) {
        toast(e.message, true);
      }
    },
  });
}

function confirmForceDelete(app) {
  showModal({
    title: `Force-delete ${app.name}'s files?`,
    body: `This skips ${app.name}'s own uninstaller completely and just moves its install folder ` +
          `straight to the Recycle Bin:\n${app.install_location}\n\n` +
          `Only do this if the real uninstaller is broken, hangs, or is missing - this is NOT a normal ` +
          `uninstall. It will likely leave behind:\n` +
          `  - Registry entries (Windows may still list it as "installed")\n` +
          `  - Start Menu shortcuts and file associations\n` +
          `  - Background services, drivers, or scheduled tasks it set up\n` +
          `  - Shared files or settings other software might expect to find\n\n` +
          `If there's any chance the normal uninstaller could still work, try that first.`,
    confirmLabel: "Force delete files",
    onConfirm: async () => {
      try {
        const res = await fetch("/api/force-delete-app", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ app_id: app.id }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Failed");
        toast(data.message);
        loadApps(true);
      } catch (e) {
        toast(e.message, true);
      }
    },
  });
}

document.querySelectorAll("th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (appsState.sort.key === key) {
      appsState.sort.dir = appsState.sort.dir === "asc" ? "desc" : "asc";
    } else {
      appsState.sort = { key, dir: key === "size_bytes" ? "desc" : "asc" };
    }
    renderApps();
  });
});

document.getElementById("app-search").addEventListener("input", renderApps);
document.getElementById("app-type-filter").addEventListener("change", renderApps);
document.getElementById("refresh-apps").addEventListener("click", () => loadApps(true));

// ---------------- Sidebar storage bar ----------------

const CATEGORY_COLORS = {
  apps: "var(--cat-apps)",
  windows: "var(--cat-windows)",
  onedrive: "var(--cat-onedrive)",
  personal: "var(--cat-personal)",
  cache: "var(--cat-cache)",
  other: "var(--cat-other)",
  free: "var(--cat-free)",
};

const overviewState = { total: 0, used: 0, free: 0, categories: [] };

async function loadOverview() {
  try {
    const res = await fetch("/api/overview");
    const data = await res.json();
    overviewState.total = data.total_bytes;
    overviewState.used = data.used_bytes;
    overviewState.free = data.free_bytes;
    overviewState.categories = data.categories.map((c) => ({ ...c, size_bytes: null }));
    document.getElementById("sidebar-drive").textContent = data.drive;
    renderSidebar();
    overviewState.categories.forEach(loadCategorySize);
  } catch (e) {
    toast("Could not load the disk usage overview.", true);
  }
}

async function loadCategorySize(cat) {
  try {
    const res = await fetch(`/api/overview/category?id=${encodeURIComponent(cat.id)}`);
    const data = await res.json();
    cat.size_bytes = data.size_bytes;
    renderSidebar();
  } catch (e) { /* ignore */ }
}

function renderSidebar() {
  const bar = document.getElementById("storage-bar");
  const legend = document.getElementById("storage-legend");
  bar.innerHTML = "";
  legend.innerHTML = "";

  const known = overviewState.categories.filter((c) => c.size_bytes != null);
  const knownSum = known.reduce((s, c) => s + c.size_bytes, 0);
  const allLoaded = overviewState.categories.every((c) => c.size_bytes != null);
  const otherSize = allLoaded ? Math.max(0, overviewState.used - knownSum) : null;

  const rows = overviewState.categories.map((c) => ({ id: c.id, label: c.label, size: c.size_bytes, cat: c, clickable: true }));
  rows.push({ id: "other", label: "Other / uncategorized", size: otherSize, clickable: true, cat: { id: "other" } });
  rows.push({ id: "free", label: "Free space", size: overviewState.free, clickable: false });

  const total = overviewState.total || 1;

  rows.forEach((row) => {
    const pct = row.size != null ? (row.size / total) * 100 : 0;
    const seg = document.createElement("button");
    seg.className = "bar-segment";
    seg.style.background = CATEGORY_COLORS[row.id] || "var(--cat-other)";
    seg.style.height = `${pct}%`;
    seg.title = `${row.label}: ${row.size != null ? fmtSize(row.size) : "calculating..."}`;
    if (row.clickable) {
      seg.addEventListener("click", () => handleCategoryClick(row.cat));
    } else {
      seg.disabled = true;
      seg.style.cursor = "default";
    }
    bar.appendChild(seg);

    const li = document.createElement("li");
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = CATEGORY_COLORS[row.id] || "var(--cat-other)";
    li.appendChild(sw);
    const lbl = document.createElement("span");
    lbl.className = "legend-label";
    lbl.textContent = row.label;
    li.appendChild(lbl);
    const sz = document.createElement("span");
    sz.className = "legend-size";
    sz.textContent = row.size != null ? fmtSize(row.size) : "…";
    li.appendChild(sz);
    if (row.clickable) li.addEventListener("click", () => handleCategoryClick(row.cat));
    legend.appendChild(li);
  });
}

function handleCategoryClick(cat) {
  if (cat.id === "windows") {
    activateTab("storage");
    storageBrowser.hasLoaded = true;
    storageBrowser.browse(cat.path);
  } else if (cat.id === "onedrive") {
    activateTab("onedrive");
    oneDriveBrowser.hasLoaded = true;
    oneDriveRootPath = cat.path;
    document.getElementById("onedrive-missing").classList.add("hidden");
    oneDriveBrowser.browse(cat.path);
  } else if (cat.id === "apps") {
    activateTab("programs");
  } else if (cat.id === "personal") {
    activateTab("storage");
    storageBrowser.hasLoaded = true;
    storageBrowser.browseVirtual("Personal files", cat.paths || []);
  } else if (cat.id === "cache") {
    activateTab("storage");
    storageBrowser.hasLoaded = true;
    storageBrowser.browseVirtual("Cache & temp data", cat.paths || []);
  } else if (cat.id === "other") {
    activateTab("storage");
    storageBrowser.hasLoaded = true;
    browseOtherCategory();
  }
}

const OTHER_CATEGORY_WARNING =
  "These items aren't recognized by SafeCleanup - they don't match Windows, an installed app, " +
  "your personal folders, cache/temp data, or OneDrive. Some could still be important (personal " +
  "data, or files an app quietly relies on) even though nothing matched. Check each item carefully " +
  "before deleting anything here.";

async function browseOtherCategory() {
  try {
    const res = await fetch("/api/overview/other");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to load");
    storageBrowser.browseVirtual("Other / uncategorized", data.paths || [], OTHER_CATEGORY_WARNING);
  } catch (e) {
    toast("Could not load uncategorized files.", true);
  }
}

// ---------------- init ----------------
loadApps();
loadOverview();
