import asyncio
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from curl_cffi.requests import AsyncSession

from config import TERABOX_COOKIE, FALLBACK_API_URL, FALLBACK_API_KEY, PROXY_URL
from utils.helpers import extract_surl, format_bytes, sanitize_filename
from utils.logger import logger


@dataclass
class TeraFile:
    file_name: str
    size: int
    size_readable: str
    dlink: str
    fs_id: str
    thumb: Optional[str] = None
    is_dir: bool = False
    path: str = ""
    duration: int = 0
    width: int = 0
    height: int = 0


class ResolverError(Exception):
    """Base exception for TeraBox resolver errors."""
    pass


class CookieExpiredError(ResolverError):
    """Raised when the TERABOX_COOKIE is invalid, expired, or triggers verify_v2."""
    pass


class ShareLinkExpiredError(ResolverError):
    """Raised when the TeraBox share link has expired or files were removed."""
    pass


class CloudflareBlockedError(ResolverError):
    """Raised when Cloudflare blocks or challenges the request."""
    pass


class TeraBoxResolver:
    """
    High-performance native resolver for TeraBox links using Chrome TLS
    impersonation via curl_cffi to bypass Cloudflare challenges and anti-bot checks.
    Supports folder recursion, pagination, and optional fallback API.
    """

    PRIMARY_DOMAINS = [
        "www.terabox.app",
        "terabox.app",
        "www.1024tera.com",
        "1024tera.com",
        "www.tibibox.com",
        "www.freeterabox.com",
        "www.teraboxapp.com",
        "dm.terabox.app",
        "terabox.com",
    ]

    def __init__(self, cookie: Optional[str] = None):
        self.cookie = cookie if cookie is not None else TERABOX_COOKIE

    def _wrap_url(self, target_url: str) -> str:
        """If PROXY_URL (Cloudflare Worker) is configured, wrap request through worker."""
        if not PROXY_URL:
            return target_url
        from urllib.parse import quote
        return f"{PROXY_URL}/?target_url={quote(target_url, safe='')}"

    def _get_cookie_dict(self) -> dict:
        """Parse raw cookie header string into a dictionary."""
        cookies = {}
        if not self.cookie:
            return cookies
        for part in self.cookie.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
        return cookies

    def _get_headers(self, referer: Optional[str] = None) -> dict:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "DNT": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if referer:
            headers["Referer"] = referer
        return headers

    def _clean_surl(self, raw_surl: str) -> str:
        """Strip leading '1' prefix from surl if present."""
        if raw_surl.startswith("1") and len(raw_surl) > 1:
            return raw_surl[1:]
        return raw_surl

    async def _extract_tokens_from_page(self, session: AsyncSession, surl: str) -> tuple[str, str, str, str]:
        """
        Fetch the share page across available domains using Chrome TLS impersonation
        and extract jsToken, dp-logid, and bdstoken.
        Returns: (js_token, dp_logid, bdstoken, final_domain)
        """
        clean_surl = self._clean_surl(surl)
        surl_variants = [clean_surl] if clean_surl == surl else [clean_surl, surl]

        for domain in self.PRIMARY_DOMAINS:
            for s_var in surl_variants:
                target_url = f"https://{domain}/sharing/link?surl={s_var}"
                fetch_url = self._wrap_url(target_url)
                headers = self._get_headers()

                try:
                    resp = await session.get(fetch_url, headers=headers, timeout=15)

                    if resp.status_code in (403, 503):
                        if "Just a moment" in resp.text or "Cloudflare" in resp.text:
                            logger.warning(f"Domain {domain} returned Cloudflare challenge. Trying next domain...")
                            continue

                    if resp.status_code != 200:
                        continue

                    html = resp.text

                    # Check for Cloudflare challenge in 200 responses
                    if "Just a moment..." in html or "cf-browser-verification" in html:
                        logger.warning(f"Domain {domain} rendered Cloudflare challenge page.")
                        continue

                    # Extract jsToken
                    js_token = None
                    m_js = re.search(r'fn%28%22(.*?)%22%29', html)
                    if m_js:
                        js_token = m_js.group(1)
                    else:
                        m_js2 = re.search(r'["\']jsToken["\']\s*[:=]\s*["\']([^"\']+)["\']', html)
                        if m_js2:
                            js_token = m_js2.group(1)

                    # Extract dp-logid
                    m_logid = re.search(r'dp-logid=([^&"\'\s]+)', html)
                    dp_logid = m_logid.group(1) if m_logid else ""

                    # Extract bdstoken
                    m_bds = re.search(r'bdstoken["\']?\s*[:=]\s*["\']([^"\']+)["\']', html)
                    bdstoken = m_bds.group(1) if m_bds else ""

                    if js_token:
                        return js_token, dp_logid, bdstoken, str(resp.url)

                except Exception as e:
                    logger.debug(f"Error fetching page from {domain} ({target_url}): {e}")
                    continue

        raise CloudflareBlockedError(
            "Failed to retrieve jsToken from all primary TeraBox domains. "
            "The domains may be temporarily rate-limited or blocked by Cloudflare."
        )

    async def _fetch_share_list(
        self,
        session: AsyncSession,
        canonical_url: str,
        shorturl: str,
        js_token: str,
        dp_logid: str,
        dir_path: Optional[str] = None,
        page: int = 1,
        num: int = 100,
    ) -> dict:
        """Call the /share/list API endpoint."""
        # Always query the unblocked primary API host
        api_url = "https://www.terabox.app/share/list"

        params = {
            "app_id": "250528",
            "web": "1",
            "channel": "dubox",
            "clienttype": "0",
            "jsToken": js_token,
            "page": str(page),
            "num": str(num),
            "by": "name",
            "order": "asc",
            "site_referer": canonical_url,
            "shorturl": shorturl,
        }

        if dir_path:
            params["dir"] = dir_path
            params["root"] = "0"
        else:
            params["root"] = "1"

        headers = self._get_headers(referer=canonical_url)
        headers.update({
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        })

        fetch_api_url = self._wrap_url(api_url)
        try:
            resp = await session.get(fetch_api_url, params=params, headers=headers, timeout=15)
        except Exception as e:
            raise ResolverError(f"Network error querying /share/list: {e}")

        if resp.status_code != 200:
            raise ResolverError(f"HTTP error {resp.status_code} while querying /share/list: {resp.text[:150]}")

        try:
            data = resp.json()
        except Exception as e:
            raise ResolverError(f"Invalid JSON returned by /share/list: {e}. Body: {resp.text[:150]}")

        errno = data.get("errno")
        errmsg = data.get("errmsg", "")

        # Check specific TeraBox error codes
        if errno in (4000020, 400210, 400141) or "need verify" in str(errmsg).lower():
            raise CookieExpiredError(
                f"TeraBox session authentication failed (errno {errno}: {errmsg}). "
                "Your TERABOX_COOKIE (ndus token) is either missing, invalid, or requires re-verification. "
                "Please update TERABOX_COOKIE in .env with a fresh browser cookie."
            )
        if errno in (-7, 110, 140, 105) or "share does not exist" in str(errmsg).lower():
            raise ShareLinkExpiredError(
                f"The TeraBox share link has expired or files have been deleted by the owner (errno {errno})."
            )
        if errno == -4:
            raise ShareLinkExpiredError(
                f"This TeraBox link is not accessible [errno -4]. "
                "The share may be private, region-restricted, or the resource no longer exists."
            )
        if errno != 0:
            raise ResolverError(f"TeraBox API returned error (errno {errno}): {errmsg}")

        return data

    async def _resolve_folder_recursive(
        self,
        session: AsyncSession,
        canonical_url: str,
        shorturl: str,
        js_token: str,
        dp_logid: str,
        dir_path: Optional[str] = None,
    ) -> list[TeraFile]:
        """Recursively retrieve all files from folders and paginate if necessary."""
        all_files: list[TeraFile] = []
        page = 1
        page_size = 100

        while True:
            data = await self._fetch_share_list(
                session=session,
                canonical_url=canonical_url,
                shorturl=shorturl,
                js_token=js_token,
                dp_logid=dp_logid,
                dir_path=dir_path,
                page=page,
                num=page_size,
            )

            items = data.get("list", [])
            if not items:
                break

            for item in items:
                is_directory = str(item.get("isdir", "0")) == "1"
                item_path = item.get("path", "")

                if is_directory:
                    # Recursively traverse directory
                    sub_files = await self._resolve_folder_recursive(
                        session=session,
                        canonical_url=canonical_url,
                        shorturl=shorturl,
                        js_token=js_token,
                        dp_logid=dp_logid,
                        dir_path=item_path,
                    )
                    all_files.extend(sub_files)
                else:
                    # Parse file
                    size_bytes = int(item.get("size", 0))
                    thumbs = item.get("thumbs", {})
                    thumb_url = (
                        thumbs.get("url3")
                        or thumbs.get("url2")
                        or thumbs.get("url1")
                        or thumbs.get("icon")
                    )

                    file_obj = TeraFile(
                        file_name=sanitize_filename(item.get("server_filename", "unnamed_file")),
                        size=size_bytes,
                        size_readable=format_bytes(size_bytes),
                        dlink=item.get("dlink", ""),
                        fs_id=str(item.get("fs_id", "")),
                        thumb=thumb_url,
                        is_dir=False,
                        path=item_path,
                        duration=int(item.get("duration", 0)),
                        width=int(item.get("width", 0)),
                        height=int(item.get("height", 0)),
                    )
                    all_files.append(file_obj)

            # Check if there are more pages
            if len(items) < page_size:
                break
            page += 1

        return all_files

    async def _resolve_fallback_api(self, url: str) -> list[TeraFile]:
        """Query fallback external API if configured."""
        if not FALLBACK_API_URL:
            raise ResolverError("No fallback resolver API configured in FALLBACK_API_URL.")

        logger.info(f"Invoking fallback resolver API for {url[:50]}...")
        headers = {"User-Agent": "Mozilla/5.0"}
        if FALLBACK_API_KEY:
            headers["Authorization"] = f"Bearer {FALLBACK_API_KEY}"

        async with aiohttp.ClientSession() as session:
            sep = "&" if "?" in FALLBACK_API_URL else "?"
            api_endpoint = f"{FALLBACK_API_URL}{sep}url={url}"
            async with session.get(api_endpoint, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ResolverError(f"Fallback API HTTP {resp.status}: {text[:100]}")

                data = await resp.json()
                file_list = data.get("files") or data.get("list") or [data] if "dlink" in data or "download_url" in data else []
                if not file_list:
                    raise ResolverError("Fallback API returned no files.")

                results = []
                for f in file_list:
                    dl = f.get("download_url") or f.get("dlink") or f.get("link", "")
                    name = sanitize_filename(f.get("filename") or f.get("server_filename") or f.get("file_name", "file"))
                    size_b = int(f.get("size", 0) or f.get("size_bytes", 0))
                    results.append(
                        TeraFile(
                            file_name=name,
                            size=size_b,
                            size_readable=format_bytes(size_b),
                            dlink=dl,
                            fs_id=str(f.get("fs_id", "")),
                            thumb=f.get("thumb") or f.get("thumbnail"),
                        )
                    )
                return results

    async def resolve(self, url: str) -> list[TeraFile]:
        """
        Primary entry point to resolve a TeraBox link.
        Returns a list of TeraFile objects.
        """
        surl = extract_surl(url)
        if not surl:
            raise ResolverError(f"Could not extract a valid TeraBox share key (surl) from: {url}")

        clean_surl = self._clean_surl(surl)
        cookie_dict = self._get_cookie_dict()

        try:
            async with AsyncSession(impersonate="chrome124", cookies=cookie_dict) as session:
                js_token, dp_logid, bdstoken, canonical_url = await self._extract_tokens_from_page(session, surl)
                files = await self._resolve_folder_recursive(
                    session=session,
                    canonical_url=canonical_url,
                    shorturl=clean_surl,
                    js_token=js_token,
                    dp_logid=dp_logid,
                )

                if not files:
                    raise ResolverError("No files found in the shared TeraBox link.")

                # Check if dlink is present
                missing_dlink = [f for f in files if not f.dlink]
                if missing_dlink:
                    if not self.cookie or "ndus" not in self.cookie:
                        raise CookieExpiredError(
                            f"Retrieved {len(files)} file metadata items, but direct download links (dlink) are missing. "
                            "TeraBox requires a valid account session cookie (ndus). Please set TERABOX_COOKIE in .env."
                        )
                    else:
                        raise CookieExpiredError(
                            f"Direct download link missing for file(s). Your TERABOX_COOKIE (ndus token) appears expired "
                            "or restricted. Please update TERABOX_COOKIE in .env with a fresh browser cookie."
                        )

                return files

        except (CloudflareBlockedError, ShareLinkExpiredError, ResolverError) as e:
            # If primary resolver fails and fallback is set, attempt fallback
            if FALLBACK_API_URL:
                logger.warning(f"Primary resolver failed ({e}). Attempting fallback API...")
                try:
                    return await self._resolve_fallback_api(url)
                except Exception as fb_err:
                    logger.error(f"Fallback API also failed: {fb_err}")
                    raise ResolverError(f"Primary resolver failed: {e}. Fallback also failed: {fb_err}") from fb_err
            raise
