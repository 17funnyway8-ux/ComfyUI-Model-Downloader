"""Standalone download worker, spawned as its own process.

Running the transfer out-of-process is what makes "cancel" instant: the parent
just kills the process, the OS drops the socket, and nothing can keep writing
to the .part file afterwards.

Protocol: JSON lines on stdout.  Exit codes: 0 ok, 2 http/network error,
3 size mismatch, 4 cancelled by parent (usually SIGKILL, so 0/-9 shows up).
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

CHUNK = 1 << 20


def emit(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def download(url: str, part: str, total_hint: int = 0) -> None:
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {
        "User-Agent": os.environ.get("MODEL_DL_UA", "comfyui-model-downloader/1.0"),
        "Accept": "*/*",
    }
    token = os.environ.get("MODEL_DL_HF_TOKEN") or ""
    if token and "huggingface" in url:
        headers["Authorization"] = f"Bearer {token}"
    if have:
        headers["Range"] = f"bytes={have}-"

    r = requests.get(url, headers=headers, stream=True, timeout=(15, 60), allow_redirects=True)
    with r:
        if r.status_code == 416 and have:
            emit(type="start", total=have, downloaded=have)
            return
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        if r.status_code == 200 and have:
            have = 0

        length = r.headers.get("Content-Length")
        total = have + int(length) if (length and length.isdigit()) else (total_hint or 0)
        emit(type="start", total=total, downloaded=have, resumed=bool(have))

        done = have
        last_t = time.monotonic()
        last_b = have
        mode = "ab" if have else "wb"
        with open(part, mode) as fh:
            for chunk in r.iter_content(CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last_t >= 0.4:
                    speed = (done - last_b) / (now - last_t)
                    emit(type="progress", downloaded=done, total=total, speed=speed)
                    last_t, last_b = now, done
            fh.flush()
            os.fsync(fh.fileno())

    size = os.path.getsize(part)
    if total and size != total:
        raise RuntimeError(f"size mismatch: {size} != {total}")
    emit(type="done", downloaded=size, total=total or size)


def main() -> int:
    cfg = json.loads(sys.argv[1])
    part = cfg["part"]
    urls = cfg.get("urls") or []
    total_hint = int(cfg.get("total") or 0)
    last = ""
    for url in urls:
        try:
            emit(type="source", url=url)
            download(url, part, total_hint)
            return 0
        except Exception as exc:  # noqa: BLE001 - report and try the mirror
            last = str(exc)[:300] or exc.__class__.__name__
            emit(type="error", error=last, url=url)
    if not urls:
        emit(type="error", error="no download url")
    else:
        emit(type="fatal", error=last)
    return 2


if __name__ == "__main__":
    sys.exit(main())
