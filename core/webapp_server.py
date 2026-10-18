import json
from pathlib import Path
from typing import Optional

from aiohttp import web

import config
from utils.helpers import format_bytes
from utils.logger import logger

# Path to static web assets
WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"

_server_runner: Optional[web.AppRunner] = None


def make_cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
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


async def handle_get_session(request: web.Request) -> web.Response:
    """Return JSON tree structure of a Mega session for the Mini App."""
    session_id = request.match_info.get("session_id", "").strip()
    if not session_id:
        return web.json_response(
            {"ok": False, "error": "Missing session_id"},
            status=400,
            headers=make_cors_headers(),
        )

    # Import locally to prevent circular import
    from core.mega_downloader import mega_mgr

    sess = mega_mgr.get_session(session_id)
    if not sess:
        return web.json_response(
            {"ok": False, "error": "Session expired or not found. Please resend the link to the bot."},
            status=404,
            headers=make_cors_headers(),
        )

    folder_info = sess.folder_info
    root_h = folder_info.get("root_h", "")
    dirs = folder_info.get("dirs", {})

    # Build clean serializable directory and file structure
    serialized_dirs = {}
    for dh, d in dirs.items():
        direct_files = []
        raw_files = d.get("direct_videos", []) + d.get("direct_photos", []) + d.get("direct_others", [])
        for f in raw_files:
            fname = f.get("name", "unnamed")
            ext = ("." + fname.split(".")[-1].lower()) if "." in fname else ""
            fsize = f.get("size", 0)
            
            # Classify type
            from core.media import VIDEO_EXTENSIONS, IMAGE_EXTENSIONS
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
    # Or get from root_dir if accurate
    if root_dir.get("total_size", 0) > 0:
        total_share_size = root_dir.get("total_size", 0)

    data = {
        "ok": True,
        "session_id": session_id,
        "root_h": root_h,
        "title": root_dir.get("name", "Cloud Share"),
        "total_files": root_dir.get("total_files", 0),
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

    from core.mega_downloader import mega_mgr

    sess = mega_mgr.get_session(session_id)
    if not sess:
        return web.json_response(
            {"ok": False, "error": "Session expired or not found."},
            status=404,
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

    # Deliver selection to the waiting session future
    if not sess.future.done():
        sess.future.set_result(("multi", selected_handles))
        logger.info(f"Mini App: user {sess.user_id} selected {len(selected_handles)} files for session {session_id}")
        return web.json_response(
            {"ok": True, "count": len(selected_handles)},
            headers=make_cors_headers(),
        )
    else:
        return web.json_response(
            {"ok": False, "error": "This session is already processing or expired."},
            status=409,
            headers=make_cors_headers(),
        )


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
