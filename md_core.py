"""Core helpers for the ComfyUI model downloader.

Everything here is deliberately dependency-light: only stdlib + ComfyUI's own
``folder_paths`` module.  No torch, no aiohttp.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import folder_paths

# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

# Hosts we are willing to fetch from.  Anything else is rejected so this API
# cannot be used as a generic SSRF gadget on a LAN-exposed ComfyUI.
ALLOWED_HOSTS = {
    "huggingface.co",
    "www.huggingface.co",
    "hf-mirror.com",
    "www.hf-mirror.com",
    "civitai.com",
    "www.civitai.com",
    "civitai.red",
    "modelscope.cn",
    "www.modelscope.cn",
    "modelscope.ai",
    "github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
}

ALLOWED_SUFFIXES = (
    ".huggingface.co",
    ".hf-mirror.com",
    ".civitai.com",
    ".modelscope.cn",
    ".githubusercontent.com",
)

HF_HOSTS = {"huggingface.co", "www.huggingface.co"}
MIRROR_HOST = os.environ.get("MODEL_DL_MIRROR", "hf-mirror.com").strip() or "hf-mirror.com"

MODEL_EXTS = (
    ".safetensors",
    ".sft",
    ".ckpt",
    ".pth",
    ".pt",
    ".bin",
    ".gguf",
    ".onnx",
    ".pkl",
    ".npz",
    ".zip",
    ".tar",
)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def is_allowed_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if host in ALLOWED_HOSTS:
        return True
    # cdn / regional subdomains of the providers above
    return any(host.endswith(suffix) and len(host) > len(suffix) for suffix in ALLOWED_SUFFIXES)


def to_mirror(url: str) -> str | None:
    """Rewrite a huggingface.co URL onto the configured mirror."""
    try:
        parts = urlsplit(url)
    except Exception:
        return None
    host = (parts.hostname or "").lower()
    if host not in HF_HOSTS:
        return None
    netloc = MIRROR_HOST
    return urlunsplit((parts.scheme or "https", netloc, parts.path, parts.query, ""))


def build_candidates(url: str, prefer_mirror: bool = False) -> list[str]:
    """Ordered list of URLs to try for one model."""
    url = (url or "").strip()
    if not url:
        return []
    mirror = to_mirror(url)
    if mirror and prefer_mirror:
        return [mirror, url]
    if mirror:
        return [url, mirror]
    return [url]


def looks_like_model_url(url: str, name: str = "") -> bool:
    if not is_allowed_url(url):
        return False
    path = (urlsplit(url).path or "").lower()
    if path.endswith(MODEL_EXTS):
        return True
    return bool(name) and name.lower().endswith(MODEL_EXTS)


# ---------------------------------------------------------------------------
# Model folders / directory inference
# ---------------------------------------------------------------------------


def folder_keys() -> set[str]:
    try:
        return set(folder_paths.folder_names_and_paths.keys())
    except Exception:
        return set()


def normalize_folder(directory: str | None) -> str | None:
    """Map a template's ``directory`` value onto a real folder_paths key."""
    if not directory:
        return None
    d = str(directory).strip().strip("/\\")
    if not d:
        return None
    keys = folder_keys()
    if d in keys:
        return d
    try:
        mapped = folder_paths.map_legacy(d)
        if mapped in keys:
            return mapped
    except Exception:
        pass
    low = d.lower()
    for k in keys:
        if k.lower() == low:
            return k
    return None


def _iter_models(obj: Any) -> Iterator[dict]:
    if isinstance(obj, dict):
        props = obj.get("properties")
        if isinstance(props, dict):
            models = props.get("models")
            if isinstance(models, list):
                for m in models:
                    if isinstance(m, dict):
                        yield m
        for key, val in obj.items():
            if key == "properties":
                continue
            yield from _iter_models(val)
    elif isinstance(obj, list):
        for val in obj:
            yield from _iter_models(val)


# --- lazily built name -> (url, directory) index over the bundled templates --

_TPL_INDEX: dict[str, dict] | None = None
_TPL_LOCK = threading.Lock()


def template_model_index() -> dict[str, dict]:
    global _TPL_INDEX
    if _TPL_INDEX is not None:
        return _TPL_INDEX
    with _TPL_LOCK:
        if _TPL_INDEX is not None:
            return _TPL_INDEX
        idx: dict[str, dict] = {}
        try:
            import importlib.util

            spec = importlib.util.find_spec("comfyui_workflow_templates_json")
            roots = list(spec.submodule_search_locations or []) if spec else []
            for root in roots:
                tdir = os.path.join(root, "templates")
                if not os.path.isdir(tdir):
                    continue
                for fn in os.listdir(tdir):
                    if not fn.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(tdir, fn), "r", encoding="utf-8") as fh:
                            data = json.load(fh)
                    except Exception:
                        continue
                    for m in _iter_models(data):
                        name = m.get("name")
                        if name and name not in idx:
                            idx[str(name)] = {
                                "url": m.get("url"),
                                "directory": m.get("directory"),
                            }
        except Exception:
            pass
        _TPL_INDEX = idx
        return idx


