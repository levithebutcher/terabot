// ==========================================
// TeraBot Cloud Explorer & Mini App Engine
// ==========================================

// Initialize Telegram WebApp SDK
const tg = window.Telegram?.WebApp;
if (tg) {
  try {
    tg.ready();
    tg.expand();
    if (tg.enableClosingConfirmation) tg.enableClosingConfirmation();
    if (tg.setHeaderColor) tg.setHeaderColor("#080c14");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#080c14");
    if (tg.BackButton) {
      tg.BackButton.onClick(() => handleBackNavigation());
    }
  } catch (e) {
    console.debug("Telegram SDK init note:", e);
  }
}

// Global Application State
let sessionData = null;
let currentDirH = null;
let pathStack = []; // [{h, name}]
let selectedHandles = new Set();
let searchQuery = "";
let sortMode = "size_desc";
let filterType = "all";
let toastTimeout = null;
let activeApiBase = "";

// ---------------- UTILITIES ----------------

function showToast(message, icon = "✨") {
  const pill = document.getElementById("toastPill");
  if (!pill) return;
  document.getElementById("toastMsg").innerText = message;
  document.getElementById("toastIcon").innerText = icon;
  pill.classList.add("show");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => {
    pill.classList.remove("show");
  }, 2400);
}

function formatBytes(bytes) {
  if (!bytes || bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return (bytes / Math.pow(k, i)).toFixed(i >= 3 ? 2 : 1) + " " + sizes[i];
}

function haptic(type = "selectionChanged") {
  try {
    if (tg?.HapticFeedback) {
      if (type === "selectionChanged") tg.HapticFeedback.selectionChanged();
      else if (type === "impact") tg.HapticFeedback.impactOccurred("medium");
      else if (type === "heavy") tg.HapticFeedback.impactOccurred("heavy");
      else if (type === "success") tg.HapticFeedback.notificationOccurred("success");
      else if (type === "warning") tg.HapticFeedback.notificationOccurred("warning");
    }
  } catch (e) {}
}

function escapeHtml(str) {
  return (str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ---------------- API DETECTION & HEALTH CHECK ----------------

async function resolveApiBase() {
  const urlParams = new URLSearchParams(window.location.search);
  let paramApi = (urlParams.get("api_base") || "").trim().replace(/\/+$/, "");

  // Ignore default placeholders
  if (paramApi.includes("your-bot-backend-url.com") || paramApi.includes("example.com")) {
    paramApi = "";
  }

  const storedApi = (localStorage.getItem("terabot_api_base") || "").trim().replace(/\/+$/, "");

  // Candidate list in order of preference
  const candidates = [];
  if (paramApi) candidates.push(paramApi);
  if (storedApi) candidates.push(storedApi);

  // If page is hosted on a regular web server (e.g. Railway or http://localhost:8080), try origin
  if (window.location.protocol.startsWith("http") && !window.location.hostname.includes("github.io")) {
    candidates.push(window.location.origin);
  }

  // Local fallback
  candidates.push("http://localhost:8080");

  const badge = document.getElementById("backendBadge");
  const netStatus = document.getElementById("networkStatus");
  const dot = document.getElementById("statusDot");

  for (const base of candidates) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 2000);
      const res = await fetch(`${base}/health`, { signal: controller.signal });
      clearTimeout(timeoutId);
      if (res.ok) {
        activeApiBase = base;
        localStorage.setItem("terabot_api_base", base);
        if (badge) {
          badge.className = "backend-badge connected";
          badge.innerText = base.includes("localhost") ? "Local API" : "Cloud API";
        }
        if (netStatus) netStatus.innerText = "Connected";
        if (dot) dot.style.background = "#10b981";
        return activeApiBase;
      }
    } catch (e) {
      // Continue probing
    }
  }

  // None responded
  activeApiBase = candidates[0] || "";
  if (badge) {
    badge.className = "backend-badge offline";
    badge.innerText = "API Offline";
  }
  if (netStatus) netStatus.innerText = "Standalone Mode";
  if (dot) dot.style.background = "#f43f5e";
  return activeApiBase;
}

// ---------------- STARTUP & DATA LOADER ----------------

