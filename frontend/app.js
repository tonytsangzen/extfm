/* Ext 文件系统浏览器 — 前端逻辑 */
"use strict";

/* ------------------------------------------------------------------ */
/* 工具                                                                */
/* ------------------------------------------------------------------ */
const $ = (sel) => document.querySelector(sel);

const PREVIEW_MAX = 256 * 1024;   // 文本/嗅探预览上限
const HEX_BYTES = 4096;           // 十六进制预览字节数

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let v = n;
  for (const u of units) {
    v /= 1024;
    if (v < 1024) return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + " " + u;
  }
  return v.toFixed(1) + " EB";
}
function fmtTime(sec) {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  if (isNaN(d)) return "—";
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}
function esc(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function baseName(p) {
  const parts = p.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || "/";
}
function parentOf(p) {
  if (p === "/" || p === "") return "/";
  const trimmed = p.replace(/\/+$/, "");
  const idx = trimmed.lastIndexOf("/");
  return idx <= 0 ? "/" : trimmed.slice(0, idx);
}
function joinPath(dir, name) {
  return (dir === "/" ? "" : dir.replace(/\/+$/, "")) + "/" + name;
}

const TYPE_ZH = {
  dir: "文件夹", file: "文件", symlink: "符号链接",
  chardev: "字符设备", blockdev: "块设备",
  fifo: "FIFO 管道", socket: "套接字", unknown: "未知",
};

const TEXT_EXTS = new Set(["txt","md","log","conf","cfg","ini","json","xml","yml","yaml",
  "csv","tsv","sh","bash","py","rb","pl","js","ts","c","h","cpp","hpp","cc","java","go",
  "rs","sql","html","css","toml","properties","service","list","sources","resolv"]);
const IMG_EXTS = new Set(["png","jpg","jpeg","gif","webp","bmp","svg","ico","tiff"]);
const ARC_EXTS = new Set(["zip","gz","xz","bz2","7z","tar","tgz","zst","rar","deb","rpm"]);
const AUD_EXTS = new Set(["mp3","flac","wav","ogg","m4a","aac"]);
const VID_EXTS = new Set(["mp4","mov","mkv","webm","avi","m4v"]);

function iconSymbol(e) {
  if (e.type === "dir") return "i-folder";
  if (e.type === "symlink") return "i-link";
  if (e.type === "chardev" || e.type === "blockdev") return "i-hdd";
  const ext = (e.name.includes(".") ? e.name.split(".").pop() : "").toLowerCase();
  if (IMG_EXTS.has(ext)) return "i-image";
  if (AUD_EXTS.has(ext)) return "i-audio";
  if (VID_EXTS.has(ext)) return "i-video";
  if (ARC_EXTS.has(ext)) return "i-archive";
  if (TEXT_EXTS.has(ext)) return "i-text";
  if (["c","h","cpp","hpp","cc","py","js","ts","java","go","rs","sh","json","xml","yml","sql","html","css"].includes(ext))
    return "i-code";
  if (e.type === "fifo" || e.type === "socket") return "i-binary";
  return "i-file";
}

/* ------------------------------------------------------------------ */
/* API 客户端                                                          */
/* ------------------------------------------------------------------ */
async function api(route, opts) {
  const res = await fetch(route, opts);
  let data = null;
  try { data = await res.json(); } catch { /* ignore */ }
  if (!res.ok || (data && data.ok === false)) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.payload = data;
    throw err;
  }
  return data;
}
const apiGet = (route) => api(route);
const apiPost = (route, body) => api(route, {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body || {}),
});

/* ------------------------------------------------------------------ */
/* 全局状态                                                            */
/* ------------------------------------------------------------------ */
const S = {
  fs: null,
  version: "",
  path: "/",
  entries: [],
  rootEntries: [],
  sel: null,               // 选中的 entry 引用（单选 + cmd 多选用 Set）
  selSet: new Set(),
  sort: {key: "name", dir: 1},
  view: localStorage.getItem("e2fs.view") || "list",
  hist: [],
  hi: -1,
  sessionDirs: [],         // 本次会话访问过的目录
  loading: false,
  clip: {op: null, paths: []},   // 剪贴板：'copy' | 'cut'
  drag: null,                    // 正在拖动的路径
  pending: {count: 0, bytes: 0, limit: 0},
  deviceGone: false,
  curMounted: false,
  curMountPoint: "",
};

const writable = () => !!(S.fs && S.fs.writable);

if (location.hash.includes("native")) document.body.classList.add("native");

/* ---- 检查 / 格式化 / 保存 ---- */
async function runCheck() {
  const repair = document.querySelector('input[name="ck-mode"]:checked').value === "repair";
  const out = $("#ck-out"), status = $("#ck-status");
  $("#ck-run").disabled = true;
  status.textContent = "执行中…";
  try {
    const d = await apiPost("/api/check", {repair});
    pollJob(d.job.id, {out: $("#ck-out"), status}, (j) => {
      $("#ck-run").disabled = false;
      $("#ck-run").textContent = "重新检查";
      if (j.returncode === 0) toast("检查完成：文件系统干净");
      else if (j.returncode === 1 || j.returncode === 2) toast("发现并已修复问题" + (repair ? "" : "（建议运行修复模式）"), true);
      else toast("检查发现问题（退出码 " + j.returncode + "）", true);
    });
  } catch (e) {
    $("#ck-run").disabled = false;
    status.textContent = "";
    toast(e.message, true);
  }
}

const FORMAT_FEATURES = [
  ["has_journal", "日志（ext3/4）"], ["extent", "extents"], ["64bit", "64 位"],
  ["dir_index", "目录哈希索引"], ["resize_inode", "在线扩容保留"],
  ["filetype", "目录项文件类型"], ["sparse_super", "稀疏超级块"],
  ["large_file", "大文件"], ["huge_file", "huge_file"],
  ["metadata_csum", "元数据校验和"], ["metadata_csum_seed", "校验和种子"],
];
function buildFeatureGrid(fstype) {
  const defaults = {ext2: ["filetype", "sparse_super", "large_file", "dir_index", "resize_inode"],
                    ext3: ["filetype", "sparse_super", "large_file", "has_journal", "dir_index", "resize_inode"],
                    ext4: ["filetype", "sparse_super", "large_file", "has_journal", "dir_index",
                           "resize_inode", "extent", "64bit", "metadata_csum"]};
  $("#ft-features").innerHTML = FORMAT_FEATURES.map(([f, label]) => {
    const on = (defaults[fstype] || []).includes(f);
    return `<label class="feat"><input type="checkbox" value="${f}" ${on ? "checked" : ""}>${label}</label>`;
  }).join("");
}

async function runFormat() {
  const target = $("#ft-target").value.trim();
  if (!target) return toast("请填写目标路径", true);
  const isNew = $("#ft-newfile").checked;
  const ok = await confirmDialog(
    "确认格式化",
    (isNew ? `将创建并格式化新镜像 ${target}（${$("#ft-size").value} MB）`
           : `将清空 ${target} 上的全部数据`)
    + `，格式化为 ${$("#ft-type").value}。此操作不可撤销！`,
    "格式化");
  if (!ok) return;
  const features = [...$("#ft-features").querySelectorAll("input:checked")].map((i) => i.value);
  const body = {
    target,
    new_file: isNew,
    size_mb: parseInt($("#ft-size").value, 10) || 64,
    type: $("#ft-type").value,
    block_size: parseInt($("#ft-bs").value, 10),
    label: $("#ft-label").value.trim(),
    uuid: $("#ft-uuid").value.trim(),
    inode_size: $("#ft-isize").value ? parseInt($("#ft-isize").value, 10) : null,
    reserved_pct: $("#ft-reserved").value === "" ? null : parseInt($("#ft-reserved").value, 10),
    features,
    force: true,
  };
  const out = $("#ft-out"), status = $("#ft-status");
  $("#ft-run").disabled = true;
  status.textContent = "执行中…";
  try {
    const d = await apiPost("/api/format", body);
    pollJob(d.job.id, {out, status}, (j) => {
      $("#ft-run").disabled = false;
      if (j.status === "done") {
        toast("格式化完成");
        updateInfo();
        navigate("/", {push: false});
      } else toast("格式化失败，详见输出", true);
    });
  } catch (e) {
    $("#ft-run").disabled = false;
    status.textContent = "";
    toast(e.message, true);
  }
}