# name heuristics, used only when neither the URL nor the templates know better
_HEURISTICS = (
    ("vae", "vae"),
    ("lora", "loras"),
    ("controlnet", "controlnet"),
    ("upscale", "upscale_models"),
    ("esrgan", "upscale_models"),
    ("clip_vision", "clip_vision"),
    ("clipvision", "clip_vision"),
    ("embedding", "embeddings"),
    ("text_encoder", "text_encoders"),
    ("t5xxl", "text_encoders"),
    ("clip_l", "text_encoders"),
    ("gemma", "text_encoders"),
    ("qwen", "text_encoders"),
    ("diffusion", "diffusion_models"),
    ("unet", "diffusion_models"),
    ("animate", "animatediff_models"),
    ("checkpoint", "checkpoints"),
)


def infer_directory(url: str = "", name: str = "") -> tuple[str, str]:
    """Guess the target folder key.  Returns ``(directory, how)``."""
    keys = folder_keys()

    # 1. a path segment that is literally a known folder key
    try:
        segments = [s for s in (urlsplit(url).path or "").split("/") if s]
    except Exception:
        segments = []
    for seg in reversed(segments[:-1] if len(segments) > 1 else segments):
        if seg in keys:
            return seg, "url"
        mapped = normalize_folder(seg)
        if mapped:
            return mapped, "url"

    # 2. the bundled template metadata
    if name:
        meta = template_model_index().get(name)
        if meta:
            mapped = normalize_folder(meta.get("directory"))
            if mapped:
                return mapped, "template"

    # 3. name heuristics
    low = (name or "").lower()
    for needle, folder in _HEURISTICS:
        if needle in low:
            mapped = normalize_folder(folder)
            if mapped:
                return mapped, "heuristic"

    return ("checkpoints" if "checkpoints" in keys else sorted(keys)[0]), "fallback"


def _safe_relpath(name: str) -> str:
    raw = str(name).replace("\\", "/").strip()
    if not raw or raw.endswith("/"):
        raise ValueError("invalid model filename")
    if raw.startswith("/"):
        raise ValueError("absolute paths are not allowed")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError("path traversal is not allowed")
    return os.path.join(*parts)


def resolve_destination(directory: str, name: str) -> str:
    """Absolute, traversal-checked destination path for one model file."""
    folder = normalize_folder(directory)
    if not folder:
        raise ValueError(f"unknown model directory: {directory!r}")

    rel = _safe_relpath(name)
    try:
        dirs = [d for d in folder_paths.get_folder_paths(folder) if os.path.isdir(d)]
    except Exception:
        dirs = []

    # prefer the directory literally named like the folder key (text_encoders
    # before clip, diffusion_models before unet)
    dirs.sort(key=lambda d: os.path.basename(os.path.normpath(d)).lower() != folder.lower())

    chosen = None
    for d in dirs:
        if os.access(d, os.W_OK):
            chosen = d
            break
    if chosen is None:
        root = os.path.join(folder_paths.models_dir, folder)
        os.makedirs(root, exist_ok=True)
        chosen = root

    root_abs = os.path.abspath(chosen)
    dest = os.path.abspath(os.path.join(root_abs, rel))
    if os.path.commonpath([dest, root_abs]) != root_abs:
        raise ValueError("destination escapes the models directory")
    if not os.path.isdir(os.path.dirname(dest)):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    return dest


# ---------------------------------------------------------------------------
# Presence checks / missing-model scan
# ---------------------------------------------------------------------------

_FALLBACK_DIRS = (
    "diffusion_models",
    "text_encoders",
    "vae",
    "loras",
    "checkpoints",
    "controlnet",
    "upscale_models",
    "clip_vision",
    "audio_encoders",
)


def _list(directory: str) -> list[str]:
    try:
        return folder_paths.get_filename_list(directory)
    except Exception:
        return []


def find_present(name: str, directory: str | None = None) -> tuple[bool, str | None]:
    """Is ``name`` present, and if so in which folder key?"""
    base = os.path.basename(str(name).replace("\\", "/"))

    def in_folder(folder: str) -> bool:
        for f in _list(folder):
            if f == name or os.path.basename(f.replace("\\", "/")) == base:
                return True
        return False

    if directory:
        folder = normalize_folder(directory)
        if folder and in_folder(folder):
            return True, folder

    for folder in _FALLBACK_DIRS:
        if folder == directory:
            continue
        if in_folder(folder):
            return True, folder
    return False, None


def check_missing(items: list[dict]) -> list[dict]:
    """Enrich a list of ``{name, directory, url}`` entries.

    Returns only the ones that are actually absent, with ``directory`` resolved.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        url = str(item.get("url") or "").strip()
        declared = item.get("directory") or ""
        folder = normalize_folder(declared)
        how = "declared" if folder else ""
        if not folder:
            folder, how = infer_directory(url, name)
        key = (folder, name)
        if key in seen:
            continue
        seen.add(key)

        present, where = find_present(name, folder)
        if present:
            continue
        out.append(
            {
                "name": name,
                "directory": folder,
                "directory_source": how,
                "url": url or (template_model_index().get(name) or {}).get("url") or "",
                "declared_directory": str(declared) if declared else "",
                "present_in": where,
            }
        )
    return out


def free_space(path: str) -> int | None:
    try:
        st = os.statvfs(os.path.dirname(path) or ".")
        return st.f_bavail * st.f_frsize
    except Exception:
        return None
