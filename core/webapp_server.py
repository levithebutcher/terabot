import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from aiohttp import web

import config
from utils.helpers import format_bytes
from utils.logger import logger

# Path to static web assets
WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"

_server_runner: Optional[web.AppRunner] = None
# In-memory session store for web-initiated explorations and TeraBox shares
_web_sessions: dict[str, dict] = {}


def make_cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


def build_webapp_url(session_id: str) -> Optional[str]:
    """Generate the full public WebApp URL with session_id and optional api_base."""
    base_url = getattr(config, "WEBAPP_URL", "").strip().rstrip("/")
    if not base_url:
        return None
    sep = "&" if ("?" in base_url or "github.io" in base_url or base_url.endswith(".html")) else ("?" if base_url.endswith("/webapp") else None)
    if sep is None:
        webapp_url = f"{base_url}/webapp?session_id={session_id}"
    else:
        webapp_url = f"{base_url}{sep}session_id={session_id}"

    api_base = getattr(config, "API_BASE_URL", "").strip().rstrip("/")
    if api_base and "your-bot-backend-url.com" not in api_base and "api_base=" not in webapp_url:
        sep2 = "&" if "?" in webapp_url else "?"
        webapp_url = f"{webapp_url}{sep2}api_base={api_base}"

    return webapp_url


def serialize_mega_dirs(folder_info: dict) -> tuple[dict, str, int, str]:
    """Convert Mega folder_info dictionary into a serializable tree."""
    from core.media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

    root_h = folder_info.get("root_h", "")
    dirs = folder_info.get("dirs", {})

    serialized_dirs = {}
    for dh, d in dirs.items():
        direct_files = []
        raw_files = d.get("direct_videos", []) + d.get("direct_photos", []) + d.get("direct_others", [])
        for f in raw_files:
            fname = f.get("name", "unnamed")
            ext = ("." + fname.split(".")[-1].lower()) if "." in fname else ""
            fsize = f.get("size", 0)

            if ext in VIDEO_EXTENSIONS:
                ftype = "video"
            elif ext in IMAGE_EXTENSIONS:
                ftype = "photo"
            else:
                ftype = "other"

            direct_files.append({
                "h": f.get("h"),
                "name": fname,
                "size": fsize,
                "formatted_size": format_bytes(fsize),
                "type": ftype,
                "ext": ext,
            })

        serialized_dirs[dh] = {
            "h": dh,
            "name": d.get("name", "Folder"),
            "path": d.get("path", d.get("name", "Folder")),
            "parent": d.get("p"),
            "active_children": d.get("active_children", []),
            "total_files": d.get("total_files", 0),
            "total_size": d.get("total_size", 0),
            "formatted_total_size": format_bytes(d.get("total_size", 0)),
            "files": direct_files,
        }

    root_dir = dirs.get(root_h, {})
    total_share_size = sum(d.get("total_size", 0) for d in dirs.values()) if dirs else 0
    if root_dir.get("total_size", 0) > 0:
        total_share_size = root_dir.get("total_size", 0)

    title = root_dir.get("name", "Mega Share")
    return serialized_dirs, root_h, total_share_size, title


def serialize_terabox_files(files: list, title: str = "TeraBox Share") -> tuple[dict, str, int, str]:
    """Convert TeraBox TeraFile list into a serializable explorer tree."""
    from core.media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

    root_h = "root"
    direct_files = []
    total_size = 0

    if files and title == "TeraBox Share":
        if len(files) == 1:
            title = files[0].file_name
        else:
            title = f"TeraBox Share ({len(files)} items)"

    for idx, f in enumerate(files):
        fname = f.file_name
        ext = ("." + fname.split(".")[-1].lower()) if "." in fname else ""
        fsize = f.size or 0
        total_size += fsize

        if ext in VIDEO_EXTENSIONS:
            ftype = "video"
        elif ext in IMAGE_EXTENSIONS:
            ftype = "photo"
        else:
            ftype = "other"

        handle = str(f.fs_id or f"tb_{idx}_{uuid.uuid4().hex[:6]}")
        direct_files.append({
            "h": handle,
            "name": fname,
            "size": fsize,
            "formatted_size": f.size_readable or format_bytes(fsize),
            "type": ftype,
            "ext": ext,
            "dlink": f.dlink or "",
            "thumb": f.thumb or "",
        })

    serialized_dirs = {
        root_h: {
            "h": root_h,
            "name": title,
            "path": f"/{title}",
            "parent": None,
            "active_children": [],
            "total_files": len(direct_files),
            "total_size": total_size,
            "formatted_total_size": format_bytes(total_size),
            "files": direct_files,
        }
    }
    return serialized_dirs, root_h, total_size, title


