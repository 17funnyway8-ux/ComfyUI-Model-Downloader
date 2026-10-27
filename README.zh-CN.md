# ComfyUI Model Downloader

给 **ComfyUI 网页版 / 容器版**补上「缺失模型一键下载到本地」——就是桌面版有、网页版没有的那个按钮。

[English](README.md)

![status](https://img.shields.io/badge/ComfyUI-0.42.1-4b8bbe) ![frontend](https://img.shields.io/badge/frontend-1.55.18-4b8bbe) ![license](https://img.shields.io/badge/license-MIT-green)

---

## 为什么需要它

官方模板打开后缺模型，面板上会给你一个「下载」按钮。但网页版点了**不会下载到本地**，只会新开一个浏览器标签页。前端源码（`missingModelMetadata-*.js`）是这样的：

```js
function downloadModel(e) {
  if (window.__comfyDesktop2?.downloadModel && !isRemote) {
    startDesktop2ModelDownload(window.__comfyDesktop2, e);   // 只有桌面版真落盘
    return;
  }
  openUrlInNewTab(e.url, e.name);   // 网页版：只是开个新标签页
}
```

于是在容器/无头部署里，你只能复制 HuggingFace 链接 → 等浏览器下完 → 再手动 `mv` 进 `models/<子目录>/`。

这个扩展把最后这一步补上：同一个按钮，改成把 URL 交给 **ComfyUI 进程内的下载队列**，直接落到正确的目录，带进度、速度、续传和取消。

## 功能

- **接管官方按钮**：缺失模型面板里的「下载 / 全部下载」不再开新标签页，而是真正入队下载。
- **侧边栏「模型下载」面板**：列出当前工作流（含子图）缺的所有模型，带目录、体积、总占用；单个下载 / 全部下载 / 刷新。
- **实时进度**：已下载 / 总体积、速度、剩余时间、取消。取消保留 `.part`，下次自动用 `Range` 续传。
- **目录四级推断**：模板 `properties.models.directory` → URL 里的目录段 → 内置模板索引 → 文件名启发式；`text_encoders` 优先于 `clip`，`diffusion_models` 优先于 `unet`，并落到真实存在且可写的那个路径。
- **镜像回退**：先直连 `huggingface.co`，失败换 `hf-mirror.com`（可配置成镜像优先；自动走 `HTTP(S)_PROXY`）。
- **安全落位**：写 `*.part`，按 `Content-Length` 校验字节数，再 `os.replace` 原子改名，`chmod 755`，可选 `chown`。
- **缓存自动失效**：ComfyUI 的模型列表按目录 mtime 失效，面板一刷新就能看到新文件。

## 依赖

- ComfyUI 新版前端（实测 **0.42.1 / frontend 1.55.18**；侧边栏 tab API 需要 ≥ 1.5x）。
- `requests`（ComfyUI 自带依赖）。
- 不需要额外 Python 包、不需要新端口、不需要额外服务。

## 安装

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/<you>/ComfyUI-Model-Downloader.git
# 重启 ComfyUI（容器：docker restart comfyui），然后刷新页面
```

也可以直接拷贝目录：

```bash
cp -r ComfyUI-Model-Downloader /path/to/ComfyUI/custom_nodes/
sudo chown -R root:root /path/to/ComfyUI/custom_nodes/ComfyUI-Model-Downloader   # 仅当 ComfyUI 以 root 运行
```

下载以 **ComfyUI 进程身份**写入，所以该用户需要对 `models/` 有写权限。官方容器里是 root，开箱可用。

## 使用

1. 左侧栏点下载图标 → **模型下载**。
2. 面板自动扫描当前工作流，列出缺失项。
3. 点「下载」或「全部下载 (xx GB)」。
4. 也可以完全不用这个面板：直接在官方「缺失模型」面板点「下载」，走的是同一个队列。

下载完成后面板自动重扫；官方面板需要点它自己的 ↻（本扩展会顺带调 `app.refreshComboInNodes()` 刷新节点下拉框）。

## 设置

`设置 → 模型下载`：

| 设置 | 默认 | 说明 |
|---|---|---|
| 接管官方「缺失模型」下载按钮 | 开 | 关掉恢复官方行为（开新标签页）。改完刷新页面生效 |
| 优先使用 HuggingFace 镜像 (hf-mirror.com) | 关 | 关 = 直连优先、失败回退；开 = 镜像优先 |

## HTTP API

所有路由同时挂在 `/api` 前缀下。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/model-downloader/ping` | 存活探测 |
| GET | `/model-downloader/config` | 镜像、并发、模型根目录、目录键 |
| POST | `/model-downloader/missing` | `{models:[{name,directory,url}]}` → 缺失项 + 体积 |
| POST | `/model-downloader/enqueue` | `{url,name,directory?}` 或数组 |
| GET | `/model-downloader/jobs` | 队列快照（进度 / 速度 / ETA / 错误） |
| POST | `/model-downloader/cancel` | `{id}`，`{"id":"all"}` 取消全部 |
| POST | `/model-downloader/clear` | 清掉已完成/失败/取消的记录 |
| GET | `/model-downloader/folders` | 目录键 → 实际路径（排查用） |

```bash
curl -X POST http://127.0.0.1:8188/model-downloader/enqueue \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors",
       "name":"qwen3vl_8b_int8_convrot.safetensors",
       "directory":"text_encoders"}'
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MODEL_DL_WORKERS` | `2` | 并发下载数 |
| `MODEL_DL_MIRROR` | `hf-mirror.com` | 回退镜像域名 |
| `MODEL_DL_OWNER` | `1000:1001` | 落位后 `chown`；空字符串 = 不改 |
| `HF_TOKEN` | – | gated 仓库用，仅发给 HF / hf-mirror |
| `MODEL_DL_UA` | Chrome UA | 自定义 User-Agent |

## 实现要点

```
                    ┌──────────────────────────────┐
 浏览器             │  md_server.py (aiohttp)      │
 ─ anchor.click() ─▶│  POST /model-downloader/...  │
   被拦截           │  队列 + 任务状态              │
                    └───────────┬──────────────────┘
                                │ spawn
                                ▼
                    ┌──────────────────────────────┐
                    │  md_worker.py（独立进程）      │
                    │  GET + Range，JSON 行上报      │
                    └───────────┬──────────────────┘
                                ▼
                       models/<folder>/<name>.part → <name>
```

- 传输跑在**独立进程**里，取消 = `kill`：即使 socket 卡死也能秒断（第一版用线程 + 阻塞读，取消要等十几秒，已废弃）。
- 子进程用 JSON 行上报进度，服务端线程转成任务状态。
- 前端 patch 的是 `HTMLAnchorElement.prototype.click`。必须是原型：官方代码创建的是**游离** `<a download>` 再 `.click()`，`document` 级监听永远收不到这个事件。
- 侧边栏 tab 用 `type: 'custom'` 注册；它的 `render(el)` 会在 Vue 每次重渲染挂载点时被再次调用，所以 DOM 构建做了幂等。

## 已知限制

- 只拦截模型类后缀（`.safetensors`、`.sft`、`.ckpt`、`.pth`、`.pt`、`.bin`、`.gguf`、`.onnx` 等）；文档、`.json` 链接照旧走浏览器。
- 模板没有 `properties.models` 元数据的，官方面板本来也没有下载按钮，这里同样不会凭空造一个。
- 官方缺失模型**列表**没有改动，只替换了它的下载动作；列表刷新仍由它自己的 ↻ 负责。
- 下载域名做了白名单（HuggingFace / hf-mirror / Civitai / ModelScope / GitHub）。ComfyUI 的 API 本身无鉴权，公网暴露请自行加反代鉴权。

## 许可

MIT — 见 [LICENSE](LICENSE)。
