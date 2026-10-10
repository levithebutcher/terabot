import re
from urllib.parse import urlparse, parse_qs

# Comprehensive list of TeraBox and mirror domains
SUPPORTED_DOMAINS = [
    "terabox.com",
    "terabox.app",
    "terabox.fun",
    "terabox.best",
    "terabox.ap",
    "terabox.club",
    "terabox.click",
    "terabox.me",
    "terabox.site",
    "terabox.pro",
    "terabox.xyz",
    "teraboxapp.com",
    "teraboxlink.com",
    "teraboxlinke.com",
    "teraboxshare.com",
    "teraboxsharefile.com",
    "teraboxurl.com",
    "teraboxfree.com",
    "terasharelink.com",
    "terasharefile.com",
    "terafileshare.com",
    "terasharedrive.com",
    "1024tera.com",
    "1024tera.co",
    "1024terabox.com",
    "1024-terabox.com",
    "tera1024box.com",
    "1024box.com",
    "4funbox.com",
    "4funbox.co",
    "mirrobox.com",
    "nephobox.com",
    "freeterabox.com",
    "momerybox.com",
    "tibibox.com",
    "gibibox.com",
    "pebibox.com",
    "dubox.com",
    "bestclouddrive.com",
]

MEGA_DOMAINS = [
    "mega.nz",
    "mega.co.nz",
    "mega.io",
]

ALL_SUPPORTED_DOMAINS = SUPPORTED_DOMAINS + MEGA_DOMAINS
DOMAIN_PATTERN = "|".join(re.escape(d) for d in ALL_SUPPORTED_DOMAINS)
URL_REGEX = re.compile(rf"https?://(?:[a-zA-Z0-9-]+\.)*(?:{DOMAIN_PATTERN})[^\s<>'\"\)]*", re.IGNORECASE)


def is_terabox_url(url: str) -> bool:
    """Check if the given URL belongs to a known TeraBox domain."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        return any(netloc == domain or netloc.endswith("." + domain) for domain in SUPPORTED_DOMAINS)
    except Exception:
        return False


def is_supported_url(url: str) -> bool:
    """Check if the given URL belongs to a supported TeraBox or Mega domain."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        return any(netloc == domain or netloc.endswith("." + domain) for domain in ALL_SUPPORTED_DOMAINS)
    except Exception:
        return False


def extract_urls(text: str) -> list[str]:
    """Find all valid TeraBox and Mega URLs inside a message text."""
    if not text:
        return []
    matches = URL_REGEX.findall(text)
    # Strip trailing punctuation marks
    cleaned = [m.rstrip(".,;:!?)]}") for m in matches]
    return [url for url in cleaned if is_supported_url(url)]


def extract_surl(url: str) -> str | None:
    """Extract the surl / key from any TeraBox share link format."""
    try:
        parsed = urlparse(url)
        # Check query parameters first (?surl=XYZ or ?surl=1XYZ)
        query = parse_qs(parsed.query)
        if "surl" in query and query["surl"]:
            surl = query["surl"][0]
            return surl

        # Check path: e.g. /s/1XYZ or /s/XYZ or /sharing/link?surl=...
        parts = [p for p in parsed.path.split("/") if p]
        if "s" in parts:
            idx = parts.index("s")
            if idx + 1 < len(parts):
                return parts[idx + 1]

        # Check /sharing/link or /wap/share/...
        if parts:
            last = parts[-1]
            if len(last) > 8 and not last.endswith((".html", ".php")):
                return last

        return None
    except Exception:
        return None


def format_bytes(size: int | float) -> str:
    """Format bytes into a human-readable string (KB, MB, GB)."""
    try:
        size = float(size)
    except (ValueError, TypeError):
        return "0 B"

    if size < 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB"]
    unit_idx = 0
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1

    if unit_idx == 0:
        return f"{int(size)} {units[unit_idx]}"
    return f"{size:.2f} {units[unit_idx]}"


def format_duration(seconds: int | float) -> str:
    """Format duration in seconds to HH:MM:SS or MM:SS."""
    try:
        sec = int(seconds)
    except (ValueError, TypeError):
        return "00:00"

    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent directory traversal and invalid Windows characters."""
    if not name:
        return "unnamed_file"
    # Replace characters invalid in Windows paths: < > : " / \ | ? *
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    cleaned = cleaned.strip(". ")
    return cleaned or "unnamed_file"
