from __future__ import annotations
import argparse
import csv
import gzip
import hashlib
import html
import json
import struct
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def _enable_windows_ansi() -> None:
    if os.name == "nt":
        try:
            os.system("")
        except Exception:
            pass


_enable_windows_ansi()

_FORCE_COLOR = os.environ.get("FORCE_COLOR", "").lower() not in {"", "0", "false", "no"}
_NO_COLOR = "NO_COLOR" in os.environ
_USE_COLOR = (sys.stdout.isatty() or _FORCE_COLOR) and not _NO_COLOR

class UI:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    PINK = "\033[38;2;241;174;214m"
    PURPLE = "\033[38;2;117;113;170m"
    MINT = "\033[38;2;126;232;211m"
    GREEN = "\033[38;2;156;230;161m"
    YELLOW = "\033[38;2;241;214;84m"
    GOLD = "\033[38;2;246;218;150m"
    BLUEGREY = "\033[38;2;178;184;207m"
    WHITE = "\033[38;2;222;225;239m"
    RED = "\033[38;2;244;123;133m"


def style(text: Any, *codes: str) -> str:
    value = str(text)
    if not _USE_COLOR or not codes:
        return value
    return "".join(codes) + value + UI.RESET


def terminal_width() -> int:
    return max(60, min(110, shutil.get_terminal_size((86, 24)).columns))


