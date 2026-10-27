/**
 * ComfyUI Model Downloader — frontend extension.
 *
 * Two jobs:
 *   1. Take over the "下载 / 全部下载" buttons of the stock missing-models
 *      panel.  Those buttons are implemented as a detached <a download> whose
 *      click() opens a new browser tab (ComfyUI Desktop is the only build that
 *      saves the file itself), so we hook HTMLAnchorElement.prototype.click and
 *      route the URL to our server-side queue instead.
 *   2. Provide a sidebar tab listing the current workflow's missing models,
 *      with real download progress, speed, cancel and "download all".
 */

import { app } from "../../scripts/app.js";

const EXT_NAME = "comfyui.model.downloader";
const SETTING_MIRROR = "ModelDownloader.preferMirror";
const SETTING_INTERCEPT = "ModelDownloader.intercept";

const MODEL_EXT_RE = /\.(safetensors|sft|ckpt|pth|pt|bin|gguf|onnx|pkl|npz)$/i;
const HOST_OK_RE =
  /(^|\.)(huggingface\.co|hf-mirror\.com|civitai\.com|civitai\.red|modelscope\.cn|modelscope\.ai)$/i;

// ---------------------------------------------------------------------------
// tiny helpers
// ---------------------------------------------------------------------------

let apiRoot = null;

async function detectApiRoot() {
  if (apiRoot !== null) return apiRoot;
  for (const root of ["/api", ""]) {
    try {
      const r = await fetch(`${root}/model-downloader/ping`, { cache: "no-store" });
      if (r.ok) {
        apiRoot = root;
        return apiRoot;
      }
    } catch (e) {
      /* try the next prefix */
    }
  }
  apiRoot = "";
  return apiRoot;
}

