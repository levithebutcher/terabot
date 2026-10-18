// Initialize Telegram WebApp SDK
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  if (tg.enableClosingConfirmation) tg.enableClosingConfirmation();
  if (tg.setHeaderColor) tg.setHeaderColor("#080c14");
  if (tg.setBackgroundColor) tg.setBackgroundColor("#080c14");
  if (tg.BackButton) {
    tg.BackButton.onClick(() => handleBackNavigation());
  }
}

// State Variables
let sessionData = null;
let currentDirH = null;
let pathStack = []; // [{h, name}]
let selectedHandles = new Set();
let searchQuery = "";
let sortMode = "size_desc";
let filterType = "all";
let toastTimeout = null;

// Toast Notification Utility
function showToast(message, icon = "✨") {
  const pill = document.getElementById("toastPill");
  document.getElementById("toastMsg").innerText = message;
  document.getElementById("toastIcon").innerText = icon;
  pill.classList.add("show");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => {
    pill.classList.remove("show");
  }, 2400);
}

// Format Bytes
function formatBytes(bytes) {
  if (!bytes || bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return (bytes / Math.pow(k, i)).toFixed(i >= 3 ? 2 : 1) + " " + sizes[i];
}

// Telegram Haptic Feedback Helper
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

// Startup & Data Loader
async function loadData() {
  const urlParams = new URLSearchParams(window.location.search);
  const sessionId = urlParams.get("session_id") || window.location.hash.replace("#", "");
  const apiBase = (urlParams.get("api_base") || "").replace(/\/+$/, "");

  if (sessionId && sessionId !== "demo") {
    try {
      const res = await fetch(`${apiBase}/api/session/${sessionId}`);
      const json = await res.json();
      if (json.ok) {
        sessionData = json;
        sessionData.apiBase = apiBase;
        document.getElementById("networkStatus").innerText = "Connected";
        initExplorer();
        return;
      } else {
        console.warn("API returned error:", json.error);
      }
    } catch (e) {
      console.warn("API request failed, loading interactive demo view:", e);
    }
  }

  // Demo Mock Data for Browser / Local Preview
  document.getElementById("networkStatus").innerText = "Demo Preview";
  sessionData = {
    ok: true,
    session_id: "demo",
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
          { h: "f_trailer", name: "Official_Trailer_4K_Atmos.mp4", size: 314572800, formatted_size: "300 MB", type: "video", ext: ".mp4" },
          { h: "f_poster", name: "StrangerThings_Poster_UltraHD.png", size: 15728640, formatted_size: "15 MB", type: "photo", ext: ".png" },
          { h: "f_soundtrack", name: "Running_Up_That_Hill_FLAC.zip", size: 125829120, formatted_size: "120 MB", type: "other", ext: ".zip" },
          { h: "f_nfo", name: "Release_Notes_x265.nfo", size: 4096, formatted_size: "4 KB", type: "other", ext: ".nfo" }
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
          { h: "f_4k_e1", name: "Stranger.Things.S04E01.Chapter.One.2160p.mkv", size: 8589934592, formatted_size: "8.00 GB", type: "video", ext: ".mkv" },
          { h: "f_4k_e2", name: "Stranger.Things.S04E02.Vecnas.Curse.2160p.mkv", size: 8388608000, formatted_size: "7.81 GB", type: "video", ext: ".mkv" },
          { h: "f_4k_e3", name: "Stranger.Things.S04E03.The.Monster.2160p.mkv", size: 8257077248, formatted_size: "7.69 GB", type: "video", ext: ".mkv" },
          { h: "f_4k_e4", name: "Stranger.Things.S04E04.Dear.Billy.2160p.mkv", size: 8318812160, formatted_size: "7.75 GB", type: "video", ext: ".mkv" }
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
          { h: "f_1080_e1", name: "Stranger.Things.S04E01.1080p.mkv", size: 3690987520, formatted_size: "3.44 GB", type: "video", ext: ".mkv" },
          { h: "f_1080_e2", name: "Stranger.Things.S04E02.1080p.mkv", size: 3623878656, formatted_size: "3.38 GB", type: "video", ext: ".mkv" },
          { h: "f_1080_e3", name: "Stranger.Things.S04E03.1080p.mkv", size: 3724541952, formatted_size: "3.47 GB", type: "video", ext: ".mkv" },
          { h: "f_1080_e4", name: "Stranger.Things.S04E04.1080p.mkv", size: 3724541952, formatted_size: "3.47 GB", type: "video", ext: ".mkv" }
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
          { h: "f_sub_en", name: "English_Complete_SDH.srt", size: 1048576, formatted_size: "1.0 MB", type: "other", ext: ".srt" },
          { h: "f_sub_hi", name: "Hindi_Dubbed_Sync.srt", size: 1048576, formatted_size: "1.0 MB", type: "other", ext: ".srt" },
          { h: "f_sub_es", name: "Spanish_LatAm.srt", size: 1048576, formatted_size: "1.0 MB", type: "other", ext: ".srt" }
        ]
      }
    }
  };
  initExplorer();
}

