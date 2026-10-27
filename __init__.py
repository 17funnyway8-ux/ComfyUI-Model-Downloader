"""ComfyUI Model Downloader — server-side model downloads for the web UI.

ComfyUI's stock "missing models" panel can only open the model URL in a new
browser tab; only ComfyUI Desktop actually saves the file.  This node adds that
missing piece: a download queue running inside the ComfyUI process, writing
straight into the right ``models/<folder>/`` path, with progress and cancel.
"""

WEB_DIRECTORY = "./web"

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

try:  # importing registers the HTTP routes on PromptServer
    from . import md_server  # noqa: F401,E402
except Exception as exc:  # pragma: no cover - never take ComfyUI down with us
    print(f"[model-downloader] API registration failed: {exc}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