async function loadData() {
  await resolveApiBase();

  const urlParams = new URLSearchParams(window.location.search);
  const sessionId = urlParams.get("session_id") || window.location.hash.replace("#", "");

  // If a live session ID was passed via Telegram button:
  if (sessionId && sessionId !== "demo") {
    try {
      const res = await fetch(`${activeApiBase}/api/session/${sessionId}`);
      const json = await res.json();
      if (json.ok) {
        sessionData = json;
        sessionData.apiBase = activeApiBase;
        document.getElementById("networkStatus").innerText = json.service === "terabox" ? "TeraBox Cloud" : "Mega Cloud";
        initExplorer();
        showToast(`Loaded ${json.total_files} files from ${json.title}`, "📂");
        return;
      }
    } catch (e) {
      console.warn("Session fetch failed:", e);
    }
  }

  // Check if session was cached in sessionStorage
  const cachedSess = sessionStorage.getItem("terabot_active_session");
  if (cachedSess) {
    try {
      const parsed = JSON.parse(cachedSess);
      if (parsed && parsed.dirs) {
        sessionData = parsed;
        initExplorer();
        return;
      }
    } catch (e) {}
  }

  // Default to interactive demo view so user can test the UI immediately
  loadSampleDemo(false);
}

// ---------------- CLOUD LINK EXPLORER (TERABOX & MEGA) ----------------

async function handleExploreUrl() {
  const input = document.getElementById("cloudUrlInput");
  const url = (input.value || "").trim();

  if (!url) {
    showToast("Please paste a Mega or TeraBox URL", "⚠️");
    input.focus();
    return;
  }

  const isMega = /mega\.(nz|co\.nz|io)/i.test(url);
  const isTera = /terabox|teraboxapp|1024tera|nephobox|4funbox/i.test(url);

  if (!isMega && !isTera) {
    showToast("Supported: TeraBox & Mega folder/file links", "⚠️");
    return;
  }

  const btn = document.getElementById("exploreUrlBtn");
  const label = document.getElementById("exploreBtnLabel");
  const spinner = document.getElementById("exploreSpinner");

  btn.disabled = true;
  label.classList.add("hidden");
  spinner.classList.remove("hidden");
  haptic("impact");

  // Re-verify API base
  if (!activeApiBase) {
    await resolveApiBase();
  }

  try {
    const res = await fetch(`${activeApiBase}/api/explore`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });

    const json = await res.json();

    if (json.ok) {
      sessionData = json;
      sessionData.apiBase = activeApiBase;
      sessionStorage.setItem("terabot_active_session", JSON.stringify(json));
      selectedHandles.clear();

      document.getElementById("networkStatus").innerText = json.service === "terabox" ? "TeraBox Cloud" : "Mega Cloud";
      initExplorer();
      haptic("success");
      showToast(`🎉 Discovered ${json.total_files} files!`, "✨");
    } else {
      haptic("warning");
      alert(`⚠️ Could not explore link:\n${json.error || 'Unknown error'}`);
    }
  } catch (err) {
    haptic("warning");
    const isOffline = !activeApiBase || activeApiBase.includes("localhost");
    const helpMsg = isOffline
      ? `❌ Could not reach bot backend at ${activeApiBase || 'localhost:8080'}.\n\nPlease ensure your Telegram bot is running locally, or configure your API Base URL in Settings (⚙️).`
      : `❌ Network error exploring link: ${err.message}`;
    alert(helpMsg);
  } finally {
    btn.disabled = false;
    label.classList.remove("hidden");
    spinner.classList.add("hidden");
  }
}

async function handlePasteUrl() {
  haptic("impact");
  try {
    if (navigator.clipboard && navigator.clipboard.readText) {
      const text = await navigator.clipboard.readText();
      if (text && text.trim()) {
        const input = document.getElementById("cloudUrlInput");
        input.value = text.trim();
        showToast("Pasted link from clipboard!", "📋");
        handleExploreUrl();
        return;
      }
    }
  } catch (e) {
    console.debug("Clipboard API notice:", e);
  }
  // Fallback focus
  const input = document.getElementById("cloudUrlInput");
  input.focus();
  input.select();
  showToast("Paste your link and click Explore", "💡");
}

// ---------------- DEMO MOCK DATA ----------------