def register_terabox_session(session_id: str, files: list, title: str = "TeraBox Share"):
    """Register an active TeraBox session for the Telegram Mini App."""
    serialized_dirs, root_h, total_size, title = serialize_terabox_files(files, title=title)
    _web_sessions[session_id] = {
        "session_id": session_id,
        "service": "terabox",
        "files": files,
        "dirs": serialized_dirs,
        "root_h": root_h,
        "title": title,
        "total_files": len(files),
        "total_size": total_size,
        "formatted_total_size": format_bytes(total_size),
        "created_at": time.time(),
    }


async def cors_preflight(request: web.Request) -> web.Response:
    """Handle CORS preflight requests."""
    return web.Response(headers=make_cors_headers())


async def handle_index(request: web.Request) -> web.Response:
    """Serve the Telegram Mini App HTML page."""
    index_file = WEBAPP_DIR / "index.html"
    if not index_file.exists():
        return web.Response(
            text="<h3>TeraBot Mini App is initializing...</h3>",
            content_type="text/html",
            headers=make_cors_headers(),
        )
    return web.FileResponse(index_file, headers=make_cors_headers())


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint for cloud platforms."""
    return web.json_response(
        {"status": "ok", "service": "terabot-miniapp"},
        headers=make_cors_headers(),
    )


async def handle_explore(request: web.Request) -> web.Response:
    """
    Resolve any TeraBox or Mega URL directly from the WebApp.
    Returns complete file hierarchy and registers a live web session.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400, headers=make_cors_headers())

    url = (body.get("url") or "").strip()
    if not url:
        return web.json_response({"ok": False, "error": "URL parameter is required"}, status=400, headers=make_cors_headers())

    session_id = uuid.uuid4().hex[:8]

    # 1. Mega Detection & Resolution
    from core.mega_downloader import is_mega_url, is_mega_folder_url, inspect_mega_folder
    if is_mega_url(url):
        if not is_mega_folder_url(url):
            return web.json_response(
                {"ok": False, "error": "Single Mega file links are downloaded directly. Please provide a Mega folder link or use the Telegram bot."},
                status=400,
                headers=make_cors_headers(),
            )
        try:
            folder_info = await inspect_mega_folder(url)
            if not folder_info or folder_info.get("total_files", 0) == 0:
                return web.json_response(
                    {"ok": False, "error": "Could not inspect Mega folder or folder is empty/private."},
                    status=400,
                    headers=make_cors_headers(),
                )

            serialized_dirs, root_h, total_size, title = serialize_mega_dirs(folder_info)
            _web_sessions[session_id] = {
                "session_id": session_id,
                "service": "mega",
                "url": url,
                "folder_info": folder_info,
                "dirs": serialized_dirs,
                "root_h": root_h,
                "title": title,
                "total_files": sum(len(d["files"]) for d in serialized_dirs.values()),
                "total_size": total_size,
                "formatted_total_size": format_bytes(total_size),
                "created_at": time.time(),
            }

            return web.json_response({
                "ok": True,
                "session_id": session_id,
                "service": "mega",
                "root_h": root_h,
                "title": title,
                "total_files": _web_sessions[session_id]["total_files"],
                "total_size": total_size,
                "formatted_total_size": format_bytes(total_size),
                "dirs": serialized_dirs,
            }, headers=make_cors_headers())
        except Exception as e:
            logger.error(f"Error exploring Mega folder: {e}", exc_info=True)
            return web.json_response({"ok": False, "error": f"Mega inspection failed: {str(e)}"}, status=500, headers=make_cors_headers())

    # 2. TeraBox Detection & Resolution
    try:
        from core.resolver import TeraBoxResolver
        resolver = TeraBoxResolver()
        files = await resolver.resolve(url)
        if not files:
            return web.json_response(
                {"ok": False, "error": "No files found in TeraBox share or link expired."},
                status=400,
                headers=make_cors_headers(),
            )

        serialized_dirs, root_h, total_size, title = serialize_terabox_files(files)
        _web_sessions[session_id] = {
            "session_id": session_id,
            "service": "terabox",
            "url": url,
            "files": files,
            "dirs": serialized_dirs,
            "root_h": root_h,
            "title": title,
            "total_files": len(files),
            "total_size": total_size,
            "formatted_total_size": format_bytes(total_size),
            "created_at": time.time(),
        }

        return web.json_response({
            "ok": True,
            "session_id": session_id,
            "service": "terabox",
            "root_h": root_h,
            "title": title,
            "total_files": len(files),
            "total_size": total_size,
            "formatted_total_size": format_bytes(total_size),
            "dirs": serialized_dirs,
        }, headers=make_cors_headers())
    except Exception as e:
        logger.error(f"Error exploring TeraBox link: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": f"TeraBox resolution failed: {str(e)}"}, status=500, headers=make_cors_headers())


