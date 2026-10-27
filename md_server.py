"""HTTP API + background download queue for the ComfyUI model downloader.

Each transfer runs in its own process (``md_worker.py``) so that cancelling is
instant and a stalled socket can never wedge the ComfyUI server.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

import requests
from aiohttp import web

from server import PromptServer

from . import md_core

routes = PromptServer.instance.routes

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "md_worker.py")
USER_AGENT = os.environ.get(
    "MODEL_DL_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 comfyui-model-downloader",
)
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""
MAX_WORKERS = max(1, int(os.environ.get("MODEL_DL_WORKERS", "2") or 2))
MAX_JOBS_KEPT = 60

_OWNER_ENV = os.environ.get("MODEL_DL_OWNER", "1000:1001")
try:
    _parts = [int(x) for x in _OWNER_ENV.split(":", 1)]
    OWNER = (_parts[0], _parts[1]) if len(_parts) == 2 else None  # noqa: PLR2004
except Exception:
    OWNER = None


class Job:
    __slots__ = (
        "id",
        "url",
        "name",
        "directory",
        "dest",
        "total",
        "downloaded",
        "status",
        "error",
        "source",
        "created_at",
        "started_at",
        "finished_at",
        "resumed",
        "directory_source",
        "prefer_mirror",
        "cancel",
        "lock",
        "speed",
        "proc",
    )

    def __init__(self, url, name, directory, dest, directory_source="", prefer_mirror=False):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.name = name
        self.directory = directory
        self.dest = dest
        self.total = 0
        self.downloaded = 0
        self.status = "queued"
        self.error = None
        self.source = ""
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.resumed = False
        self.directory_source = directory_source
        self.prefer_mirror = prefer_mirror
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.speed = 0.0
        self.proc = None

    def update(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self.lock:
            elapsed = (self.finished_at or time.time()) - self.started_at if self.started_at else 0.0
            remaining = None
            if self.status == "running" and self.speed > 1 and self.total > self.downloaded:
                remaining = (self.total - self.downloaded) / self.speed
            return {
                "id": self.id,
                "url": self.url,
                "name": self.name,
                "directory": self.directory,
                "directory_source": self.directory_source,
                "dest": self.dest,
                "total": self.total,
                "downloaded": self.downloaded,
                "progress": (self.downloaded / self.total) if self.total else None,
                "speed": self.speed,
                "eta": remaining,
                "status": self.status,
                "cancel_requested": self.cancel.is_set(),
                "error": self.error,
                "source": self.source,
                "resumed": self.resumed,
                "elapsed": elapsed,
                "created_at": self.created_at,
            }


class Manager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.q: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.size_cache: dict[str, int | None] = {}
        self.size_lock = threading.Lock()
        self._workers: list[threading.Thread] = []

    def start(self) -> None:
        if self._workers:
            return
        for i in range(MAX_WORKERS):
            t = threading.Thread(target=self._loop, name=f"model-dl-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    # -- queue --------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            job_id = self.q.get()
            try:
                job = self.jobs.get(job_id)
                if job is None or job.status == "canceled":
                    continue
                self._run(job)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[model-downloader] worker error: {exc}")
            finally:
                self.q.task_done()

    def enqueue(self, url: str, name: str, directory: str | None = None, prefer_mirror: bool = False):
        url = (url or "").strip()
        name = (name or "").strip()
        if not url:
            return {"ok": False, "error": "缺少下载地址"}
        if not md_core.is_allowed_url(url):
            return {"ok": False, "error": f"不允许的下载域名: {md_core.host_of(url) or url}"}

        folder, how = (md_core.normalize_folder(directory), "declared") if directory else (None, "")
        if not folder:
            folder, how = md_core.infer_directory(url, name)
        if not name:
            name = os.path.basename(url.split("?")[0])
        try:
            dest = md_core.resolve_destination(folder, name)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        with self.lock:
            for job in self.jobs.values():
                if job.dest == dest and job.status in ("queued", "running"):
                    return {"ok": True, "deduped": True, "job": job.snapshot()}
            if os.path.exists(dest) and not os.path.exists(dest + ".part"):
                return {"ok": False, "error": "文件已存在", "existing": dest}

        job = Job(url, name, folder, dest, directory_source=how, prefer_mirror=prefer_mirror)
        with self.lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            self._trim()
        self.start()
        self.q.put(job.id)
        print(f"[model-downloader] queued {folder}/{name} <- {url}")
        return {"ok": True, "job": job.snapshot()}

    def _trim(self) -> None:
        while len(self.order) > MAX_JOBS_KEPT:
            old = self.order[0]
            job = self.jobs.get(old)
            if job and job.status in ("queued", "running"):
                break
            self.order.pop(0)
            self.jobs.pop(old, None)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancel.set()
        proc = job.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        if job.status == "queued":
            job.update(status="canceled", finished_at=time.time())
        return True

    def snapshot(self) -> list[dict]:
        with self.lock:
            ids = list(reversed(self.order))
        out = []
        for jid in ids:
            job = self.jobs.get(jid)
            if job:
                out.append(job.snapshot())
        return out

    # -- size probe ---------------------------------------------------------
    def probe_size(self, url: str, prefer_mirror: bool = False) -> int | None:
        with self.size_lock:
            if url in self.size_cache:
                return self.size_cache[url]
        size = None
        for cand in md_core.build_candidates(url, prefer_mirror):
            try:
                r = requests.head(cand, allow_redirects=True, timeout=(8, 15), headers=_headers(cand))
                if r.status_code in (200, 206):
                    for key in ("X-Linked-Size", "Content-Length"):
                        val = r.headers.get(key)
                        if val and val.isdigit() and int(val) > 0:
                            size = int(val)
                            break
            except Exception:
                pass
            if size:
                break
        with self.size_lock:
            self.size_cache[url] = size
        return size

    # -- run one job --------------------------------------------------------
    def _run(self, job: Job) -> None:
        if job.cancel.is_set():
            job.update(status="canceled", finished_at=time.time())
            return

        part = job.dest + ".part"
        job.update(status="running", started_at=time.time())
        cfg = {
            "urls": md_core.build_candidates(job.url, prefer_mirror=job.prefer_mirror),
            "part": part,
            "total": job.total or 0,
        }
        env = dict(os.environ)
        if HF_TOKEN:
            env["MODEL_DL_HF_TOKEN"] = HF_TOKEN
        env["MODEL_DL_UA"] = USER_AGENT

        try:
            proc = subprocess.Popen(
                [sys.executable, WORKER, json.dumps(cfg)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except Exception as exc:
            job.update(status="error", error=f"无法启动下载进程: {exc}", finished_at=time.time())
            return

        job.proc = proc
        last_error = ""
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            kind = msg.get("type")
            if kind == "start":
                job.update(total=int(msg.get("total") or 0), downloaded=int(msg.get("downloaded") or 0), resumed=bool(msg.get("resumed")))
            elif kind == "progress":
                job.update(
                    downloaded=int(msg.get("downloaded") or 0),
                    total=int(msg.get("total") or job.total),
                    speed=float(msg.get("speed") or 0.0),
                )
            elif kind == "source":
                job.update(source=str(msg.get("url") or ""))
            elif kind == "done":
                job.update(downloaded=int(msg.get("downloaded") or 0), total=int(msg.get("total") or 0))
            elif kind in ("error", "fatal"):
                last_error = str(msg.get("error") or "")
        rc = proc.wait()
        job.proc = None

        if job.cancel.is_set():
            job.update(status="canceled", finished_at=time.time(), speed=0.0)
            print(f"[model-downloader] canceled {job.name} (part kept: {part})")
            return

        if rc != 0:
            job.update(
                status="error",
                error=last_error or f"下载失败（退出码 {rc}）",
                finished_at=time.time(),
                speed=0.0,
            )
            print(f"[model-downloader] error {job.name}: {job.error}")
            return

        size = os.path.getsize(part) if os.path.exists(part) else 0
        if job.total and size != job.total:
            job.update(status="error", error=f"大小不符（{size} != {job.total}）", finished_at=time.time())
            return
        try:
            os.replace(part, job.dest)
        except Exception as exc:
            job.update(status="error", error=f"重命名失败: {exc}", finished_at=time.time())
            return
        _fix_owner(job.dest)
        job.update(
            status="completed",
            downloaded=size,
            total=job.total or size,
            finished_at=time.time(),
            speed=0.0,
        )
        print(f"[model-downloader] done {job.directory}/{job.name} ({size} bytes)")


def _headers(url: str = "") -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if HF_TOKEN:
        host = md_core.host_of(url)
        if host in md_core.HF_HOSTS or host.endswith(md_core.MIRROR_HOST):
            h["Authorization"] = f"Bearer {HF_TOKEN}"
    return h


def _fix_owner(path: str) -> None:
    try:
        os.chmod(path, 0o755)
    except Exception:
        pass
    if OWNER:
        try:
            os.chown(path, OWNER[0], OWNER[1])
        except Exception:
            pass


manager = Manager()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _json(payload, status: int = 200):
    return web.json_response(payload, status=status)


@routes.get("/model-downloader/ping")
async def ping(_request):
    return _json({"ok": True, "version": 2, "mirror": md_core.MIRROR_HOST, "workers": MAX_WORKERS})


@routes.get("/model-downloader/config")
async def get_config(_request):
    return _json(
        {
            "ok": True,
            "mirror": md_core.MIRROR_HOST,
            "workers": MAX_WORKERS,
            "models_dir": getattr(md_core.folder_paths, "models_dir", ""),
            "folders": sorted(md_core.folder_keys()),
            "owner": OWNER,
            "templates_indexed": len(md_core.template_model_index()),
        }
    )


@routes.get("/model-downloader/jobs")
async def get_jobs(_request):
    return _json({"ok": True, "jobs": manager.snapshot()})


@routes.post("/model-downloader/enqueue")
async def post_enqueue(request):
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "invalid json"}, 400)

    if isinstance(body, list):
        results = []
        for item in body:
            if not isinstance(item, dict):
                results.append({"ok": False, "error": "invalid item"})
                continue
            try:
                results.append(
                    manager.enqueue(
                        url=str(item.get("url") or ""),
                        name=str(item.get("name") or ""),
                        directory=item.get("directory") or None,
                        prefer_mirror=bool(item.get("prefer_mirror")),
                    )
                )
            except Exception as exc:
                results.append({"ok": False, "error": str(exc)})
        return _json({"ok": True, "results": results})

    if not isinstance(body, dict):
        return _json({"ok": False, "error": "invalid body"}, 400)
    return _json(
        manager.enqueue(
            url=body.get("url", ""),
            name=body.get("name", ""),
            directory=body.get("directory") or None,
            prefer_mirror=bool(body.get("prefer_mirror")),
        )
    )


@routes.post("/model-downloader/cancel")
async def post_cancel(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    job_id = str((body or {}).get("id") or "")
    if job_id in ("", "all", "*"):
        for job in manager.snapshot():
            if job["status"] in ("queued", "running"):
                manager.cancel(job["id"])
        return _json({"ok": True})
    return _json({"ok": manager.cancel(job_id)})


@routes.post("/model-downloader/clear")
async def post_clear(_request):
    with manager.lock:
        for jid in list(manager.order):
            job = manager.jobs.get(jid)
            if job and job.status in ("completed", "error", "canceled"):
                manager.jobs.pop(jid, None)
                manager.order.remove(jid)
    return _json({"ok": True})


@routes.post("/model-downloader/missing")
async def post_missing(request):
    """Body: {models:[{name,directory,url}], prefer_mirror?:bool, with_sizes?:bool}"""
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json({"ok": False, "error": "invalid body"}, 400)
    models = body.get("models")
    if not isinstance(models, list):
        return _json({"ok": False, "error": "models[] required"}, 400)
    prefer_mirror = bool(body.get("prefer_mirror"))
    with_sizes = body.get("with_sizes", True)

    loop = asyncio.get_running_loop()
    missing = await loop.run_in_executor(None, md_core.check_missing, models)

    if with_sizes and missing:
        sem = asyncio.Semaphore(min(6, len(missing)))

        async def fill(entry):
            url = entry.get("url") or ""
            if url:
                async with sem:
                    entry["size"] = await loop.run_in_executor(None, manager.probe_size, url, prefer_mirror)
            dest = None
            try:
                dest = md_core.resolve_destination(entry["directory"], entry["name"])
            except Exception:
                dest = None
            if entry.get("size") and dest:
                free = md_core.free_space(dest)
                if free is not None and free < entry["size"]:
                    entry["insufficient_space"] = True

        await asyncio.gather(*(fill(e) for e in missing))

    return _json({"ok": True, "missing": missing})


@routes.get("/model-downloader/folders")
async def get_folders(_request):
    out = {}
    for key in sorted(md_core.folder_keys()):
        try:
            out[key] = md_core.folder_paths.get_folder_paths(key)
        except Exception:
            out[key] = []
    return _json({"ok": True, "folders": out})