function loadSampleDemo(showToastMsg = true) {
  sessionData = {
    ok: true,
    session_id: "demo",
    service: "terabox",
    root_h: "dir_root",
    title: "Stranger Things Season 4 [Complete 4K HDR & 1080p]",
    total_files: 14,
    total_size: 48318382080,
    dirs: {
      "dir_root": {
        h: "dir_root",
        name: "Stranger Things Season 4",
        path: "/Stranger Things Season 4",
        parent: null,
        active_children: ["dir_4k", "dir_1080p", "dir_subs"],
        total_files: 14,
        total_size: 48318382080,
        files: [
          { h: "f_trailer", name: "Official_Trailer_4K_Atmos.mp4", size: 314572800, formatted_size: "300 MB", type: "video", ext: ".mp4", dlink: "https://sample-videos.com/video321/mp4/720/big_buck_bunny_720p_1mb.mp4" },
          { h: "f_poster", name: "StrangerThings_Poster_UltraHD.png", size: 15728640, formatted_size: "15 MB", type: "photo", ext: ".png", dlink: "https://picsum.photos/1200/800" },
          { h: "f_soundtrack", name: "Running_Up_That_Hill_FLAC.zip", size: 125829120, formatted_size: "120 MB", type: "other", ext: ".zip", dlink: "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf" },
          { h: "f_nfo", name: "Release_Notes_x265.nfo", size: 4096, formatted_size: "4 KB", type: "other", ext: ".nfo", dlink: "https://www.w3.org/TR/PNG/iso_8859-1.txt" }
        ]
      },
      "dir_4k": {
        h: "dir_4k",
        name: "4K HDR Dolby Vision [2160p]",
        path: "/Stranger Things Season 4/4K HDR Dolby Vision",
        parent: "dir_root",
        active_children: [],
        total_files: 4,
        total_size: 33554432000,
        files: [
          { h: "f_4k_e1", name: "Stranger.Things.S04E01.Chapter.One.2160p.mkv", size: 8589934592, formatted_size: "8.00 GB", type: "video", ext: ".mkv", dlink: "#" },
          { h: "f_4k_e2", name: "Stranger.Things.S04E02.Vecnas.Curse.2160p.mkv", size: 8388608000, formatted_size: "7.81 GB", type: "video", ext: ".mkv", dlink: "#" },
          { h: "f_4k_e3", name: "Stranger.Things.S04E03.The.Monster.2160p.mkv", size: 8257077248, formatted_size: "7.69 GB", type: "video", ext: ".mkv", dlink: "#" },
          { h: "f_4k_e4", name: "Stranger.Things.S04E04.Dear.Billy.2160p.mkv", size: 8318812160, formatted_size: "7.75 GB", type: "video", ext: ".mkv", dlink: "#" }
        ]
      },
      "dir_1080p": {
        h: "dir_1080p",
        name: "1080p WEB-DL x265 HEVC",
        path: "/Stranger Things Season 4/1080p WEB-DL x265",
        parent: "dir_root",
        active_children: [],
        total_files: 4,
        total_size: 14763950080,
        files: [
          { h: "f_1080_e1", name: "Stranger.Things.S04E01.1080p.mkv", size: 3690987520, formatted_size: "3.44 GB", type: "video", ext: ".mkv", dlink: "#" },
          { h: "f_1080_e2", name: "Stranger.Things.S04E02.1080p.mkv", size: 3623878656, formatted_size: "3.38 GB", type: "video", ext: ".mkv", dlink: "#" },
          { h: "f_1080_e3", name: "Stranger.Things.S04E03.1080p.mkv", size: 3724541952, formatted_size: "3.47 GB", type: "video", ext: ".mkv", dlink: "#" },
          { h: "f_1080_e4", name: "Stranger.Things.S04E04.1080p.mkv", size: 3724541952, formatted_size: "3.47 GB", type: "video", ext: ".mkv", dlink: "#" }
        ]
      },
      "dir_subs": {
        h: "dir_subs",
        name: "Subtitles Multilingual Pack",
        path: "/Stranger Things Season 4/Subtitles Multilingual",
        parent: "dir_root",
        active_children: [],
        total_files: 3,
        total_size: 3145728,
        files: [
          { h: "f_sub_en", name: "English_Complete_SDH.srt", size: 1048576, formatted_size: "1.0 MB", type: "other", ext: ".srt", dlink: "#" },
          { h: "f_sub_hi", name: "Hindi_Dubbed_Sync.srt", size: 1048576, formatted_size: "1.0 MB", type: "other", ext: ".srt", dlink: "#" },
          { h: "f_sub_es", name: "Spanish_LatAm.srt", size: 1048576, formatted_size: "1.0 MB", type: "other", ext: ".srt", dlink: "#" }
        ]
      }
    }
  };

  initExplorer();
  if (showToastMsg) {
    showToast("Loaded Sample Demo Explorer", "🎮");
  }
}