function initExplorer() {
  currentDirH = sessionData.root_h;
  pathStack = [{ h: sessionData.root_h, name: sessionData.title || "Root" }];
  document.getElementById("statCounter").innerText = `${sessionData.total_files} Files • ${formatBytes(sessionData.total_size)}`;
  renderExplorer();
}

// Helper: Count files by category in the current directory
function updateChipCounters(files) {
  let videos = 0, photos = 0, others = 0;
  files.forEach(f => {
    if (f.type === "video") videos++;
    else if (f.type === "photo") photos++;
    else others++;
  });
  document.getElementById("chipCountAll").innerText = files.length;
  document.getElementById("chipCountVideo").innerText = videos;
  document.getElementById("chipCountPhoto").innerText = photos;
  document.getElementById("chipCountOther").innerText = others;
}

// Render Current Directory
function renderExplorer() {
  updateBreadcrumbs();
  updateBackButton();

  const main = document.getElementById("mainContent");
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

  // Collect direct subfolders and direct files
  let childDirs = (currentDir.active_children || [])
    .map(h => sessionData.dirs[h])
    .filter(Boolean);

  let directFiles = currentDir.files || [];

  // Update chip counters for all direct files in current dir
  updateChipCounters(directFiles);

  // Filter by Type
  if (filterType !== "all") {
    directFiles = directFiles.filter(f => f.type === filterType);
  }

  // Filter by Search Query
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
      // Check if all files in this subfolder are currently selected
      const subfolderFiles = getAllFilesRecursive(d.h);
      const allSubSelected = subfolderFiles.length > 0 && subfolderFiles.every(f => selectedHandles.has(f.h));
      const someSubSelected = subfolderFiles.some(f => selectedHandles.has(f.h));

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
          <div class="icon-wrapper ${iconClass}" onclick="event.stopPropagation(); showFileDetail('${f.h}')">
            ${iconSymbol}
          </div>
          <div class="card-content">
            <div class="card-title">${escapeHtml(f.name)}</div>
            <div class="card-meta">
              <span class="ext-badge">${f.ext ? f.ext.replace('.', '').toUpperCase() : 'FILE'}</span>
              <span class="meta-size">${formatBytes(f.size)}</span>
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
        <div class="state-description">Try adjusting your search query or reset filter category.</div>
        <button class="batch-action-btn" style="margin-top: 14px;" onclick="clearSearch(); setFilter('all', document.querySelector('.chip-btn'))">
          Reset Filters
        </button>
      </div>
    `;
  }

  main.innerHTML = html;
  updateActionBar();
}

// Helper: Collect all files within a subfolder tree recursively
function getAllFilesRecursive(dirH) {
  let results = [];
  const dir = sessionData.dirs[dirH];
  if (!dir) return results;
  if (dir.files) {
    results.push(...dir.files);
  }
  (dir.active_children || []).forEach(childH => {
    results.push(...getAllFilesRecursive(childH));
  });
  return results;
}

// Toggle all files in a subfolder from folder card
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

// Open Folder Navigation
function openFolder(h, name) {
  haptic("impact");
  currentDirH = h;
  pathStack.push({ h, name });
  renderExplorer();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// Back Navigation
function handleBackNavigation() {
  if (pathStack.length > 1) {
    haptic("impact");
    pathStack.pop();
    currentDirH = pathStack[pathStack.length - 1].h;
    renderExplorer();
  }
}

// Breadcrumb Jump
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
  let html = "";
  pathStack.forEach((crumb, idx) => {
    const isLast = idx === pathStack.length - 1;
    html += `
      <div class="crumb-pill ${isLast ? 'active' : ''}" onclick="navigateBreadcrumb(${idx})">
        ${idx === 0 ? '🏠 ' : '📁 '}${escapeHtml(crumb.name)}
      </div>
    `;
    if (!isLast) {
      html += `<span class="crumb-divider">/</span>`;
    }
  });
  wrap.innerHTML = html;
  wrap.scrollLeft = wrap.scrollWidth;
}

function updateBackButton() {
  if (tg?.BackButton) {
    if (pathStack.length > 1) {
      tg.BackButton.show();
    } else {
      tg.BackButton.hide();
    }
  }
}

// File Selection Handlers
function toggleFileSelection(h) {
  haptic("selectionChanged");
  if (selectedHandles.has(h)) {
    selectedHandles.delete(h);
  } else {
    selectedHandles.add(h);
  }
  renderExplorer();
}

function toggleSelectAllCurrent(currentlyAllSelected) {
  haptic("impact");
  const currentDir = sessionData.dirs[currentDirH];
  if (!currentDir || !currentDir.files) return;

  currentDir.files.forEach(f => {
    if (currentlyAllSelected) {
      selectedHandles.delete(f.h);
    } else {
      selectedHandles.add(f.h);
    }
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

// Action Bar & Telegram MainButton Synchronization
function updateActionBar() {
  const count = selectedHandles.size;
  let totalBytes = 0;

  // Calculate total bytes of selected handles across all folders
  Object.values(sessionData.dirs).forEach(d => {
    (d.files || []).forEach(f => {
      if (selectedHandles.has(f.h)) {
        totalBytes += f.size;
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

  // 2. Floating On-Screen Bar (For Browser Preview / Fallback)
  const bar = document.getElementById("bottomBar");
  if (count > 0) {
    document.getElementById("barCount").innerText = countText;
    document.getElementById("barSize").innerText = `${sizeText} Total`;
    document.getElementById("downloadBtn").innerHTML = `<span>🚀 Download (${count})</span>`;
    bar.classList.add("active");
  } else {
    bar.classList.remove("active");
  }
}

// Search and Sort Event Handlers
function handleSearch(val) {
  searchQuery = val;
  const clearBtn = document.getElementById("searchClearBtn");
  if (val.trim()) {
    clearBtn.classList.add("visible");
  } else {
    clearBtn.classList.remove("visible");
  }
  renderExplorer();
}

function clearSearch() {
  const input = document.getElementById("searchInput");
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
  el.classList.add("active");
  renderExplorer();
}

// File Detail Modal Sheet
function showFileDetail(h) {
  let targetFile = null;
  for (const d of Object.values(sessionData.dirs)) {
    const found = (d.files || []).find(f => f.h === h);
    if (found) { targetFile = found; break; }
  }
  if (!targetFile) return;

  document.getElementById("modalFileName").innerText = targetFile.name;
  document.getElementById("modalFileSize").innerText = formatBytes(targetFile.size);
  document.getElementById("modalFileType").innerText = targetFile.type.toUpperCase();
  document.getElementById("modalFileExt").innerText = targetFile.ext || "None";
  document.getElementById("modalFileHandle").innerText = targetFile.h;

  document.getElementById("detailModal").classList.add("open");
  haptic("impact");
}

function closeDetailModal(e) {
  document.getElementById("detailModal").classList.remove("open");
}

// Submit Selection to Bot Backend
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

  // Demo Mode Handler
  if (sessionData.session_id === "demo") {
    const msg = `✅ Demo Success!\n\nSelected: ${handles.length} files.\nIn Telegram, the bot will immediately begin downloading and uploading these files to your chat!`;
    if (tg?.showAlert) {
      tg.showAlert(msg);
    } else {
      alert(msg);
    }
    if (tg?.MainButton) tg.MainButton.hideProgress();
    return;
  }

  try {
    const base = sessionData.apiBase || "";
    const res = await fetch(`${base}/api/session/${sessionData.session_id}/download`, {
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
      const alertMsg = `🚀 Starting download for ${handles.length} selected files in Telegram!`;
      if (tg?.showAlert) {
        tg.showAlert(alertMsg, () => {
          tg.close();
        });
      } else {
        alert(alertMsg);
        window.close();
      }
    } else {
      haptic("warning");
      alert(`⚠️ Notice: ${json.error || 'Failed to submit selection'}`);
    }
  } catch (err) {
    haptic("warning");
    alert(`❌ Network error submitting selection: ${err.message}`);
  } finally {
    if (tg?.MainButton) tg.MainButton.hideProgress();
  }
}

function escapeHtml(str) {
  return (str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Startup on DOM Ready
window.addEventListener("DOMContentLoaded", loadData);