def format_hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def ui_divider(title: str, *, top_pad: bool = True) -> None:
    width = terminal_width()
    label = f" {title} "
    side = max(2, (width - len(label)) // 2)
    left = "─" * side
    right = "─" * max(2, width - side - len(label))
    if top_pad:
        print()
    print(
        style(left, UI.PURPLE, UI.DIM)
        + style(label, UI.PINK, UI.BOLD)
        + style(right, UI.PURPLE, UI.DIM),
        flush=True,
    )


def ui_tree(text: str, *, last: bool = False, indent: int = 0, tone: str = "normal") -> None:
    branch = "└─" if last else "├─"
    prefix = "│  " * max(0, indent) + branch + " "
    color = {
        "good": UI.MINT,
        "warn": UI.YELLOW,
        "bad": UI.RED,
        "accent": UI.PINK,
        "dim": UI.BLUEGREY,
    }.get(tone, UI.WHITE)
    print(style(prefix, UI.PURPLE) + style(text, color), flush=True)


def ui_kv(key: str, value: Any, *, last: bool = False, indent: int = 0) -> None:
    branch = "└─" if last else "├─"
    prefix = "│  " * max(0, indent) + branch + " "
    print(
        style(prefix, UI.PURPLE)
        + style(f"{key}: ", UI.BLUEGREY)
        + style(value, UI.WHITE, UI.BOLD),
        flush=True,
    )


def ui_banner(action: str, locale: str, country: str, output_root: Path) -> None:
    ui_divider("Panini FIFA Collection Downloader")
    print(style("  Session", UI.MINT, UI.BOLD))
    ui_kv("Action", action)
    ui_kv("Locale", locale)
    ui_kv("Country", country)
    ui_kv("Output", output_root, last=True)


def ui_summary(title: str, rows: Sequence[Tuple[str, Any]], *, elapsed: Optional[float] = None) -> None:
    ui_divider(title)
    for i, (key, value) in enumerate(rows):
        ui_kv(key, value, last=(i == len(rows) - 1 and elapsed is None))
    if elapsed is not None:
        ui_tree(
            "Processed in " + style(format_elapsed(elapsed), UI.YELLOW, UI.BOLD),
            last=True,
            tone="dim",
        )


def _speed_text(done: int, started: float) -> str:
    elapsed = max(0.001, time.monotonic() - started)
    speed = done / elapsed
    if speed >= 1024 * 1024:
        return f"{speed / 1024 / 1024:.1f} MB/s"
    if speed >= 1024:
        return f"{speed / 1024:.0f} KB/s"
    return f"{speed:.0f} B/s"


def print_download_progress(done: int, total: int, started: float, force: bool = False) -> None:
    width = terminal_width()
    progress_width = max(16, min(44, width - 42))
    elapsed = max(0.001, time.monotonic() - started)

    if total > 0:
        ratio = min(1.0, max(0.0, done / total))
        pct = int(ratio * 100)
        filled = min(progress_width, int(ratio * progress_width))
        if done >= total:
            filled = progress_width
            pct = 100
            eta = 0.0
        else:
            speed = done / elapsed if done else 0.0
            eta = (total - done) / speed if speed > 0 else 0.0
        bar = "━" * filled + "·" * (progress_width - filled)
        text = (
            style("   └─ ", UI.PURPLE)
            + style(bar, UI.GREEN)
            + "  "
            + style(f"{pct:3d}%", UI.MINT, UI.BOLD)
            + style("  •  ", UI.BLUEGREY)
            + style(format_hms(eta), UI.YELLOW, UI.BOLD)
            + style("  •  ", UI.BLUEGREY)
            + style(_speed_text(done, started), UI.GOLD)
        )
    else:
        pulse = int(elapsed * 5) % progress_width
        bar = "━" * pulse + "·" * (progress_width - pulse)
        text = (
            style("   └─ ", UI.PURPLE)
            + style(bar, UI.GREEN)
            + "  "
            + style(" --%", UI.MINT, UI.BOLD)
            + style("  •  ", UI.BLUEGREY)
            + style(format_hms(elapsed), UI.YELLOW, UI.BOLD)
        )

    print("\r\033[2K" + text, end="", flush=True)
    if force:
        print(flush=True)


def log(msg: str) -> None:
    if msg is None:
        return

    text = str(msg)
    if text.startswith("\n"):
        print()
        text = text.lstrip("\n")

    stripped = text.strip()
    if not stripped:
        return

    summary_match = re.fullmatch(r"===\s*(.*?)\s*===", stripped)
    if summary_match:
        ui_divider(summary_match.group(1), top_pad=False)
        return

    group_match = re.fullmatch(r"\[(\d+)/(\d+)\]\s+Group\s+(.+)", stripped)
    if group_match:
        current, total, group = group_match.groups()
        print()
        print(
            style(current, UI.MINT, UI.BOLD)
            + style(f"/{total}", UI.BLUEGREY)
            + style("  Group ", UI.BLUEGREY)
            + style(group, UI.WHITE, UI.BOLD),
            flush=True,
        )
        return

    prefixes = {
        "[BOOT]": ("◆", UI.PINK),
        "[DOWNLOAD]": ("↓", UI.PINK),
        "[SKIP]": ("↷", UI.YELLOW),
        "[WARN]": ("!", UI.YELLOW),
        "[ERROR]": ("×", UI.RED),
        "[WEBGL EXTRACTION]": ("◆", UI.MINT),
    }

    for prefix, (symbol, color) in prefixes.items():
        if stripped.startswith(prefix):
            body = stripped[len(prefix):].lstrip()
            print(
                style(f"{symbol} ", color, UI.BOLD)
                + style(body, color if prefix in {"[WARN]", "[ERROR]"} else UI.WHITE),
                flush=True,
            )
            return

    if stripped.startswith("[INFO]"):
        body = stripped[len("[INFO]"):].lstrip()
        important_info = (
            "Found ",
            "Config: ",
            "Querying status:",
            "WebGL contains ",
            "Trying official download:",
        )
        if body.startswith(important_info):
            print(style("• ", UI.MINT, UI.BOLD) + style(body, UI.WHITE), flush=True)
        return

    if stripped.startswith("[OK]"):
        body = stripped[len("[OK]"):].lstrip()

        if body.startswith("Downloaded:"):
            return

        important_ok = (
            "Guest session ready.",
            "Current manifest downloaded",
            "Current config downloaded",
            "Visual assets discovered from current JSON responses:",
            "Album metadata saved:",
            "Album visual asset saved:",
            "Official album response/file(s) saved:",
            "Raw UPDATE export:",
            "Result saved to:",
        )
        group_result = bool(re.match(r"^[A-Z0-9_-]+:\s+\d+\s+PNG records,", body))
        if body.startswith(important_ok) or group_result:
            print(style("✓ ", UI.MINT, UI.BOLD) + style(body, UI.WHITE), flush=True)
        return

    return


def safe_filename(value: str, max_len: int = 120) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    if not value:
        value = "unnamed"
    return value[:max_len]


def natural_key(text: str) -> Tuple[Any, ...]:
    parts = re.split(r"(\d+)", text or "")
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(block_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def make_session(extra_headers: Optional[Dict[str, str]] = None) -> requests.Session:
    browser_user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    )
    base_headers = {
        "User-Agent": browser_user_agent,
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(base_headers)
    if extra_headers:
        s.headers.update(extra_headers)
    return s


def html_headers(referer: Optional[str] = None) -> Dict[str, str]:
    browser_accept = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    )
    out = {
        "Accept": browser_accept,
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        out["Referer"] = referer
    return out


def unity_headers(origin: bool = False, ajax: bool = False) -> Dict[str, str]:
    base_url = "https://paninicollection.fifa.com"
    game_flash_url = f"{base_url}/game/flash?start_view=frontapp"
    unity_x_user_agent = "Unity/1.4.0 (Windows 10) Unity/6000.0.65f1 webgl_hires"
    out = {
        "X-User-Agent": unity_x_user_agent,
        "Accept": "*/*",
        "Referer": game_flash_url,
    }
    if origin:
        out["Origin"] = base_url
    if ajax:
        out["X-Requested-With"] = "XMLHttpRequest"
        out["Accept"] = "text/html, */*; q=0.01"
    return out


def extract_csrf(html_text: str) -> Tuple[str, str]:
    def meta(name: str) -> Optional[str]:
        patterns = [
            rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(name)}["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html_text, flags=re.I)
            if m:
                return html.unescape(m.group(1))
        return None

    param = meta("csrf-param") or "authenticity_token"
    token = meta("csrf-token")
    if not token:
        raise RuntimeError("CSRF token was not found in /launch.")
    return param, token


def mobile_common(locale: str) -> Dict[str, str]:
    client = "webgl_hires_webgl"
    platform = "WebGL"
    client_version = "1.4.0"
    return {
        "client": client,
        "locale": locale,
        "platform": platform,
        "version": client_version,
    }


def mobile_post(
    session: requests.Session,
    url: str,
    data: Dict[str, Any],
    timeout: int = 45,
) -> Any:
    r = session.post(
        url,
        data=data,
        headers={**unity_headers(origin=True), "Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def mobile_get(session: requests.Session, url: str, timeout: int = 45) -> Any:
    r = session.get(url, headers=unity_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def bootstrap_guest_session(
    session: requests.Session,
    locale: str,
    country: str,
    device_model: str = "Chrome 150.0.0.0",
) -> Dict[str, Any]:
    base_url = "https://paninicollection.fifa.com"
    launch_url = f"{base_url}/launch"
    guest_create_url = f"{base_url}/guest/create"
    game_flash_url = f"{base_url}/game/flash?start_view=frontapp"
    webgl_url = f"{base_url}/game/webgl"
    mobile_version_url = f"{base_url}/mobile/version.json"
    mobile_boot_url = f"{base_url}/mobile/boot.json"
    mobile_login_url = f"{base_url}/mobile/session/login_via_access_token.json"
    mobile_profile_status_url = f"{base_url}/mobile/user_profile/status.json"
    mobile_countries_url = f"{base_url}/mobile/countries.json"
    mobile_profile_update_url = f"{base_url}/mobile/user_profile/update.json"
    mobile_optins_status_url = f"{base_url}/mobile/optins/status.json"
    log("[BOOT] Starting guest session...")

    r0 = session.get(base_url + "/", headers=html_headers(), timeout=45, allow_redirects=True)
    r0.raise_for_status()
    launch_html = r0.text if "/launch" in urlparse(r0.url).path else ""
    if not launch_html or "csrf-token" not in launch_html:
        r0 = session.get(launch_url, headers=html_headers(), timeout=45)
        r0.raise_for_status()
        launch_html = r0.text
    csrf_param, csrf_token = extract_csrf(launch_html)
    log("[OK] Initial CSRF token and cookies obtained.")

    create_data = {"_method": "POST", csrf_param: csrf_token}
    r1 = session.post(
        guest_create_url,
        data=create_data,
        headers={**html_headers(launch_url), "Origin": base_url, "Cache-Control": "max-age=0"},
        timeout=45,
        allow_redirects=True,
    )
    r1.raise_for_status()
    access_cookie = session.cookies.get("panstwc_access_token")
    if not access_cookie:
        raise RuntimeError("The server did not provide panstwc_access_token when creating the guest.")
    log("[OK] Guest created; new access token received.")

    if "/game/flash" not in urlparse(r1.url).path:
        rg = session.get(game_flash_url, headers=html_headers(launch_url), timeout=45)
        rg.raise_for_status()
    rw = session.get(
        webgl_url,
        headers={**unity_headers(ajax=True), "Referer": game_flash_url},
        timeout=45,
    )
    rw.raise_for_status()
    webgl_html = rw.text
    log("[OK] WebGL view initialized.")

    version_data = mobile_get(session, mobile_version_url)
    boot_data = mobile_post(session, mobile_boot_url, mobile_common("en"))
    boot_token = ((boot_data or {}).get("session") or {}).get("access_token") if isinstance(boot_data, dict) else None
    access_token = str(boot_token or access_cookie)
    if not access_token:
        raise RuntimeError("Could not obtain access_token from boot/cookie.")
    log(f"[OK] Mobile boot completed (server version: {version_data.get('version', '?') if isinstance(version_data, dict) else '?'}).")

    login_payload = mobile_common(locale)
    login_payload.update({"access_token": access_token, "device_model": device_model})
    login_data = mobile_post(session, mobile_login_url, login_payload)
    if not isinstance(login_data, dict) or not login_data.get("access_token"):
        raise RuntimeError("login_via_access_token did not return a valid session.")
    log("[OK] Guest session ready.")

    profile_status = mobile_get(session, mobile_profile_status_url)
    countries_data = None
    profile_update = None
    if isinstance(profile_status, dict) and profile_status.get("needs_country"):
        countries_data = mobile_post(session, mobile_countries_url, mobile_common(locale))
        available = {
            str(x.get("uid", "")).upper()
            for x in (countries_data.get("countries", []) if isinstance(countries_data, dict) else [])
            if isinstance(x, dict)
        }
        country = country.upper()
        if available and country not in available:
            raise RuntimeError(f"Country {country!r} is not listed in /mobile/countries.json.")
        profile_update = mobile_post(
            session,
            mobile_profile_update_url,
            {"country": country, "locale": locale},
        )
        if isinstance(profile_update, dict) and not profile_update.get("success", False):
            raise RuntimeError(f"Could not save country {country} in the guest profile.")
        log(f"[OK] Guest country configured: {country}.")

    if isinstance(profile_status, dict) and (
        profile_status.get("needs_username") or profile_status.get("needs_username_generator")
    ):
        log("[WARN] The server requests username completion; the reference  did not contain that flow.")

    try:
        optins_data = mobile_get(session, mobile_optins_status_url)
    except Exception as exc:
        optins_data = None
        log(f"[WARN] Could not query optins/status: {exc}")

    return {
        "version": version_data,
        "boot": boot_data,
        "login": login_data,
        "profile_status": profile_status,
        "countries": countries_data,
        "profile_update": profile_update,
        "optins": optins_data,
        "webgl_html": webgl_html,
    }


def unwrap_action(response: Any, action: str) -> Optional[Dict[str, Any]]:
    if isinstance(response, dict):
        return response if response.get("action") == action else None
    if isinstance(response, list):
        for item in response:
            if isinstance(item, dict) and item.get("action") == action:
                return item
    return None


def post_api(session: requests.Session, url: str, payload_json: Any, locale: str) -> Any:
    data = {
        "json": json.dumps(payload_json, ensure_ascii=False, separators=(",", ":")),
        "locale": locale,
    }
    headers = {**unity_headers(origin=True), "Content-Type": "application/x-www-form-urlencoded"}
    r = session.post(url, data=data, headers=headers, timeout=45)
    r.raise_for_status()
    return r.json()


def get_manifest(session: requests.Session, boot_data: Any = None) -> Dict[str, str]:
    base_url = "https://paninicollection.fifa.com"
    game_flash_url = f"{base_url}/game/flash?start_view=frontapp"
    manifest_url = f"{base_url}/manifest_update.json?client=webgl_hires_webgl"
    config_key = "config/config.json"
    url = manifest_url
    if isinstance(boot_data, dict):
        candidate = ((boot_data.get("assets") or {}).get("manifest"))
        if isinstance(candidate, str) and candidate.startswith("http"):
            url = candidate
    log(f"[INFO] Downloading current manifest: {url}")
    r = session.get(url, headers={"Accept": "*/*", "Referer": game_flash_url}, timeout=45)
    r.raise_for_status()
    manifest = r.json()
    if not isinstance(manifest, dict):
        raise RuntimeError("The manifest does not have the expected format.")
    if config_key not in manifest:
        raise RuntimeError(f"The manifest does not contain {config_key}.")
    log(f"[OK] Current manifest downloaded ({len(manifest)} entries).")
    return manifest


def get_config(session: requests.Session, manifest: Dict[str, str]) -> Dict[str, Any]:
    base_url = "https://paninicollection.fifa.com"
    game_flash_url = f"{base_url}/game/flash?start_view=frontapp"
    config_key = "config/config.json"
    rel = manifest.get(config_key)
    if not rel:
        raise RuntimeError(f"{config_key} was not found in the manifest.")
    url = urljoin(f"{base_url}/assets/", rel)
    log(f"[INFO] Downloading current config: {url}")
    r = session.get(url, headers={"Accept": "*/*", "Referer": game_flash_url}, timeout=60)
    r.raise_for_status()
    cfg = r.json()
    if not isinstance(cfg, dict) or "stickers" not in cfg:
        raise RuntimeError("The downloaded config does not contain 'stickers'.")
    log(f"[OK] Current config downloaded ({len(cfg.get('stickers', []))} records).")
    return cfg


def get_game_config(session: requests.Session, locale: str) -> Any:
    base_url = "https://paninicollection.fifa.com"
    game_config_url = f"{base_url}/api/game_config.json"
    try:
        return post_api(session, game_config_url, {}, locale)
    except Exception as exc:
        log(f"[WARN] Could not query game_config: {exc}")
        return None


def get_init(session: requests.Session, locale: str) -> Any:
    base_url = "https://paninicollection.fifa.com"
    init_url = f"{base_url}/api/init.json"
    try:
        return post_api(session, init_url, {}, locale)
    except Exception as exc:
        log(f"[WARN] Could not query init: {exc}")
        return None


def query_sticker_status(
    session: requests.Session,
    sticker_ids: Sequence[int],
    locale: str,
    batch_size: int = 100,
) -> List[Any]:
    base_url = "https://paninicollection.fifa.com"
    status_url = f"{base_url}/api/challenge_get_sticker_status.json"
    results: List[Any] = []
    ids = list(dict.fromkeys(int(x) for x in sticker_ids))
    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        log(f"[INFO] Querying status: {batch[0]}..{batch[-1]} ({len(batch)} IDs)")
        response = post_api(session, status_url, {"stickers": batch}, locale)
        results.append(response)
    return results


def bundle_map_from_manifest(manifest: Dict[str, str]) -> Dict[str, str]:
    base_url = "https://paninicollection.fifa.com"
    out: Dict[str, str] = {}
    pattern = re.compile(r"^unitybundles/stickers/([^/]+)\.unity3d$", re.I)
    for logical, physical in manifest.items():
        m = pattern.match(logical)
        if not m:
            continue
        group = m.group(1).upper()
        out[group] = urljoin(f"{base_url}/assets/", physical)
    return dict(sorted(out.items()))


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    overwrite: bool = False,
    timeout: int = 120,
) -> Path:
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        log(f"[SKIP] Already exists: {dest.name}")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()

    log(f"[DOWNLOAD] {dest.name}")
    started = time.monotonic()
    last_draw = 0.0
    with session.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", "0") or 0)
        downloaded = 0
        r.raw.decode_content = False
        with tmp.open("wb") as f:
            for chunk in r.raw.stream(256 * 1024, decode_content=False):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_draw >= 0.10:
                    print_download_progress(downloaded, total, started)
                    last_draw = now
        if total and downloaded != total:
            print()
            raise IOError(f"Incomplete download: {downloaded}/{total} bytes for {url}")
    print_download_progress(downloaded, total or downloaded, started, force=True)
    tmp.replace(dest)
    log(f"[OK] Downloaded: {dest.name} ({dest.stat().st_size / 1024 / 1024:.2f} MB)")
    return dest


def require_unitypy():
    try:
        import UnityPy
        return UnityPy
    except ImportError as exc:
        raise RuntimeError(
            "UnityPy is missing. Install the dependencies with:\n"
            "    pip install requests UnityPy Pillow\n"
            "and run the script again."
        ) from exc


def object_path_id(obj: Any) -> int:
    for attr in ("path_id", "m_PathID"):
        try:
            val = getattr(obj, attr)
            return int(val)
        except Exception:
            pass
    return 0


def parse_unity_object(obj: Any) -> Any:
    if hasattr(obj, "parse_as_object"):
        return obj.parse_as_object()
    if hasattr(obj, "read"):
        return obj.read()
    raise AttributeError("Incompatible UnityPy object: parse_as_object/read is unavailable")


def detect_index_from_name(name: str, expected: set[int]) -> Optional[int]:
    numbers = [int(x) for x in re.findall(r"\d+", name or "")]
    for n in reversed(numbers):
        if n in expected:
            return n
    return None


def sticker_records_by_ref(config: Dict[str, Any]) -> Dict[Tuple[str, int], List[Dict[str, Any]]]:
    refs: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for sticker in config.get("stickers", []):
        image = sticker.get("image") or {}
        uid = image.get("slice_uid")
        idx = image.get("slice_index")
        if uid is None or idx is None:
            continue
        refs[(str(uid).upper(), int(idx))].append(sticker)
    return refs


def select_primary_record(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    regular = [x for x in records if not (x.get("info") or {}).get("golden", False)]
    if regular:
        return sorted(regular, key=lambda x: int(x.get("id", 0)))[0]
    return sorted(records, key=lambda x: int(x.get("id", 0)))[0]


def sticker_filename(sticker: Dict[str, Any], source_kind: str = "") -> str:
    sid = int(sticker.get("id", 0))
    group = safe_filename(str(sticker.get("group_uid", "UNK")), 24)
    idx = int(sticker.get("index_in_group", (sticker.get("image") or {}).get("slice_index", 0)))
    label = safe_filename(str(sticker.get("label", "sticker")), 80)
    golden = bool((sticker.get("info") or {}).get("golden", False))
    suffix = "_GOLDEN_BASE" if golden else ""
    if source_kind:
        suffix += f"_{safe_filename(source_kind, 20)}"
    return f"{sid:04d}_{group}_{idx:02d}_{label}{suffix}.png"


def render_cosmic_deluxe_png(source_png: Path, dest_png: Path, overwrite: bool = False) -> bool:
    if dest_png.exists() and not overwrite:
        return False
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is missing. Run: pip install Pillow") from exc

    dest_png.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_png) as im:
        base = im.convert("RGBA")
        w, h = base.size
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay, "RGBA")
        palette = [
            (255, 70, 180, 48),
            (80, 210, 255, 52),
            (110, 255, 185, 46),
            (255, 220, 80, 48),
            (155, 105, 255, 52),
        ]
        band = max(18, min(w, h) // 8)
        i = 0
        for x in range(-h, w + h, band):
            color = palette[i % len(palette)]
            draw.polygon([(x, 0), (x + band, 0), (x - h + band, h), (x - h, h)], fill=color)
            i += 1
        shine_w = max(8, w // 18)
        cx = w // 2
        draw.polygon(
            [(cx - shine_w, 0), (cx + shine_w, 0), (cx - h // 3 + shine_w, h), (cx - h // 3 - shine_w, h)],
            fill=(255, 255, 255, 42),
        )
        result = Image.alpha_composite(base, overlay)
        result.putalpha(base.getchannel("A"))
        result.save(dest_png, "PNG", optimize=True)
    return True


def export_all_unity_images(
    unity_path: Path,
    output_dir: Path,
    overwrite: bool = False,
    prefix: str = "asset",
) -> Dict[str, Any]:
    UnityPy = require_unitypy()
    env = UnityPy.load(str(unity_path))
    output_dir.mkdir(parents=True, exist_ok=True)
    exported = 0
    errors = 0
    records: List[Dict[str, Any]] = []
    seen_names: Dict[str, int] = defaultdict(int)

    objects = [o for o in env.objects if getattr(getattr(o, "type", None), "name", "") in {"Sprite", "Texture2D"}]
    total = len(objects)
    for n, obj in enumerate(objects, start=1):
        typ = getattr(getattr(obj, "type", None), "name", "")
        try:
            data = parse_unity_object(obj)
            name = str(getattr(data, "m_Name", "") or f"{typ}_{object_path_id(obj)}")
            clean = safe_filename(name, 90)
            seen_names[clean] += 1
            suffix = f"_{seen_names[clean]}" if seen_names[clean] > 1 else ""
            pid = object_path_id(obj)
            out = output_dir / f"{prefix}_{pid:010d}_{typ}_{clean}{suffix}.png"
            if overwrite or not out.exists():
                image = data.image
                image.save(out, format="PNG")
            try:
                width, height = data.image.size
            except Exception:
                width = height = 0
            records.append({
                "type": typ,
                "name": name,
                "path_id": pid,
                "width": width,
                "height": height,
                "png": str(out),
            })
            exported += 1
        except Exception as exc:
            errors += 1
            log(f"[WARN] Could not export {typ} #{n}/{total}: {exc}")
    return {"exported": exported, "errors": errors, "assets": records}


def extract_group_bundle(
    bundle_path: Path,
    group: str,
    config: Dict[str, Any],
    output_root: Path,
    overwrite: bool = False,
) -> Dict[str, Any]:
    UnityPy = require_unitypy()
    refs = sticker_records_by_ref(config)
    expected_refs = sorted(
        [(idx, records) for (uid, idx), records in refs.items() if uid == group.upper()],
        key=lambda x: x[0],
    )
    expected_indices = {idx for idx, _ in expected_refs}

    if not expected_refs:
        return {"group": group, "status": "no_references", "exported": 0}

    env = UnityPy.load(str(bundle_path))

    sprites: List[Tuple[str, int, Any]] = []
    textures: List[Tuple[str, int, Any]] = []
    for obj in env.objects:
        typ = getattr(getattr(obj, "type", None), "name", "")
        if typ not in {"Sprite", "Texture2D"}:
            continue
        try:
            data = parse_unity_object(obj)
            name = str(getattr(data, "m_Name", "") or f"{typ}_{object_path_id(obj)}")
            item = (name, object_path_id(obj), data)
            if typ == "Sprite":
                sprites.append(item)
            else:
                textures.append(item)
        except Exception as exc:
            log(f"[WARN] Could not read {typ} del bundle {group}: {exc}")

    assets = sprites if sprites else textures
    asset_type = "Sprite" if sprites else "Texture2D"
    assets.sort(key=lambda x: (natural_key(x[0]), x[1]))

    mapping: Dict[int, Tuple[str, int, Any]] = {}
    used_asset_ids = set()

    for asset in assets:
        idx = detect_index_from_name(asset[0], expected_indices)
        if idx is not None and idx not in mapping:
            mapping[idx] = asset
            used_asset_ids.add(id(asset[2]))

    remaining_indices = [idx for idx, _ in expected_refs if idx not in mapping]
    remaining_assets = [a for a in assets if id(a[2]) not in used_asset_ids]
    if remaining_indices and len(remaining_assets) >= len(remaining_indices):
        for idx, asset in zip(remaining_indices, remaining_assets):
            mapping[idx] = asset

    source_dir = output_root / "sticker_sources" / group.upper()
    per_id_dir = output_root / "stickers_png" / group.upper()
    source_dir.mkdir(parents=True, exist_ok=True)
    per_id_dir.mkdir(parents=True, exist_ok=True)

    exported_sources = 0
    exported_ids = 0
    exported_deluxe = 0
    missing: List[int] = []
    exported_meta: List[Dict[str, Any]] = []

    for idx, records in expected_refs:
        asset = mapping.get(idx)
        if asset is None:
            missing.append(idx)
            continue

        asset_name, path_id, data = asset
        primary = select_primary_record(records)
        src_name = sticker_filename(primary)
        src_path = source_dir / src_name

        if overwrite or not src_path.exists():
            try:
                image = data.image
                image.save(src_path, format="PNG")
                exported_sources += 1
            except Exception as exc:
                log(f"[WARN] Error exporting {group}:{idx} ({asset_name}): {exc}")
                missing.append(idx)
                continue

        for sticker in records:
            out_name = sticker_filename(sticker)
            out_path = per_id_dir / out_name
            if overwrite or not out_path.exists():
                shutil.copy2(src_path, out_path)
            exported_ids += 1

            if bool((sticker.get("info") or {}).get("golden", False)):
                deluxe_group = safe_filename(str(sticker.get("group_uid", group)), 40)
                deluxe_dir = output_root / "deluxe" / "cosmic_png" / deluxe_group
                deluxe_name = out_path.stem.replace("_GOLDEN_BASE", "") + "_DELUXE_COSMIC.png"
                deluxe_path = deluxe_dir / deluxe_name
                try:
                    if render_cosmic_deluxe_png(src_path, deluxe_path, overwrite=overwrite):
                        exported_deluxe += 1
                except Exception as exc:
                    log(f"[WARN] Could not render Deluxe/Cosmic {sticker.get('id')}: {exc}")

        exported_meta.append(
            {
                "slice_uid": group.upper(),
                "slice_index": idx,
                "asset_type": asset_type,
                "asset_name": asset_name,
                "path_id": path_id,
                "source_png": str(src_path.relative_to(output_root)),
                "sticker_ids": [int(x.get("id", 0)) for x in records],
            }
        )

    raw_update_report = None
    if group.upper() == "UPDATE":
        try:
            raw_update_report = export_all_unity_images(
                bundle_path,
                output_root / "update_set" / "all_assets_png",
                overwrite=overwrite,
                prefix="UPDATE",
            )
            log(f"[OK] Raw UPDATE export: {raw_update_report.get('exported', 0)} Unity images exported.")
        except Exception as exc:
            raw_update_report = {"exported": 0, "errors": 1, "error": str(exc)}
            log(f"[WARN] Redundant UPDATE export failed: {exc}")

    return {
        "group": group.upper(),
        "status": "ok" if not missing else "partial",
        "asset_type": asset_type,
        "assets_found": len(assets),
        "expected_slices": len(expected_refs),
        "exported_sources": exported_sources,
        "exported_sticker_records": exported_ids,
        "exported_deluxe_cosmic": exported_deluxe,
        "missing_slice_indices": missing,
        "raw_update_export": raw_update_report,
        "assets": exported_meta,
    }


def download_and_extract_stickers(
    session: requests.Session,
    manifest: Dict[str, str],
    config: Dict[str, Any],
    output_root: Path,
    overwrite: bool = False,
    keep_bundles: bool = True,
) -> Dict[str, Any]:
    bundle_map = bundle_map_from_manifest(manifest)
    if not bundle_map:
        raise RuntimeError("No unitybundles/stickers entries were found in the manifest.")

    bundle_dir = output_root / "_bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    reports = []

    log(f"[INFO] Found {len(bundle_map)} hires sticker bundles.")
    log(f"[INFO] Config: {len(config.get('stickers', []))} sticker records.")

    for n, (group, url) in enumerate(bundle_map.items(), start=1):
        log(f"\n[{n}/{len(bundle_map)}] Group {group}")
        physical_name = Path(urlparse(url).path).name
        bundle_path = bundle_dir / physical_name
        try:
            download_file(session, url, bundle_path, overwrite=overwrite)
            report = extract_group_bundle(
                bundle_path=bundle_path,
                group=group,
                config=config,
                output_root=output_root,
                overwrite=overwrite,
            )
            reports.append(report)
            log(
                f"[OK] {group}: {report.get('exported_sticker_records', 0)} PNG records, "
                f"{report.get('expected_slices', 0)} expected slices."
            )
        except Exception as exc:
            reports.append({"group": group, "status": "error", "error": str(exc)})
            log(f"[ERROR] {group}: {exc}")

    if not keep_bundles:
        shutil.rmtree(bundle_dir, ignore_errors=True)

    report_path = output_root / "sticker_extraction_report.json"
    report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")

    total_records = sum(int(r.get("exported_sticker_records", 0)) for r in reports)
    errors = [r for r in reports if r.get("status") == "error"]
    partials = [r for r in reports if r.get("status") == "partial"]
    return {
        "groups": len(bundle_map),
        "records_exported": total_records,
        "errors": len(errors),
        "partials": len(partials),
        "report": str(report_path),
    }


def get_webgl_data_url(session: requests.Session, webgl_html: Optional[str] = None) -> str:
    base_url = "https://paninicollection.fifa.com"
    game_flash_url = f"{base_url}/game/flash?start_view=frontapp"
    webgl_url = f"{base_url}/game/webgl"
    html_text = webgl_html or ""
    if not html_text:
        r = session.get(
            webgl_url,
            headers={**unity_headers(ajax=True), "Referer": game_flash_url},
            timeout=45,
        )
        r.raise_for_status()
        html_text = r.text
    m = re.search(r'["\']dataUrl["\']\s*:\s*["\']([^"\']+)["\']', html_text, flags=re.I)
    if not m:
        raise RuntimeError("Unity WebGL dataUrl was not found in /game/webgl.")
    return urljoin(base_url, html.unescape(m.group(1)))


def normalize_webgl_data(source: Path, dest: Path, overwrite: bool = False) -> Path:
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        with gzip.open(source, "rb") as src, dest.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    else:
        shutil.copy2(source, dest)
    return dest


def unpack_unity_web_data(data_path: Path, output_dir: Path, overwrite: bool = False) -> List[Path]:
    raw = data_path.read_bytes()
    signature = b"UnityWebData1.0\x00"
    if not raw.startswith(signature):
        return []
    pos = len(signature)
    if len(raw) < pos + 4:
        return []
    header_len = struct.unpack_from("<I", raw, pos)[0]
    pos += 4
    if header_len <= pos or header_len > len(raw):
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    files: List[Path] = []
    while pos < header_len:
        if pos + 12 > header_len:
            break
        data_offset, data_len, path_len = struct.unpack_from("<III", raw, pos)
        pos += 12
        if path_len <= 0 or pos + path_len > header_len:
            break
        rel = raw[pos : pos + path_len].decode("utf-8", errors="replace")
        pos += path_len
        if data_offset + data_len > len(raw):
            continue
        rel = rel.replace("\\", "/").lstrip("/")
        parts = [safe_filename(x, 100) for x in rel.split("/") if x not in {"", ".", ".."}]
        if not parts:
            parts = [f"webdata_{len(files):04d}.bin"]
        out = output_dir.joinpath(*parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not out.exists():
            out.write_bytes(raw[data_offset : data_offset + data_len])
        files.append(out)
    return files


def load_webgl_unity_environment(data_path: Path, unpack_dir: Path) -> Tuple[Any, List[Path], str]:
    UnityPy = require_unitypy()
    try:
        return UnityPy.load(str(data_path)), [], "direct"
    except Exception as direct_exc:
        unpacked = unpack_unity_web_data(data_path, unpack_dir, overwrite=False)
        if not unpacked:
            raise RuntimeError(f"UnityPy could not open webgl.data and it could not be unpacked: {direct_exc}") from direct_exc
        try:
            return UnityPy.load(str(unpack_dir)), unpacked, "UnityWebData1.0"
        except Exception as unpack_exc:
            raise RuntimeError(
                f"Could not open webgl.data or its {len(unpacked)} internal files: {unpack_exc}"
            ) from unpack_exc


def classify_webgl_image(name: str, width: int, height: int, chapters: Sequence[str]) -> List[str]:
    low = name.lower()
    tokens = {x for x in re.split(r"[^a-z0-9]+", low) if x}
    tokens_with_underscore = {x for x in re.split(r"[^a-z0-9_]+", low) if x}
    cats: List[str] = []
    if any(k in low for k in ["deluxe", "premium", "cosmic", "golden", "shiny", "holo"]):
        cats.append("deluxe")
    if "update" in low:
        cats.append("update")
    chapter_tokens = {str(x).lower() for x in chapters if x}
    chapter_simple = {x.replace("_", "").replace("-", "") for x in chapter_tokens}
    album_words = ["album", "page", "pages", "chapter", "spread", "book", "background", "pagebg", "album_bg"]
    compact_name = re.sub(r"[^a-z0-9]", "", low)
    chapter_hit = bool(tokens.intersection(chapter_tokens) or tokens_with_underscore.intersection(chapter_tokens))
    if not chapter_hit:
        chapter_hit = any(len(ch) >= 3 and ch in compact_name for ch in chapter_simple)
    large_page_like = width >= 700 and height >= 500
    if any(k in low for k in album_words) or chapter_hit or large_page_like:
        cats.append("album")
    return list(dict.fromkeys(cats))


def extract_webgl_resources(
    session: requests.Session,
    webgl_html: Optional[str],
    game_config: Any,
    output_root: Path,
    overwrite: bool = False,
    export_all: bool = True,
) -> Dict[str, Any]:
    url = get_webgl_data_url(session, webgl_html)
    webgl_dir = output_root / "_webgl"
    physical = Path(urlparse(url).path).name or "webgl.data.gz"
    downloaded = download_file(session, url, webgl_dir / physical, overwrite=overwrite, timeout=300)
    data_path = normalize_webgl_data(downloaded, webgl_dir / "webgl.data", overwrite=overwrite)
    env, unpacked, mode = load_webgl_unity_environment(data_path, webgl_dir / "unpacked")

    game_action = unwrap_action(game_config, "game_config") if game_config is not None else None
    chapters = list((game_action or {}).get("album_chapters", []))

    all_dir = output_root / "webgl_assets_png" / "ALL"
    album_dir = output_root / "album" / "pages_and_backgrounds_png"
    deluxe_dir = output_root / "deluxe" / "webgl_assets_png"
    update_dir = output_root / "update_set" / "webgl_assets_png"
    for d in [all_dir, album_dir, deluxe_dir, update_dir]:
        d.mkdir(parents=True, exist_ok=True)

    report: List[Dict[str, Any]] = []
    counters = defaultdict(int)
    objects = [o for o in env.objects if getattr(getattr(o, "type", None), "name", "") in {"Sprite", "Texture2D"}]
    total = len(objects)
    log(f"[INFO] WebGL contains {total} Sprite/Texture2D candidates.")

    for n, obj in enumerate(objects, start=1):
        typ = getattr(getattr(obj, "type", None), "name", "")
        try:
            data = parse_unity_object(obj)
            name = str(getattr(data, "m_Name", "") or f"{typ}_{object_path_id(obj)}")
            image = data.image
            width, height = image.size
            cats = classify_webgl_image(name, width, height, chapters)
            if not export_all and not cats:
                continue
            pid = object_path_id(obj)
            filename = f"{pid:010d}_{typ}_{safe_filename(name, 100)}.png"
            canonical_dir = all_dir if export_all else (
                album_dir if "album" in cats else deluxe_dir if "deluxe" in cats else update_dir
            )
            canonical = canonical_dir / filename
            if overwrite or not canonical.exists():
                image.save(canonical, format="PNG")
            counters["all"] += 1

            category_paths = []
            for cat, d in [("album", album_dir), ("deluxe", deluxe_dir), ("update", update_dir)]:
                if cat not in cats:
                    continue
                target = d / filename
                if target != canonical and (overwrite or not target.exists()):
                    try:
                        os.link(canonical, target)
                    except Exception:
                        shutil.copy2(canonical, target)
                counters[cat] += 1
                category_paths.append(str(target.relative_to(output_root)))

            report.append({
                "path_id": pid,
                "type": typ,
                "name": name,
                "width": width,
                "height": height,
                "categories": cats,
                "png": str(canonical.relative_to(output_root)),
                "category_pngs": category_paths,
            })
            if n % 100 == 0 or n == total:
                log(f"[WEBGL EXTRACTION] {n}/{total}")
        except Exception as exc:
            counters["errors"] += 1
            if counters["errors"] <= 20:
                log(f"[WARN] WebGL {typ} #{n}: {exc}")

    report_path = output_root / "webgl_assets_report.json"
    report_path.write_text(
        json.dumps(
            {
                "url": url,
                "load_mode": mode,
                "unpacked_files": [str(x) for x in unpacked],
                "chapters": chapters,
                "counts": dict(counters),
                "assets": report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"counts": dict(counters), "report": str(report_path), "load_mode": mode}


def iter_json_urls(value: Any) -> Iterable[str]:
    base_url = "https://paninicollection.fifa.com"
    if isinstance(value, dict):
        for v in value.values():
            yield from iter_json_urls(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_json_urls(v)
    elif isinstance(value, str):
        text = html.unescape(value.strip())
        if text.startswith("/"):
            text = urljoin(base_url, text)
        if text.startswith("http://") or text.startswith("https://"):
            yield text


def download_discovered_visual_assets(
    session: requests.Session,
    sources: Sequence[Any],
    output_root: Path,
    overwrite: bool = False,
) -> Dict[str, List[str]]:
    seen = set()
    saved: Dict[str, List[str]] = defaultdict(list)
    for source in sources:
        for url in iter_json_urls(source):
            if url in seen:
                continue
            seen.add(url)
            low = url.lower()
            path = urlparse(url).path
            ext = Path(path).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            if "paninicollection.fifa.com" not in urlparse(url).netloc:
                continue
            if any(k in low for k in ["deluxe", "premium", "cosmic", "golden", "shiny", "holo"]):
                cat = "deluxe"
                target_dir = output_root / "deluxe" / "http_assets"
            elif "update" in low:
                cat = "update"
                target_dir = output_root / "update_set" / "http_assets"
            elif "album" in low:
                cat = "album"
                target_dir = output_root / "album" / "http_assets"
            else:
                continue
            name = Path(path).name or f"{cat}_{len(saved[cat]) + 1}{ext}"
            try:
                p = download_file(session, url, target_dir / name, overwrite=overwrite, timeout=90)
                saved[cat].append(str(p))
            except Exception as exc:
                log(f"[WARN] {cat} asset not downloaded {url}: {exc}")
    return dict(saved)


def album_index_rows(config: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for s in sorted(config.get("stickers", []), key=lambda x: int(x.get("order_index", 0))):
        info = s.get("info") or {}
        image = s.get("image") or {}
        yield {
            "id": s.get("id"),
            "sort_id": s.get("sort_id"),
            "order_index": s.get("order_index"),
            "label": s.get("label"),
            "group_uid": s.get("group_uid"),
            "index_in_group": s.get("index_in_group"),
            "slice_uid": image.get("slice_uid"),
            "slice_index": image.get("slice_index"),
            "golden": info.get("golden", False),
            "needed_for_completion": info.get("needed_for_completion", False),
            "regular_version_of": info.get("regular_version_of"),
            "background_id": info.get("background_id"),
        }


def save_album_metadata(
    config: Dict[str, Any],
    game_config: Any,
    init_data: Any,
    output_root: Path,
) -> Dict[str, Path]:
    album_dir = output_root / "album"
    album_dir.mkdir(parents=True, exist_ok=True)

    files: Dict[str, Path] = {}

    config_path = album_dir / "full_album_config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    files["config"] = config_path

    groups_path = album_dir / "album_groups.json"
    groups_path.write_text(json.dumps(config.get("groups", []), ensure_ascii=False, indent=2), encoding="utf-8")
    files["groups"] = groups_path

    game_action = unwrap_action(game_config, "game_config") if game_config is not None else None
    chapters = (game_action or {}).get("album_chapters", [])
    chapters_path = album_dir / "album_chapters.json"
    chapters_path.write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")
    files["chapters"] = chapters_path

    csv_path = album_dir / "album_sticker_index.csv"
    rows = list(album_index_rows(config))
    if rows:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    files["index_csv"] = csv_path

    if game_config is not None:
        p = album_dir / "game_config.json"
        p.write_text(json.dumps(game_config, ensure_ascii=False, indent=2), encoding="utf-8")
        files["game_config"] = p
    if init_data is not None:
        p = album_dir / "init.json"
        p.write_text(json.dumps(init_data, ensure_ascii=False, indent=2), encoding="utf-8")
        files["init"] = p

    return files


def extension_from_response(response: requests.Response, default: str = ".bin") -> str:
    ctype = (response.headers.get("Content-Type") or "").lower()
    path_ext = Path(urlparse(response.url).path).suffix.lower()
    if "application/pdf" in ctype:
        return ".pdf"
    if "application/zip" in ctype or "application/x-zip" in ctype:
        return ".zip"
    if "application/json" in ctype:
        return ".json"
    if "text/html" in ctype:
        return ".html"
    if "image/png" in ctype:
        return ".png"
    if "image/jpeg" in ctype:
        return ".jpg"
    if path_ext in {".pdf", ".zip", ".png", ".jpg", ".jpeg", ".webp"}:
        return path_ext
    return default


def likely_download_links(html_text: str, base_url: str) -> List[str]:
    links = []
    seen = set()
    for m in re.finditer(r"href\s*=\s*[\"']([^\"']+)[\"']", html_text, flags=re.I):
        href = html.unescape(m.group(1).strip())
        url = urljoin(base_url, href)
        low = url.lower()
        if any(token in low for token in [".pdf", ".zip", "download", "album_completed"]):
            if url not in seen:
                seen.add(url)
                links.append(url)
    return links


def save_response_content(response: requests.Response, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return path


def try_download_official_album(
    session: requests.Session,
    game_config: Any,
    output_root: Path,
    overwrite: bool = False,
) -> Dict[str, Any]:
    album_dir = output_root / "album" / "official"
    album_dir.mkdir(parents=True, exist_ok=True)

    game_action = unwrap_action(game_config, "game_config") if game_config is not None else None
    reward = (game_action or {}).get("download_reward") or {}
    candidates = []
    if reward.get("url"):
        candidates.append(("album_regular", str(reward["url"])))
    if reward.get("golden_url"):
        candidates.append(("album_golden", str(reward["golden_url"])))

    if not candidates:
        return {"status": "no_url", "saved": []}

    saved: List[str] = []
    notes: List[str] = []

    for label, url in candidates:
        try:
            log(f"[INFO] Trying official download: {label}")
            r = session.get(url, timeout=60, allow_redirects=True)
            r.raise_for_status()
            ext = extension_from_response(r, ".bin")
            out = album_dir / f"{label}{ext}"
            if overwrite or not out.exists():
                save_response_content(r, out)
            saved.append(str(out))
            if ext == ".html":
                for i, link in enumerate(likely_download_links(r.text, r.url), start=1):
                    try:
                        rr = session.get(link, timeout=90, allow_redirects=True)
                        rr.raise_for_status()
                        ext2 = extension_from_response(rr, ".bin")
                        if ext2 == ".html" and rr.url == r.url:
                            continue
                        p2 = album_dir / f"{label}_file_{i}{ext2}"
                        if overwrite or not p2.exists():
                            save_response_content(rr, p2)
                        saved.append(str(p2))
                    except Exception as exc:
                        notes.append(f"Could not download internal link {link}: {exc}")
        except Exception as exc:
            notes.append(f"{label}: {exc}")

    return {"status": "ok" if saved else "not_downloaded", "saved": saved, "notes": notes}


def save_album_ad_asset(session: requests.Session, init_data: Any, output_root: Path, overwrite: bool = False) -> Optional[Path]:
    panini_ads = unwrap_action(init_data, "panini_ads") if init_data is not None else None
    album = (panini_ads or {}).get("album") or {}
    store = album.get("panini_store") or {}
    url = store.get("image_url")
    if not url:
        return None
    ext = Path(urlparse(url).path).suffix or ".jpg"
    dest = output_root / "album" / f"album_panini_store{ext}"
    try:
        return download_file(session, str(url), dest, overwrite=overwrite, timeout=60)
    except Exception as exc:
        log(f"[WARN] Could not save album visual asset: {exc}")
        return None


def save_run_info(
    output_root: Path,
    manifest: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]],
    bootstrap: Optional[Dict[str, Any]] = None,
) -> None:
    script_version = "3.0"
    profile = ((bootstrap or {}).get("login") or {}).get("profile") if isinstance(bootstrap, dict) else None
    info = {
        "generated_at_unix": int(time.time()),
        "script_version": script_version,
        "session_source": "fresh_guest_http_bootstrap",
        "_used_at_runtime": False,
        "guest": bool((profile or {}).get("guest", True)) if isinstance(profile, dict) else True,
        "manifest_entries": len(manifest or {}),
        "sticker_records": len((config or {}).get("stickers", [])),
        "unique_image_slices": len(
            {
                (
                    str((st.get("image") or {}).get("slice_uid")),
                    (st.get("image") or {}).get("slice_index"),
                )
                for st in (config or {}).get("stickers", [])
                if st.get("image")
            }
        ),
        "note": (
            "This downloader creates a new guest session from scratch and does not read captured /cookies/tokens. "
            "Golden/Cosmic variants reuse the base image; the exact finish is applied by the shader/material."
        ),
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "download_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def interactive_menu() -> str:
    ui_divider("Panini FIFA Collection Downloader")
    print(style("  Actions", UI.MINT, UI.BOLD))
    options = [
        ("1", "Query sticker status"),
        ("2", "Download stickers + UPDATE + Deluxe/Cosmic versions"),
        ("3", "Save album + pages/backgrounds + official download"),
        ("4", "Extract WebGL extras (pages / Deluxe / Update)"),
        ("5", "Run EVERYTHING"),
        ("6", "Exit"),
    ]
    for i, (number, label) in enumerate(options):
        branch = "└─" if i == len(options) - 1 else "├─"
        print(
            style(f"  {branch} ", UI.PURPLE)
            + style(number, UI.MINT, UI.BOLD)
            + style("  ", UI.BLUEGREY)
            + style(label, UI.WHITE),
            flush=True,
        )
    prompt = style("\nSelect an option ", UI.BLUEGREY) + style("[1-6]", UI.YELLOW, UI.BOLD) + style(": ", UI.BLUEGREY)
    choice = input(prompt).strip()
    return {
        "1": "status",
        "2": "stickers",
        "3": "album",
        "4": "extras",
        "5": "all",
        "6": "exit",
    }.get(choice, "all")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download and extract Panini FIFA Collection using a fresh guest session; "
            "This downloader does not use  files."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--action",
        choices=["status", "stickers", "album", "extras", "all"],
        help="Action to run; if omitted, the menu is shown",
    )
    parser.add_argument("--ids", nargs="*", type=int, help="IDs for --action status")
    parser.add_argument("--locale", default="pt-BR", help="API locale")
    parser.add_argument("--country", default="BRA", help="ISO-3 country UID for the new guest")
    parser.add_argument("--device-model", default="Chrome 150.0.0.0", help="device_model sent to the WebGL login")
    parser.add_argument("--out", type=Path, default=Path("panini_2026_download"), help="Output folder")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    parser.add_argument(
        "--delete-bundles",
        action="store_true",
        help="Delete .unity3d files after extracting the PNG files",
    )
    parser.add_argument(
        "--classified-extras-only",
        action="store_true",
        help="In webgl.data, export only album/Deluxe/Update candidates instead of all images",
    )
    return parser.parse_args()


def main() -> int:
    run_started = time.monotonic()
    args = parse_args()
    action = args.action or interactive_menu()
    if action == "exit":
        return 0

    output_root: Path = args.out
    output_root.mkdir(parents=True, exist_ok=True)
    ui_banner(action, args.locale, args.country, output_root)

    session = make_session()
    bootstrap = bootstrap_guest_session(
        session=session,
        locale=args.locale,
        country=args.country,
        device_model=args.device_model,
    )
    webgl_html = bootstrap.get("webgl_html") if isinstance(bootstrap, dict) else None

    manifest: Optional[Dict[str, str]] = None
    config: Optional[Dict[str, Any]] = None
    game_config: Any = None
    init_data: Any = None

    if action == "status":
        ids = args.ids
        if not ids:
            raw = input(style("Sticker IDs separated by spaces ", UI.BLUEGREY) + style("(e.g. 257 258 259): ", UI.YELLOW)).strip()
            ids = [int(x) for x in raw.split() if x.strip().isdigit()]
        if not ids:
            raise ValueError("No sticker IDs were provided.")
        result = query_sticker_status(session, ids, args.locale)
        out = output_root / "sticker_status.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        save_run_info(output_root, None, None, bootstrap)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        log(f"[OK] Result saved to: {out}")
        return 0

    manifest = get_manifest(session, bootstrap.get("boot") if isinstance(bootstrap, dict) else None)
    config = get_config(session, manifest)
    game_config = get_game_config(session, args.locale)
    init_data = get_init(session, args.locale)
    save_run_info(output_root, manifest, config, bootstrap)

    discovered = download_discovered_visual_assets(
        session,
        [bootstrap.get("boot"), bootstrap.get("login"), game_config, init_data],
        output_root,
        overwrite=args.overwrite,
    )
    if discovered:
        log("[OK] Visual assets discovered from current JSON responses: " + ", ".join(f"{k}={len(v)}" for k, v in discovered.items()))

    if action in {"album", "all"}:
        files = save_album_metadata(config, game_config, init_data, output_root)
        log("[OK] Album metadata saved:")
        file_items = list(files.items())
        for i, (key, path) in enumerate(file_items):
            ui_kv(key, path, last=(i == len(file_items) - 1), indent=1)

        ad = save_album_ad_asset(session, init_data, output_root, overwrite=args.overwrite)
        if ad:
            log(f"[OK] Album visual asset saved: {ad}")

        official = try_download_official_album(
            session=session,
            game_config=game_config,
            output_root=output_root,
            overwrite=args.overwrite,
        )
        if official.get("saved"):
            log("[OK] Official album response/file(s) saved:")
            saved_items = list(official["saved"])
            for i, p in enumerate(saved_items):
                ui_tree(str(p), last=(i == len(saved_items) - 1), indent=1, tone="dim")
        else:
            log("[WARN] Could not obtain an official album file with this new session.")
        for note in official.get("notes", []):
            log(f"[WARN] {note}")

    if action in {"album", "extras", "all"}:
        try:
            webgl_summary = extract_webgl_resources(
                session=session,
                webgl_html=webgl_html,
                game_config=game_config,
                output_root=output_root,
                overwrite=args.overwrite,
                export_all=not args.classified_extras_only,
            )
            counts = webgl_summary.get("counts", {})
            ui_summary(
                "WEBGL / PAGES / DELUXE",
                [
                    ("Images exported", counts.get("all", 0)),
                    ("Album page/background candidates", counts.get("album", 0)),
                    ("Deluxe/Cosmic assets", counts.get("deluxe", 0)),
                    ("Update assets", counts.get("update", 0)),
                    ("Report", webgl_summary.get("report")),
                ],
            )
        except Exception as exc:
            log(f"[WARN] Could not extract webgl.data assets: {exc}")

    if action in {"stickers", "all"}:
        summary = download_and_extract_stickers(
            session=session,
            manifest=manifest,
            config=config,
            output_root=output_root,
            overwrite=args.overwrite,
            keep_bundles=not args.delete_bundles,
        )
        deluxe_total = 0
        try:
            report_data = json.loads(Path(summary["report"]).read_text(encoding="utf-8"))
            deluxe_total = sum(int(x.get("exported_deluxe_cosmic", 0)) for x in report_data)
        except Exception:
            pass
        ui_summary(
            "STICKER SUMMARY",
            [
                ("Groups processed", summary["groups"]),
                ("Sticker records exported to PNG", summary["records_exported"]),
                ("Rendered Deluxe/Cosmic versions", deluxe_total),
                ("Groups with errors", summary["errors"]),
                ("Partial groups", summary["partials"]),
                ("Report", summary["report"]),
            ],
        )

    ui_divider("Completed")
    print(style("✓ ", UI.MINT, UI.BOLD) + style("Everything was saved to", UI.WHITE))
    ui_tree(str(output_root.resolve()), last=True, tone="accent")
    print(
        style("Processed all selected tasks in ", UI.BLUEGREY)
        + style(format_elapsed(time.monotonic() - run_started), UI.YELLOW, UI.BOLD),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n" + style("! Cancelled by user.", UI.YELLOW, UI.BOLD))
        raise SystemExit(130)
    except Exception as exc:
        print("\n" + style("× ERROR: ", UI.RED, UI.BOLD) + style(exc, UI.RED), file=sys.stderr)
        raise SystemExit(1)