async def handle_direct_links(request: web.Request) -> web.Response:
    """Retrieve direct download links for selected items in a session."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400, headers=make_cors_headers())

    session_id = body.get("session_id", "").strip()
    selected_handles = set(str(h) for h in body.get("selected_handles", []))

    sess = _web_sessions.get(session_id)
    if not sess:
        return web.json_response({"ok": False, "error": "Session expired or not found."}, status=404, headers=make_cors_headers())

    links = []
    if sess.get("service") == "terabox":
        for d in sess.get("dirs", {}).values():
            for f in d.get("files", []):
                if str(f.get("h")) in selected_handles:
                    links.append({
                        "h": f.get("h"),
                        "name": f.get("name"),
                        "size": f.get("size"),
                        "formatted_size": f.get("formatted_size"),
                        "dlink": f.get("dlink"),
                        "thumb": f.get("thumb"),
                        "type": f.get("type"),
                    })

    return web.json_response({"ok": True, "service": sess.get("service"), "links": links}, headers=make_cors_headers())


async def handle_get_session(request: web.Request) -> web.Response:
    """Return JSON tree structure of a session for the Mini App."""
    session_id = request.match_info.get("session_id", "").strip()
    if not session_id:
        return web.json_response(
            {"ok": False, "error": "Missing session_id"},
            status=400,
            headers=make_cors_headers(),
        )

    # 1. Check in-memory web sessions (TeraBox & web-initiated Mega)
    if session_id in _web_sessions:
        sess_data = _web_sessions[session_id]
        return web.json_response({
            "ok": True,
            "session_id": session_id,
            "service": sess_data.get("service", "cloud"),
            "root_h": sess_data.get("root_h", "root"),
            "title": sess_data.get("title", "Cloud Share"),
            "total_files": sess_data.get("total_files", 0),
            "total_size": sess_data.get("total_size", 0),
            "formatted_total_size": sess_data.get("formatted_total_size", "0 B"),
            "dirs": sess_data.get("dirs", {}),
        }, headers=make_cors_headers())

    # 2. Check Mega Explorer Manager
    from core.mega_downloader import mega_mgr
    sess = mega_mgr.get_session(session_id)
    if not sess:
        return web.json_response(
            {"ok": False, "error": "Session expired or not found. Please resend the link to the bot."},
            status=404,
            headers=make_cors_headers(),
        )

    serialized_dirs, root_h, total_share_size, title = serialize_mega_dirs(sess.folder_info)

    data = {
        "ok": True,
        "session_id": session_id,
        "service": "mega",
        "root_h": root_h,
        "title": title,
        "total_files": sum(len(d["files"]) for d in serialized_dirs.values()),
        "total_size": total_share_size,
        "formatted_total_size": format_bytes(total_share_size),
        "dirs": serialized_dirs,
    }
    return web.json_response(data, headers=make_cors_headers())


async def handle_post_download(request: web.Request) -> web.Response:
    """Handle download selection submitted from the Mini App."""
    session_id = request.match_info.get("session_id", "").strip()
    if not session_id:
        return web.json_response(
            {"ok": False, "error": "Missing session_id"},
            status=400,
            headers=make_cors_headers(),
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "Invalid JSON body"},
            status=400,
            headers=make_cors_headers(),
        )

    selected_handles = body.get("selected_handles", [])
    if not selected_handles or not isinstance(selected_handles, list):
        return web.json_response(
            {"ok": False, "error": "No files selected."},
            status=400,
            headers=make_cors_headers(),
        )

    # 1. Check if there is an active prompt in task_mgr (TeraBox prompt)
    from core.task_manager import task_mgr
    prompt_info = task_mgr.get_prompt(session_id)
    if prompt_info:
        owner_id, future = prompt_info
        if not future.done():
            task_mgr.resolve_prompt(session_id, owner_id, ("multi", selected_handles))
            logger.info(f"Mini App: user {owner_id} selected {len(selected_handles)} files for TeraBox session {session_id}")
            return web.json_response(
                {"ok": True, "count": len(selected_handles), "service": "terabox"},
                headers=make_cors_headers(),
            )
        else:
            return web.json_response(
                {"ok": False, "error": "This session is already processing or expired."},
                status=409,
                headers=make_cors_headers(),
            )

    # 2. Check Mega Explorer Manager
    from core.mega_downloader import mega_mgr
    sess = mega_mgr.get_session(session_id)
    if sess:
        if not sess.future.done():
            sess.future.set_result(("multi", selected_handles))
            logger.info(f"Mini App: user {sess.user_id} selected {len(selected_handles)} files for Mega session {session_id}")
            return web.json_response(
                {"ok": True, "count": len(selected_handles), "service": "mega"},
                headers=make_cors_headers(),
            )
        else:
            return web.json_response(
                {"ok": False, "error": "This session is already processing or expired."},
                status=409,
                headers=make_cors_headers(),
            )

    # 3. Check Web Sessions (standalone web explore)
    if session_id in _web_sessions:
        return web.json_response(
            {"ok": True, "count": len(selected_handles), "service": _web_sessions[session_id].get("service")},
            headers=make_cors_headers(),
        )

    return web.json_response(
        {"ok": False, "error": "Session expired or not found."},
        status=404,
        headers=make_cors_headers(),
    )


async def ensure_colab_tunnel(port: int) -> Optional[str]:
    """If running in Google Colab or remote Linux environment without API_BASE_URL, spawn free cloudflared tunnel."""
    if config.API_BASE_URL and "trycloudflare" not in config.API_BASE_URL:
        return config.API_BASE_URL

    cf_bin = shutil.which("cloudflared")
    if not cf_bin:
        logger.info("📦 Checking/installing cloudflared for public Telegram Mini App tunnel...")
        try:
            cmd = "wget -q -nc https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"
            proc = await asyncio.create_subprocess_shell(cmd)
            await proc.communicate()
            cf_bin = "/usr/local/bin/cloudflared" if os.path.exists("/usr/local/bin/cloudflared") else None
        except Exception as e:
            logger.debug(f"Could not install cloudflared: {e}")
            return None

    if not cf_bin or not os.path.exists(cf_bin):
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            cf_bin, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read_tunnel_url():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace")
                m = re.search(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com", decoded)
                if m:
                    return m.group(0)
            return None

        tunnel_url = await asyncio.wait_for(read_tunnel_url(), timeout=18)
        if tunnel_url:
            config.API_BASE_URL = tunnel_url
            logger.info(f"🌐 Cloudflare Tunnel Online: {tunnel_url}")
            if config.WEBAPP_URL:
                sep = "&" if ("?" in config.WEBAPP_URL or "github.io" in config.WEBAPP_URL) else "/webapp?"
                full_app_url = f"{config.WEBAPP_URL.rstrip('/')}{sep}api_base={tunnel_url}"
                logger.info(f"📱 Telegram Mini App Link: {full_app_url}")
            return tunnel_url
    except Exception as e:
        logger.debug(f"Tunnel creation notice: {e}")
    return None


async def start_webapp_server():
    """Start the aiohttp web server hosting the Telegram Mini App & API."""
    global _server_runner
    if _server_runner is not None:
        return

    port = config.WEBAPP_PORT
    app = web.Application()

    # Preflight CORS
    app.router.add_route("OPTIONS", "/{tail:.*}", cors_preflight)

    # WebApp Static and Root
    app.router.add_get("/", handle_index)
    app.router.add_get("/webapp", handle_index)
    app.router.add_get("/health", handle_health)

    # Mini App API
    app.router.add_post("/api/explore", handle_explore)
    app.router.add_post("/api/direct_links", handle_direct_links)
    app.router.add_get("/api/session/{session_id}", handle_get_session)
    app.router.add_post("/api/session/{session_id}/download", handle_post_download)

    # Static assets (Direct and Prefixed)
    if WEBAPP_DIR.exists():
        css_file = WEBAPP_DIR / "style.css"
        js_file = WEBAPP_DIR / "app.js"
        if css_file.exists():
            app.router.add_get("/style.css", lambda req: web.FileResponse(css_file, headers=make_cors_headers()))
            app.router.add_get("/webapp/style.css", lambda req: web.FileResponse(css_file, headers=make_cors_headers()))
        if js_file.exists():
            app.router.add_get("/app.js", lambda req: web.FileResponse(js_file, headers=make_cors_headers()))
            app.router.add_get("/webapp/app.js", lambda req: web.FileResponse(js_file, headers=make_cors_headers()))
        app.router.add_static("/static/", path=WEBAPP_DIR, name="static")

    _server_runner = web.AppRunner(app)
    await _server_runner.setup()
    site = web.TCPSite(_server_runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🚀 Telegram Mini App server running at http://0.0.0.0:{port} (Public URL: {config.WEBAPP_URL or 'not configured'})")

    # Auto-spawn public tunnel if running on Google Colab or Linux without explicit API_BASE_URL
    if os.path.exists("/content") or (sys.platform.startswith("linux") and not config.API_BASE_URL):
        asyncio.create_task(ensure_colab_tunnel(port))


async def stop_webapp_server():
    """Gracefully terminate web app server on bot shutdown."""
    global _server_runner
    if _server_runner is not None:
        try:
            await _server_runner.cleanup()
            logger.info("Telegram Mini App server shut down gracefully.")
        except Exception as e:
            logger.debug(f"Error closing Mini App server: {e}")
        _server_runner = None
