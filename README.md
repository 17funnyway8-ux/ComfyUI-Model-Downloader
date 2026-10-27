# ComfyUI Model Downloader

One-click model downloads for the **ComfyUI web UI** — the button ComfyUI Desktop has and the browser build doesn't.

[中文说明](README.zh-CN.md)

![status](https://img.shields.io/badge/ComfyUI-0.42.1-4b8bbe) ![frontend](https://img.shields.io/badge/frontend-1.55.18-4b8bbe) ![license](https://img.shields.io/badge/license-MIT-green)

---

## Why this exists

When you open an official template in the browser build and models are missing, ComfyUI shows a **Download** button. Clicking it does *not* download anything into `models/` — it opens a new browser tab. Here is the actual frontend code (`missingModelMetadata-*.js`):

```js
function downloadModel(e) {
  if (window.__comfyDesktop2?.downloadModel && !isRemote) {
    startDesktop2ModelDownload(window.__comfyDesktop2, e);   // Desktop only
    return;
  }
  openUrlInNewTab(e.url, e.name);   // web build: just opens a tab
}
```

So on a headless/container install you end up copy-pasting HuggingFace links, waiting for the browser, then `mv`-ing files into the right `models/<subdir>/` by hand.

This extension replaces that last mile: the same button now hands the URL to a download queue **inside the ComfyUI process**, which writes straight to the correct folder, with progress, speed, resume and cancel.

## Features

- **Takes over the stock buttons** — `下载 / 全部下载` in the missing-models panel now enqueue a real download instead of opening a tab.
- **"Model Download" sidebar tab** — lists every model missing from the current workflow (subgraphs included) with folder, size and total footprint; per-item download, download-all, refresh.
- **Live progress** — bytes/total, speed, ETA, cancel. Cancelling keeps the `.part` file and the next attempt resumes with an HTTP `Range` request.
- **Folder inference with four fallbacks** — template `properties.models.directory` → directory segment in the URL → index of the bundled templates → filename heuristics. Prefers `text_encoders` over `clip`, `diffusion_models` over `unet`, and writes into whichever candidate path actually exists and is writable.
- **Mirror fallback** — tries `huggingface.co` first, then `hf-mirror.com` (configurable; honours `HTTP(S)_PROXY`).
- **Safe landing** — writes `*.part`, verifies byte count against `Content-Length`, then `os.replace` into place, `chmod 755`, optional `chown`.
- **ComfyUI's file cache invalidates itself** (mtime based), so a panel refresh is enough to see the new model.

## Requirements

- ComfyUI with the new frontend (tested on **0.42.1 / frontend 1.55.18**; the sidebar-tab API needs ≥ 1.5x).
- `requests` — already a ComfyUI dependency.
- No extra Python packages, no extra port, no separate service.

## Install

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/17funnyway8-ux/ComfyUI-Model-Downloader.git
# restart ComfyUI (container: docker restart comfyui), then reload the page
```

Manual copy works too:

```bash
cp -r ComfyUI-Model-Downloader /path/to/ComfyUI/custom_nodes/
sudo chown -R root:root /path/to/ComfyUI/custom_nodes/ComfyUI-Model-Downloader   # only if ComfyUI runs as root
```

The downloader writes as **the ComfyUI process user**, so that user needs write access to `models/`. In the official container that's root, and it just works.

## Usage

1. Click the download icon in the left sidebar → **Model Download**.
2. The panel scans the open workflow and lists what's missing.
3. Hit **Download** / **Download all (x GB)**.
4. Or skip the panel entirely: use the stock missing-models panel and press **Download** — same queue.

After a download finishes the panel rescans automatically; the stock panel needs its own ↻ (this extension also calls `app.refreshComboInNodes()` so node widgets pick the new file up).

## Settings

`Settings → Model Download`:

| Setting | Default | Description |
|---|---|---|
| Intercept the stock "missing model" download buttons | on | Off restores upstream behaviour (opens a browser tab). Requires a page reload. |
| Prefer HuggingFace mirror (hf-mirror.com) | off | Off = direct first, mirror on failure; on = mirror first. |

## HTTP API

Every route is also mounted under the `/api` prefix.

| Method | Path | Description |
|---|---|---|
| GET | `/model-downloader/ping` | liveness |
| GET | `/model-downloader/config` | mirror, workers, models root, folder keys |
| POST | `/model-downloader/missing` | `{models:[{name,directory,url}]}` → missing entries + sizes |
| POST | `/model-downloader/enqueue` | `{url,name,directory?}` or a list of them |
| GET | `/model-downloader/jobs` | queue snapshot (progress, speed, ETA, error) |
| POST | `/model-downloader/cancel` | `{id}`, or `{"id":"all"}` |
| POST | `/model-downloader/clear` | drop finished/failed/cancelled records |
| GET | `/model-downloader/folders` | folder key → paths (debugging) |

```bash
curl -X POST http://127.0.0.1:8188/model-downloader/enqueue \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors",
       "name":"qwen3vl_8b_int8_convrot.safetensors",
       "directory":"text_encoders"}'
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MODEL_DL_WORKERS` | `2` | concurrent downloads |
| `MODEL_DL_MIRROR` | `hf-mirror.com` | mirror host used for the HF fallback |
| `MODEL_DL_OWNER` | `1000:1001` | `chown` applied after landing; empty string disables it |
| `HF_TOKEN` | – | for gated repos (sent to HF / hf-mirror only) |
| `MODEL_DL_UA` | Chrome UA | override the User-Agent |

## How it works

```
                    ┌──────────────────────────────┐
 browser            │  md_server.py (aiohttp)      │
 ─ anchor.click() ─▶│  POST /model-downloader/...  │
   intercepted      │  queue + job state           │
                    └───────────┬──────────────────┘
                                │ spawn
                                ▼
                    ┌──────────────────────────────┐
                    │  md_worker.py (own process)  │
                    │  GET + Range, JSON lines     │
                    └───────────┬──────────────────┘
                                ▼
                       models/<folder>/<name>.part → <name>
```

- Transfers run in a **separate process**, so `cancel` is a `kill`: it works even when the socket is stuck (a thread doing a blocking read cannot be interrupted reliably — that was the first, broken design).
- The transfer process reports progress as JSON lines on stdout; the server thread turns them into job state.
- The frontend intercepts `HTMLAnchorElement.prototype.click`. It must be the prototype: the stock code builds a **detached** `<a download>` and calls `.click()` on it, so a `document`-level listener never sees the event.
- The sidebar tab is registered with `type: 'custom'`; its `render(el)` is called again on every Vue re-render of the mount point, so DOM construction is idempotent.

## Limitations

- Only model-like URLs are intercepted (`.safetensors`, `.sft`, `.ckpt`, `.pth`, `.pt`, `.bin`, `.gguf`, `.onnx`, …). Links to docs or `.json` still open in the browser.
- Templates without `properties.models` metadata have no download button upstream either; nothing is invented for them.
- The stock missing-models **list** is untouched — only its download action is replaced, and its own ↻ still governs its refresh.
- Download hosts are allow-listed (HuggingFace, hf-mirror, Civitai, ModelScope, GitHub). ComfyUI's API has no authentication; if you expose it publicly, put auth in front of it.

## License

MIT — see [LICENSE](LICENSE).