// ---------------- EXPLORER RENDERER ----------------

function initExplorer() {
  currentDirH = sessionData.root_h || Object.keys(sessionData.dirs)[0] || "root";
  pathStack = [{ h: currentDirH, name: sessionData.title || "Root" }];
  
  const counter = document.getElementById("statCounter");
  if (counter) {
    counter.innerText = `${sessionData.total_files} Files • ${formatBytes(sessionData.total_size)}`;
  }
  renderExplorer();
}

function updateChipCounters(files) {
  let videos = 0, photos = 0, others = 0;
  files.forEach(f => {
    if (f.type === "video") videos++;
    else if (f.type === "photo") photos++;
    else others++;
  });
  const elAll = document.getElementById("chipCountAll");
  const elVid = document.getElementById("chipCountVideo");
  const elPho = document.getElementById("chipCountPhoto");
  const elOth = document.getElementById("chipCountOther");

  if (elAll) elAll.innerText = files.length;
  if (elVid) elVid.innerText = videos;
  if (elPho) elPho.innerText = photos;
  if (elOth) elOth.innerText = others;
}

function renderExplorer() {
  updateBreadcrumbs();
  updateBackButton();

  const main = document.getElementById("mainContent");
  if (!main) return;

  const currentDir = sessionData.dirs[currentDirH];

  if (!currentDir) {
    main.innerHTML = `
      <div class="state-container">
        <div class="state-icon-box">⚠️</div>
        <div class="state-header">Folder Not Found</div>
        <div class="state-description">The requested folder could not be found or has expired.</div>
      </div>
    `;
    return;
  }

  let childDirs = (currentDir.active_children || [])
    .map(h => sessionData.dirs[h])
    .filter(Boolean);

  let directFiles = currentDir.files || [];

  updateChipCounters(directFiles);

  // Type Filter
  if (filterType !== "all") {
    directFiles = directFiles.filter(f => f.type === filterType);
  }

  // Search Filter
  if (searchQuery.trim()) {
    const q = searchQuery.toLowerCase();
    childDirs = childDirs.filter(d => d.name.toLowerCase().includes(q));
    directFiles = directFiles.filter(f => f.name.toLowerCase().includes(q));
  }

  // Sorting
  if (sortMode === "size_desc") {
    childDirs.sort((a, b) => b.total_size - a.total_size);
    directFiles.sort((a, b) => b.size - a.size);
  } else if (sortMode === "size_asc") {
    childDirs.sort((a, b) => a.total_size - b.total_size);
    directFiles.sort((a, b) => a.size - b.size);
  } else if (sortMode === "name_asc") {
    childDirs.sort((a, b) => a.name.localeCompare(b.name));
    directFiles.sort((a, b) => a.name.localeCompare(b.name));
  } else if (sortMode === "name_desc") {
    childDirs.sort((a, b) => b.name.localeCompare(a.name));
    directFiles.sort((a, b) => b.name.localeCompare(a.name));
  }

  let html = "";

  // Section: Subfolders
  if (childDirs.length > 0) {
    html += `
      <div class="section-head">
        <div class="section-title">📂 Subfolders (${childDirs.length})</div>
      </div>
      <div class="cards-flow">
    `;
    childDirs.forEach(d => {
      const subFiles = getAllFilesRecursive(d.h);
      const allSubSelected = subFiles.length > 0 && subFiles.every(f => selectedHandles.has(f.h));
      const someSubSelected = subFiles.some(f => selectedHandles.has(f.h));

      html += `
        <div class="card ${allSubSelected ? 'selected' : ''}" onclick="openFolder('${d.h}', '${escapeHtml(d.name)}')">
          <div class="icon-wrapper icon-folder">📁</div>
          <div class="card-content">
            <div class="card-title">${escapeHtml(d.name)}</div>
            <div class="card-meta">
              <span class="ext-badge">${d.total_files} files</span>
              <span class="meta-size">${formatBytes(d.total_size)}</span>
            </div>
          </div>
          <button class="folder-select-all" onclick="event.stopPropagation(); toggleSubfolderFiles('${d.h}', ${allSubSelected})">
            ${allSubSelected ? '✓ Selected' : (someSubSelected ? '⚡ Add All' : '+ Select All')}
          </button>
          <div class="folder-nav-btn">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="9 18 15 12 9 6"></polyline>
            </svg>
          </div>
        </div>
      `;
    });
    html += `</div>`;
  }

  // Section: Files
  if (directFiles.length > 0) {
    const allSelected = directFiles.every(f => selectedHandles.has(f.h));
    html += `
      <div class="section-head">
        <div class="section-title">📄 Files (${directFiles.length})</div>
        <div class="section-actions">
          <button class="batch-action-btn" onclick="toggleSelectAllCurrent(${allSelected})">
            ${allSelected ? '✕ Deselect All' : '✓ Select All'}
          </button>
        </div>
      </div>
      <div class="cards-flow">
    `;

    directFiles.forEach(f => {
      const isSelected = selectedHandles.has(f.h);
      let iconClass = "icon-doc";
      let iconSymbol = "📄";
      if (f.type === "video") {
        iconClass = "icon-video";
        iconSymbol = "🎬";
      } else if (f.type === "photo") {
        iconClass = "icon-photo";
        iconSymbol = "🖼️";
      } else if (f.ext === ".zip" || f.ext === ".rar" || f.ext === ".7z" || f.ext === ".tar") {
        iconClass = "icon-archive";
        iconSymbol = "📦";
      }

      html += `
        <div class="card ${isSelected ? 'selected' : ''}" onclick="toggleFileSelection('${f.h}')">
          <div class="icon-wrapper ${iconClass}" onclick="event.stopPropagation(); showFileDetail('${f.h}')" title="Click for details & direct link">
            ${iconSymbol}
          </div>
          <div class="card-content">
            <div class="card-title">${escapeHtml(f.name)}</div>
            <div class="card-meta">
              <span class="ext-badge">${f.ext ? f.ext.replace('.', '').toUpperCase() : 'FILE'}</span>
              <span class="meta-size">${f.formatted_size || formatBytes(f.size)}</span>
              ${f.dlink ? '<span class="ext-badge" style="background: rgba(16, 185, 129, 0.2); color: #34d399;">DIRECT</span>' : ''}
            </div>
          </div>
          <div class="checkbox-box">
            <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
        </div>
      `;
    });
    html += `</div>`;
  }

  // Empty State
  if (childDirs.length === 0 && directFiles.length === 0) {
    html = `
      <div class="state-container">
        <div class="state-icon-box">🔍</div>
        <div class="state-header">No Matching Items</div>
        <div class="state-description">Try adjusting your search query, reset filters, or paste a new link above.</div>
        <button class="batch-action-btn" style="margin-top: 14px;" onclick="clearSearch(); setFilter('all', document.querySelector('.chip-btn'))">
          Reset Filters
        </button>
      </div>
    `;
  }

  main.innerHTML = html;
  updateActionBar();
}