function showActionMenu(anchor) {
  const menu = $("#dropdown");
  const canWrite = writable();
  const items = [];
  items.push({icon: "i-up", label: "上级目录", key: "⌘↑", fn: goUp});
  items.push({icon: "i-edit", label: "前往文件夹…", key: "⌘⇧G", fn: beginEditPath});
  items.push({icon: "i-refresh", label: "刷新", fn: () => navigate(S.path, {push: false})});
  items.push({sep: true});
  items.push({icon: "i-shield", label: "文件系统检查…", fn: () => {
    $("#ck-target").textContent = S.fs.device;
    openDlg("dlg-check");
  }});
  items.push({icon: "i-disc", label: "格式化…", fn: () => {
    $("#ft-target").value = S.fs.device;
    $("#ft-out").hidden = true;
    buildFeatureGrid($("#ft-type").value);
    openDlg("dlg-format");
  }});
  items.push({icon: "i-save", label: "保存到其他磁盘 / 文件…", fn: () => {
    $("#sv-source").textContent = `${S.fs.label || S.fs.device} · ${fmtBytes(S.fs.capacity)}`
      + (S.pending.count ? ` · 缓冲 ${S.pending.count} 项改动` : "");
    $("#sv-out").hidden = true;
    $("#sv-progress").hidden = true;
    openDlg("dlg-save");
  }});
  items.push({icon: "i-info", label: "设备信息…", fn: showDeviceInfo});
  items.push({sep: true});
  if (S.pending.count)
    items.push({icon: "i-download", label: `写回磁盘 (${S.pending.count})`, fn: async () => {
      try {
        const d = await apiPost("/api/flush");
        toast(`已写回 ${d.flushed} 项改动到磁盘`);
        await refreshAfterWrite([]);
      } catch (e) {
        toast(`写回失败：${e.message}`, true);
      }
    }});
  items.push({icon: "i-swap", label: "卸载", fn: ejectSession});

  menu.innerHTML = items.map((it, i) => it.sep
    ? `<div class="sep"></div>`
    : `<div class="item" data-i="${i}"><svg><use href="#${it.icon}"/></svg>${it.label}`
      + (it.key ? `<span class="hint-key">${it.key}</span>` : "") + `</div>`).join("");
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  menu.style.top = r.bottom + 6 + "px";
  menu.style.left = Math.max(8, r.right - menu.offsetWidth) + "px";
  menu.querySelectorAll(".item").forEach((el) =>
    el.addEventListener("click", () => { hideDropdown(); items[+el.dataset.i].fn(); }));
}

function hideDropdown() { $("#dropdown").hidden = true; }

async function ejectSession() {
  if (S.pending.count > 0) {
    const ok = await confirmDialog(
      "卸载并写回",
      `有 ${S.pending.count} 项改动缓冲在内存中（${fmtBytes(S.pending.bytes)}）。\n` +
      `卸载前会把它们写回磁盘。是否继续？`, "写回并卸载");
    if (!ok) return;
  }
  try {
    const d = await apiPost("/api/close");
    if (d.flushed) toast(`已写回 ${d.flushed} 项改动到磁盘`);
    else if (d.ok === false) return toast(d.error || "卸载失败", true);
  } catch (e) {
    return toast(`卸载失败：${e.message}`, true);
  }
  if (S.curMounted) {
    try {
      await apiPost("/api/unmount", {device: S.fs.device});
      toast(`已卸载 ${baseName(S.fs.device)}`);
    } catch { /* 卷可能已被外部卸载 */ }
    S.curMounted = false;
  }
  S.fs = null;
  S.pending = {count: 0, bytes: 0, limit: 0};
  renderPending();
  showLaunch();
}

async function runSave() {
  const target = $("#sv-target").value.trim();
  if (!target) return toast("请填写目标路径", true);
  const mode = document.querySelector('input[name="sv-mode"]:checked').value;
  const force = $("#sv-force").checked;
  const out = $("#sv-out"), status = $("#sv-status"), prog = $("#sv-progress");
  $("#sv-run").disabled = true;
  status.textContent = "执行中…";
  try {
    const d = await apiPost("/api/save", {target, mode, force});
    pollJob(d.job.id, {out, status, progress: prog}, (j) => {
      $("#sv-run").disabled = false;
      if (j.status === "done") toast("保存完成");
      else toast("保存失败，详见输出", true);
    });
  } catch (e) {
    $("#sv-run").disabled = false;
    status.textContent = "";
    toast(e.message, true);
  }
}

/* ------------------------------------------------------------------ */
/* 写操作：剪贴板 / 重命名 / 新建文件夹 / 系统打开                      */
/* ------------------------------------------------------------------ */
function clipSet(op) {
  if (!S.selSet.size) return;
  if (!writable()) return toast("当前文件系统不可写", true);
  S.clip = {op, paths: [...S.selSet].map((n) => joinPath(S.path, n))};
  toast(`已${op === "copy" ? "复制" : "剪切"} ${S.clip.paths.length} 项`);
  renderEntries();
}

async function doPaste(targetDir) {
  targetDir = targetDir || S.path;
  if (!S.clip.paths.length) return;
  if (!writable()) return toast("当前文件系统不可写", true);
  const {op, paths} = S.clip;
  try {
    const d = await apiPost(op === "copy" ? "/api/copy" : "/api/move",
                            {paths, target: targetDir});
    const n = d.results.filter((r) => !r.skipped).length;
    if (op === "cut") S.clip = {op: null, paths: []};
    toast(`已${op === "copy" ? "复制" : "移动"} ${n} 项到 ${targetDir}`);
    applyWriteResp(d);
    await refreshAfterWrite([targetDir, ...paths.map(parentOf)]);
  } catch (e) {
    toast(e.message, true);
  }
}