async function apiGet(path) {
  const root = await detectApiRoot();
  const r = await fetch(`${root}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function apiPost(path, body) {
  const root = await detectApiRoot();
  const r = await fetch(`${root}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function pref(id, dflt) {
  try {
    const v = app.extensionManager?.setting?.get?.(id, dflt);
    return v === undefined || v === null ? dflt : v;
  } catch (e) {
    return dflt;
  }
}

function fmtBytes(n) {
  if (!n || n < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtEta(s) {
  if (!s || s <= 0 || !isFinite(s)) return "";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h${Math.round((s % 3600) / 60)}m`;
}

let toastTimer = null;
function toast(text, kind = "info") {
  let el = document.getElementById("md-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "md-toast";
    el.style.cssText = [
      "position:fixed",
      "right:18px",
      "bottom:18px",
      "z-index:100000",
      "max-width:380px",
      "padding:10px 14px",
      "border-radius:8px",
      "font-size:12px",
      "line-height:1.5",
      "color:#fff",
      "box-shadow:0 6px 24px rgba(0,0,0,.45)",
      "transition:opacity .25s ease",
      "pointer-events:none",
      "white-space:pre-wrap",
    ].join(";");
    document.body.appendChild(el);
  }
  el.style.background =
    kind === "error" ? "rgba(190,45,45,.95)" : kind === "ok" ? "rgba(30,120,70,.95)" : "rgba(35,35,40,.95)";
  el.textContent = text;
  el.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.style.opacity = "0";
  }, 4200);
}

// ---------------------------------------------------------------------------
// graph → model list
// ---------------------------------------------------------------------------

function collectModelsFromGraph() {
  const found = new Map();

  const visit = (obj) => {
    if (!obj || typeof obj !== "object") return;
    if (Array.isArray(obj)) {
      for (const v of obj) visit(v);
      return;
    }
    const props = obj.properties;
    if (props && Array.isArray(props.models)) {
      for (const m of props.models) {
        if (!m || typeof m !== "object" || !m.name) continue;
        if (!found.has(m.name)) {
          found.set(m.name, {
            name: m.name,
            url: m.url || "",
            directory: m.directory || "",
          });
        }
      }
    }
    for (const k in obj) {
      if (k === "properties") continue;
      visit(obj[k]);
    }
  };

  try {
    visit(app.graph?.serialize ? app.graph.serialize() : null);
  } catch (e) {
    console.warn("[model-downloader] serialize failed", e);
  }

  // subgraph definitions live beside the root graph
  try {
    const subs = app.graph?.subgraphs;
    if (subs) {
      const list = typeof subs.values === "function" ? [...subs.values()] : Object.values(subs);
      for (const s of list) visit(s?.serialize ? s.serialize() : s);
    }
  } catch (e) {
    /* ignore */
  }

  return [...found.values()];
}

// ---------------------------------------------------------------------------
// state + rendering
// ---------------------------------------------------------------------------

const state = {
  missing: [],
  jobs: [],
  scanning: false,
  error: "",
  lastScan: 0,
};

let rootEl = null;
let refreshTimer = null;
let pollTimer = null;
const reported = new Set();

function el(tag, style, text) {
  const e = document.createElement(tag);
  if (style) e.style.cssText = style;
  if (text !== undefined) e.textContent = text;
  return e;
}

const S = {
  section: "margin:10px 10px 14px;border:1px solid var(--border-color,#3a3a3a);border-radius:8px;overflow:hidden;",
  head: "display:flex;align-items:center;gap:6px;padding:7px 10px;background:var(--comfy-menu-bg,#222);font-size:12px;font-weight:600;",
  body: "padding:6px 8px;display:flex;flex-direction:column;gap:6px;",
  row: "display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:6px;background:rgba(255,255,255,.04);font-size:12px;",
  name: "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;",
  sub: "font-size:11px;opacity:.65;",
  btn: "cursor:pointer;border:1px solid var(--border-color,#4a4a4a);background:transparent;color:var(--fg-color,#ddd);border-radius:5px;padding:2px 8px;font-size:11px;line-height:18px;",
  btnMain: "cursor:pointer;border:none;background:#3a6df0;color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;width:100%;",
  bar: "height:4px;border-radius:2px;background:rgba(255,255,255,.12);overflow:hidden;",
  fill: "height:100%;background:#3a6df0;width:0%;transition:width .3s linear;",
};

function render() {
  if (!rootEl) return;
  rootEl.innerHTML = "";

  // ---- missing models -----------------------------------------------------
  const sec1 = el("div", S.section);
  const head1 = el("div", S.head);
  head1.appendChild(el("span", "", "当前工作流缺失模型"));
  const count = el("span", "opacity:.6;font-weight:400;", `(${state.missing.length})`);
  head1.appendChild(count);
  head1.appendChild(el("span", "flex:1"));
  const refreshBtn = el("button", S.btn, state.scanning ? "扫描中…" : "刷新");
  refreshBtn.title = "重新扫描当前工作流 / 模型目录";
  refreshBtn.onclick = () => refreshMissing(true);
  head1.appendChild(refreshBtn);
  sec1.appendChild(head1);

  const body1 = el("div", S.body);
  if (state.error) {
    body1.appendChild(el("div", "color:#e07070;font-size:11px;", state.error));
  }
  if (!state.missing.length) {
    body1.appendChild(
      el("div", "font-size:11px;opacity:.55;padding:4px 2px;", state.scanning ? "正在扫描…" : "没有缺失模型 ✔")
    );
  } else {
    for (const m of state.missing) {
      const row = el("div", S.row);
      const info = el("div", S.name);
      const title = el("div", S.name, m.name);
      title.title = `${m.directory}/${m.name}`;
      info.appendChild(title);
      const meta = [];
      meta.push(m.directory);
      if (m.size) meta.push(fmtBytes(m.size));
      if (m.directory_source && m.directory_source !== "declared") meta.push(`目录推断:${m.directory_source}`);
      if (m.insufficient_space) meta.push("⚠ 空间不足");
      info.appendChild(el("div", S.sub, meta.join(" · ")));
      row.appendChild(info);
      const btn = el("button", S.btn, "下载");
      btn.onclick = () => downloadOne(m, btn);
      row.appendChild(btn);
      body1.appendChild(row);
    }

    const total = state.missing.reduce((a, m) => a + (m.size || 0), 0);
    const all = el("button", S.btnMain, `全部下载${total ? ` (${fmtBytes(total)})` : ""}`);
    all.onclick = () => downloadAll(all);
    body1.appendChild(all);
  }
  sec1.appendChild(body1);
  rootEl.appendChild(sec1);

  // ---- queue --------------------------------------------------------------
  const sec2 = el("div", S.section);
  const head2 = el("div", S.head);
  const active = state.jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  head2.appendChild(el("span", "", "下载队列"));
  head2.appendChild(el("span", "opacity:.6;font-weight:400;", `(${state.jobs.length}${active ? ` / 进行中 ${active}` : ""})`));
  head2.appendChild(el("span", "flex:1"));
  if (state.jobs.length) {
    const clear = el("button", S.btn, "清空已完成");
    clear.onclick = async () => {
      await apiPost("/model-downloader/clear", {});
      pollJobs(true);
    };
    head2.appendChild(clear);
  }
  sec2.appendChild(head2);

  const body2 = el("div", S.body);
  if (!state.jobs.length) {
    body2.appendChild(el("div", "font-size:11px;opacity:.55;padding:4px 2px;", "队列为空"));
  }
  for (const j of state.jobs.slice(0, 40)) {
    body2.appendChild(renderJob(j));
  }
  sec2.appendChild(body2);
  rootEl.appendChild(sec2);
}

function renderJob(j) {
  const wrap = el("div", "display:flex;flex-direction:column;gap:4px;padding:6px;border-radius:6px;background:rgba(255,255,255,.04);");
  const row = el("div", "display:flex;align-items:center;gap:8px;font-size:12px;");
  const name = el("div", S.name, j.name);
  name.title = j.dest || j.name;
  row.appendChild(name);

  let statusText =
    {
      queued: "排队中",
      running: j.progress != null ? `${(j.progress * 100).toFixed(1)}%` : fmtBytes(j.downloaded),
      completed: "完成",
      error: "失败",
      canceled: "已取消",
    }[j.status] || j.status;
  if (j.cancel_requested && j.status === "running") statusText = "取消中…";
  const color =
    j.status === "completed" ? "#4caf7d" : j.status === "error" ? "#e07070" : j.status === "canceled" ? "#999" : "#9db4ff";
  row.appendChild(el("span", `color:${color};font-size:11px;`, statusText));

  if (j.status === "running" || j.status === "queued") {
    const cancel = el("button", S.btn, "✕");
    cancel.title = "取消（已下载部分会保留，可续传）";
    cancel.onclick = async () => {
      await apiPost("/model-downloader/cancel", { id: j.id });
      pollJobs(true);
    };
    row.appendChild(cancel);
  }
  wrap.appendChild(row);

  if (j.status === "running" || j.status === "queued") {
    const bar = el("div", S.bar);
    const fill = el("div", S.fill);
    fill.style.width = `${Math.max(0, Math.min(100, (j.progress || 0) * 100))}%`;
    bar.appendChild(fill);
    wrap.appendChild(bar);
    const bits = [];
    bits.push(j.total ? `${fmtBytes(j.downloaded)} / ${fmtBytes(j.total)}` : fmtBytes(j.downloaded));
    if (j.speed) bits.push(`${fmtBytes(j.speed)}/s`);
    const eta = fmtEta(j.eta);
    if (eta) bits.push(`剩余 ${eta}`);
    bits.push(`${j.directory}`);
    wrap.appendChild(el("div", S.sub, bits.join(" · ")));
  } else if (j.status === "error") {
    wrap.appendChild(el("div", "color:#e07070;font-size:11px;", j.error || "下载失败"));
  } else if (j.status === "completed") {
    wrap.appendChild(el("div", S.sub, `${fmtBytes(j.total || j.downloaded)} · ${j.dest || ""}`));
  }
  return wrap;
}

// ---------------------------------------------------------------------------
// actions
// ---------------------------------------------------------------------------

async function refreshMissing(force = false) {
  if (!rootEl) return;
  if (state.scanning && !force) return;
  if (!force && Date.now() - state.lastScan < 1500) return;
  state.scanning = true;
  state.error = "";
  render();
  try {
    const models = collectModelsFromGraph();
    if (!models.length) {
      state.missing = [];
    } else {
      const res = await apiPost("/model-downloader/missing", {
        models,
        prefer_mirror: !!pref(SETTING_MIRROR, false),
        with_sizes: true,
      });
      if (!res.ok) throw new Error(res.error || "扫描失败");
      state.missing = res.missing || [];
    }
    state.lastScan = Date.now();
  } catch (e) {
    state.error = `扫描失败: ${e?.message || e}`;
  } finally {
    state.scanning = false;
    render();
  }
}

async function enqueue(url, name, directory) {
  try {
    const res = await apiPost("/model-downloader/enqueue", {
      url,
      name,
      directory: directory || undefined,
      prefer_mirror: !!pref(SETTING_MIRROR, false),
    });
    if (res.ok) {
      toast(`已加入下载队列：${name || url}`, "ok");
      pollJobs(true);
      return true;
    }
    toast(`无法下载 ${name || ""}\n${res.error || ""}`.trim(), "error");
    return false;
  } catch (e) {
    toast(`请求失败：${e?.message || e}`, "error");
    return false;
  }
}

async function downloadOne(m, btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = "…";
    btn.style.opacity = ".6";
  }
  await enqueue(m.url, m.name, m.directory);
  setTimeout(() => {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "下载";
      btn.style.opacity = "1";
    }
  }, 900);
}

async function downloadAll(btn) {
  if (!state.missing.length) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "提交中…";
  }
  const payload = state.missing.map((m) => ({
    url: m.url,
    name: m.name,
    directory: m.directory,
    prefer_mirror: !!pref(SETTING_MIRROR, false),
  }));
  try {
    const res = await apiPost("/model-downloader/enqueue", payload);
    const ok = (res.results || []).filter((r) => r.ok).length;
    const bad = (res.results || []).length - ok;
    toast(`已提交 ${ok} 个下载${bad ? `，${bad} 个失败` : ""}`, bad ? "error" : "ok");
  } catch (e) {
    toast(`请求失败：${e?.message || e}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      render();
    }
    pollJobs(true);
  }
}

async function pollJobs(force = false) {
  if (!rootEl && !force) return;
  try {
    const res = await apiGet("/model-downloader/jobs");
    const jobs = res.jobs || [];
    let finishedSomething = false;
    for (const j of jobs) {
      if (j.status === "completed" && !reported.has(j.id)) {
        reported.add(j.id);
        finishedSomething = true;
      }
      if ((j.status === "error" || j.status === "canceled") && !reported.has(j.id)) {
        reported.add(j.id);
        if (j.status === "error") toast(`下载失败：${j.name}\n${j.error || ""}`.trim(), "error");
      }
    }
    state.jobs = jobs;
    render();
    if (finishedSomething) {
      toast("模型下载完成，已刷新模型列表", "ok");
      try {
        app.refreshComboInNodes?.();
      } catch (e) {
        /* older frontends */
      }
      refreshMissing(true);
    }
  } catch (e) {
    /* server restarting — keep quiet */
  }
}

// ---------------------------------------------------------------------------
// interception of the stock "下载" buttons
// ---------------------------------------------------------------------------

function isModelLink(a) {
  try {
    const href = a.href || a.getAttribute("href") || "";
    const name = a.getAttribute("download") || "";
    const u = new URL(href, window.location.href);
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    if (!HOST_OK_RE.test(u.hostname)) return false;
    return MODEL_EXT_RE.test(u.pathname) || MODEL_EXT_RE.test(name);
  } catch (e) {
    return false;
  }
}

function installInterceptor() {
  if (!pref(SETTING_INTERCEPT, true)) return;
  const proto = HTMLAnchorElement.prototype;
  if (proto.__mdPatched) return;

  const orig = proto.click;
  proto.click = function () {
    try {
      if (this.hasAttribute && this.hasAttribute("download") && isModelLink(this)) {
        const name = this.getAttribute("download") || "";
        console.log("[model-downloader] intercepted", name, this.href);
        enqueue(this.href, name, undefined);
        return;
      }
    } catch (e) {
      console.warn("[model-downloader] intercept failed", e);
    }
    return orig.apply(this, arguments);
  };
  proto.__mdPatched = true;

  // anchors that actually live in the DOM (our own buttons, future UI)
  document.addEventListener(
    "click",
    (ev) => {
      const a = ev.target?.closest?.("a[download]");
      if (a && isModelLink(a)) {
        ev.preventDefault();
        ev.stopPropagation();
        enqueue(a.href, a.getAttribute("download") || "", undefined);
      }
    },
    true
  );
  console.log("[model-downloader] stock download buttons intercepted");
}

// ---------------------------------------------------------------------------
// sidebar tab
// ---------------------------------------------------------------------------

function buildPanel(container) {
  // the sidebar calls render() again on every Vue re-render of the mount point,
  // so building the DOM must be idempotent
  if (container.__mdRoot && container.contains(container.__mdRoot)) {
    rootEl = container.__mdRoot;
    render();
    pollJobs(true);
    return;
  }
  container.style.cssText = "height:100%;overflow:auto;color:var(--fg-color,#ddd);padding-bottom:20px;";
  rootEl = el("div", "display:flex;flex-direction:column;height:100%;");
  container.__mdRoot = rootEl;
  container.appendChild(rootEl);
  render();
  refreshMissing(true);
  pollJobs(true);

  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    if (rootEl?.isConnected && rootEl.offsetParent !== null) refreshMissing();
  }, 6000);

  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (rootEl?.isConnected && rootEl.offsetParent !== null) pollJobs();
  }, 1000);
}