function getAllFilesRecursive(dirH) {
  let results = [];
  const dir = sessionData.dirs[dirH];
  if (!dir) return results;
  if (dir.files) results.push(...dir.files);
  (dir.active_children || []).forEach(childH => {
    results.push(...getAllFilesRecursive(childH));
  });
  return results;
}

function toggleSubfolderFiles(dirH, currentlyAllSelected) {
  haptic("impact");
  const files = getAllFilesRecursive(dirH);
  if (currentlyAllSelected) {
    files.forEach(f => selectedHandles.delete(f.h));
    showToast(`Deselected ${files.length} files in folder`, "✕");
  } else {
    files.forEach(f => selectedHandles.add(f.h));
    showToast(`Selected ${files.length} files in folder`, "✓");
  }
  renderExplorer();
}

function openFolder(h, name) {
  haptic("impact");
  currentDirH = h;
  pathStack.push({ h, name });
  renderExplorer();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function handleBackNavigation() {
  if (pathStack.length > 1) {
    haptic("impact");
    pathStack.pop();
    currentDirH = pathStack[pathStack.length - 1].h;
    renderExplorer();
  }
}

function navigateBreadcrumb(index) {
  if (index === "root") {
    pathStack = [pathStack[0]];
    currentDirH = pathStack[0].h;
  } else {
    pathStack = pathStack.slice(0, index + 1);
    currentDirH = pathStack[pathStack.length - 1].h;
  }
  haptic("impact");
  renderExplorer();
}

function updateBreadcrumbs() {
  const wrap = document.getElementById("breadcrumbs");
  if (!wrap) return;
  let html = "";
  pathStack.forEach((crumb, idx) => {
    const isLast = idx === pathStack.length - 1;
    html += `
      <div class="crumb-pill ${isLast ? 'active' : ''}" onclick="navigateBreadcrumb(${idx})">
        ${idx === 0 ? '🏠 ' : '📁 '}${escapeHtml(crumb.name)}
      </div>
    `;
    if (!isLast) html += `<span class="crumb-divider">/</span>`;
  });
  wrap.innerHTML = html;
  wrap.scrollLeft = wrap.scrollWidth;
}

function updateBackButton() {
  if (tg?.BackButton) {
    if (pathStack.length > 1) tg.BackButton.show();
    else tg.BackButton.hide();
  }
}

function toggleFileSelection(h) {
  haptic("selectionChanged");
  if (selectedHandles.has(h)) selectedHandles.delete(h);
  else selectedHandles.add(h);
  renderExplorer();
}

function toggleSelectAllCurrent(currentlyAllSelected) {
  haptic("impact");
  const currentDir = sessionData.dirs[currentDirH];
  if (!currentDir || !currentDir.files) return;

  currentDir.files.forEach(f => {
    if (currentlyAllSelected) selectedHandles.delete(f.h);
    else selectedHandles.add(f.h);
  });
  showToast(currentlyAllSelected ? "Deselected all in view" : "Selected all in view", "✓");
  renderExplorer();
}

function clearAllSelections() {
  haptic("warning");
  selectedHandles.clear();
  showToast("Cleared all selections", "🗑️");
  renderExplorer();
}

// ---------------- ACTION BAR & BUTTONS ----------------

function updateActionBar() {
  const count = selectedHandles.size;
  let totalBytes = 0;

  Object.values(sessionData.dirs).forEach(d => {
    (d.files || []).forEach(f => {
      if (selectedHandles.has(f.h)) {
        totalBytes += f.size || 0;
      }
    });
  });

  const countText = `${count} File${count === 1 ? '' : 's'} Selected`;
  const sizeText = formatBytes(totalBytes);
  const btnText = `🚀 Download (${count} • ${sizeText})`;

  // 1. Native Telegram MainButton
  if (tg?.MainButton) {
    if (count > 0) {
      tg.MainButton.setText(btnText);
      tg.MainButton.show();
      tg.MainButton.enable();
      tg.MainButton.onClick(submitDownload);
    } else {
      tg.MainButton.hide();
    }
  }

  // 2. Floating On-Screen Bar
  const bar = document.getElementById("bottomBar");
  if (bar) {
    if (count > 0) {
      document.getElementById("barCount").innerText = countText;
      document.getElementById("barSize").innerText = `${sizeText} Total`;
      document.getElementById("downloadBtn").innerHTML = `<span>🚀 Send to Bot (${count})</span>`;
      document.getElementById("directDownloadBtn").innerHTML = `<span>📥 Direct Link (${count})</span>`;
      bar.classList.add("active");
    } else {
      bar.classList.remove("active");
    }
  }
}

// ---------------- SEARCH, SORT & FILTERS ----------------

function handleSearch(val) {
  searchQuery = val;
  const clearBtn = document.getElementById("searchClearBtn");
  if (clearBtn) {
    if (val.trim()) clearBtn.classList.add("visible");
    else clearBtn.classList.remove("visible");
  }
  renderExplorer();
}

function clearSearch() {
  const input = document.getElementById("searchInput");
  if (!input) return;
  input.value = "";
  handleSearch("");
  input.focus();
}

function handleSort(val) {
  sortMode = val;
  haptic("impact");
  renderExplorer();
}

function setFilter(type, el) {
  filterType = type;
  haptic("impact");
  document.querySelectorAll(".chip-btn").forEach(c => c.classList.remove("active"));
  if (el) el.classList.add("active");
  renderExplorer();
}

// ---------------- FILE DETAIL MODAL ----------------

function showFileDetail(h) {
  let targetFile = null;
  for (const d of Object.values(sessionData.dirs)) {
    const found = (d.files || []).find(f => f.h === h);
    if (found) { targetFile = found; break; }
  }
  if (!targetFile) return;

  document.getElementById("modalFileName").innerText = targetFile.name;
  document.getElementById("modalFileSize").innerText = targetFile.formatted_size || formatBytes(targetFile.size);
  document.getElementById("modalFileType").innerText = (targetFile.type || "FILE").toUpperCase();
  document.getElementById("modalFileExt").innerText = targetFile.ext || "None";
  document.getElementById("modalFileHandle").innerText = targetFile.h;

  const actionArea = document.getElementById("modalActionArea");
  if (actionArea) {
    if (targetFile.dlink && targetFile.dlink !== "#") {
      actionArea.innerHTML = `
        <a href="${targetFile.dlink}" target="_blank" rel="noopener noreferrer" class="link-action-btn" style="flex:1; text-align:center; padding: 10px; background: var(--accent); color:#fff; text-decoration:none; border-radius:8px;">
          📥 Download This File Directly
        </a>
      `;
    } else {
      actionArea.innerHTML = ``;
    }
  }

  document.getElementById("detailModal").classList.add("open");
  haptic("impact");
}

function closeDetailModal() {
  document.getElementById("detailModal").classList.remove("open");
}

// ---------------- DIRECT DOWNLOAD / LINKS MODAL ----------------

async function handleDirectDownload() {
  if (selectedHandles.size === 0) {
    showToast("Please select at least 1 file", "⚠️");
    return;
  }
  haptic("heavy");

  const handles = Array.from(selectedHandles);
  const links = [];

  // Scan current session files
  for (const d of Object.values(sessionData.dirs)) {
    for (const f of d.files || []) {
      if (selectedHandles.has(f.h)) {
        links.push({
          h: f.h,
          name: f.name,
          size: f.size,
          formatted_size: f.formatted_size || formatBytes(f.size),
          dlink: f.dlink || ""
        });
      }
    }
  }

  // If TeraBox dlinks aren't in memory yet, ask server
  const missingDlinks = links.filter(l => !l.dlink);
  if (missingDlinks.length > 0 && activeApiBase && sessionData.session_id !== "demo") {
    try {
      showToast("Fetching direct links from server...", "⏳");
      const res = await fetch(`${activeApiBase}/api/direct_links`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionData.session_id,
          selected_handles: handles
        })
      });
      const json = await res.json();
      if (json.ok && json.links) {
        json.links.forEach(serverLink => {
          const match = links.find(l => l.h === serverLink.h);
          if (match) match.dlink = serverLink.dlink;
        });
      }
    } catch (e) {
      console.warn("Direct links fetch failed:", e);
    }
  }

  renderDirectLinksModal(links);
}