function startRename(entry) {
  if (!entry) return;
  if (!writable()) return toast("当前文件系统不可写", true);
  const key = CSS.escape(entry.name);
  const host = document.querySelector(`#rows tr[data-name="${key}"] .nm`)
    || document.querySelector(`#gridview .tile[data-name="${key}"] .nm`);
  if (!host) return;
  const old = entry.name;
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = old;
  input.spellcheck = false;
  host.replaceChildren(input);
  input.focus();
  const dot = old.lastIndexOf(".");
  input.setSelectionRange(0, dot > 0 ? dot : old.length);
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const nv = input.value.trim();
    if (!commit || !nv || nv === old) {
      renderEntries();
      return;
    }
    if (nv.includes("/")) {
      toast("名称不能包含 /", true);
      renderEntries();
      return;
    }
    try {
      const d = await apiPost("/api/rename", {path: joinPath(S.path, old), name: nv});
      toast(`已重命名为 ${nv}`);
      applyWriteResp(d);
      await refreshAfterWrite([S.path]);
    } catch (e) {
      toast(e.message, true);
      renderEntries();
    }
  };
  input.addEventListener("keydown", (ev) => {
    ev.stopPropagation();
    if (ev.key === "Enter") finish(true);
    else if (ev.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(false));
}

async function newFolder() {
  if (!writable()) return toast("当前文件系统不可写", true);
  const names = new Set(S.entries.map((e) => e.name));
  let nm = "新建文件夹";
  let i = 2;
  while (names.has(nm)) nm = `新建文件夹 ${i++}`;
  try {
    const d = await apiPost("/api/mkdir", {path: joinPath(S.path, nm)});
    await refreshAfterWrite([S.path]);
    applyWriteResp(d);
    startRename({name: nm});
  } catch (e) {
    toast(e.message, true);
  }
}

async function sysOpen(path) {
  try {
    const d = await apiGet(`/api/open-with?path=${encodeURIComponent(path)}`);
    if (d.ok) toast(`已交给系统打开 ${baseName(path)}`);
    else toast(d.error || "打开失败", true);
  } catch (e) {
    toast(e.message, true);
  }
}

async function updateInfo() {
  try {
    const d = await apiGet("/api/info");
    if (d.open) {
      S.fs = d.fs;
      if (d.pending) S.pending = d.pending;
      renderDeviceCard();
      renderStatus();
      renderPending();
    }
  } catch { /* ignore */ }
}

function renderPending() {
  renderStatus();     // 缓冲计数在状态栏与齿轮菜单中展示
}

function applyWriteResp(d) {
  if (d && d.pending) {
    S.pending = d.pending;
    renderPending();
  }
  if (d && d.flushed) {
    toast("内存缓冲达到上限，已自动写回磁盘");
    updateInfo();
  }
}

async function refreshAfterWrite(affectedDirs = []) {
  const cur = S.path;
  const touched = affectedDirs.some((d) =>
    d === cur || cur.startsWith(d === "/" ? "/" : d.replace(/\/+$/, "") + "/"));
  if (touched || !affectedDirs.length) await navigate(cur, {push: false});
  await updateInfo();
}

/* 拖放 */
function clearDropHints() {
  document.querySelectorAll(".drop-hover").forEach((el) =>
    el.classList.remove("drop-hover"));
}

async function dropTransfer(targetDir, copy) {
  const paths = S.drag ? S.drag.paths : [];
  S.drag = null;
  clearDropHints();
  if (!paths.length) return;
  for (const p of paths) {
    const base = p.replace(/\/+$/, "");
    if (targetDir === base || targetDir.startsWith(base + "/")) {
      return toast("不能移动到其自身或子目录内", true);
    }
  }
  try {
    const d = await apiPost(copy ? "/api/copy" : "/api/move",
                            {paths, target: targetDir});
    const n = d.results.filter((r) => !r.skipped).length;
    toast(`${copy ? "已复制" : "已移动"} ${n} 项到 ${targetDir}`);
    applyWriteResp(d);
    await refreshAfterWrite([targetDir, ...paths.map(parentOf)]);
  } catch (e) {
    toast(e.message, true);
  }
}

function enableDropTarget(el, getDir) {
  el.addEventListener("dragover", (ev) => {
    if (!S.drag || !writable()) return;
    ev.preventDefault();
    ev.stopPropagation();
    ev.dataTransfer.dropEffect = ev.altKey ? "copy" : "move";
    el.classList.add("drop-hover");
  });
  el.addEventListener("dragleave", () => el.classList.remove("drop-hover"));
  el.addEventListener("drop", (ev) => {
    if (!S.drag || !writable()) return;
    ev.preventDefault();
    ev.stopPropagation();
    dropTransfer(getDir(), ev.altKey);
  });
}

/* ------------------------------------------------------------------ */
/* 提示                                                                */
/* ------------------------------------------------------------------ */
function toast(msg, isErr) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.innerHTML = `<svg><use href="#${isErr ? "i-warn" : "i-info"}"/></svg><span>${esc(msg)}</span>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3600);
  setTimeout(() => el.remove(), 4000);
}

/* ------------------------------------------------------------------ */
/* 启动页                                                              */
/* ------------------------------------------------------------------ */
/* ---- 维护任务对话框（检查/格式化/保存） ---- */
const DIALOGS = ["dlg-check", "dlg-format", "dlg-save"];
function openDlg(id) {
  $("#modal-mask").hidden = false;
  DIALOGS.forEach((d) => { $("#" + d).hidden = d !== id; });
  $("#preview").hidden = true;
  $("#props").hidden = true;
  $("#confirm").hidden = true;
}
function closeDlg(id) { $("#" + id).hidden = true; maybeHideMask(); }
function maybeHideMask() {
  const anyOpen = DIALOGS.some((d) => !$("#" + d).hidden)
    || !$("#preview").hidden || !$("#props").hidden || !$("#confirm").hidden;
  if (!anyOpen) $("#modal-mask").hidden = true;
}
function pollJob(jid, {out, status, progress}, onDone) {
  const timer = setInterval(async () => {
    try {
      const d = await apiGet(`/api/job?id=${encodeURIComponent(jid)}`);
      const j = d.job;
      if (out) {
        out.hidden = !j.output;
        out.textContent = j.output || "";
        out.scrollTop = out.scrollHeight;
      }
      if (progress) {
        const p = j.progress;
        if (p && p.total) {
          progress.hidden = false;
          progress.firstElementChild.style.width =
            Math.min(100, p.done * 100 / p.total).toFixed(1) + "%";
        }
      }
      if (status) status.textContent = j.status === "running"
        ? "执行中…" : (j.status === "done" ? `完成（退出码 ${j.returncode}）`
                                          : `失败（退出码 ${j.returncode}）`);
      if (j.status !== "running") {
        clearInterval(timer);
        updateInfo();
        if (onDone) onDone(j);
      }
    } catch (e) {
      clearInterval(timer);
      if (status) status.textContent = `轮询失败: ${e.message}`;
    }
  }, 600);
  return timer;
}

let cfResolve = null;

function confirmDialog(title, body, okLabel = "确定") {
  $("#modal-mask").hidden = false;
  $("#preview").hidden = true;
  $("#props").hidden = true;
  const cf = $("#confirm");
  cf.hidden = false;
  $("#cf-title").textContent = title;
  $("#cf-body").textContent = body;
  $("#cf-ok").textContent = okLabel;
  return new Promise((resolve) => {
    cfResolve = resolve;
  });
}

function resolveConfirm(v) {
  if (cfResolve) {
    const r = cfResolve;
    cfResolve = null;
    $("#confirm").hidden = true;
    $("#modal-mask").hidden = true;
    r(v);
  }
}

function showLaunch() {
  $("#browser").hidden = true;
  $("#launch").hidden = false;
  renderRecents();
  $("#open-path").focus();
  doScan(true);   // 自动探测可移动设备与镜像
}

function showBrowser() {
  $("#launch").hidden = true;
  $("#browser").hidden = false;
}

function getRecents() {
  try { return JSON.parse(localStorage.getItem("e2fs.recent") || "[]"); }
  catch { return []; }
}
function pushRecent(path) {
  /* 稳定排序：新文件插入最前；重复打开不改变已有顺序 */
  const list = getRecents();
  if (!list.includes(path)) list.unshift(path);
  localStorage.setItem("e2fs.recent", JSON.stringify(list.slice(0, 8)));
}
function renderRecents() {
  const list = getRecents();
  $("#recents-wrap").hidden = list.length === 0;
  $("#recents").innerHTML = list.map((p) =>
    `<button class="chip" data-path="${esc(p)}" title="${esc(p)}">
       <svg width="13" height="13"><use href="#i-hdd"/></svg>
       <span class="nm">${esc(baseName(p))}</span>
     </button>`).join("");
  $("#recents").querySelectorAll(".chip").forEach((el) =>
    el.addEventListener("click", () => openDevice(el.dataset.path)));
}

async function openDevice(path, opts = {}) {
  if (!path) return;
  const btn = $("#btn-open");
  const ro = $("#open-ro")?.checked || path.startsWith("/dev/");
  try {
    const data = await apiPost("/api/open",
      {path, readOnly: ro, unmountFirst: !!opts.unmountFirst});
    S.fs = data.fs;
    S.version = data.version || "";
    S.hist = [];
    S.hi = -1;
    S.sessionDirs = [];
    S.clip = {op: null, paths: []};
    S.pending = {count: 0, bytes: 0, limit: 0};
    S.deviceGone = false;
    $("#gone-banner").hidden = true;
    renderPending();
    pushRecent(path);
    renderFav();
    showBrowser();
    renderDeviceCard();
    await navigate("/", {reset: true});
    await updateInfo();
    refreshSidebarMounts();
    if (!S.fs.writable)
      toast(ro ? "已按只读方式打开（块设备始终只读访问）"
               : "文件系统不可写，已只读打开");
  } catch (e) {
    const p = e.payload || {};
    if (p.need_permission && !opts.retryAfterElevate) {
      const ok = await confirmDialog(
        "需要授权读取块设备",
        `访问 ${path} 需要管理员授权。\n点击“授权”后将弹出系统密码框，` +
        `仅授予该设备的只读权限（不修改任何数据）。是否继续？`,
        "授权并打开");
      if (ok) {
        try {
          await elevateDevices([path]);
          return openDevice(path, {retryAfterElevate: true});
        } catch (e2) {
          toast(`授权失败：${e2.message}`, true);
        }
      }
    } else if (p.busy) {
      // 已被系统挂载：ext 分区 → 卸载后浏览；macOS 卷 → Finder 显示
      const isLinux = (p.content || "").toLowerCase().includes("linux");
      if (isLinux || !p.mount_point) {
        const ok = await confirmDialog(
          "设备已被系统挂载",
          `要浏览该 ext 分区，需要先卸载系统挂载${p.mount_point ? "（当前挂载点 " + p.mount_point + "）" : ""}。\n是否卸载并打开？`,
          "卸载并打开");
        if (ok) {
          try {
            return openDevice(path, {unmountFirst: true});
          } catch (e2) {
            toast(`打开失败：${e2.message}`, true);
          }
        }
      } else {
        toast("该卷是 macOS 文件系统（非 ext），已在 Finder 中显示");
        revealPath(p.mount_point || path);
      }
    } else {
      toast(`打开失败：${e.message}`, true);
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "打开";
    }
  }
}

function renderDeviceCard() {
  const fs = S.fs;
  const used = fs.capacity - fs.free_bytes;
  const pct = fs.capacity ? Math.max(0, Math.min(100, used * 100 / fs.capacity)) : 0;
  const name = fs.label || baseName(fs.device);
  $("#dev-name").textContent = name;
  const badge = $("#dev-badge");
  badge.textContent = fs.writable ? fs.fstype : "只读";
  badge.className = "badge" + (fs.writable ? "" : " warn");
  $("#dev-cap").className = pct > 90 ? "warn" : "";
  $("#dev-cap").style.width = pct.toFixed(1) + "%";
  const ej = $("#dev-eject");
  if (ej && !ej.dataset.bound) {
    ej.dataset.bound = "1";
    ej.addEventListener("click", (ev) => {
      ev.stopPropagation();
      ejectSession();
    });
  }
  const row = $("#side-device");
  if (row && !row.dataset.bound) {
    row.dataset.bound = "1";
    row.addEventListener("click", () => S.fs && navigate("/"));
  }
}

function mountRow(r) {
  const nm = r.volume_name || baseName(r.path);
  const sub = r.mounted ? (r.mount_point || "已挂载")
    : (r.denied ? "需要授权" : (r.ok ? "未挂载 · 可用本工具浏览" : (r.content || "未挂载")));
  const tri = r.denied ? ""
    : `<button class="tri-btn" data-mounted="${r.mounted ? 1 : 0}"
        title="${r.mounted ? "卸载" : "挂载"}">
         <svg class="${r.mounted ? "" : "flip"}"><use href="#i-eject"/></svg>
       </button>`;
  return `<div class="mount-row ${r.denied ? "dim" : ""}" data-path="${esc(r.path)}"
            title="${esc(r.path)}">
    <svg class="dev-ic"><use href="#i-hdd"/></svg>
    <div class="dev-info">
      <div class="dev-name">${esc(nm)}</div>
      <div class="dev-sub">${esc(sub)}</div>
    </div>
    ${tri}
  </div>`;
}

async function mountDevice(path, mounted) {
  try {
    const d = mounted
      ? await apiPost("/api/unmount", {device: path})
      : await apiPost("/api/mount", {device: path});
    if (d.ok) {
      toast(mounted ? `已卸载 ${baseName(path)}` : `已挂载 ${baseName(path)}${d.mount_point ? " → " + d.mount_point : ""}`);
    } else {
      toast(d.output || "操作失败", true);
    }
    refreshSidebarMounts();
    renderFav();
  } catch (e) {
    toast(e.message, true);
    refreshSidebarMounts();
  }
}

/* 设备信息（Finder“显示简介”风格） */
function showDeviceInfo() {
  const fs = S.fs;
  const used = fs.capacity - fs.free_bytes;
  const rows = [
    ["名称", fs.label || "（未命名）"],
    ["类型", fs.fstype.toUpperCase()],
    ["设备", fs.device],
    ["总容量", fmtBytes(fs.capacity)],
    ["已用", `${fmtBytes(used)}（${pctOf(fs)}%）`],
    ["可用", fmtBytes(fs.free_bytes)],
    ["块大小", `${fs.block_size} B`],
    ["inode", `${(fs.inodes - fs.free_inodes).toLocaleString()} / ${fs.inodes.toLocaleString()}`],
    ["UUID", fs.uuid],
    ["状态", fs.state === "clean" ? "干净" : "有错误（未正常卸载）"],
    ["访问", fs.writable ? "可读写" : "只读"],
  ];
  $("#modal-mask").hidden = false;
  $("#preview").hidden = true;
  $("#confirm").hidden = true;
  $("#props").hidden = false;
  $("#pr-path").textContent = "设备信息";
  $("#pr-body").innerHTML =
    `<div class="kv">` + rows.map(([k, v]) => `<div>${k}</div><div>${esc(v)}</div>`).join("") + `</div>`;
  function pctOf(f) { return f.capacity ? Math.max(0, Math.min(100, Math.round((f.capacity - f.free_bytes) * 100 / f.capacity))) : 0; }
}

function renderFav() {
  /* 收藏：最新挂载（打开）过的文件，最新在前 */
  const list = getRecents();
  $("#side-fav").innerHTML = list.length ? list.map((p) => {
    const cur = S.fs && S.fs.device === p;
    return `<div class="place fav ${cur ? "active" : ""}" data-path="${esc(p)}" title="${esc(p)}">
      <svg><use href="#i-disc"/></svg>
      <span class="fav-name">${esc(baseName(p))}</span>
      ${cur ? `<span class="badge ok-mnt">已打开</span>` : ""}
    </div>`;
  }).join("")
    : `<div class="place" style="pointer-events:none"><span class="muted">暂无挂载记录</span></div>`;
  $("#side-fav").querySelectorAll(".place").forEach((el) =>
    el.addEventListener("click", () => openDevice(el.dataset.path)));
}

/* 位置：当前挂载的文件/设备 + 可挂载的设备 */
async function refreshSidebarMounts() {
  try {
    const d = await apiGet("/api/scan");
    const cur = S.fs ? S.fs.device : null;
    const devs = (d.devices || []).filter((r) => r.path !== cur);
    const mounted = devs.filter((r) => r.mounted);
    // 可挂载分组只保留 ext2/3/4 设备（ok=探测为 ext；denied=需授权后才能确认）
    const mountable = devs.filter((r) => !r.mounted && (r.ok || r.denied));
    let html = "";
    if (mounted.length)
      html += `<div class="side-sub">已挂载</div>` + mounted.map(mountRow).join("");
    if (mountable.length)
      html += `<div class="side-sub">可挂载</div>` + mountable.map(mountRow).join("");
    $("#side-mounts").innerHTML = html
      || `<div class="place" style="pointer-events:none"><span class="muted">未发现设备</span></div>`;
    $("#side-mounts").querySelectorAll(".mount-row").forEach((el) => {
      const r = devs.find((x) => x.path === el.dataset.path);
      el.addEventListener("click", () => openDevice(r.path));
      const tri = el.querySelector(".tri-btn");
      if (tri && !r.denied) {
        tri.addEventListener("click", (ev) => {
          ev.stopPropagation();
          mountDevice(r.path, tri.dataset.mounted === "1");
        });
      }
    });
  } catch { /* 扫描失败时静默 */ }
}

/* ------------------------------------------------------------------ */
/* 导航                                                                */
/* ------------------------------------------------------------------ */
async function navigate(path, opts = {}) {
  if (S.loading) return;
  S.loading = true;
  path = normalizePath(path);
  try {
    const data = await apiGet(`/api/list?path=${encodeURIComponent(path)}`);
    S.path = path;
    S.entries = data.entries;
    S.selSet.clear();
    if (path === "/") S.rootEntries = data.entries;
    if (!S.sessionDirs.includes(path)) {
      S.sessionDirs.push(path);
      if (S.sessionDirs.length > 40) S.sessionDirs.shift();
    }
    if (opts.reset) { S.hist = [path]; S.hi = 0; }
    else if (opts.push !== false) {
      S.hist = S.hist.slice(0, S.hi + 1);
      if (S.hist[S.hi] !== path) { S.hist.push(path); S.hi = S.hist.length - 1; }
    } else if (S.hist[S.hi] !== path) {
      if (S.hist[S.hi] === undefined) S.hist.push(path);
      else S.hist[S.hi] = path;
    }
    $("#search").value = "";
    renderAll();
  } catch (e) {
    toast(`无法打开目录 ${path}：${e.message}`, true);
  } finally {
    S.loading = false;
  }
}

function normalizePath(p) {
  if (!p) return "/";
  p = p.replace(/\/+$/, "");
  if (p === "") p = "/";
  return p.startsWith("/") ? p : "/" + p;
}

function renderAll() {
  renderBreadcrumb();
  renderEntries();
  renderStatus();
  updateNavButtons();
  // Finder 式工具栏标题
  const name = S.path === "/" ? (S.fs ? (S.fs.label || baseName(S.fs.device)) : "/")
                              : baseName(S.path);
  $("#tb-name").textContent = name;
  document.title = `${name} — Ext 文件系统浏览器`;
}

function updateNavButtons() {
  $("#nav-back").disabled = S.hi <= 0;
  $("#nav-fwd").disabled = S.hi >= S.hist.length - 1;
  $("#nav-up").disabled = S.path === "/";
}

function goBack() { if (S.hi > 0) { S.hi--; navigate(S.hist[S.hi], {push: false}); } }
function goFwd() { if (S.hi < S.hist.length - 1) { S.hi++; navigate(S.hist[S.hi], {push: false}); } }
function goUp() { if (S.path !== "/") navigate(parentOf(S.path)); }

function renderBreadcrumb() {
  const bc = $("#breadcrumb");
  const segs = S.path === "/" ? [] : S.path.split("/").filter(Boolean);
  const rootName = S.fs ? (S.fs.label || baseName(S.fs.device)) : "/";
  let html = `<span class="crumb ${segs.length ? "" : "last"}" data-path="/"><svg><use href="#${S.fs ? "i-disc" : "i-home"}"/></svg>${esc(rootName)}</span>`;
  let acc = "";
  segs.forEach((seg, i) => {
    acc += "/" + seg;
    html += `<svg class="crumb-sep"><use href="#i-chev"/></svg>
             <span class="crumb ${i === segs.length - 1 ? "last" : ""}" data-path="${esc(acc)}">${esc(seg)}</span>`;
  });
  bc.innerHTML = html;
  bc.querySelectorAll(".crumb").forEach((el) => {
    el.addEventListener("click", () => navigate(el.dataset.path));
    if (writable()) enableDropTarget(el, () => el.dataset.path);
  });
  bc.scrollLeft = bc.scrollWidth;
}

function beginEditPath() {
  const bc = $("#breadcrumb");
  bc.innerHTML = "";
  const input = document.createElement("input");
  input.value = S.path;
  input.spellcheck = false;
  input.style.cssText = "flex:1;border:none;outline:none;background:none";
  bc.appendChild(input);
  input.focus();
  input.select();
  const done = (commit) => {
    if (commit) navigate(input.value);
    else renderBreadcrumb();
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") done(true);
    else if (ev.key === "Escape") done(false);
    ev.stopPropagation();
  });
  input.addEventListener("blur", () => done(false));
}

/* ------------------------------------------------------------------ */
/* 条目渲染（列表 / 网格）                                             */
/* ------------------------------------------------------------------ */
function visibleEntries() {
  const kw = $("#search").value.trim().toLowerCase();
  let list = S.entries;
  if (kw) list = list.filter((e) => e.name.toLowerCase().includes(kw));
  const {key, dir} = S.sort;
  const typeRank = (e) => (e.type === "dir" ? 0 : 1);
  return list.slice().sort((a, b) => {
    if (typeRank(a) !== typeRank(b)) return typeRank(a) - typeRank(b);
    let r = 0;
    if (key === "size" || key === "mtime") r = (a[key] || 0) - (b[key] || 0);
    else if (key === "type") r = TYPE_ZH[a.type].localeCompare(TYPE_ZH[b.type], "zh");
    else if (key === "perms") r = a.perms.localeCompare(b.perms);
    else r = a.name.localeCompare(b.name, "zh-CN");
    return r * dir;
  });
}

function renderEntries() {
  const list = visibleEntries();
  const emptyEl = $("#empty");
  const hasAny = S.entries.length > 0;
  const filtered = $("#search").value.trim() !== "" && hasAny && list.length === 0;

  $("#list-wrap").hidden = S.view !== "list";
  $("#gridview").hidden = S.view !== "grid";

  if (!hasAny || filtered) {
    // 空目录 / 无搜索结果：必须清空旧列表并隐藏表格，
    // 否则残留上一个目录的内容，造成路径与显示不一致
    $("#rows").innerHTML = "";
    $("#gridview").innerHTML = "";
    $("#list-wrap").hidden = true;
    $("#gridview").hidden = true;
    emptyEl.hidden = false;
    emptyEl.innerHTML = !hasAny
      ? `<svg><use href="#i-folder"/></svg><div>此文件夹为空</div>`
      : `<svg><use href="#i-search"/></svg><div>没有匹配的项目</div>`;
    renderStatus();
    return;
  }
  emptyEl.hidden = true;

  // 表头排序指示
  document.querySelectorAll("#listview thead th").forEach((th) => {
    const arrow = th.querySelector(".arrow");
    if (arrow) arrow.remove();
    if (th.dataset.key === S.sort.key) {
      const s = document.createElement("span");
      s.className = "arrow";
      s.textContent = S.sort.dir > 0 ? "▲" : "▼";
      th.appendChild(s);
    }
  });

  if (S.view === "list") {
    $("#rows").innerHTML = list.map((e) => {
      const isSel = S.selSet.has(e.name);
      const isCut = S.clip.op === "cut" &&
        S.clip.paths.includes(joinPath(S.path, e.name));
      const linkTo = e.type === "symlink"
        ? `<span class="lnk">→ ${esc(e.target || "(无法读取目标)")}</span>` : "";
      return `<tr data-name="${esc(e.name)}" draggable="${writable()}"
              class="${isSel ? "selected" : ""} ${isCut ? "cut" : ""}">
        <td><div class="fcell"><svg><use href="#${iconSymbol(e)}"/></svg>
          <div style="min-width:0"><div class="nm">${esc(e.name)}</div>${linkTo}</div></div></td>
        <td class="dim">${e.type === "dir" ? "—" : fmtBytes(e.size)}</td>
        <td class="dim">${TYPE_ZH[e.type] || e.type}</td>
        <td class="dim">${fmtTime(e.mtime)}</td>
        <td>${esc(e.perms)}</td>
      </tr>`;
    }).join("");
    bindItemEvents($("#rows"), "tr");
  } else {
    $("#gridview").innerHTML = list.map((e) => {
      const isSel = S.selSet.has(e.name);
      const isCut = S.clip.op === "cut" &&
        S.clip.paths.includes(joinPath(S.path, e.name));
      return `<div class="tile ${isSel ? "selected" : ""} ${isCut ? "cut" : ""}"
              data-name="${esc(e.name)}" draggable="${writable()}" title="${esc(e.name)}">
        <svg><use href="#${iconSymbol(e)}"/></svg><div class="nm">${esc(e.name)}</div></div>`;
    }).join("");
    bindItemEvents($("#gridview"), ".tile");
  }
}

function entryByName(name) {
  return S.entries.find((e) => e.name === name);
}

function updateSelectionDom() {
  document.querySelectorAll("#rows tr[data-name], #gridview .tile[data-name]")
    .forEach((el) => el.classList.toggle("selected", S.selSet.has(el.dataset.name)));
}

function bindItemEvents(container, selector) {
  container.querySelectorAll(selector).forEach((el) => {
    const name = el.dataset.name;
    el.addEventListener("click", (ev) => {
      if (ev.metaKey || ev.ctrlKey) {
        S.selSet.has(name) ? S.selSet.delete(name) : S.selSet.add(name);
      } else if (S.selSet.has(name)) {
        return;   // 已选中：不动 DOM，保证双击可用
      } else {
        S.selSet.clear();
        S.selSet.add(name);
      }
      updateSelectionDom();
      renderStatus();
    });
    el.addEventListener("dblclick", () => activateEntry(entryByName(name)));
    el.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      if (!S.selSet.has(name)) {
        S.selSet.clear();
        S.selSet.add(name);
        updateSelectionDom();
        renderStatus();
      }
      showCtxMenu(ev.clientX, ev.clientY, entryByName(name));
    });

    // 拖动（拖出 = 携带选中集）
    el.addEventListener("dragstart", (ev) => {
      const names = S.selSet.has(name) ? [...S.selSet] : [name];
      S.drag = {paths: names.map((n) => joinPath(S.path, n))};
      ev.dataTransfer.setData("text/plain", names.join("\n"));
      ev.dataTransfer.effectAllowed = "copyMove";
    });
    el.addEventListener("dragend", () => {
      S.drag = null;
      clearDropHints();
    });

    // 拖入文件夹（普通 = 移动，⌥ = 复制）
    const entry = entryByName(name);
    if (entry && entry.type === "dir" && name !== ".") {
      enableDropTarget(el, () => joinPath(S.path, name));
    }
  });
}

async function activateEntry(e) {
  if (!e) return;
  if (e.name === ".") return navigate(S.path);   // 背景右键“打开”
  const p = joinPath(S.path, e.name);
  if (e.type === "dir") return navigate(p);
  try {
    const st = (await apiGet(`/api/stat?path=${encodeURIComponent(p)}`));
    if (st.resolved && st.resolved.type === "dir") return navigate(p);
    if (st.resolved && !["file", "symlink", "unknown"].includes(st.resolved.type)) {
      return toast(`不支持预览${TYPE_ZH[st.resolved.type] || "该类型"}：${e.name}`, true);
    }
    if (st.is_symlink && !st.resolved) {
      return toast(`符号链接目标不存在：${st.symlink}`, true);
    }
    openPreview(p, st);
  } catch (err) {
    toast(`${e.name}：${err.message}`, true);
  }
}

function renderStatus() {
  const fs = S.fs;
  const kw = $("#search").value.trim();
  const total = S.entries.length;
  const shown = visibleEntries().length;
  let left = `${total} 个项目`;
  if (kw && shown !== total) left = `筛选出 ${shown} / ${total} 项`;
  if (S.selSet.size) left += `，已选中 ${S.selSet.size} 项`;
  if (S.pending.count) left += `，缓冲 ${S.pending.count} 项改动`;
  $("#st-left").textContent = left;
  const used = fs.capacity - fs.free_bytes;
  $("#st-right").textContent =
    `${fs.fstype.toUpperCase()} · 已用 ${fmtBytes(used)} / 共 ${fmtBytes(fs.capacity)}` +
    ` · 块 ${fs.block_size} B${fs.writable ? "" : " · 只读"}${S.version ? " · libext2fs " + S.version : ""}`;
}

/* ------------------------------------------------------------------ */
/* 预览                                                                */
/* ------------------------------------------------------------------ */
function closeModal() {
  resolveConfirm(false);
  $("#modal-mask").hidden = true;
  $("#preview").hidden = true;
  $("#props").hidden = true;
  $("#confirm").hidden = true;
  const body = $("#pv-body");
  body.querySelectorAll("video,audio,img").forEach((el) => {
    if (el.tagName === "VIDEO" || el.tagName === "AUDIO") el.pause();
    if (el.src && el.src.startsWith("blob:")) URL.revokeObjectURL(el.src);
  });
}

function sniff(buf) {
  const u8 = new Uint8Array(buf);
  const ascii = (off, n) => String.fromCharCode(...u8.slice(off, off + n));
  if (u8.length > 8 && u8[0] === 0x89 && ascii(1, 3) === "PNG") return "image";
  if (u8.length > 3 && u8[0] === 0xFF && u8[1] === 0xD8 && u8[2] === 0xFF) return "image";
  if (u8.length > 6 && ascii(0, 3) === "GIF") return "image";
  if (u8.length > 12 && ascii(0, 4) === "RIFF" && ascii(8, 4) === "WEBP") return "image";
  if (u8.length > 2 && u8[0] === 0x42 && u8[1] === 0x4D) return "image";
  if (u8.length > 12 && ascii(4, 4) === "ftyp") return "video";
  if (u8.length > 3 && (ascii(0, 3) === "ID3" || (u8[0] === 0xFF && (u8[1] & 0xE0) === 0xE0))) return "audio";
  if (u8.length > 4 && ascii(0, 4) === "OggS") return "audio";
  if (u8.length > 4 && ascii(0, 4) === "RIFF") return "audio";
  // 文本嗅探：前 8KB 无 NUL，且大部分字节可打印
  const n = Math.min(u8.length, 8192);
  let ctl = 0;
  for (let i = 0; i < n; i++) {
    const b = u8[i];
    if (b === 0) return "hex";
    if (b < 9 || (b > 13 && b < 32)) ctl++;
  }
  if (n > 0 && ctl / n < 0.05) return "text";
  return "hex";
}

async function openPreview(path, st) {
  const res = st.resolved || st.raw;
  const size = res.size;
  $("#modal-mask").hidden = false;
  $("#props").hidden = true;
  $("#preview").hidden = false;
  $("#pv-name").textContent = baseName(path);
  $("#pv-icon").innerHTML = `<use href="#${iconSymbol({...res, name: baseName(path)})}"/>`;
  $("#pv-meta").textContent = `${TYPE_ZH[res.type] || res.type} · ${fmtBytes(size)} · ${esc(path)}`;
  $("#pv-note").textContent = "加载中…";
  $("#pv-download").dataset.path = path;
  const body = $("#pv-body");
  body.innerHTML = `<div class="empty"><span class="loading"></span><div>正在读取…</div></div>`;

  let buf;
  try {
    buf = await (await fetch(
      `/api/file?path=${encodeURIComponent(path)}&max=${PREVIEW_MAX}`)).arrayBuffer();
  } catch (e) {
    body.innerHTML = `<div class="empty"><svg><use href="#i-warn"/></svg><div>读取失败</div></div>`;
    $("#pv-note").textContent = "";
    return;
  }
  const truncated = size > PREVIEW_MAX;
  const kind = sniff(buf);

  if (kind === "image") {
    const url = URL.createObjectURL(new Blob([buf]));
    body.innerHTML = `<div class="pv-image"><img src="${url}" alt=""></div>`;
  } else if (kind === "video") {
    body.innerHTML = `<div class="pv-media"><video controls preload="metadata"
        src="/api/file?path=${encodeURIComponent(path)}"></video></div>`;
  } else if (kind === "audio") {
    body.innerHTML = `<div class="pv-media"><audio controls
        src="/api/file?path=${encodeURIComponent(path)}"></audio></div>`;
  } else if (kind === "text") {
    const text = new TextDecoder("utf-8", {fatal: false}).decode(buf);
    body.innerHTML = `<pre class="pv-text">${esc(text)}</pre>`;
  } else {
    body.innerHTML = `<table class="pv-hex">${hexTable(new Uint8Array(buf.slice(0, HEX_BYTES)))}</table>`;
  }

  let note = "";
  if (kind === "hex") note = `二进制文件，仅显示前 ${HEX_BYTES} 字节的十六进制`;
  else if (kind === "text") note = truncated ? `仅预览前 ${fmtBytes(PREVIEW_MAX)}` : "";
  else if (kind === "image" && truncated) note = "预览不完整，仅前 " + fmtBytes(PREVIEW_MAX);
  else note = "";
  $("#pv-note").textContent = note;
}

function hexTable(u8) {
  const rows = [];
  for (let off = 0; off < u8.length; off += 16) {
    const slice = u8.slice(off, off + 16);
    const hex = Array.from(slice).map((b) => b.toString(16).padStart(2, "0"));
    let line = "";
    while (hex.length < 16) hex.push("  ");
    for (let i = 0; i < 16; i += 2) {
      line += hex[i] + hex[i + 1] + " ";
    }
    const asc = Array.from(slice)
      .map((b) => (b >= 0x20 && b < 0x7F ? String.fromCharCode(b) : ".")).join("");
    rows.push(`<tr><td class="off">${off.toString(16).padStart(8, "0")}</td>
      <td>${line}</td><td class="asc">${esc(asc)}</td></tr>`);
  }
  return rows.join("");
}

/* ------------------------------------------------------------------ */
/* 属性                                                                */
/* ------------------------------------------------------------------ */
async function openProps(path) {
  try {
    const st = await apiGet(`/api/stat?path=${encodeURIComponent(path)}`);
    const r = st.resolved || st.raw;
    const rows = [
      ["种类", (TYPE_ZH[r.type] || r.type) + (st.is_symlink ? "（符号链接）" : "")],
      ["位置", st.path],
      ["大小", r.type === "dir" ? `${fmtBytes(r.size)}（目录项）` : `${fmtBytes(r.size)}（${r.size.toLocaleString()} 字节）`],
      ["占用空间", fmtBytes(r.blocks_512 * 512)],
      ["权限", `${r.perms}（八进制 ${r.mode.toString(8)}）`],
      ["所有者", `uid=${r.uid}  gid=${r.gid}`],
      ["硬链接数", String(r.nlink)],
      ["inode", String(r.ino)],
      ["修改时间", fmtTime(r.mtime)],
      ["访问时间", fmtTime(r.atime)],
      ["变更时间", fmtTime(r.ctime)],
    ];
    if (st.is_symlink) {
      rows.push(["链接目标", st.symlink || "（无法读取）"]);
      rows.push(["目标状态", st.resolved ? "有效" : "失效（悬空链接）"]);
    }
    $("#modal-mask").hidden = false;
    $("#preview").hidden = true;
    $("#props").hidden = false;
    $("#pr-path").textContent = path;
    $("#pr-body").innerHTML =
      `<div class="kv">` + rows.map(([k, v]) => `<div>${k}</div><div>${esc(v)}</div>`).join("") + `</div>`;
  } catch (e) {
    toast(`获取属性失败：${e.message}`, true);
  }
}

/* ------------------------------------------------------------------ */
/* 右键菜单                                                            */
/* ------------------------------------------------------------------ */
function hideCtxMenu() { $("#ctxmenu").hidden = true; }

function showCtxMenu(x, y, e) {
  const menu = $("#ctxmenu");
  const p = joinPath(S.path, e.name);
  const isBg = e.name === ".";
  const canWrite = writable();
  const items = [];

  if (isBg) {
    /* Finder：文件夹空白处右键 */
    if (canWrite)
      items.push({label: "新建文件夹", key: "⇧⌘N", fn: newFolder});
    if (S.clip.paths.length)
      items.push({label: "粘贴", key: "⌘V", fn: () => doPaste()});
    items.push({sep: true});
    items.push({label: "刷新", fn: () => navigate(S.path, {push: false})});
  } else {
    /* Finder：条目右键 */
    if (e.type === "dir") {
      items.push({label: "打开", fn: () => navigate(p)});
    } else {
      items.push({label: "打开", fn: () => sysOpen(p)});
      items.push({label: "快速查看", key: "Space", fn: () => openPreviewPath(p)});
    }
    items.push({sep: true});
    items.push({label: "剪切", key: "⌘X", fn: () => clipSet("cut")});
    items.push({label: "拷贝", key: "⌘C", fn: () => clipSet("copy")});
    if (e.type === "dir" && S.clip.paths.length)
      items.push({label: "粘贴到该文件夹", key: "⌘V", fn: () => doPaste(p)});
    items.push({sep: true});
    items.push({label: "拷贝为路径名称", key: "⌥⌘C", fn: () => {
      navigator.clipboard?.writeText(p).then(() => toast("已拷贝路径"));
    }});
    items.push({sep: true});
    if (e.type !== "dir")
      items.push({label: "下载", fn: () => download(p)});
    if (canWrite) {
      items.push({label: "重新命名", key: "F2", fn: () => startRename(e)});
    }
    items.push({label: "显示简介", key: "⌘I", fn: () => openProps(p)});
  }

  menu.innerHTML = items.map((it, i) => it.sep
    ? `<div class="sep"></div>`
    : `<div class="item ${it.dim ? "dim" : ""}" data-i="${i}"><span>${it.label}</span>`
      + (it.key ? `<span class="key">${it.key}</span>` : "") + `</div>`).join("");
  menu.hidden = false;
  menu.querySelectorAll(".item").forEach((el) =>
    el.addEventListener("click", () => { hideCtxMenu(); items[+el.dataset.i].fn(); }));

  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = Math.min(x, innerWidth - mw - 8) + "px";
  menu.style.top = Math.min(y, innerHeight - mh - 8) + "px";
}

/* 快速查看：直接预览选中项 */
function quickLookSelected() {
  if (S.selSet.size !== 1) return;
  const e = entryByName([...S.selSet][0]);
  if (e && e.type !== "dir") activateEntry(e);
}

async function openPreviewPath(p) {
  try {
    const st = await apiGet(`/api/stat?path=${encodeURIComponent(p)}`);
    if (st.resolved && st.resolved.type === "dir") return navigate(p);
    openPreview(p, st);
  } catch (e) {
    toast(e.message, true);
  }
}

function download(path) {
  const a = document.createElement("a");
  a.href = `/api/download?path=${encodeURIComponent(path)}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* ------------------------------------------------------------------ */
/* 事件绑定                                                            */
/* ------------------------------------------------------------------ */
function bindEvents() {
  // 启动页
  $("#btn-open").addEventListener("click", () => openDevice($("#open-path").value.trim()));
  $("#open-path").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") openDevice($("#open-path").value.trim());
  });
  $("#btn-scan").addEventListener("click", () => doScan(false));
  $("#btn-perm").addEventListener("click", async (ev) => {
    const devs = JSON.parse(ev.currentTarget.dataset.devs || "[]");
    if (!devs.length) return;
    try {
      await elevateDevices(devs);
    } catch (e) {
      toast(`授权失败：${e.message}`, true);
    }
  });

  // 确认弹窗
  $("#cf-ok").addEventListener("click", () => resolveConfirm(true));
  $("#cf-cancel").addEventListener("click", () => resolveConfirm(false));

  $("#ft-type").addEventListener("change", () =>
    buildFeatureGrid($("#ft-type").value));
  document.querySelectorAll(".dlg-close").forEach((b) =>
    b.addEventListener("click", (ev) =>
      closeDlg(ev.currentTarget.closest(".modal").id)));
  $("#gone-close").addEventListener("click", async () => {
    const ok = await confirmDialog(
      "放弃缓冲改动并关闭",
      `设备已被移除/卸载，缓冲的 ${S.pending.count} 项改动无法写回。\n确定放弃并关闭文件系统？`,
      "放弃并关闭");
    if (!ok) return;
    try {
      await apiPost("/api/close", {discard: true});
      toast("已关闭（缓冲改动已放弃）", true);
    } catch (e) {
      toast(e.message, true);
    }
    S.fs = null;
    S.deviceGone = false;
    S.pending = {count: 0, bytes: 0, limit: 0};
    renderPending();
    $("#gone-banner").hidden = true;
    showLaunch();
  });
  $("#ck-run").addEventListener("click", runCheck);
  $("#ft-run").addEventListener("click", runFormat);
  $("#sv-run").addEventListener("click", runSave);

  // 工具栏
  $("#nav-back").addEventListener("click", goBack);
  $("#nav-fwd").addEventListener("click", goFwd);
  $("#nav-up").addEventListener("click", goUp);
  $("#path-refresh").addEventListener("click", () => navigate(S.path, {push: false}));
  $("#btn-edit-path").addEventListener("click", beginEditPath);
  $("#btn-sidebar").addEventListener("click", () =>
    document.body.classList.toggle("no-sidebar"));
  $("#btn-actions").addEventListener("click", (ev) => {
    ev.stopPropagation();
    const menu = $("#dropdown");
    if (menu.hidden) showActionMenu(ev.currentTarget);
    else hideDropdown();
  });
  $("#search").addEventListener("input", () => { renderEntries(); renderStatus(); });
  $("#view-toggle").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      S.view = b.dataset.view;
      localStorage.setItem("e2fs.view", S.view);
      $("#view-toggle").querySelectorAll("button").forEach((x) =>
        x.classList.toggle("active", x === b));
      renderEntries();
    }));
  $("#view-toggle").querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === S.view));

  // 列表排序
  document.querySelectorAll("#listview thead th").forEach((th) =>
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (S.sort.key === key) S.sort.dir *= -1;
      else S.sort = {key, dir: 1};
      renderEntries();
    }));

  // 弹窗
  $("#pv-close").addEventListener("click", closeModal);
  $("#pr-close").addEventListener("click", closeModal);
  $("#modal-mask").addEventListener("mousedown", (ev) => {
    if (ev.target === $("#modal-mask")) closeModal();
  });
  $("#pv-download").addEventListener("click", (ev) =>
    download(ev.currentTarget.dataset.path));

  // 全局
  document.addEventListener("mousedown", (ev) => {
    if (!$("#ctxmenu").contains(ev.target)) hideCtxMenu();
    if (!$("#dropdown").contains(ev.target)
        && ev.target.closest("#btn-actions") === null) hideDropdown();
  });
  $("#content").addEventListener("contextmenu", (ev) => {
    if (ev.target.closest("tr") || ev.target.closest(".tile")) return;
    ev.preventDefault();
    showCtxMenu(ev.clientX, ev.clientY, {name: ".", type: "dir"});
  });
  document.addEventListener("click", (ev) => {
    if (ev.target.closest("#content") && !ev.target.closest("tr") && !ev.target.closest(".tile")) {
      if (S.selSet.size) { S.selSet.clear(); updateSelectionDom(); renderStatus(); }
    }
  });

  document.addEventListener("keydown", (ev) => {
    const inInput = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
    if (ev.key === "Escape") { closeModal(); hideCtxMenu(); return; }
    if (inInput) return;
    if (ev.metaKey && ev.key === "ArrowLeft") { ev.preventDefault(); goBack(); }
    else if (ev.metaKey && ev.key === "ArrowRight") { ev.preventDefault(); goFwd(); }
    else if (ev.metaKey && ev.key === "ArrowUp") { ev.preventDefault(); goUp(); }
    else if (ev.metaKey && ev.key === "f") { ev.preventDefault(); $("#search").focus(); }
    else if (ev.metaKey && ev.shiftKey && ev.key.toLowerCase() === "g") {
      ev.preventDefault(); beginEditPath();
    }
    else if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "c") { clipSet("copy"); }
    else if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "x") { clipSet("cut"); }
    else if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "v") { doPaste(); }
    else if (ev.key === "F2" && S.selSet.size === 1) {
      startRename(entryByName([...S.selSet][0]));
    }
    else if (ev.key === "Backspace") { ev.preventDefault(); goUp(); }
    else if (ev.key === "Enter" && S.selSet.size === 1) {
      const name = [...S.selSet][0];
      activateEntry(entryByName(name));
    }
  });

  // 内容区空白处作为拖放目标（移动/复制到当前目录）
  enableDropTarget($("#content"), () => S.path);
}

/* ------------------------------------------------------------------ */
/* 入口                                                                */
/* ------------------------------------------------------------------ */
async function init() {
  bindEvents();
  try {
    const data = await apiGet("/api/info");
    $("#ver").textContent = data.version || "";
    if (data.open) {
      S.fs = data.fs;
      S.version = data.version || "";
      showBrowser();
      renderDeviceCard();
      await navigate("/", {reset: true});
      await updateInfo();          // 同步缓冲指示
      renderFav();
      refreshSidebarMounts();
    } else {
      showLaunch();
    }
  } catch (e) {
    showLaunch();
    toast(`连接后端失败：${e.message}`, true);
  }
  startStatePoll();
}

init();