app.registerExtension({
  name: EXT_NAME,
  settings: [
    {
      id: SETTING_INTERCEPT,
      name: "接管官方「缺失模型」下载按钮",
      type: "boolean",
      defaultValue: true,
      tooltip: "开启后，模板缺失模型面板里的「下载 / 全部下载」会改为下载到本地 models 目录，而不是打开浏览器新标签页。",
    },
    {
      id: SETTING_MIRROR,
      name: "优先使用 HuggingFace 镜像 (hf-mirror.com)",
      type: "boolean",
      defaultValue: false,
      tooltip: "关闭时先直连 huggingface.co，失败自动回退镜像；开启则镜像优先。",
    },
  ],
  async setup() {
    installInterceptor();
    try {
      const ok = await detectApiRoot();
      console.log("[model-downloader] ready, api root =", apiRoot || "(same origin)", ok);
    } catch (e) {
      console.warn("[model-downloader] backend not reachable", e);
    }

    try {
      app.extensionManager.registerSidebarTab({
        id: "model-downloader",
        icon: "icon-[lucide--download]",
        title: "模型下载",
        tooltip: "缺失模型一键下载到本地 models 目录",
        label: "模型下载",
        type: "custom",
        render: (elm) => buildPanel(elm),
        destroy: () => {
          clearInterval(refreshTimer);
          clearInterval(pollTimer);
        },
      });
    } catch (e) {
      console.warn("[model-downloader] sidebar tab registration failed", e);
    }
  },
});

// patch as early as possible — the stock panel may be used before setup()
try {
  installInterceptor();
} catch (e) {
  /* settings store not ready yet; setup() retries */
}