function renderDirectLinksModal(links) {
  const container = document.getElementById("directLinksList");
  if (!container) return;

  let html = "";
  links.forEach(l => {
    const hasUrl = l.dlink && l.dlink !== "#";
    html += `
      <div class="direct-link-card">
        <div class="direct-link-info">
          <div class="direct-link-name">${escapeHtml(l.name)}</div>
          <div class="direct-link-meta">${l.formatted_size} • ${hasUrl ? 'Direct Link Ready' : 'Decryption Required'}</div>
        </div>
        <div class="direct-link-actions">
          ${hasUrl ? `
            <button class="link-action-btn" onclick="copySingleLink('${l.dlink}')">📋 Copy</button>
            <a href="${l.dlink}" target="_blank" download="${escapeHtml(l.name)}" rel="noopener noreferrer" class="link-action-btn" style="background:var(--accent);">📥 Download</a>
          ` : `
            <button class="link-action-btn" onclick="submitDownload()">🚀 Send to Bot</button>
          `}
        </div>
      </div>
    `;
  });

  container.innerHTML = html;
  window._activeDirectLinks = links;
  document.getElementById("directLinksModal").classList.add("open");
}

function closeDirectLinksModal() {
  document.getElementById("directLinksModal").classList.remove("open");
}

function copyAllDirectLinks() {
  const links = window._activeDirectLinks || [];
  const urls = links.map(l => l.dlink).filter(u => u && u !== "#").join("\n");
  if (!urls) {
    showToast("No direct links available to copy", "⚠️");
    return;
  }
  navigator.clipboard.writeText(urls).then(() => {
    haptic("success");
    showToast("Copied all direct download links!", "📋");
  }).catch(() => {
    alert("Clipboard copy failed. Please copy manually.");
  });
}

function copySingleLink(url) {
  navigator.clipboard.writeText(url).then(() => {
    haptic("success");
    showToast("Copied download link!", "📋");
  });
}

// ---------------- TELEGRAM BOT DISPATCH ----------------

async function submitDownload() {
  if (selectedHandles.size === 0) {
    showToast("Please select at least 1 file", "⚠️");
    return;
  }
  haptic("heavy");

  const handles = Array.from(selectedHandles);

  if (tg?.MainButton) {
    tg.MainButton.showProgress();
  }

  // Demo Mode
  if (sessionData.session_id === "demo") {
    const msg = `✅ Demo Success!\n\nSelected: ${handles.length} files.\nIn production, TeraBot will download and upload these files directly to your Telegram chat & Storage Channel!`;
    if (tg?.showAlert) tg.showAlert(msg);
    else alert(msg);
    if (tg?.MainButton) tg.MainButton.hideProgress();
    return;
  }

  try {
    const res = await fetch(`${activeApiBase}/api/session/${sessionData.session_id}/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        selected_handles: handles,
        init_data: tg?.initData || ""
      })
    });

    const json = await res.json();
    if (json.ok) {
      haptic("success");
      const alertMsg = `🚀 Starting download for ${handles.length} selected files in Telegram!\n\nPlease check your Telegram chat with the bot.`;
      if (tg?.showAlert) {
        tg.showAlert(alertMsg, () => tg.close());
      } else {
        alert(alertMsg);
      }
    } else {
      haptic("warning");
      alert(`⚠️ Notice: ${json.error || 'Failed to submit selection to bot.'}`);
    }
  } catch (err) {
    haptic("warning");
    alert(`❌ Network error submitting selection: ${err.message}\n\nPlease verify backend URL at: ${activeApiBase}`);
  } finally {
    if (tg?.MainButton) tg.MainButton.hideProgress();
  }
}

// ---------------- API CONFIG MODAL ----------------

function openApiConfigModal() {
  const modal = document.getElementById("apiConfigModal");
  const input = document.getElementById("apiBaseInput");
  const statusEl = document.getElementById("apiModalStatus");
  if (input) input.value = activeApiBase || localStorage.getItem("terabot_api_base") || "http://localhost:8080";
  if (statusEl) statusEl.innerText = `Active API: ${activeApiBase || 'None'}`;
  modal.classList.add("open");
}

function closeApiConfigModal() {
  document.getElementById("apiConfigModal").classList.remove("open");
}

async function testCustomApi() {
  const input = document.getElementById("apiBaseInput");
  const statusEl = document.getElementById("apiModalStatus");
  const base = (input.value || "").trim().replace(/\/+$/, "");

  statusEl.innerText = "Connecting...";
  try {
    const res = await fetch(`${base}/health`);
    const json = await res.json();
    if (json.status === "ok") {
      statusEl.innerHTML = `<span style="color:#10b981;font-weight:700;">✅ Connected (${json.service})</span>`;
    } else {
      statusEl.innerHTML = `<span style="color:#f59e0b;">⚠️ Server responded with unexpected status</span>`;
    }
  } catch (e) {
    statusEl.innerHTML = `<span style="color:#f43f5e;">❌ Connection failed (${e.message})</span>`;
  }
}

function saveCustomApi() {
  const input = document.getElementById("apiBaseInput");
  const base = (input.value || "").trim().replace(/\/+$/, "");
  activeApiBase = base;
  localStorage.setItem("terabot_api_base", base);
  closeApiConfigModal();
  resolveApiBase();
  showToast("API URL saved and connected!", "💾");
}

// ---------------- INITIALIZATION ON DOM READY ----------------

window.addEventListener("DOMContentLoaded", loadData);
