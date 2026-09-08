
import asyncio
import os
import json
import gzip
import io
import csv
import unicodedata
import re
import time
import sqlite3
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote, unquote
from html import escape, unescape
from html.parser import HTMLParser

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AION2 Server v59 SelfDBCompare")

# Static compare site (Netlify / local file / other domain) must be able
# to call the Render API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# =========================================================
# Common
# =========================================================

SERVER_ID = 2002
SERVER_NAME = "지켈"
KST = ZoneInfo("Asia/Seoul")

# =========================================================
# Persistent state storage
# =========================================================
def _select_aion2_data_dir():
    env_dir = str(os.getenv("AION2_DATA_DIR") or "").strip()
    candidates = []
    if env_dir:
        candidates.append(Path(env_dir))
    if Path("/var/data").exists():
        candidates.append(Path("/var/data/aion2-bot"))
    candidates.append(Path("/tmp/aion2-bot"))
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return directory
        except Exception:
            continue
    return Path("/tmp")

AION2_DATA_DIR = _select_aion2_data_dir()
AION2_STORAGE_PERSISTENT = not str(AION2_DATA_DIR).startswith("/tmp")

def _state_path(env_name, filename, legacy_paths=()):
    explicit = str(os.getenv(env_name) or "").strip()
    target = Path(explicit) if explicit else (AION2_DATA_DIR / filename)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if not target.exists():
        for legacy in legacy_paths:
            old = Path(legacy)
            try:
                if old.exists() and old.is_file():
                    target.write_bytes(old.read_bytes())
                    break
            except Exception:
                continue
    return target

def _atomic_json_write(path, data):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        backup = path.with_suffix(path.suffix + ".bak")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if path.exists():
            try:
                backup.write_bytes(path.read_bytes())
            except Exception:
                pass
        tmp.replace(path)
        return True
    except Exception:
        return False

def _safe_json_load(path, default):
    path = Path(path)
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        try:
            if candidate.exists():
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(raw, type(default)):
                    return raw
        except Exception:
            continue
    return default

# Character API - current NotMeter endpoint
NOTMETER_API = "https://notmeter.59-27-108-81.sslip.io"
NOTMETER_CHARACTER_API = "https://notmeter.112-168-140-142.sslip.io"

# Field boss public cache.
# NotMeter itself uses GitHub first, then its VPS endpoint.
FIELD_BOSS_URLS = [
    "https://raw.githubusercontent.com/Not4You-Dev/NotMeter-Web/main/presence/notmeter-field-boss-public.json",
    f"{NOTMETER_API}/field-boss/v1/public",
]

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Origin": "https://notmeter.com",
    "Referer": "https://notmeter.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

PLAYNC_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Origin": "https://aion2.plaync.com",
    "Referer": "https://aion2.plaync.com/",
    "User-Agent": HEADERS["User-Agent"],
}

HTTP_TIMEOUT = httpx.Timeout(connect=2.0, read=6.0, write=2.0, pool=2.0)

CACHE_TTL = 180
FIELD_BOSS_CACHE_TTL = 120
_cache = {}
_http_client = None


def format_board_alert_title_only(board_type, title):
    title = str(title or "").strip()

    if board_type in ("notice", "공지"):
        lowered = title.casefold()
        compact = re.sub(r"[^0-9a-z가-힣]+", "", lowered)
        if "점검" in title or "maintenance" in lowered:
            header = "🔧 AION2 점검 공지"
        elif (
            "라이브" in title or "생방송" in title or "생중계" in title or
            "방송" in title or "live" in lowered or "onair" in compact or
            "stream" in lowered or "쇼케이스" in title or "showcase" in lowered
        ):
            header = "🔴 AION2 라이브 공지"
        else:
            header = "📢 AION2 공지"
    elif board_type in ("cm", "CM"):
        header = "📢 AION2 CM"
    else:
        header = "🆕 AION2 업데이트"

    return f"{header}\n{title}"

def kakao_text(text: str):
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": text[:1000]
                    }
                }
            ]
        }
    }

def clean_command(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

def cache_get(key, ttl=CACHE_TTL):
    item = _cache.get(key)
    if not item:
        return None
    saved, value = item
    if time.time() - saved > ttl:
        _cache.pop(key, None)
        return None
    return value

def cache_set(key, value):
    _cache[key] = (time.time(), value)

async def get_http_client():
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client

async def http_json(url: str, params=None, timeout=None):
    client = await get_http_client()
    response = await client.get(url, params=params, timeout=timeout or HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()

# =========================================================
# Character search - ALL Korean servers / optimized
# =========================================================

PERCENT_STONE_IDS = {
    "AmplifyAllDamage",
    "AmplifyWeaponDamage",
    "PvEAmplifyDamage",
    "AmplifyBossDamage",
    "AmplifyCriticalDamage",
    "AmplifyBackAttack",
    "AmplifyFrontAttack",
}

STONE_ORDER = [
    "전방 피해 증폭",
    "후방 피해 증폭",
    "무기 피해 증폭",
    "치명타 피해 증폭",
    "피해 증폭",
    "PVE 피해 증폭",
    "보스 피해 증폭",
    "공격력",
    "치명타",
    "치명타 저항",
    "추가 명중",
    "막기",
    "방어력",
    "생명력",
    "추가 회피",
    "정신력",
]

def number_from_value(value):
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else 0.0

def pretty_number(v):
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.1f}".rstrip("0").rstrip(".")

async def character_api_get(path, params, timeout=None):
    return await http_json(NOTMETER_CHARACTER_API + path, params=params, timeout=timeout)

def parse_character_query(text: str):
    text = str(text or "").strip()
    m = re.match(r"^(.+?)\[(.+?)\]$", text)
    if not m:
        return text, None
    return m.group(1).strip(), m.group(2).strip()

def row_name(row):
    return str(row.get("name") or row.get("characterName") or "").strip()

def row_server_name(row):
    return str(
        row.get("serverName")
        or row.get("server")
        or row.get("worldName")
        or ""
    ).strip()

def row_server_id(row):
    try:
        return int(row.get("serverId") or 0)
    except Exception:
        return 0

def row_character_id(row):
    return str(
        row.get("characterId")
        or row.get("id")
        or row.get("characterKey")
        or ""
    ).strip()

async def search_characters_all_servers(nickname: str):
    cache_key = f"char-search:{nickname.casefold()}"
    cached = cache_get(cache_key, 180)
    if cached is not None:
        return cached

    data = await character_api_get(
        "/character/v1/search",
        {
            "name": nickname,
            "region": "kr",
            "lang": "ko",
            "fast": "1",
        },
        timeout=httpx.Timeout(connect=1.0, read=2.8, write=1.0, pool=1.0),
    )

    results = data.get("results") or data.get("characters") or []
    target = nickname.casefold()

    exact = [
        row for row in results
        if isinstance(row, dict) and row_name(row).casefold() == target
    ]

    exact.sort(
        key=lambda row: (
            0 if row_name(row) == nickname else 1,
            -int(row.get("combatPower") or 0),
            row_server_id(row) or 999999,
        )
    )
    cache_set(cache_key, exact)
    return exact

async def get_profile(server_id: int, character_id: str, fast=False):
    params = {
        "serverId": int(server_id),
        "characterId": character_id,
        "region": "kr",
        "lang": "ko",
    }
    if fast:
        params["fast"] = "1"

    timeout = (
        httpx.Timeout(connect=1.0, read=2.5, write=1.0, pool=1.0)
        if fast
        else httpx.Timeout(connect=1.0, read=4.2, write=1.0, pool=1.0)
    )
    return await character_api_get("/character/v1/profile", params, timeout=timeout)

def _collect_stone_lists(node, out, depth=0):
    if depth > 10:
        return
    if isinstance(node, dict):
        stones = node.get("magicStoneStat")
        if isinstance(stones, list):
            out.append(stones)
        for key, value in node.items():
            if key != "magicStoneStat" and isinstance(value, (dict, list)):
                _collect_stone_lists(value, out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                _collect_stone_lists(value, out, depth + 1)

def aggregate_magic_stones(profile_json):
    totals = defaultdict(float)
    ids_by_name = {}
    stone_lists = []

    item_details = profile_json.get("itemDetails")
    if isinstance(item_details, dict):
        for item in item_details.values():
            if isinstance(item, dict):
                stones = item.get("magicStoneStat")
                if isinstance(stones, list):
                    stone_lists.append(stones)
    elif isinstance(item_details, list):
        for item in item_details:
            if isinstance(item, dict):
                stones = item.get("magicStoneStat")
                if isinstance(stones, list):
                    stone_lists.append(stones)

    if not stone_lists:
        _collect_stone_lists(profile_json, stone_lists)

    # Deduplicate by content instead of Python object identity.
    seen = set()
    for stones in stone_lists:
        for stone in stones:
            if not isinstance(stone, dict):
                continue
            stat_id = str(stone.get("id") or "").strip()
            name = str(stone.get("name") or "").strip()
            value_raw = str(stone.get("value") or "").strip()
            key = (stat_id, name, value_raw, str(stone.get("icon") or ""))
            if not name:
                continue
            # Same stat may legitimately occur on multiple equipment pieces.
            # Do not dedupe identical values across different pieces.
            totals[name] += number_from_value(value_raw)
            ids_by_name[name] = stat_id

    formatted = []
    used = set()

    for name in STONE_ORDER:
        if name not in totals:
            continue
        value = totals[name]
        stat_id = ids_by_name.get(name, "")
        if stat_id in PERCENT_STONE_IDS:
            formatted.append((name, f"+{pretty_number(value / 100.0)}%"))
        else:
            formatted.append((name, f"+{pretty_number(value)}"))
        used.add(name)

    for name, value in totals.items():
        if name in used:
            continue
        stat_id = ids_by_name.get(name, "")
        if stat_id in PERCENT_STONE_IDS:
            formatted.append((name, f"+{pretty_number(value / 100.0)}%"))
        else:
            formatted.append((name, f"+{pretty_number(value)}"))

    return formatted

def profile_info(profile_json, requested_name="", fallback_server="", fallback_row=None):
    profile = (profile_json.get("info") or {}).get("profile") or {}
    fallback_row = fallback_row or {}

    combat_power = (
        profile.get("combatPower")
        or fallback_row.get("combatPower")
        or fallback_row.get("power")
        or 0
    )

    return {
        "name": profile.get("characterName") or row_name(fallback_row) or requested_name,
        "job": profile.get("className") or fallback_row.get("className") or fallback_row.get("job") or "확인 실패",
        "combatPower": int(combat_power or 0),
        "serverId": int(profile.get("serverId") or row_server_id(fallback_row) or 0),
        "server": profile.get("serverName") or row_server_name(fallback_row) or fallback_server or "확인 실패",
        "level": int(profile.get("characterLevel") or fallback_row.get("level") or 0),
        "race": profile.get("raceName") or "",
        "title": profile.get("titleName") or "",
        "profileImage": profile.get("profileImage") or fallback_row.get("profileImage") or "",
    }

def character_card_url(name, server):
    return (
        "https://aion2-kakao-bot.onrender.com/c/"
        + quote(str(name), safe="")
        + "/"
        + quote(str(server), safe="")
    )

def format_character_from_data(info, stones):
    """Direct character command: return only the Kakao preview URL.

    The card URL includes the current combat power so Kakao gets a new preview
    cache key whenever the official value changes.
    """
    name = str(info.get("name") or "").strip()
    server = str(info.get("server") or "").strip()
    cp = int(info.get("combatPower") or 0)

    if not name:
        return None

    if server:
        card = character_card_url(name, server)
        # Always use a fresh preview URL. Combat power can stay unchanged even
        # when equipment/magic stones changed, so CP-only cache busting can show
        # an old Kakao card.
        card += "?v=" + str(cp or 0) + "&t=" + str(int(time.time()))
        return card

    # Server-less detail is only a fallback path; keep a minimal response.
    return name


async def load_detail(row, nickname):
    sid = row_server_id(row)
    cid = row_character_id(row)
    if not sid or not cid:
        return {
            "row": row,
            "profile": {},
            "info": profile_info({}, nickname, row_server_name(row), row),
            "stones": [],
        }

    profile = {}
    try:
        # Full profile first because this contains itemDetails/magicStoneStat.
        profile = await get_profile(sid, cid, fast=False)
    except Exception:
        # Never turn a valid search hit into "not found".
        try:
            profile = await get_profile(sid, cid, fast=True)
        except Exception:
            profile = {}

    info = profile_info(profile, nickname, row_server_name(row), row)
    stones = aggregate_magic_stones(profile) if profile else []

    return {
        "row": row,
        "profile": profile,
        "info": info,
        "stones": stones,
    }

async def resolve_character(nickname: str, server_name: str | None = None):
    candidates = await search_characters_all_servers(nickname)

    if server_name:
        target = server_name.casefold()

        # Fast path when search response contains serverName.
        named = [
            row for row in candidates
            if row_server_name(row) and row_server_name(row).casefold() == target
        ]
        if named:
            candidates = named
        else:
            # Search response may omit serverName. Only inspect rows until matched.
            checked = await asyncio.gather(
                *[load_detail(row, nickname) for row in candidates[:12]]
            )
            matched = [
                d for d in checked
                if str(d["info"]["server"]).casefold() == target
            ]
            if not matched:
                return {"type": "none"}
            return {"type": "detail", **matched[0]}

    if not candidates:
        return {"type": "none"}

    if len(candidates) == 1:
        detail = await load_detail(candidates[0], nickname)
        return {"type": "detail", **detail}

    # Duplicate nickname: DO NOT fetch every full profile.
    # Search API result is enough to show server choices, which is much faster.
    items = []
    for row in candidates[:12]:
        info = profile_info({}, nickname, row_server_name(row), row)
        items.append({"row": row, "info": info})

    # If server names are absent, fetch FAST profiles concurrently only.
    if any(not item["info"]["server"] or item["info"]["server"] == "확인 실패" for item in items):
        async def enrich(item):
            row = item["row"]
            sid = row_server_id(row)
            cid = row_character_id(row)
            if not sid or not cid:
                return item
            try:
                p = await get_profile(sid, cid, fast=True)
                item["info"] = profile_info(p, nickname, row_server_name(row), row)
            except Exception:
                pass
            return item

        items = await asyncio.gather(*[enrich(item) for item in items])

    items.sort(key=lambda x: x["info"]["combatPower"], reverse=True)
    return {"type": "multiple", "items": items}

def format_character_multiple(nickname, items):
    lines = [f"🔎 {nickname} · 전 서버", ""]

    for item in items[:10]:
        info = item["info"]
        cp = round(info["combatPower"] / 1000) if info["combatPower"] else "-"
        lines.append(f"• {info['server']} · {info['job']} · {cp}")

    lines += [
        "",
        f"예) !지켈{nickname}",
        f"예) !{nickname}지켈",
    ]
    return "\n".join(lines)


# =========================================================
# Full server-name character search
# !윤이시엘 / !시엘윤이 / !윤이지켈 / !지켈윤이
# =========================================================

# NotMeter/AION2 server lists are sequential:
# Elyos  = 1001 + index
# Asmodian = 2001 + index
SERVER_NAMES_ELYOS = (
    "시엘", "네자칸", "바이젤", "카이시넬", "유스티엘", "아리엘", "프레기온", "메스람타에다",
    "히타니에", "나니아", "타하바타", "루터스", "페르노스", "다미누", "카사카", "바카르마",
    "챈가룽", "코치룽", "이슈타르", "티아마트", "포에타", "베르테론", "나트하라", "탈리스라",
    "주미온", "나히드", "아사르", "칼리드", "라세이스", "페리온", "드라마타", "레다", "아울도르",
    "바크론", "나룬", "가르투아", "클로리스", "이오네", "테이나", "디모네스", "바고트", "아테론",
    "루틸리스", "실리아토르", "이드리스", "사티아", "에스티안", "라후", "라누만", "히브란",
    "우라훔", "라크슈미", "타몬", "티에", "두두리", "데르코스", "둔둔몽", "홀리아울",
)

SERVER_NAMES_ASMODIAN = (
    "이스라펠", "지켈", "트리니엘", "루미엘", "마르쿠탄", "아스펠", "에레슈키갈", "브리트라",
    "네몬", "하달", "루드라", "울고른", "무닌", "오다르", "젠카카", "크로메데", "콰이링",
    "바바룽", "파프니르", "인드나흐", "이스할겐", "알트가르드", "아그니타", "아티엘", "발데마르",
    "라그타", "게로드", "우르드", "에코", "지젤", "카샤파", "스토프", "베르크", "누아쿰",
    "그리실라", "산트라스", "루벤", "휴고", "크라키", "히스탄", "라트만", "시게베르트",
    "나즈문", "겔코스", "파톤", "펠레이르", "엘비다", "케투", "파이디온", "노툰", "무르트",
    "로탄", "쿠하푸", "두안카", "브로크", "왈터", "푸라킨", "이그누스",
)

SERVER_ID_MAP = {}

for index, name in enumerate(SERVER_NAMES_ELYOS):
    SERVER_ID_MAP[name] = 1001 + index

for index, name in enumerate(SERVER_NAMES_ASMODIAN):
    SERVER_ID_MAP[name] = 2001 + index

SERVER_NAMES = tuple(SERVER_ID_MAP.keys())


# =========================================================
# Own Character DB
# - DB first
# - NotMeter refresh/fill when available
# - If NotMeter is unavailable, last saved character data is still returned.
#
# Persistence:
#   Render Persistent Disk mount path recommended: /var/data
#   Optional env: CHARACTER_DB_PATH=/var/data/aion2_characters.db
# =========================================================

def _default_character_db_path():
    explicit = str(os.getenv("CHARACTER_DB_PATH") or "").strip()
    if explicit:
        return explicit
    return str(_state_path(
        "CHARACTER_DB_PATH",
        "aion2_characters.db",
        legacy_paths=("/tmp/aion2_characters.db", "/var/data/aion2_characters.db"),
    ))


CHARACTER_DB_PATH = _default_character_db_path()

CHARACTER_DB_LOCK = asyncio.Lock()


def _db_connect():
    path = Path(CHARACTER_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        timeout=5,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS characters (
            name_key TEXT NOT NULL,
            name TEXT NOT NULL,
            server_id INTEGER NOT NULL,
            server_name TEXT NOT NULL,
            character_id TEXT DEFAULT '',
            job TEXT DEFAULT '',
            combat_power INTEGER DEFAULT 0,
            level INTEGER DEFAULT 0,
            race TEXT DEFAULT '',
            profile_image TEXT DEFAULT '',
            source TEXT DEFAULT 'notmeter',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (name_key, server_id)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_characters_name
        ON characters(name_key)
    """)

    # v59: persist the full detailed profile so compare works from our own DB.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(characters)").fetchall()}
    if "full_profile_json" not in columns:
        conn.execute("ALTER TABLE characters ADD COLUMN full_profile_json TEXT DEFAULT ''")
    if "profile_updated_at" not in columns:
        conn.execute("ALTER TABLE characters ADD COLUMN profile_updated_at TEXT DEFAULT ''")
    if "official_compare_json" not in columns:
        conn.execute("ALTER TABLE characters ADD COLUMN official_compare_json TEXT DEFAULT ''")
    if "official_compare_updated_at" not in columns:
        conn.execute("ALTER TABLE characters ADD COLUMN official_compare_updated_at TEXT DEFAULT ''")

    conn.commit()
    return conn


def _db_info_from_row(row):
    return {
        "name": str(row["name"] or ""),
        "server": str(row["server_name"] or ""),
        "serverId": int(row["server_id"] or 0),
        "characterId": str(row["character_id"] or ""),
        "job": str(row["job"] or ""),
        "combatPower": int(row["combat_power"] or 0),
        "level": int(row["level"] or 0),
        "race": str(row["race"] or ""),
        "profileImage": str(row["profile_image"] or ""),
        "source": str(row["source"] or "db"),
        "updatedAt": str(row["updated_at"] or ""),
    }


async def character_db_upsert(info, character_id="", source="notmeter"):
    info = dict(info or {})

    name = str(info.get("name") or "").strip()
    server_name = str(info.get("server") or "").strip()
    server_id = int(
        info.get("serverId")
        or SERVER_ID_MAP.get(server_name)
        or 0
    )

    if not name or not server_id:
        return False

    if not server_name:
        for sname, sid in SERVER_ID_MAP.items():
            if int(sid) == server_id:
                server_name = sname
                break

    character_id = str(
        character_id
        or info.get("characterId")
        or ""
    ).strip()

    now = datetime.now(KST).isoformat()

    values = (
        name.casefold(),
        name,
        server_id,
        server_name,
        character_id,
        str(info.get("job") or ""),
        int(info.get("combatPower") or 0),
        int(info.get("level") or 0),
        str(info.get("race") or ""),
        str(info.get("profileImage") or ""),
        str(source or "notmeter"),
        now,
    )

    async with CHARACTER_DB_LOCK:
        conn = _db_connect()
        try:
            conn.execute("""
                INSERT INTO characters (
                    name_key,
                    name,
                    server_id,
                    server_name,
                    character_id,
                    job,
                    combat_power,
                    level,
                    race,
                    profile_image,
                    source,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name_key, server_id)
                DO UPDATE SET
                    name=excluded.name,
                    server_name=excluded.server_name,
                    character_id=CASE
                        WHEN excluded.character_id != ''
                        THEN excluded.character_id
                        ELSE characters.character_id
                    END,
                    job=CASE
                        WHEN excluded.job != ''
                        THEN excluded.job
                        ELSE characters.job
                    END,
                    combat_power=CASE
                        WHEN excluded.combat_power > 0
                        THEN excluded.combat_power
                        ELSE characters.combat_power
                    END,
                    level=CASE
                        WHEN excluded.level > 0
                        THEN excluded.level
                        ELSE characters.level
                    END,
                    race=CASE
                        WHEN excluded.race != ''
                        THEN excluded.race
                        ELSE characters.race
                    END,
                    profile_image=CASE
                        WHEN excluded.profile_image != ''
                        THEN excluded.profile_image
                        ELSE characters.profile_image
                    END,
                    source=excluded.source,
                    updated_at=excluded.updated_at
            """, values)

            conn.commit()
            return True

        finally:
            conn.close()


async def character_db_save_full_profile(nickname, server_name, profile, character_id=""):
    """Persist the last good detailed profile for self-DB compare."""
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip()
    server_id = int(SERVER_ID_MAP.get(server_name) or 0)
    if not nickname or not server_id or not isinstance(profile, dict) or not profile:
        return False

    try:
        raw = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return False

    now = datetime.now(KST).isoformat()
    async with CHARACTER_DB_LOCK:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM characters WHERE name_key=? AND server_id=?",
                (nickname.casefold(), server_id),
            ).fetchone()
            if not row:
                conn.execute(
                    """INSERT INTO characters
                    (name_key,name,server_id,server_name,character_id,updated_at,full_profile_json,profile_updated_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (nickname.casefold(), nickname, server_id, server_name, str(character_id or ""), now, raw, now),
                )
            else:
                conn.execute(
                    """UPDATE characters
                    SET full_profile_json=?, profile_updated_at=?,
                        character_id=CASE WHEN ? != '' THEN ? ELSE character_id END
                    WHERE name_key=? AND server_id=?""",
                    (raw, now, str(character_id or ""), str(character_id or ""), nickname.casefold(), server_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()


async def character_db_get_full_profile(nickname, server_name):
    """Return (row-like dict, full_profile) from our persistent DB when available."""
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip()
    server_id = int(SERVER_ID_MAP.get(server_name) or 0)
    if not nickname or not server_id:
        return None, None

    async with CHARACTER_DB_LOCK:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT * FROM characters WHERE name_key=? AND server_id=? LIMIT 1",
                (nickname.casefold(), server_id),
            ).fetchone()
            if not row:
                return None, None
            d = dict(row)
            raw = str(d.get("full_profile_json") or "")
            if not raw:
                return d, None
            try:
                profile = json.loads(raw)
            except Exception:
                return d, None
            return d, profile if isinstance(profile, dict) else None
        finally:
            conn.close()



async def character_db_save_official_compare(nickname, server_name, payload):
    """Persist the last successful NC-official compare payload separately from legacy full_profile_json."""
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip()
    server_id = int(SERVER_ID_MAP.get(server_name) or 0)
    if not nickname or not server_id or not isinstance(payload, dict) or not payload.get("ok"):
        return False
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return False
    now = datetime.now(KST).isoformat()
    async with CHARACTER_DB_LOCK:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM characters WHERE name_key=? AND server_id=?",
                (nickname.casefold(), server_id),
            ).fetchone()
            if not row:
                info = payload.get("info") or {}
                conn.execute(
                    """INSERT INTO characters
                    (name_key,name,server_id,server_name,character_id,job,combat_power,level,profile_image,source,updated_at,official_compare_json,official_compare_updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        nickname.casefold(), nickname, server_id, server_name,
                        str(info.get("characterId") or ""), str(info.get("job") or ""),
                        int(info.get("combatPower") or 0), int(info.get("level") or 0),
                        str(info.get("profileImage") or ""), "plaync-official", now, raw, now,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE characters
                    SET official_compare_json=?, official_compare_updated_at=?
                    WHERE name_key=? AND server_id=?""",
                    (raw, now, nickname.casefold(), server_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()


async def character_db_get_official_compare(nickname, server_name, max_age_seconds=900):
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip()
    server_id = int(SERVER_ID_MAP.get(server_name) or 0)
    if not nickname or not server_id:
        return None
    async with CHARACTER_DB_LOCK:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT official_compare_json, official_compare_updated_at FROM characters WHERE name_key=? AND server_id=? LIMIT 1",
                (nickname.casefold(), server_id),
            ).fetchone()
            if not row:
                return None
            raw = str(row["official_compare_json"] or "")
            updated = str(row["official_compare_updated_at"] or "")
            if not raw:
                return None
            if max_age_seconds is not None and updated:
                try:
                    age = (datetime.now(KST) - datetime.fromisoformat(updated)).total_seconds()
                    if age > float(max_age_seconds):
                        return None
                except Exception:
                    pass
            try:
                data = json.loads(raw)
            except Exception:
                return None
            return data if isinstance(data, dict) and data.get("ok") else None
        finally:
            conn.close()

async def character_db_get(nickname, server_name=None):
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip() or None

    if not nickname:
        return []

    async with CHARACTER_DB_LOCK:
        conn = _db_connect()

        try:
            if server_name:
                server_id = SERVER_ID_MAP.get(server_name)
                if not server_id:
                    return []

                rows = conn.execute("""
                    SELECT *
                    FROM characters
                    WHERE name_key = ?
                      AND server_id = ?
                    ORDER BY combat_power DESC
                """, (
                    nickname.casefold(),
                    int(server_id),
                )).fetchall()

            else:
                rows = conn.execute("""
                    SELECT *
                    FROM characters
                    WHERE name_key = ?
                    ORDER BY combat_power DESC, server_id ASC
                """, (
                    nickname.casefold(),
                )).fetchall()

            return [
                _db_info_from_row(row)
                for row in rows
            ]

        finally:
            conn.close()


async def character_db_stats():
    async with CHARACTER_DB_LOCK:
        conn = _db_connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM characters"
            ).fetchone()[0]

            recent = conn.execute("""
                SELECT
                    name,
                    server_name,
                    job,
                    combat_power,
                    updated_at
                FROM characters
                ORDER BY updated_at DESC
                LIMIT 20
            """).fetchall()

            return {
                "path": CHARACTER_DB_PATH,
                "persistentLikely": CHARACTER_DB_PATH.startswith("/var/data/"),
                "count": int(total),
                "recent": [
                    {
                        "name": str(row["name"] or ""),
                        "server": str(row["server_name"] or ""),
                        "job": str(row["job"] or ""),
                        "combatPower": int(row["combat_power"] or 0),
                        "updatedAt": str(row["updated_at"] or ""),
                    }
                    for row in recent
                ],
            }
        finally:
            conn.close()


def _db_resolved_from_infos(infos):
    infos = list(infos or [])

    if not infos:
        return {"type": "none"}

    if len(infos) == 1:
        info = infos[0]
        return {
            "type": "detail",
            "row": {
                "name": info.get("name"),
                "serverName": info.get("server"),
                "serverId": info.get("serverId"),
                "characterId": info.get("characterId"),
                "className": info.get("job"),
                "combatPower": info.get("combatPower"),
                "characterLevel": info.get("level"),
            },
            "profile": {},
            "info": info,
            "stones": [],
            "fromDb": True,
        }

    return {
        "type": "multiple",
        "items": [
            {
                "row": {
                    "name": info.get("name"),
                    "serverName": info.get("server"),
                    "serverId": info.get("serverId"),
                    "characterId": info.get("characterId"),
                    "className": info.get("job"),
                    "combatPower": info.get("combatPower"),
                    "characterLevel": info.get("level"),
                },
                "info": info,
            }
            for info in infos
        ],
        "fromDb": True,
    }


async def _save_notmeter_resolved(resolved):
    if not isinstance(resolved, dict):
        return

    if resolved.get("type") == "detail":
        info = resolved.get("info") or {}
        row = resolved.get("row") or {}
        await character_db_upsert(
            info,
            character_id=row_character_id(row),
            source="notmeter",
        )
        return

    if resolved.get("type") == "multiple":
        for item in resolved.get("items") or []:
            info = item.get("info") or {}
            row = item.get("row") or {}
            await character_db_upsert(
                info,
                character_id=row_character_id(row),
                source="notmeter",
            )


def _drop_character_live_caches(nickname, server_name=None, rows=None):
    """Drop only character-related caches for a truly fresh direct lookup."""
    nick_cf = str(nickname or "").strip().casefold()
    server_cf = str(server_name or "").strip().casefold()

    for key in list(_cache.keys()):
        skey = str(key)
        low = skey.casefold()

        if skey.startswith(f"char-search:{nick_cf}"):
            _cache.pop(key, None)
            continue

        if skey.startswith("official-char-search-v32:"):
            if nick_cf and nick_cf in low and (not server_cf or server_cf in low or ":*" in low):
                _cache.pop(key, None)
                continue

        if skey.startswith("official-json:") and "/search/aion2/search/v2/character" in skey:
            if nick_cf and f"keyword={nickname}" in skey:
                _cache.pop(key, None)

    for row in (rows or []):
        try:
            sid = int(row.get("serverId") or 0)
        except Exception:
            sid = 0
        cid = str(row.get("characterId") or "").strip()
        if sid and cid:
            _cache.pop(f"official-char-detail:{sid}:{cid}", None)
            for key in list(_cache.keys()):
                skey = str(key)
                if (
                    skey.startswith("official-json:")
                    and "/api/character/info" in skey
                    and f"characterId={cid}" in skey
                    and f"serverId={sid}" in skey
                ):
                    _cache.pop(key, None)


async def _official_get_json_live(url, params=None, timeout=None):
    """One-shot official request for direct character lookup.

    This intentionally bypasses every local cache and adds a cache-buster so
    stale search/detail responses are not reused by intermediate caches.
    """
    params = dict(params or {})
    params["_ts"] = int(time.time() * 1000)
    headers = dict(OFFICIAL_API_HEADERS)
    headers["Cache-Control"] = "no-cache, no-store, max-age=0"
    headers["Pragma"] = "no-cache"
    client = await get_http_client()
    response = await client.get(
        url,
        params=params,
        headers=headers,
        timeout=timeout or httpx.Timeout(
            connect=2.0, read=4.0, write=2.0, pool=2.0
        ),
    )
    response.raise_for_status()
    return response.json()


def _official_info_from_live_data(row, data):
    server_id = int(row.get("serverId") or 0)
    profile = data.get("profile") or {}

    def _to_int(value, default=0):
        try:
            return int(str(value or default).replace(",", ""))
        except Exception:
            return int(default or 0)

    cp = _to_int(
        profile.get("combatPower") or data.get("combatPower") or 0
    )
    level = _to_int(
        profile.get("level")
        or profile.get("characterLevel")
        or data.get("level")
        or row.get("characterLevel")
        or 0
    )
    item_level = _to_int(
        profile.get("itemLevel") or data.get("itemLevel") or 0
    )

    return {
        "name": _strip_html(
            profile.get("name")
            or profile.get("characterName")
            or data.get("name")
            or row.get("name")
            or ""
        ),
        "server": _strip_html(
            profile.get("serverName")
            or data.get("serverName")
            or row.get("serverName")
            or ""
        ),
        "serverId": server_id,
        "characterId": str(row.get("characterId") or ""),
        "job": _strip_html(
            profile.get("className")
            or data.get("className")
            or row.get("className")
            or ""
        ),
        "combatPower": cp,
        "itemLevel": item_level,
        "level": level,
        "race": "천족" if server_id < 2000 else "마족",
        "profileImage": str(
            profile.get("profileImage")
            or profile.get("imageUrl")
            or data.get("profileImage")
            or ""
        ),
        "officialUrl": str(row.get("officialUrl") or ""),
    }


async def _fresh_official_character(nickname, server_name=None):
    """Fast, uncached NC lookup used by direct character commands."""
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip() or None
    if not nickname:
        return None

    # Server-specific commands are the common path. Do exactly two live calls:
    # search once, detail once. No 120/180-second application cache is used.
    if server_name:
        sid = SERVER_ID_MAP.get(server_name)
        if not sid:
            return None

        try:
            data = await _official_get_json_live(
                OFFICIAL_CHARACTER_SEARCH_API,
                params={
                    "keyword": nickname,
                    "race": _official_server_race(sid),
                    "serverId": int(sid),
                },
            )
        except Exception:
            return None

        row = None
        for item in (data.get("list") or []):
            item_name = _strip_html(item.get("name"))
            try:
                item_sid = int(item.get("serverId") or sid)
            except Exception:
                continue
            if (
                item_name.casefold() != nickname.casefold()
                or item_sid != int(sid)
            ):
                continue
            cid = unquote(
                str(
                    item.get("characterId")
                    or item.get("charId")
                    or item.get("id")
                    or ""
                ).strip()
            )
            if not cid:
                continue
            row = {
                "name": item_name,
                "serverName": _strip_html(item.get("serverName"))
                or server_name,
                "serverId": item_sid,
                "className": _strip_html(item.get("className"))
                or _strip_html(item.get("jobName")),
                "characterLevel": int(
                    item.get("characterLevel") or item.get("level") or 0
                ),
                "characterId": cid,
                "officialUrl": (
                    f"{OFFICIAL_CHARACTER_BASE}/ko-kr/characters/"
                    f"{item_sid}/{quote(cid, safe='')}"
                ),
            }
            break

        if not row:
            return None

        try:
            detail = await _official_get_json_live(
                OFFICIAL_CHARACTER_INFO_API,
                params={
                    "lang": "ko",
                    "characterId": row["characterId"],
                    "serverId": int(row["serverId"]),
                },
            )
            info = _official_info_from_live_data(row, detail)
        except Exception:
            return None

        if str(info.get("name") or "").casefold() != nickname.casefold():
            return None

        return {
            "type": "detail",
            "row": row,
            "profile": {},
            "info": info,
            "stones": [],
        }

    # No server supplied: retain the existing all-server resolver.
    rows = await official_search_characters(nickname, None)
    if not rows:
        return None
    details = await asyncio.gather(
        *[official_load_detail(r) for r in rows[:12]],
        return_exceptions=True,
    )
    valid = []
    for row, info in zip(rows[:12], details):
        if isinstance(info, Exception) or not isinstance(info, dict):
            continue
        if str(info.get("name") or "").casefold() == nickname.casefold():
            valid.append((row, info))
    if not valid:
        return None
    valid.sort(
        key=lambda x: int(x[1].get("combatPower") or 0),
        reverse=True,
    )
    row, info = valid[0]
    return {
        "type": "detail",
        "row": row,
        "profile": {},
        "info": info,
        "stones": [],
    }


async def own_resolve_character(nickname, server_name=None):
    """Direct lookup: NC official live data first, DB only as last fallback."""
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip() or None

    # Fastest/latest path for a server-specific lookup: if we already know the
    # official characterId, skip the search API and fetch live character info
    # directly. This avoids stale search results and cuts the normal request to
    # one official NC call.
    if server_name:
        sid = SERVER_ID_MAP.get(server_name)
        if sid:
            try:
                db_rows = await character_db_get(nickname, server_name)
                for db_row in (db_rows or []):
                    cid = str(
                        db_row.get("characterId")
                        or db_row.get("character_id")
                        or ""
                    ).strip()
                    if not cid:
                        continue
                    row = {
                        "name": str(db_row.get("name") or nickname),
                        "serverName": str(
                            db_row.get("serverName")
                            or db_row.get("server_name")
                            or db_row.get("server")
                            or server_name
                        ),
                        "serverId": int(sid),
                        "characterId": cid,
                        "className": str(
                            db_row.get("job")
                            or db_row.get("className")
                            or ""
                        ),
                        "characterLevel": int(
                            db_row.get("level")
                            or db_row.get("characterLevel")
                            or 0
                        ),
                    }
                    detail = await _official_get_json_live(
                        OFFICIAL_CHARACTER_INFO_API,
                        params={
                            "lang": "ko",
                            "characterId": cid,
                            "serverId": int(sid),
                        },
                        timeout=httpx.Timeout(
                            connect=1.8, read=3.5, write=1.8, pool=1.8
                        ),
                    )
                    info = _official_info_from_live_data(row, detail)
                    if (
                        str(info.get("name") or "").casefold()
                        == nickname.casefold()
                    ):
                        resolved = {
                            "type": "detail",
                            "row": row,
                            "profile": {},
                            "info": info,
                            "stones": [],
                        }
                        await _save_notmeter_resolved(resolved)
                        return resolved
            except Exception:
                pass

    # 1) Official NC source, with character caches explicitly cleared.
    try:
        official = await asyncio.wait_for(
            _fresh_official_character(nickname, server_name),
            timeout=5.0,
        )
        if official:
            await _save_notmeter_resolved(official)
            return official
    except Exception:
        pass

    # 2) Existing notmeter route, also bypass its 180s search cache.
    _cache.pop(f"char-search:{nickname.casefold()}", None)
    try:
        fresh = await asyncio.wait_for(
            resolve_character(nickname, server_name),
            timeout=2.5,
        )
        if fresh and fresh.get("type") != "none":
            await _save_notmeter_resolved(fresh)
            return fresh
    except Exception:
        pass

    # 3) Last saved value only if both live sources fail.
    db_infos = await character_db_get(nickname, server_name)
    return _db_resolved_from_infos(db_infos)


async def _lookup_detail_with_saved_stones(resolved, nickname, server_name=None):
    """
    v61 shared lookup path:
    - basic character identity comes from our DB resolver
    - magic stones come from the same saved full profile used by compare
    - when no saved full profile exists yet, try one detailed fetch and persist it
    """
    if not isinstance(resolved, dict) or resolved.get("type") != "detail":
        return resolved

    info = resolved.get("info") or {}
    actual_server = str(server_name or info.get("server") or "").strip()
    actual_name = str(info.get("name") or nickname or "").strip()
    if not actual_name or not actual_server or actual_server not in SERVER_ID_MAP:
        return resolved

    profile = resolved.get("profile") if isinstance(resolved.get("profile"), dict) else None
    stones = resolved.get("stones") or []

    # Prefer a full profile already attached to the fresh resolver result.
    if profile:
        fresh_stones = aggregate_magic_stones(profile)
        if fresh_stones:
            stones = fresh_stones
        row = resolved.get("row") or {}
        try:
            await character_db_save_full_profile(
                actual_name, actual_server, profile, row_character_id(row)
            )
        except Exception:
            pass

    # Then use the same full-profile DB as the compare screen.
    if not stones:
        try:
            _, saved_profile = await character_db_get_full_profile(actual_name, actual_server)
            if saved_profile:
                stones = aggregate_magic_stones(saved_profile)
        except Exception:
            pass

    # First-time detailed lookup: fetch once, persist, and reuse for compare too.
    if not stones:
        try:
            _, fetched_profile = await _full_profile_for_exact_character(actual_name, actual_server)
            if fetched_profile:
                stones = aggregate_magic_stones(fetched_profile)
        except Exception:
            pass

    resolved = dict(resolved)
    resolved["stones"] = stones
    return resolved


async def own_character_lookup_smart(body):
    body = str(body or "").strip()

    nickname, explicit_server = parse_character_query(body)

    if explicit_server:
        resolved = await own_resolve_character(
            nickname,
            explicit_server,
        )

    else:
        parsed = split_server_and_nickname(body)

        if parsed:
            nickname, server_name = parsed
            resolved = await own_resolve_character(
                nickname,
                server_name,
            )

        else:
            nickname = body
            resolved = await own_resolve_character(
                nickname,
                None,
            )

    if resolved.get("type") == "none":
        return None

    if resolved.get("type") == "multiple":
        return format_character_multiple(
            nickname,
            resolved.get("items") or [],
        )

    # Direct character lookup must prepare the stone snapshot before returning
    # the Kakao card URL. The card page itself stays fast and reads this saved
    # full profile locally, so opening the card shows the magic-stone total
    # immediately without doing the expensive equipment crawl in the OG route.
    try:
        resolved = await asyncio.wait_for(
            _lookup_detail_with_saved_stones(
                resolved,
                nickname,
                None,
            ),
            timeout=5.5,
        )
    except Exception:
        pass

    return format_character_from_data(
        resolved.get("info") or {},
        resolved.get("stones") or [],
    )



# =========================================================
# Official AION2 character source - DIRECT JSON API
# =========================================================

OFFICIAL_CHARACTER_BASE = "https://aion2.plaync.com"

OFFICIAL_CHARACTER_SEARCH_API = (
    OFFICIAL_CHARACTER_BASE +
    "/ko-kr/api/search/aion2/search/v2/character"
)

OFFICIAL_CHARACTER_INFO_API = (
    OFFICIAL_CHARACTER_BASE +
    "/api/character/info"
)

OFFICIAL_API_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": "https://aion2.plaync.com/ko-kr/characters/index",
    "User-Agent": HEADERS["User-Agent"],
}

AION2_JOB_NAMES = (
    "검성", "수호성", "살성", "궁성",
    "마도성", "정령성", "치유성", "호법성", "권성",
)

SERVER_NAME_BY_ID = {
    int(server_id): name
    for name, server_id in SERVER_ID_MAP.items()
}


def _official_server_race(server_id):
    try:
        server_id = int(server_id)
    except Exception:
        return None

    return 1 if server_id < 2000 else 2


def _strip_html(value):
    value = str(value or "")
    value = re.sub(r"<[^>]+>", "", value)
    return unescape(value).strip()


async def _official_get_json(url, params=None, timeout=None):
    # NC can return 429 when the compare page is refreshed repeatedly.
    # Reuse identical responses briefly and honor Retry-After instead of
    # hammering the endpoint again.
    params = dict(params or {})
    cache_key = "official-json:" + str(url) + "?" + "&".join(
        f"{k}={params[k]}" for k in sorted(params)
    )
    cached = cache_get(cache_key, 120)
    if cached is not None:
        return cached

    client = await get_http_client()
    last_response = None
    for attempt in range(4):
        response = await client.get(
            url,
            params=params,
            headers=OFFICIAL_API_HEADERS,
            timeout=timeout or httpx.Timeout(
                connect=3.0,
                read=12.0,
                write=3.0,
                pool=2.0,
            ),
        )
        last_response = response
        if response.status_code != 429:
            response.raise_for_status()
            data = response.json()
            cache_set(cache_key, data)
            return data

        retry_after = response.headers.get("retry-after")
        try:
            wait_s = float(retry_after) if retry_after else min(1.5 * (2 ** attempt), 6.0)
        except Exception:
            wait_s = min(1.5 * (2 ** attempt), 6.0)
        await asyncio.sleep(max(0.8, min(wait_s, 8.0)))

    if last_response is not None:
        last_response.raise_for_status()
    raise httpx.HTTPStatusError("official request failed", request=None, response=None)


async def _official_search_html_fallback(nickname, server_name=None):
    """
    Official-site fallback.
    The character search page can contain character URLs inside serialized
    page data instead of ordinary <a> tags, so scan the raw HTML rather than
    relying on DOM anchor parsing.
    """
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip() or None

    if server_name:
        server_ids = [SERVER_ID_MAP.get(server_name)]
    else:
        server_ids = list(SERVER_NAME_BY_ID.keys())

    server_ids = [sid for sid in server_ids if sid]

    client = await get_http_client()
    results = []
    seen = set()

    # For a server-specific lookup only one page is needed.
    # For all-server lookup, query race-wide pages first.
    query_sets = []

    if server_name:
        sid = int(server_ids[0])
        query_sets.append((_official_server_race(sid), sid))
    else:
        query_sets.append((1, ""))
        query_sets.append((2, ""))

    for race, sid in query_sets:
        try:
            res = await client.get(
                "https://aion2.plaync.com/ko-kr/characters/index",
                params={
                    "keyword": nickname,
                    "race": race,
                    "serverId": sid,
                },
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ko-KR,ko;q=0.9",
                    "Referer": "https://aion2.plaync.com/ko-kr/characters/index",
                    "User-Agent": HEADERS["User-Agent"],
                },
                timeout=httpx.Timeout(
                    connect=3.0,
                    read=15.0,
                    write=3.0,
                    pool=2.0,
                ),
            )

            if res.status_code < 200 or res.status_code >= 300:
                continue

            raw = res.text or ""

            # Decode common Next/React serialized escaping.
            normalized = (
                raw
                .replace("\\/", "/")
                .replace("\\u002F", "/")
                .replace("\\u003D", "=")
                .replace("\\u0026", "&")
                .replace("&quot;", '"')
                .replace("&amp;", "&")
            )

            # Find every official character URL embedded anywhere in the page.
            matches = re.findall(
                r"/ko-kr/characters/(\d+)/([^\"'<>\s?&]+)",
                normalized,
            )

            for sid_text, char_key in matches:
                try:
                    found_sid = int(sid_text)
                except Exception:
                    continue

                if server_name and found_sid != int(server_ids[0]):
                    continue

                char_key = unquote(str(char_key or "").strip())
                if not char_key:
                    continue

                key = (found_sid, char_key)
                if key in seen:
                    continue
                seen.add(key)

                row = {
                    "name": nickname,
                    "serverName": SERVER_NAME_BY_ID.get(found_sid, ""),
                    "serverId": found_sid,
                    "className": "",
                    "characterLevel": 0,
                    "characterId": char_key,
                    "officialUrl": (
                        f"https://aion2.plaync.com/ko-kr/characters/"
                        f"{found_sid}/{quote(char_key, safe='')}"
                    ),
                }

                # Verify the candidate against the official profile page.
                info = await official_load_detail(row)

                if str(info.get("name") or "").casefold() != nickname.casefold():
                    continue

                if server_name and str(info.get("server") or "") != server_name:
                    continue

                row["name"] = info.get("name") or nickname
                row["serverName"] = info.get("server") or row["serverName"]
                row["className"] = info.get("job") or ""
                row["characterLevel"] = info.get("level") or 0

                results.append(row)

        except Exception:
            continue

    unique = {}
    for row in results:
        unique[
            (
                int(row.get("serverId") or 0),
                str(row.get("characterId") or ""),
            )
        ] = row

    rows = list(unique.values())
    rows.sort(
        key=lambda row: (
            int(row.get("serverId") or 999999),
            str(row.get("className") or ""),
        )
    )
    return rows


async def official_search_characters(nickname, server_name=None):
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip() or None

    if not nickname:
        return []

    cache_key = (
        f"official-char-search-v32:{nickname.casefold()}:"
        f"{(server_name or '*').casefold()}"
    )

    cached = cache_get(cache_key, 180)
    if cached is not None:
        return cached

    async def fetch_api(race, server_id=""):
        try:
            data = await _official_get_json(
                OFFICIAL_CHARACTER_SEARCH_API,
                params={
                    "keyword": nickname,
                    "race": int(race),
                    "serverId": int(server_id) if server_id else "",
                },
            )

            rows = []

            for item in (data.get("list") or []):
                item_name = _strip_html(item.get("name"))

                if item_name.casefold() != nickname.casefold():
                    continue

                sid = item.get("serverId") or server_id

                try:
                    sid = int(sid)
                except Exception:
                    continue

                char_id = (
                    item.get("characterId")
                    or item.get("charId")
                    or item.get("id")
                    or ""
                )

                char_id = unquote(str(char_id or "").strip())
                if not char_id:
                    continue

                rows.append({
                    "name": item_name,
                    "serverName": (
                        _strip_html(item.get("serverName"))
                        or SERVER_NAME_BY_ID.get(sid, "")
                    ),
                    "serverId": sid,
                    "className": (
                        _strip_html(item.get("className"))
                        or _strip_html(item.get("jobName"))
                    ),
                    "characterLevel": int(item.get("characterLevel") or item.get("level") or 0),
                    "characterId": char_id,
                    "officialUrl": (
                        f"{OFFICIAL_CHARACTER_BASE}/ko-kr/characters/"
                        f"{sid}/{quote(char_id, safe='')}"
                    ),
                })

            return rows

        except Exception:
            return []

    # 1) Official JSON API first
    rows = []

    if server_name:
        server_id = SERVER_ID_MAP.get(server_name)

        if not server_id:
            return []

        rows = await fetch_api(
            _official_server_race(server_id),
            server_id,
        )

        rows = [
            row for row in rows
            if (
                str(row.get("name") or "").casefold() == nickname.casefold()
                and int(row.get("serverId") or 0) == int(server_id)
            )
        ]

    else:
        a, b = await asyncio.gather(
            fetch_api(1, ""),
            fetch_api(2, ""),
        )
        rows = [
            row for row in (a + b)
            if str(row.get("name") or "").casefold() == nickname.casefold()
        ]

    # 2) If the API returns zero, use the official web page as fallback.
    if not rows:
        rows = await _official_search_html_fallback(
            nickname,
            server_name,
        )

    unique = {}
    for row in rows:
        unique[
            (
                int(row.get("serverId") or 0),
                str(row.get("characterId") or ""),
            )
        ] = row

    rows = list(unique.values())

    rows.sort(
        key=lambda row: (
            int(row.get("serverId") or 999999),
            str(row.get("className") or ""),
        )
    )

    cache_set(cache_key, rows)
    return rows


async def official_load_detail(row):
    server_id = int(row.get("serverId") or 0)
    character_id = str(row.get("characterId") or "")

    cache_key = (
        f"official-char-detail:"
        f"{server_id}:{character_id}"
    )

    cached = cache_get(cache_key, 180)
    if cached is not None:
        return cached

    info = {
        "name": str(row.get("name") or ""),
        "server": str(row.get("serverName") or ""),
        "serverId": server_id,
        "characterId": character_id,
        "job": str(row.get("className") or ""),
        "combatPower": 0,
        "itemLevel": 0,
        "level": int(row.get("characterLevel") or 0),
        "race": "천족" if server_id < 2000 else "마족",
        "profileImage": "",
        "officialUrl": str(row.get("officialUrl") or ""),
    }

    try:
        data = await _official_get_json(
            OFFICIAL_CHARACTER_INFO_API,
            params={
                "lang": "ko",
                "characterId": character_id,
                "serverId": server_id,
            },
        )

        profile = data.get("profile") or {}

        cp = (
            profile.get("combatPower")
            or data.get("combatPower")
            or 0
        )

        try:
            cp = int(str(cp).replace(",", ""))
        except Exception:
            cp = 0

        job = (
            profile.get("className")
            or data.get("className")
            or info["job"]
        )

        level = (
            profile.get("level")
            or profile.get("characterLevel")
            or data.get("level")
            or info["level"]
        )

        try:
            level = int(level)
        except Exception:
            level = info["level"]

        item_level = (
            profile.get("itemLevel")
            or data.get("itemLevel")
            or 0
        )

        try:
            item_level = int(str(item_level).replace(",", ""))
        except Exception:
            item_level = 0

        profile_image = (
            profile.get("profileImage")
            or profile.get("imageUrl")
            or data.get("profileImage")
            or ""
        )

        server_name = (
            profile.get("serverName")
            or data.get("serverName")
            or info["server"]
        )

        char_name = (
            profile.get("name")
            or profile.get("characterName")
            or data.get("name")
            or info["name"]
        )

        info.update({
            "name": _strip_html(char_name),
            "server": _strip_html(server_name),
            "job": _strip_html(job),
            "combatPower": cp,
            "itemLevel": item_level,
            "level": level,
            "profileImage": str(profile_image or ""),
        })

    except Exception:
        pass

    cache_set(cache_key, info)
    return info


async def official_resolve_character(nickname, server_name=None):
    rows = await official_search_characters(
        nickname,
        server_name=server_name,
    )

    if not rows:
        return {"type": "none"}

    if len(rows) == 1:
        info = await official_load_detail(rows[0])

        return {
            "type": "detail",
            "row": rows[0],
            "profile": {},
            "info": info,
            "stones": [],
        }

    details = await asyncio.gather(
        *[
            official_load_detail(row)
            for row in rows[:20]
        ]
    )

    items = []

    for row, info in zip(
        rows[:20],
        details,
    ):
        items.append({
            "row": row,
            "info": info,
        })

    items.sort(
        key=lambda x: int(
            x["info"].get("combatPower") or 0
        ),
        reverse=True,
    )

    return {
        "type": "multiple",
        "items": items,
    }


async def official_character_lookup_smart(body):
    body = str(body or "").strip()

    nickname, explicit_server = parse_character_query(body)

    if explicit_server:
        resolved = await official_resolve_character(
            nickname,
            explicit_server,
        )

    else:
        parsed = split_server_and_nickname(body)

        if parsed:
            nickname, server_name = parsed

            resolved = await official_resolve_character(
                nickname,
                server_name,
            )

        else:
            nickname = body

            resolved = await official_resolve_character(
                nickname,
                None,
            )

    if resolved["type"] == "none":
        return None

    if resolved["type"] == "multiple":
        return format_character_multiple(
            nickname,
            resolved["items"],
        )

    return format_character_from_data(
        resolved["info"],
        [],
    )


def split_server_and_nickname(text: str):
    """
    서버명을 닉네임 앞/뒤 어느 쪽에 붙여도 인식.
      윤이시엘 -> ("윤이", "시엘")
      시엘윤이 -> ("윤이", "시엘")
      윤이지켈 -> ("윤이", "지켈")
      지켈윤이 -> ("윤이", "지켈")
    """
    text = str(text or "").strip()
    folded = text.casefold()

    # 긴 서버명을 먼저 검사해서 짧은 이름 오인식 최소화
    for server in sorted(SERVER_NAMES, key=len, reverse=True):
        sf = server.casefold()

        if folded.startswith(sf) and len(text) > len(server):
            nickname = text[len(server):].strip()
            if nickname:
                return nickname, server

        if folded.endswith(sf) and len(text) > len(server):
            nickname = text[:-len(server)].strip()
            if nickname:
                return nickname, server

    return None


async def search_character_on_server(nickname: str, server_name: str):
    """
    특정 서버 검색.
    1) serverId를 넣은 검색을 먼저 시도
    2) 결과가 없으면 전 서버 검색으로 fallback
    """
    target_id = SERVER_ID_MAP.get(server_name)
    target_name = server_name.casefold()
    target_nickname = nickname.casefold()

    rows = []

    if target_id:
        try:
            data = await character_api_get(
                "/character/v1/search",
                {
                    "name": nickname,
                    "serverId": target_id,
                    "region": "kr",
                    "lang": "ko",
                    "fast": "1",
                },
                timeout=httpx.Timeout(
                    connect=1.0,
                    read=3.2,
                    write=1.0,
                    pool=1.0,
                ),
            )

            rows = data.get("results") or data.get("characters") or []
        except Exception:
            rows = []

    # Direct search result
    matched = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        if row_name(row).casefold() != target_nickname:
            continue

        sid = row_server_id(row)
        sname = row_server_name(row)

        if target_id and sid == target_id:
            matched.append(row)
        elif sname and sname.casefold() == target_name:
            matched.append(row)

    if matched:
        return matched

    # Fallback: regular all-server exact-name search
    all_rows = await search_characters_all_servers(nickname)

    for row in all_rows:
        sid = row_server_id(row)
        sname = row_server_name(row)

        if target_id and sid == target_id:
            matched.append(row)
        elif sname and sname.casefold() == target_name:
            matched.append(row)

    return matched


async def character_lookup_server_fast(nickname: str, server_name: str):
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip()

    if not nickname or server_name not in SERVER_ID_MAP:
        return None

    matched = await search_character_on_server(
        nickname,
        server_name,
    )

    if not matched:
        return None

    matched.sort(
        key=lambda row: int(row.get("combatPower") or 0),
        reverse=True,
    )

    # 상세 프로필은 최종 선택 1명만 조회
    detail = await load_detail(matched[0], nickname)

    return format_character_from_data(
        detail["info"],
        detail.get("stones") or [],
    )


async def character_lookup_smart(body: str):
    return await own_character_lookup_smart(body)


async def character_lookup(nickname_query: str):
    return await own_character_lookup_smart(nickname_query)


async def debug_official_equipped_stats_v2(nickname: str = '윤이', server: str = '지켈'):
    nickname = str(nickname or '').strip()
    server = str(server or '').strip()
    server_id = int(SERVER_ID_MAP.get(server) or 0)
    if not nickname or not server_id:
        return {'ok': False, 'error': 'nickname/server 확인 필요'}

    # v72: use the strict official resolver here too. Compare calls this loader,
    # so fixing only /api/compare resolver was not enough; this function used to
    # re-run the old DB/search path and could still return characterId not found.
    row = await _official_resolve_character_strict(nickname, server)
    if not row or not row.get('characterId'):
        return {'ok': False, 'error': f'{nickname}[{server}] characterId not found'}

    cid = str(row.get('characterId') or '').strip()
    sid = int(row.get('serverId') or server_id)

    # Fresh character/detail/compare data: clear only this character's official
    # info/equipment/item caches before loading. This prevents an old 2m/30m
    # snapshot from being shown after equipment or magic-stone changes.
    for cache_key in list(_cache.keys()):
        skey = str(cache_key)
        if (
            skey.startswith('official-json:')
            and f'characterId={cid}' in skey
            and f'serverId={sid}' in skey
            and (
                '/api/character/info' in skey
                or '/api/character/equipment' in skey
            )
        ):
            _cache.pop(cache_key, None)
        elif skey.startswith(f'official-eq-item:{sid}:{cid}:'):
            _cache.pop(cache_key, None)

    params = {'lang': 'ko', 'characterId': cid, 'serverId': sid}
    try:
        equipment_payload = await _official_get_json(
            f'{OFFICIAL_CHARACTER_BASE}/api/character/equipment',
            params=params,
            timeout=httpx.Timeout(connect=4.0, read=20.0, write=4.0, pool=4.0),
        )
        info_payload = await _official_get_json(
            OFFICIAL_CHARACTER_INFO_API,
            params=params,
            timeout=httpx.Timeout(connect=4.0, read=20.0, write=4.0, pool=4.0),
        )
    except Exception as e:
        return {'ok': False, 'error': f'official base API 실패: {type(e).__name__}: {str(e)[:300]}'}

    eq_obj = equipment_payload.get('equipment') if isinstance(equipment_payload, dict) and isinstance(equipment_payload.get('equipment'), dict) else {}
    eq_list = eq_obj.get('equipmentList') if isinstance(eq_obj.get('equipmentList'), list) else []

    headers = dict(OFFICIAL_API_HEADERS)
    headers['Referer'] = f'{OFFICIAL_CHARACTER_BASE}/ko-kr/characters'
    base = f'{OFFICIAL_CHARACTER_BASE}/api/character/equipment/item'
    sem = asyncio.Semaphore(8)

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=headers) as client:
        async def fetch_one(x):
            if not isinstance(x, dict):
                return {'ok': False, 'error': 'bad equipment row'}
            item_id = x.get('id')
            enchant = int(x.get('enchantLevel') or 0)
            exceed = int(x.get('exceedLevel') or 0)
            slot_pos = int(x.get('slotPos') or 0)
            q = {
                'lang': 'ko', 'id': item_id, 'characterId': cid, 'serverId': sid,
                'enchantLevel': enchant, 'exceedLevel': exceed, 'slotPos': slot_pos,
            }
            detail_cache_key = f"official-eq-item:{sid}:{cid}:{item_id}:{enchant}:{exceed}:{slot_pos}"
            cached_detail = cache_get(detail_cache_key, 1800)
            if isinstance(cached_detail, dict):
                data = cached_detail
                return {
                    'ok': True, 'status': 200, 'slotPos': slot_pos, 'slot': x.get('slotPosName'),
                    'id': item_id, 'name': x.get('name'), 'enchantLevel': enchant,
                    'exceedLevel': exceed, 'data': data,
                }
            async with sem:
                try:
                    r = None
                    for attempt in range(4):
                        r = await client.get(base, params=q)
                        if r.status_code != 429:
                            break
                        retry_after = r.headers.get('retry-after')
                        try:
                            wait_s = float(retry_after) if retry_after else min(1.0 * (2 ** attempt), 5.0)
                        except Exception:
                            wait_s = min(1.0 * (2 ** attempt), 5.0)
                        await asyncio.sleep(max(0.7, min(wait_s, 6.0)))
                    data = r.json() if r is not None and r.status_code == 200 else None
                    if isinstance(data, dict):
                        cache_set(detail_cache_key, data)
                    return {
                        'ok': r.status_code == 200 and isinstance(data, dict),
                        'status': r.status_code,
                        'slotPos': slot_pos,
                        'slot': x.get('slotPosName'),
                        'id': item_id,
                        'name': x.get('name'),
                        'enchantLevel': enchant,
                        'exceedLevel': exceed,
                        'data': data if isinstance(data, dict) else None,
                    }
                except Exception as e:
                    return {
                        'ok': False, 'slotPos': slot_pos, 'slot': x.get('slotPosName'),
                        'id': item_id, 'name': x.get('name'), 'error': repr(e),
                    }
        details = await asyncio.gather(*(fetch_one(x) for x in eq_list))

    def parse_num(v):
        if v is None:
            return None, False
        s = str(v).strip().replace(',', '')
        is_pct = '%' in s
        s = s.replace('%', '').replace('+', '')
        try:
            return float(s), is_pct
        except Exception:
            return None, is_pct

    buckets = {}
    def add_bucket(source, stat_id, stat_name, raw_value, item_name, slot):
        num, is_pct = parse_num(raw_value)
        key = str(stat_id or stat_name or '').strip()
        if not key or num is None:
            return
        k = f'{source}:{key}'
        b = buckets.setdefault(k, {
            'source': source, 'id': stat_id, 'name': stat_name, 'unit': '%' if is_pct else 'flat',
            'sum': 0.0, 'entries': []
        })
        # Keep percent and flat variants separate if an id ever mixes units.
        if b['unit'] != ('%' if is_pct else 'flat'):
            k2 = f'{k}:{"pct" if is_pct else "flat"}'
            b = buckets.setdefault(k2, {
                'source': source, 'id': stat_id, 'name': stat_name, 'unit': '%' if is_pct else 'flat',
                'sum': 0.0, 'entries': []
            })
        b['sum'] += num
        b['entries'].append({'item': item_name, 'slot': slot, 'value': raw_value})

    def extract_item_skill_options(data):
        out, seen = [], set()
        if not isinstance(data, dict):
            return out
        skip_top = {'mainStats','subStats','magicStoneStat','godStoneStat','sources'}
        def walk(node, path=''):
            if isinstance(node, dict):
                path_l = path.lower()
                kind = str(node.get('type') or node.get('category') or node.get('optionType') or '').lower()
                name = node.get('skillName') or node.get('name') or node.get('label')
                skillish = ('skill' in path_l or 'passive' in path_l or '스킬' in path_l or 'skill' in kind or 'passive' in kind)
                if skillish and isinstance(name, str) and name.strip():
                    value = node.get('value')
                    if value in (None, ''):
                        value = node.get('skillLevel')
                    if value in (None, ''):
                        value = node.get('level')
                    if value in (None, ''):
                        value = node.get('extra')
                    key = (name.strip(), str(value or ''), str(node.get('id') or node.get('skillId') or ''))
                    if key not in seen:
                        seen.add(key)
                        out.append({
                            'id': node.get('id') or node.get('skillId'),
                            'name': name.strip(),
                            'value': value,
                            'level': node.get('skillLevel') or node.get('level'),
                            'icon': node.get('icon') or '',
                            'path': path,
                        })
                for k, v in node.items():
                    if not path and k in skip_top:
                        continue
                    walk(v, f'{path}.{k}' if path else str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f'{path}[{i}]')
        walk(data)
        return out

    compact_items = []
    success_count = 0
    for d in details:
        if not d.get('ok') or not isinstance(d.get('data'), dict):
            compact_items.append({k: d.get(k) for k in ('ok','status','slotPos','slot','id','name','error') if k in d})
            continue
        success_count += 1
        data = d['data']
        item_name = data.get('name') or d.get('name')
        slot = d.get('slot')
        main_stats = data.get('mainStats') if isinstance(data.get('mainStats'), list) else []
        sub_stats = data.get('subStats') if isinstance(data.get('subStats'), list) else []
        stones = data.get('magicStoneStat') if isinstance(data.get('magicStoneStat'), list) else []
        godstones = data.get('godStoneStat') if isinstance(data.get('godStoneStat'), list) else []

        for s in main_stats:
            if isinstance(s, dict):
                add_bucket('main.value', s.get('id'), s.get('name'), s.get('value'), item_name, slot)
                if s.get('extra') not in (None, '', '0', '0%'):
                    add_bucket('main.extra', s.get('id'), s.get('name'), s.get('extra'), item_name, slot)
        for s in sub_stats:
            if isinstance(s, dict):
                add_bucket('sub', s.get('id'), s.get('name'), s.get('value'), item_name, slot)
        for s in stones:
            if isinstance(s, dict):
                add_bucket('magicStone', s.get('id'), s.get('name'), s.get('value'), item_name, slot)

        compact_items.append({
            'ok': True,
            'slotPos': d.get('slotPos'), 'slot': slot, 'id': data.get('id'), 'name': item_name,
            'icon': data.get('icon') or '', 'grade': data.get('gradeName') or data.get('grade') or '',
            'enchantLevel': d.get('enchantLevel'), 'exceedLevel': d.get('exceedLevel'),
            'mainStats': main_stats, 'subStats': sub_stats, 'magicStoneStat': stones, 'godStoneStat': godstones,
            'skillOptions': extract_item_skill_options(data),
            'sources': data.get('sources') if isinstance(data.get('sources'), list) else [],
        })

    sums = list(buckets.values())
    sums.sort(key=lambda x: (str(x.get('name') or ''), str(x.get('source') or '')))
    for b in sums:
        b['sum'] = round(b['sum'], 4)

    info_stat_obj = info_payload.get('stat') if isinstance(info_payload, dict) and isinstance(info_payload.get('stat'), dict) else {}
    info_stat_list = info_stat_obj.get('statList') if isinstance(info_stat_obj.get('statList'), list) else []

    skill_obj = equipment_payload.get('skill') if isinstance(equipment_payload, dict) and isinstance(equipment_payload.get('skill'), dict) else {}
    skill_list = skill_obj.get('skillList') if isinstance(skill_obj.get('skillList'), list) else []

    return {
        'ok': True,
        'schema': 'OFFICIAL_EQUIPPED_STATS_V2',
        'source': ['plaync-character-info','plaync-character-equipment','plaync-character-equipment-item'],
        'name': nickname, 'server': server, 'serverId': sid, 'characterId': cid,
        'equipmentCount': len(eq_list), 'detailSuccessCount': success_count,
        'infoStatList': info_stat_list,
        'profileData': info_payload.get('profile') if isinstance(info_payload, dict) else None,
        'titleData': info_payload.get('title') if isinstance(info_payload, dict) else None,
        'daevanionData': info_payload.get('daevanion') if isinstance(info_payload, dict) else None,
        'petwingData': equipment_payload.get('petwing') if isinstance(equipment_payload, dict) else None,
        'skillCount': len(skill_list),
        'skills': [
            {'id': s.get('id'), 'name': s.get('name'), 'category': s.get('category'), 'level': s.get('skillLevel'),
             'acquired': s.get('acquired'), 'equip': s.get('equip'), 'icon': s.get('icon') or '', 'needLevel': s.get('needLevel')}
            for s in skill_list if isinstance(s, dict)
        ],
        'aggregatedRawSums': sums,
        'items': compact_items,
    }



def _official_magic_stone_totals(src):
    """Single source of truth for NC official equipped magic-stone totals."""
    groups = {}
    pct_ids = {
        "AmplifyWeaponDamage", "AmplifyAllDamage",
        "PvEAmplifyDamage", "AmplifyBossDamage",
        "AmplifyCriticalDamage", "AmplifyBackAttack",
        "AmplifyFrontAttack",
    }
    for item in (src or {}).get("items") or []:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        for ms in item.get("magicStoneStat") or []:
            if not isinstance(ms, dict):
                continue
            stat_id = str(ms.get("id") or "").strip()
            name = (
                _canonical_from_stat_id(stat_id)
                or _canonical_offense_name(ms.get("name"))
                or str(ms.get("name") or stat_id).strip()
            )
            if not name:
                continue
            raw = str(ms.get("value") or "").replace("+", "").replace(",", "").strip()
            try:
                value = float(raw.replace("%", ""))
            except Exception:
                continue
            if stat_id in pct_ids and "%" not in raw:
                value /= 100.0
            g = groups.setdefault(name, {"name": name, "count": 0, "total": 0.0})
            g["count"] += 1
            g["total"] += value

    rows = []
    for g in groups.values():
        total = round(float(g.get("total") or 0.0), 4)
        count = int(g.get("count") or 0)
        rows.append({
            "name": g.get("name") or "마석",
            "count": count,
            "total": total,
            "average": round(total / count, 4) if count else 0.0,
        })

    priority = {
        "전방 피해 증폭": 0,
        "후방 피해 증폭": 1,
        "무기 피해 증폭": 2,
        "치명타 피해 증폭": 3,
        "피해 증폭": 4,
        "PVE 피해 증폭": 5,
        "보스 피해 증폭": 6,
    }
    rows.sort(key=lambda r: (priority.get(str(r.get("name") or ""), 999), str(r.get("name") or "")))
    return rows

def _official_magic_stones_for_card(src):
    rows = _official_magic_stone_totals(src)
    percent_names = {
        "전방 피해 증폭", "후방 피해 증폭", "무기 피해 증폭",
        "치명타 피해 증폭", "피해 증폭", "PVE 피해 증폭",
        "보스 피해 증폭",
    }
    return [
        (
            str(row.get("name") or "마석"),
            "+" + pretty_number(float(row.get("total") or 0.0)) + ("%" if str(row.get("name") or "") in percent_names else ""),
        )
        for row in rows
    ]

async def character_card_data(nickname: str, server_name: str):
    # Card basic info stays on the normal fresh character resolver.
    # Magic-stone totals, however, must use the same NC official equipment/item
    # source as the detail/compare page so the card never shows an older
    # NotMeter/full-profile snapshot when equipment was changed recently.
    resolved = await own_resolve_character(
        nickname,
        server_name,
    )

    if resolved.get("type") != "detail":
        return None

    info = resolved.get("info") or {}
    row = resolved.get("row") or {}
    sid = row_server_id(row) or SERVER_ID_MAP.get(server_name)
    cid = row_character_id(row)
    stones = []

    # 1) Fresh NC official equipment/item data for the card.
    # Clear only this character's official equipment caches. No other feature
    # state/cache is touched.
    if sid and cid:
        try:
            sid = int(sid)
            cid = str(cid).strip()

            for key in list(_cache.keys()):
                skey = str(key)
                if (
                    skey.startswith("official-json:")
                    and f"characterId={cid}" in skey
                    and f"serverId={sid}" in skey
                    and (
                        "/api/character/equipment" in skey
                        or "/api/character/info" in skey
                    )
                ):
                    _cache.pop(key, None)
                elif skey.startswith(f"official-eq-item:{sid}:{cid}:"):
                    _cache.pop(key, None)

            src = await asyncio.wait_for(
                debug_official_equipped_stats_v2(
                    nickname=str(info.get("name") or nickname),
                    server=str(info.get("server") or server_name),
                ),
                timeout=12.0,
            )

            if isinstance(src, dict) and src.get("ok"):
                stones = _official_magic_stones_for_card(src)
        except Exception:
            stones = []

    # 2) Fallback only: legacy live full profile, then saved profile.
    # These paths are used only when NC official equipment detail is temporarily
    # unavailable, so a successful official response always wins.
    if not stones and sid and cid:
        try:
            profile = await asyncio.wait_for(
                get_profile(int(sid), cid, fast=False),
                timeout=4.8,
            )
            if profile:
                stones = aggregate_magic_stones(profile)
                try:
                    await character_db_save_full_profile(
                        str(info.get("name") or nickname),
                        str(info.get("server") or server_name),
                        profile,
                        cid,
                    )
                except Exception:
                    pass
        except Exception:
            pass

    if not stones:
        try:
            _, saved_profile = await character_db_get_full_profile(
                str(info.get("name") or nickname),
                str(info.get("server") or server_name),
            )
            if saved_profile:
                stones = aggregate_magic_stones(saved_profile)
        except Exception:
            pass

    # Display order only: put the requested damage-amplification stones first.
    # No data source or calculation is changed here.
    _stone_display_priority = {
        "전방 피해 증폭": 0,
        "후방 피해 증폭": 1,
        "무기 피해 증폭": 2,
        "치명타 피해 증폭": 3,
        "피해 증폭": 4,
        "PVE 피해 증폭": 5,
        "보스 피해 증폭": 6,
    }
    stones = sorted(
        stones,
        key=lambda x: (_stone_display_priority.get(str(x[0]), 999), str(x[0]))
    )

    return {
        "info": info,
        "stones": stones,
    }



# =========================================================
# Detailed Character Compare API
# =========================================================

COMPARE_STAT_KEYS = (
    "attack", "attackPower", "physicalAttack", "magicAttack",
    "accuracy", "hit", "critical", "criticalHit",
    "hp", "maxHp", "defense", "physicalDefense", "magicDefense",
    "weaponDamageIncrease", "backDamageIncrease", "rearDamageIncrease",
    "frontDamageIncrease", "criticalDamageIncrease",
    "bossDamageIncrease", "pveDamageIncrease",
    "attackSpeed", "castSpeed", "moveSpeed",
)

# These final offensive stats are percentages even when the API returns a
# numeric value without a literal '%' sign.  Keeping this separate from the
# display-name parser prevents valid numeric API fields from being rendered as
# plain numbers.
PERCENT_CANONICAL_STATS = {
    "공격력 증가율",
    "피해 증폭",
    "무기 피해 증폭",
    "PVE 피해 증폭",
    "보스 피해 증폭",
    "치명타 피해 증폭",
    "전방 피해 증폭",
    "후방 피해 증폭",
    "공격 속도",
    "시전 속도",
}

def _safe_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("%", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None

def _walk_dicts(node, depth=0):
    if depth > 8:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _walk_dicts(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, (dict, list)):
                yield from _walk_dicts(v, depth + 1)

def _detect_stat_unit(raw):
    s = str(raw or "").strip()
    if "%" in s:
        return "percent"
    if re.search(r"\b(?:ms|sec|s)\b", s, re.I):
        return "time"
    return "number"


STAT_ID_CANONICAL_MAP = {
    # Known NotMeter/AION2 internal stat ids.
    "AmplifyWeaponDamage": "무기 피해 증폭",
    "AmplifyCriticalDamage": "치명타 피해 증폭",
    "AmplifyBackAttack": "후방 피해 증폭",
    "AmplifyRearAttack": "후방 피해 증폭",
    "AmplifyFrontAttack": "전방 피해 증폭",
    "AmplifyBossDamage": "보스 피해 증폭",
    "AmplifyPveDamage": "PVE 피해 증폭",
    "AmplifyPVEDamage": "PVE 피해 증폭",
    "WeaponDamageIncrease": "무기 피해 증폭",
    "BossDamageIncrease": "보스 피해 증폭",
    "PveDamageIncrease": "PVE 피해 증폭",
    "PVEDamageIncrease": "PVE 피해 증폭",
    "RearDamageIncrease": "후방 피해 증폭",
    "BackDamageIncrease": "후방 피해 증폭",
    "FrontDamageIncrease": "전방 피해 증폭",
    "CriticalDamageIncrease": "치명타 피해 증폭",
}


def _canonical_from_stat_id(value):
    raw = str(value or "").strip()
    if not raw:
        return None

    direct = STAT_ID_CANONICAL_MAP.get(raw)
    if direct:
        return direct

    folded = re.sub(r"[^a-z0-9]", "", raw.lower())
    for key, canonical in STAT_ID_CANONICAL_MAP.items():
        if re.sub(r"[^a-z0-9]", "", key.lower()) == folded:
            return canonical

    # Also accept the public/camelCase keys already listed in the canonical aliases.
    for canonical, aliases in OFFENSE_CANONICAL_GROUPS.items():
        for alias in (canonical, *aliases):
            if re.sub(r"[^a-z0-9가-힣]", "", str(alias).lower()) == re.sub(r"[^a-z0-9가-힣]", "", raw.lower()):
                return canonical
    return None


def _normalize_percent_internal(canonical, numeric, raw, source_key=""):
    """Convert known internal hundredth-percent values to display percentage-points."""
    if numeric is None or canonical not in PERCENT_CANONICAL_STATS:
        return numeric

    # A literal percent sign is already display-scale.
    if "%" in str(raw or ""):
        return float(numeric)

    key = str(source_key or "")
    key_low = key.lower()
    # NotMeter magic-stone Amplify* values use hundredths of a percent
    # (e.g. 250 -> 2.5%).  Apply only to clearly identified internal ids.
    if key in PERCENT_STONE_IDS or "amplify" in key_low:
        return float(numeric) / 100.0

    return float(numeric)


def extract_profile_stats(profile):
    """
    Recover offensive final stats from the full character payload.

    Key rule:
    - search explicit stat containers first
    - then recursively accept ONLY exact/near-exact known offensive stat labels
    - never use arbitrary equipment option numbers as character final stats
    """
    if not isinstance(profile, dict):
        return []

    stats = {}

    offensive_names = []
    for canonical, aliases in OFFENSE_CANONICAL_GROUPS.items():
        offensive_names.append(canonical)
        offensive_names.extend(list(aliases))

    def canonical_exact(name):
        by_id = _canonical_from_stat_id(name)
        if by_id:
            return by_id
        src = re.sub(r"\s+", " ", str(name or "").strip())
        low = src.lower()
        if not low:
            return None

        # exact/normalized alias match first
        for canonical, aliases in OFFENSE_CANONICAL_GROUPS.items():
            choices = [canonical, *aliases]
            for alias in choices:
                a = re.sub(r"\s+", " ", str(alias).strip()).lower()
                if low == a:
                    return canonical

        # controlled suffix/prefix variants commonly used by API labels
        cleaned = re.sub(r"[\[\](){}:：]", " ", low)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        for canonical, aliases in OFFENSE_CANONICAL_GROUPS.items():
            for alias in [canonical, *aliases]:
                a = re.sub(r"\s+", " ", str(alias).strip()).lower()
                if cleaned in (
                    f"{a} 수치", f"{a} 능력치", f"최종 {a}", f"{a} 최종",
                    f"{a} 증가", f"{a} 증가율"
                ):
                    return canonical
        return None

    def add(name, raw, source_key="", priority=0):
        canonical = canonical_exact(name)
        if not canonical:
            return
        numeric = _safe_num(raw)
        if numeric is None:
            return

        unit = _detect_stat_unit(raw)
        if canonical in PERCENT_CANONICAL_STATS and unit == "number":
            unit = "percent"
        numeric = _normalize_percent_internal(canonical, numeric, raw, source_key or name)
        row = {
            "name": canonical,
            "originalName": str(name or ""),
            "value": numeric,
            "raw": raw,
            "unit": unit,
            "sourceKey": str(source_key or ""),
            "confidencePriority": int(priority or 0),
            "_priority": priority,
        }

        old = stats.get(canonical)
        if old is None:
            stats[canonical] = row
            return

        # Prefer higher-confidence containers and display values.
        if priority > old.get("_priority", 0):
            stats[canonical] = row
        elif priority == old.get("_priority", 0):
            if old.get("unit") != "percent" and unit == "percent":
                stats[canonical] = row

    def scan_container(node, priority=100, prefix=""):
        if isinstance(node, dict):
            # row-shaped object
            row_name = (
                node.get("name")
                or node.get("statName")
                or node.get("displayName")
                or node.get("optionName")
                or node.get("label")
            )
            stat_identity = (
                node.get("statId") or node.get("statID") or node.get("id")
                or node.get("key") or node.get("code") or node.get("type")
            )
            raw = node.get("displayValue")
            if raw is None: raw = node.get("value")
            if raw is None: raw = node.get("statValue")
            if raw is None: raw = node.get("amount")
            if raw is None: raw = node.get("finalValue")
            if row_name:
                add(row_name, raw, stat_identity or prefix, priority)
            # Critical fallback: some payloads expose only an internal stat id.
            if stat_identity and _canonical_from_stat_id(stat_identity):
                add(stat_identity, raw, stat_identity, priority + 1)

            # dict-shaped stat map
            for k, v in node.items():
                if isinstance(v, (str, int, float)):
                    add(k, v, k, priority)
                elif isinstance(v, (dict, list)):
                    scan_container(v, priority, f"{prefix}.{k}" if prefix else str(k))

        elif isinstance(node, list):
            for i, item in enumerate(node):
                if isinstance(item, (dict, list)):
                    scan_container(item, priority, f"{prefix}[{i}]")

    # Highest confidence: explicit final-stat containers.
    for key in (
        "stats", "stat", "statList", "characterStats", "combatStats",
        "battleStats", "additionalStats", "finalStats", "finalStat",
        "abilityStats", "abilities", "combatPowerStats",
    ):
        if key in profile:
            scan_container(profile.get(key), priority=300, prefix=key)

    info = profile.get("info")
    if isinstance(info, dict):
        for key in (
            "stats", "stat", "statList", "characterStats", "combatStats",
            "battleStats", "additionalStats", "finalStats", "abilities",
        ):
            if key in info:
                scan_container(info.get(key), priority=280, prefix=f"info.{key}")

    # Lower-confidence recursive recovery. Still only exact known offensive labels.
    # Explicitly skip equipment/item/arcana/skill trees to avoid mixing item options
    # with the final character stat panel.
    skip_keys = (
        "item", "equipment", "equip", "weapon", "armor", "accessory",
        "arcana", "skill", "passive", "active", "magicstone", "stone",
    )

    def walk(node, path="", depth=0):
        if depth > 10:
            return
        if isinstance(node, dict):
            low_path = path.lower()
            if any(k in low_path for k in skip_keys):
                return

            row_name = (
                node.get("name")
                or node.get("statName")
                or node.get("displayName")
                or node.get("label")
            )
            stat_identity = (
                node.get("statId") or node.get("statID") or node.get("id")
                or node.get("key") or node.get("code") or node.get("type")
            )
            raw = node.get("displayValue")
            if raw is None: raw = node.get("value")
            if raw is None: raw = node.get("statValue")
            if raw is None: raw = node.get("amount")
            if raw is None: raw = node.get("finalValue")
            if row_name:
                add(row_name, raw, stat_identity or path, 120)
            if stat_identity and _canonical_from_stat_id(stat_identity):
                add(stat_identity, raw, stat_identity, 121)

            for k, v in node.items():
                p = f"{path}.{k}" if path else str(k)
                if isinstance(v, (str, int, float)):
                    add(k, v, p, 110)
                elif isinstance(v, (dict, list)):
                    walk(v, p, depth + 1)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}[{i}]", depth + 1)

    walk(profile)

    rows = []
    for canonical in OFFENSE_CANONICAL_GROUPS.keys():
        row = stats.get(canonical)
        if row:
            row.pop("_priority", None)
            rows.append(row)

    return rows



def extract_visible_base_stats(profile):
    """Reconstruct offensive base stats from visible non-manastone option sources.

    The public character payload does not always expose the in-game final stat panel.
    When that panel is absent, compare still has enough visible additive sources
    (equipment options, title/collection/engraving/arcana option rows, etc.) to
    reconstruct a useful base value.  Magic stones are deliberately excluded here
    because they are aggregated separately and added exactly once by
    build_combined_offense().

    This is a fallback only: explicit/final stats from extract_profile_stats() win.
    """
    if not isinstance(profile, dict):
        return []

    totals = {}
    units = {}
    sources = {}

    # Sources that can carry additive character options. Skills/passives are not
    # summed here because their effects may be conditional or already reflected
    # elsewhere. Magic-stone/socket branches are always excluded.
    source_words = (
        "item", "equipment", "equip", "gear", "option", "stat", "effect",
        "bonus", "title", "collection", "engraving", "soul", "artifact",
        "arcana", "profile",
    )
    skip_words = (
        "magicstone", "magic_stone", "manastone", "socket", "stone",
        "skill", "passive", "active",
    )

    def add(identity, raw, path):
        canonical = _canonical_from_stat_id(identity) or _canonical_offense_name(identity)
        if not canonical:
            return
        numeric = _safe_num(raw)
        if numeric is None:
            return
        unit = _detect_stat_unit(raw)
        if canonical in PERCENT_CANONICAL_STATS:
            unit = "percent"
            numeric = _normalize_percent_internal(canonical, numeric, raw, identity)
        totals[canonical] = totals.get(canonical, 0.0) + float(numeric)
        units[canonical] = unit
        sources.setdefault(canonical, []).append(path)

    def walk(node, path="", depth=0, source_context=False):
        if depth > 12:
            return
        low_path = path.lower()
        if any(w in low_path for w in skip_words):
            return

        if isinstance(node, dict):
            # A branch becomes eligible once its path looks like a visible stat/
            # equipment/title/etc. source. This avoids summing unrelated numbers.
            ctx = source_context or any(w in low_path for w in source_words)

            row_name = (
                node.get("name") or node.get("statName") or node.get("displayName")
                or node.get("optionName") or node.get("effectName") or node.get("label")
            )
            stat_id = (
                node.get("statId") or node.get("statID") or node.get("id")
                or node.get("key") or node.get("code") or node.get("type")
            )
            raw = node.get("displayValue")
            if raw is None: raw = node.get("value")
            if raw is None: raw = node.get("statValue")
            if raw is None: raw = node.get("amount")
            if raw is None: raw = node.get("finalValue")

            if ctx and raw is not None:
                if stat_id and (_canonical_from_stat_id(stat_id) or _canonical_offense_name(stat_id)):
                    add(stat_id, raw, path)
                elif row_name and (_canonical_from_stat_id(row_name) or _canonical_offense_name(row_name)):
                    add(row_name, raw, path)

            # Dict-shaped stat maps such as {AmplifyWeaponDamage: 250}.
            if ctx:
                for k, v in node.items():
                    if isinstance(v, (str, int, float)):
                        if _canonical_from_stat_id(k) or _canonical_offense_name(k):
                            add(k, v, f"{path}.{k}" if path else str(k))

            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    p = f"{path}.{k}" if path else str(k)
                    walk(v, p, depth + 1, ctx or any(w in str(k).lower() for w in source_words))

        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}[{i}]", depth + 1, source_context)

    walk(profile)

    rows = []
    for canonical in OFFENSE_CANONICAL_GROUPS.keys():
        if canonical not in totals:
            continue
        rows.append({
            "name": canonical,
            "originalName": canonical,
            "value": float(totals[canonical]),
            "raw": float(totals[canonical]),
            "unit": units.get(canonical, "percent" if canonical in PERCENT_CANONICAL_STATS else "number"),
            "sourceKey": "reconstructed-visible-sources",
            "reconstructed": True,
            "sourceCount": len(sources.get(canonical, [])),
        })
    return rows


def merge_explicit_and_reconstructed_stats(explicit_rows, reconstructed_rows):
    """Prefer true final-stat containers; otherwise use reconstructed visible sources.

    extract_profile_stats() also has a low-confidence recursive recovery pass.
    Those rows are useful when nothing else exists, but they must not overwrite a
    richer reconstructed sum (e.g. one title option replacing all equipment + title
    contributions). Priority >=250 means the value came from an explicit/final
    character stat container and is safe to prefer.
    """
    merged = {}
    for row in reconstructed_rows or []:
        name = _canonical_offense_name(row.get("name"))
        if name:
            merged[name] = dict(row)
    for row in explicit_rows or []:
        name = _canonical_offense_name(row.get("name"))
        if not name:
            continue
        r = dict(row)
        prio = int(r.get("confidencePriority") or 0)
        if name not in merged or prio >= 250:
            r["reconstructed"] = False
            merged[name] = r
    return [merged[n] for n in OFFENSE_CANONICAL_GROUPS.keys() if n in merged]

def _normalize_stone(stone):
    if not isinstance(stone, dict):
        return None

    name = str(
        stone.get("name")
        or stone.get("statName")
        or stone.get("optionName")
        or ""
    ).strip()

    raw = stone.get("value")
    if raw is None:
        raw = stone.get("statValue")

    if not name and raw is None:
        return None

    return {
        "id": str(stone.get("id") or ""),
        "name": name or "마석",
        "value": raw,
        "numeric": _safe_num(raw),
        "icon": str(stone.get("icon") or ""),
    }

def _equipment_rows(profile):
    """
    Recover equipped items from multiple known payload shapes.
    Keeps only item-like objects with an equipment slot / enhance / grade signal.
    """
    if not isinstance(profile, dict):
        return []

    candidate_collections = []
    seen_collections = set()

    def add_collection(value):
        if not isinstance(value, (dict, list)):
            return
        ident = id(value)
        if ident in seen_collections:
            return
        seen_collections.add(ident)
        candidate_collections.append(value)

    # Known direct shapes.
    for key in (
        "itemDetails", "equipment", "equipments", "equipmentList",
        "equippedItems", "equipItemList", "items", "gear", "gears",
    ):
        if key in profile:
            add_collection(profile.get(key))

    info = profile.get("info")
    if isinstance(info, dict):
        for key in (
            "itemDetails", "equipment", "equipments", "equipmentList",
            "equippedItems", "equipItemList", "gear",
        ):
            if key in info:
                add_collection(info.get(key))

    # Find equipment-named nested collections.
    for d in _walk_dicts(profile):
        for k, v in d.items():
            kl = str(k).lower()
            if any(tag in kl for tag in ("equipment", "equippeditem", "equipitem", "itemdetail", "gear")):
                add_collection(v)

    rows = []
    seen_items = set()

    slot_words = (
        "무기","상의","하의","장갑","신발","투구","어깨","망토",
        "목걸이","귀걸이","반지","벨트","팔찌","보조","날개",
        "weapon","head","chest","pants","glove","boots","shoulder",
        "necklace","earring","ring","belt","bracelet","cloak","wing",
    )

    def normalize_item(item, slot_key=""):
        if not isinstance(item, dict):
            return None

        name = str(
            item.get("name")
            or item.get("itemName")
            or item.get("equipmentName")
            or item.get("displayName")
            or item.get("title")
            or ""
        ).strip()

        slot = str(
            item.get("slotName")
            or item.get("slot")
            or item.get("equipSlot")
            or item.get("equipmentSlot")
            or item.get("partName")
            or item.get("part")
            or slot_key
            or ""
        ).strip()

        grade = str(
            item.get("gradeName")
            or item.get("grade")
            or item.get("rarityName")
            or item.get("rarity")
            or item.get("tierName")
            or item.get("tier")
            or ""
        ).strip()

        enhance = None
        for k in ("enhanceLevel","enchantLevel","reinforceLevel","upgradeLevel","enhancementLevel"):
            if item.get(k) is not None:
                enhance = item.get(k)
                break

        level = None
        for k in ("itemLevel","level","requiredLevel","gearLevel"):
            if item.get(k) is not None:
                level = item.get(k)
                break

        # Require an item identity plus some equipment signal.
        slot_low = slot.lower()
        has_slot_signal = any(w.lower() in slot_low for w in slot_words)
        has_equipment_signal = bool(
            has_slot_signal or enhance is not None or grade
            or item.get("equipped") is True or item.get("isEquipped") is True
        )
        if not name or not has_equipment_signal:
            return None

        icon = str(
            item.get("icon")
            or item.get("iconUrl")
            or item.get("image")
            or item.get("imageUrl")
            or item.get("thumbnail")
            or ""
        ).strip()

        stones = []
        for stone_key in (
            "magicStoneStat", "magicStones", "magicStoneList",
            "manastones", "stones", "socketOptions", "socketStats",
        ):
            node = item.get(stone_key)
            if isinstance(node, list):
                for stone in node:
                    s = _normalize_stone(stone)
                    if s:
                        stones.append(s)

        options = []
        for option_key in (
            "options", "optionStats", "additionalStats", "stats",
            "effects", "bonusStats", "randomOptions",
        ):
            node = item.get(option_key)
            if isinstance(node, list):
                for op in node:
                    if not isinstance(op, dict):
                        continue
                    oname = str(
                        op.get("name") or op.get("statName") or
                        op.get("optionName") or op.get("effectName") or ""
                    ).strip()
                    oval = op.get("displayValue")
                    if oval is None: oval = op.get("value")
                    if oval is None: oval = op.get("statValue")
                    if oname and oval is not None:
                        options.append({
                            "name": oname,
                            "value": oval,
                            "numeric": _safe_num(oval),
                        })

        key = (
            str(item.get("id") or item.get("itemId") or ""),
            name, slot, str(enhance),
        )
        if key in seen_items:
            return None
        seen_items.add(key)

        return {
            "slot": slot,
            "name": name,
            "grade": grade,
            "enhance": enhance,
            "itemLevel": level,
            "level": level,
            "icon": icon,
            "magicStones": stones,
            "options": options,
        }

    def scan_collection(collection, slot_hint=""):
        if isinstance(collection, list):
            for item in collection:
                if isinstance(item, dict):
                    row = normalize_item(item, slot_hint)
                    if row:
                        rows.append(row)
                    else:
                        # Some APIs wrap item data one level deeper.
                        for k, v in item.items():
                            if isinstance(v, dict):
                                row = normalize_item(v, str(k))
                                if row:
                                    rows.append(row)
        elif isinstance(collection, dict):
            # Collection may be slot -> item.
            for k, v in collection.items():
                if isinstance(v, dict):
                    row = normalize_item(v, str(k))
                    if row:
                        rows.append(row)
                    else:
                        for k2, v2 in v.items():
                            if isinstance(v2, dict):
                                row = normalize_item(v2, str(k2))
                                if row:
                                    rows.append(row)
                elif isinstance(v, list):
                    scan_collection(v, str(k))

    for collection in candidate_collections:
        scan_collection(collection)

    # Stable equipment-like order.
    order = ["무기","투구","상의","하의","장갑","신발","어깨","망토","목걸이","귀걸이","반지","벨트","팔찌","날개"]
    def sk(row):
        slot = str(row.get("slot") or "")
        for i, word in enumerate(order):
            if word in slot:
                return (i, slot, row.get("name") or "")
        return (999, slot, row.get("name") or "")

    return sorted(rows, key=sk)

def _stone_totals_from_equipment(equipment):
    totals = {}
    counts = {}

    for item in equipment:
        for stone in item.get("magicStones") or []:
            name = str(stone.get("name") or "마석")
            value = stone.get("numeric")
            if value is None:
                continue

            totals[name] = totals.get(name, 0.0) + float(value)
            counts[name] = counts.get(name, 0) + 1

    rows = []
    for name in sorted(totals.keys()):
        count = counts.get(name, 0)
        total = totals[name]
        rows.append({
            "name": name,
            "count": count,
            "total": total,
            "average": (total / count) if count else 0,
        })

    return rows


def _stone_totals_from_profile(profile):
    """Profile-wide magic-stone aggregation used by compare.

    Character lookup already succeeds by recursively discovering magicStoneStat.
    Compare must use the same broad discovery instead of relying only on normalized
    equipment rows, because some stored profiles contain skills/stones but no
    equipment slot metadata.
    """
    if not isinstance(profile, dict):
        return []

    stone_lists = []
    _collect_stone_lists(profile, stone_lists)

    totals = {}
    counts = {}
    ids = {}

    for stones in stone_lists:
        for stone in stones or []:
            if not isinstance(stone, dict):
                continue
            sid = str(
                stone.get("id") or stone.get("statId") or stone.get("key") or ""
            ).strip()
            name = str(
                stone.get("name") or stone.get("statName") or stone.get("optionName") or ""
            ).strip()
            canonical = _canonical_offense_name(name) or _canonical_from_stat_id(sid)
            if not canonical:
                continue

            raw = stone.get("displayValue")
            if raw is None: raw = stone.get("value")
            if raw is None: raw = stone.get("statValue")
            numeric = _safe_num(raw)
            if numeric is None:
                continue

            numeric = _normalize_percent_internal(canonical, numeric, raw, sid or name)
            totals[canonical] = totals.get(canonical, 0.0) + float(numeric)
            counts[canonical] = counts.get(canonical, 0) + 1
            if sid:
                ids[canonical] = sid

    rows = []
    for canonical in OFFENSE_CANONICAL_GROUPS.keys():
        if canonical not in totals:
            continue
        count = counts.get(canonical, 0)
        total = totals[canonical]
        rows.append({
            "name": canonical,
            "count": count,
            "total": total,
            "average": (total / count) if count else 0.0,
            "sourceId": ids.get(canonical, ""),
        })
    return rows

AION2_RESEARCH_RULES = {
    "updated": "2026-09-04",
    "scope": "PvE relative damage / character comparison",
    "principle": "공식으로 확인되지 않은 항목은 추정치로 표시하고 검증식에 강제 적용하지 않는다.",
    "rules": [
        {
            "id": "skill_damage_structure",
            "label": "스킬 피해 구조",
            "confidence": "high",
            "summary": "스킬 피해는 스킬 레벨별 고정 피해와 공격력 영향을 함께 받는다.",
            "sources": ["Aion2t client skill DB", "Inven damage experiment 909"],
        },
        {
            "id": "amp_bucket",
            "label": "피해 증폭 버킷",
            "confidence": "high",
            "summary": "일반/PvE/보스/종족 피해 증폭은 같은 합연산 축으로 취급한다.",
            "sources": ["Inven damage experiment 909", "Taiwan experiment summary"],
        },
        {
            "id": "directional_amp",
            "label": "전방/후방 피해 증폭",
            "confidence": "high",
            "summary": "후방 피해 증폭은 별도 독립 배율로 관측됐다. 전방 계열은 동일한 방향성 축으로 분리해 계산한다.",
            "sources": ["Inven rear damage experiment 966", "Taiwan experiment summary"],
        },
        {
            "id": "critical",
            "label": "치명타",
            "confidence": "high",
            "summary": "치명 기본 배율은 150%, 치명타 피해 증폭은 여기에 가산한다.",
            "sources": ["Inven damage experiment 909"],
        },
        {
            "id": "hard_hit",
            "label": "강타",
            "confidence": "high",
            "summary": "PvE 강타 발동 피해는 2배로 관측된다. 발동률 1%의 단순 기대값은 약 +1%다.",
            "sources": ["Inven damage experiment 909", "Inven hard-hit guide 1138"],
        },
        {
            "id": "perfect",
            "label": "완벽",
            "confidence": "medium",
            "summary": "최대 공격력 적용 계열로 보이며 평균 DPS 기여는 매우 작게 관측된다. 검증 딜지수에는 강제 반영하지 않는다.",
            "sources": ["Inven damage experiment 909", "Atool combat-power breakdown"],
        },
        {
            "id": "penetration",
            "label": "관통",
            "confidence": "medium",
            "summary": "실험상 관통의 일부가 스킬별 고정 추가 피해처럼 작동한다. 스킬별 타수/적용 방식 차이 때문에 % 배율로 합치지 않는다.",
            "sources": ["Inven damage experiment 909"],
        },
        {
            "id": "multi_hit",
            "label": "다단 히트",
            "confidence": "medium",
            "summary": "연쇄 추가타 구조이며 타수별 추가 피해가 다르다. 확률 매핑이 확정된 경우에만 별도 기대값 계산에 사용한다.",
            "sources": ["Atool combat-power breakdown", "Inven damage experiment 909"],
        },
        {
            "id": "weapon_amp",
            "label": "무기 피해 증폭",
            "confidence": "medium",
            "summary": "무기/장비 공격력 계열에 관여하는 것은 확인되지만 전체 스킬 최종피해에 단순 1:1 곱하는 공식은 확정하지 않는다.",
            "sources": ["Atool combat-power breakdown", "Inven max-attack experiment 1328"],
        },
        {
            "id": "accuracy_gate",
            "label": "명중 조건",
            "confidence": "high",
            "summary": "명중 부족으로 막기가 발생하면 최종 피해가 크게 감소하므로 강타/치명 효율보다 먼저 명중 조건을 확인한다.",
            "sources": ["Inven hard-hit guide 1138"],
        },
        {
            "id": "skill_score",
            "label": "스킬 성장 점수",
            "confidence": "medium",
            "summary": "아툴은 액티브/패시브 레벨당 1.35%와 특정 레벨 보너스 및 딜지분 가중치를 사용하는 비공식 PVE 점수 모델을 공개한다. 실제 DPS 공식과 구분한다.",
            "sources": ["Atool skill statistics"],
        },
    ],
}

OFFENSE_CANONICAL_GROUPS = {
    "공격력": (
        "공격력", "마법 공격력", "물리 공격력",
        "attack", "attackPower", "physicalAttack", "magicAttack",
    ),
    "추가 공격력": ("추가 공격력", "additionalAttack", "additionalAttackPower"),
    "최소 공격력": ("최소 공격력", "minAttack", "minimumAttack"),
    "최대 공격력": ("최대 공격력", "maxAttack", "maximumAttack"),
    "PVE 공격력": ("PVE 공격력", "PvE 공격력", "몬스터 공격력", "pveAttack", "pveAttackPower"),
    "보스 공격력": ("보스 공격력", "bossAttack", "bossAttackPower"),
    "공격력 증가율": ("공격력 증가율", "공격력 증가", "공증", "attackIncrease", "attackPowerIncrease"),
    "피해 증폭": ("피해 증폭", "피해 증가", "damageIncrease", "damageAmplify"),
    "무기 피해 증폭": (
        "무기 피해 증폭", "무기 피해 증가", "무기 피해", "무피",
        "weaponDamageIncrease", "weaponDamageAmplify",
    ),
    "PVE 피해 증폭": (
        "PVE 피해 증폭", "PvE 피해 증폭", "PVE 피해 증가",
        "몬스터 피해 증폭", "몬스터 피해 증가",
        "pveDamageIncrease", "pveDamageAmplify", "monsterDamageIncrease",
    ),
    "보스 피해 증폭": (
        "보스 피해 증폭", "보스 피해 증가", "보스 피해",
        "bossDamageIncrease", "bossDamageAmplify",
    ),
    "치명타 피해 증폭": (
        "치명타 피해 증폭", "치명타 피해 증가", "치명타 피해", "치피",
        "criticalDamageIncrease", "criticalDamageAmplify",
    ),
    "전방 피해 증폭": (
        "전방 피해 증폭", "전방 피해 증가", "전방 피해", "전피",
        "frontDamageIncrease", "frontDamageAmplify",
    ),
    "후방 피해 증폭": (
        "후방 피해 증폭", "후방 피해 증가", "후방 피해", "후피",
        "rearDamageIncrease", "backDamageIncrease", "rearDamageAmplify", "backDamageAmplify",
    ),
    "치명타": ("치명타", "치명", "critical", "criticalHit", "criticalRate"),
    "명중": ("명중", "적중", "accuracy", "hit", "hitRate"),
    "강타": ("강타", "hardHit", "hardHitRate"),
    "완벽": ("완벽", "perfect", "perfectRate"),
    "관통": ("관통", "방어구 관통", "penetration", "armorPenetration"),
    "공격 속도": ("공격 속도", "공속", "attackSpeed"),
    "시전 속도": ("시전 속도", "시속", "castSpeed"),
}

def _canonical_offense_name(name):
    src = re.sub(r"\s+", " ", str(name or "").strip())
    low = src.lower()
    if not low:
        return None

    # Exact match first. This is critical for names such as
    # "무기 피해 증폭" / "보스 피해 증폭" / "후방 피해 증폭".
    # The old substring-only matcher saw the generic alias "피해 증폭" first
    # and collapsed every specific damage-amplification stat into that bucket.
    exact_candidates = []
    for canonical, aliases in OFFENSE_CANONICAL_GROUPS.items():
        for alias in (canonical, *aliases):
            a = re.sub(r"\s+", " ", str(alias).strip()).lower()
            if low == a:
                return canonical
            exact_candidates.append((len(a), a, canonical))

    # Controlled fallback for labels with prefixes/suffixes: longest alias wins,
    # so a specific stat can never be swallowed by the generic "피해 증폭".
    for _, alias, canonical in sorted(exact_candidates, key=lambda x: x[0], reverse=True):
        if alias and alias in low:
            return canonical
    return None

def _stone_offense_map(stone_rows):
    """
    Convert aggregated magic-stone rows into canonical offense groups.
    We only combine values when the stone names clearly map to an offensive stat.
    """
    result = {}

    for row in stone_rows or []:
        canonical = _canonical_offense_name(row.get("name"))
        if not canonical:
            continue

        result.setdefault(
            canonical,
            {
                "name": canonical,
                "stoneCount": 0,
                "stoneTotal": 0.0,
                "stoneAverage": 0.0,
            },
        )

        result[canonical]["stoneCount"] += int(row.get("count") or 0)
        result[canonical]["stoneTotal"] += float(row.get("total") or 0)

    for row in result.values():
        if row["stoneCount"]:
            row["stoneAverage"] = row["stoneTotal"] / row["stoneCount"]

    return result

def _base_offense_map(stats):
    result = {}

    for row in stats or []:
        canonical = _canonical_offense_name(row.get("name"))
        if not canonical:
            continue

        # Prefer the first explicit stat row for that canonical group.
        if canonical not in result:
            result[canonical] = {
                "name": canonical,
                "baseValue": float(row.get("value") or 0),
                "baseRaw": row.get("raw"),
                "unit": row.get("unit") or "number",
                "sourceKey": row.get("sourceKey") or "",
                "reconstructed": bool(row.get("reconstructed") or False),
            }

    return result

def build_combined_offense(stats, stone_rows):
    """
    Combined view = character final/base offensive stat + related magic-stone total.

    Important:
    We do NOT blindly add incompatible percent/internal values.
    A numeric 'combinedValue' is only provided when both sides use a plain
    comparable number scale. Percent/base-stat rows are shown side-by-side.
    """
    base_map = _base_offense_map(stats)
    stone_map = _stone_offense_map(stone_rows)

    names = []
    for canonical in OFFENSE_CANONICAL_GROUPS.keys():
        if canonical in base_map or canonical in stone_map:
            names.append(canonical)

    rows = []

    for name in names:
        base = base_map.get(name) or {
            "baseValue": 0.0,
            "baseRaw": None,
            "unit": "number",
            "sourceKey": "",
        }
        stone = stone_map.get(name) or {
            "stoneCount": 0,
            "stoneTotal": 0.0,
            "stoneAverage": 0.0,
        }

        combined = None

        # Explicit matching offensive values can be combined. For percentage
        # stats the stone value is treated as percentage-points.
        if base.get("unit") in ("number", "percent"):
            combined = (
                float(base.get("baseValue") or 0)
                + float(stone.get("stoneTotal") or 0)
            )

        rows.append({
            "name": name,
            "baseValue": float(base.get("baseValue") or 0),
            "baseRaw": base.get("baseRaw"),
            "unit": base.get("unit") or "number",
            "stoneCount": int(stone.get("stoneCount") or 0),
            "stoneTotal": float(stone.get("stoneTotal") or 0),
            "stoneAverage": float(stone.get("stoneAverage") or 0),
            "combinedValue": combined,
            "baseReconstructed": bool(base.get("reconstructed") or False),
        })

    return rows


# =========================================================
# AION2 PRO DETAIL / DAMAGE ENGINE
# =========================================================

def _find_named_collections(profile, keywords):
    found = []
    seen = set()

    def walk(node, depth=0):
        if depth > 10:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_low = str(key).lower()
                if any(k.lower() in key_low for k in keywords):
                    if isinstance(value, (list, dict)):
                        ident = id(value)
                        if ident not in seen:
                            seen.add(ident)
                            found.append(value)
                if isinstance(value, (list, dict)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (list, dict)):
                    walk(value, depth + 1)

    walk(profile)
    return found


def _detail_card(item, kind):
    if not isinstance(item, dict):
        return None

    name = str(
        item.get("name")
        or item.get("skillName")
        or item.get("arcanaName")
        or item.get("displayName")
        or item.get("title")
        or ""
    ).strip()
    if not name:
        return None

    level = item.get("level")
    for key in ("skillLevel", "enhanceLevel", "masteryLevel", "gradeLevel"):
        if level is None:
            level = item.get(key)

    icon = str(
        item.get("icon")
        or item.get("iconUrl")
        or item.get("image")
        or item.get("imageUrl")
        or item.get("thumbnail")
        or ""
    ).strip()

    grade = str(
        item.get("gradeName")
        or item.get("grade")
        or item.get("rarity")
        or item.get("tier")
        or ""
    ).strip()

    category = str(
        item.get("typeName")
        or item.get("category")
        or item.get("type")
        or kind
    ).strip()

    description = str(
        item.get("description")
        or item.get("desc")
        or item.get("tooltip")
        or item.get("effectDescription")
        or item.get("effect")
        or ""
    ).strip()

    options = []
    for key in (
        "options", "optionStats", "stats", "effects",
        "effectList", "additionalStats", "passiveEffects",
    ):
        node = item.get(key)
        if not isinstance(node, list):
            continue
        for row in node:
            if not isinstance(row, dict):
                continue
            op_name = str(
                row.get("name")
                or row.get("statName")
                or row.get("effectName")
                or ""
            ).strip()
            op_value = (
                row.get("displayValue")
                if row.get("displayValue") is not None
                else row.get("value")
            )
            op_desc = str(
                row.get("description")
                or row.get("desc")
                or ""
            ).strip()
            if op_name:
                options.append({
                    "name": op_name,
                    "value": op_value,
                    "description": op_desc,
                })

    return {
        "name": name,
        "level": level,
        "icon": icon,
        "grade": grade,
        "category": category,
        "description": description,
        "options": options,
    }


def _cards_from_collections(collections, kind, limit=100):
    cards = []
    seen = set()

    def add(item):
        card = _detail_card(item, kind)
        if not card:
            return
        key = (card.get("name"), str(card.get("level")), card.get("category"))
        if key in seen:
            return
        seen.add(key)
        cards.append(card)

    for collection in collections:
        if isinstance(collection, list):
            for item in collection:
                if isinstance(item, dict):
                    add(item)
        elif isinstance(collection, dict):
            if any(
                key in collection
                for key in ("name", "skillName", "arcanaName", "displayName")
            ):
                add(collection)
            else:
                for value in collection.values():
                    if isinstance(value, dict):
                        add(value)
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict):
                                add(item)

        if len(cards) >= limit:
            break

    return cards[:limit]


def extract_arcana(profile):
    return _cards_from_collections(
        _find_named_collections(
            profile,
            ("arcana", "arcanas", "arcanaList", "arcanaInfo", "equippedArcana"),
        ),
        "아르카나",
        60,
    )


def extract_skills(profile):
    cards = _cards_from_collections(
        _find_named_collections(
            profile,
            (
                "activeSkill", "activeSkills", "skillList",
                "equippedSkill", "characterSkill", "skills",
            ),
        ),
        "스킬",
        120,
    )
    return [
        c for c in cards
        if "passive" not in str(c.get("category") or "").lower()
        and "패시브" not in str(c.get("category") or "")
    ]



def _dedupe_and_filter_level_rows(rows, min_level=16):
    """Deduplicate by name and keep only level >= min_level."""
    best = {}

    for row in rows or []:
        if not isinstance(row, dict):
            continue

        name = str(row.get("name") or "").strip()
        if not name:
            continue

        level = _safe_num(row.get("level"))
        if level is None:
            level = _safe_num(row.get("skillLevel"))
        if level is None:
            level = _safe_num(row.get("passiveLevel"))
        if level is None or level < min_level:
            continue

        key = re.sub(r"\s+", " ", name).strip().lower()
        normalized = dict(row)
        normalized["level"] = int(level) if float(level).is_integer() else level

        old = best.get(key)
        if old is None or float(normalized["level"]) > float(old.get("level") or 0):
            best[key] = normalized

    return sorted(
        best.values(),
        key=lambda x: (-float(x.get("level") or 0), str(x.get("name") or ""))
    )


def extract_passives(profile):
    return _cards_from_collections(
        _find_named_collections(
            profile,
            (
                "passiveSkill", "passiveSkills", "passive",
                "trait", "traits", "talent", "talents",
            ),
        ),
        "패시브",
        120,
    )


def _canon_map(rows):
    result = {}
    for row in rows or []:
        name = str(row.get("name") or "")
        if not name:
            continue
        result[name] = row
    return result


def _stone_canon_map(rows):
    result = {}
    for row in rows or []:
        canonical = _canonical_offense_name(row.get("name"))
        if not canonical:
            continue
        target = result.setdefault(
            canonical,
            {"count": 0, "total": 0.0, "average": 0.0},
        )
        target["count"] += int(row.get("count") or 0)
        target["total"] += float(row.get("total") or 0)

    for row in result.values():
        if row["count"]:
            row["average"] = row["total"] / row["count"]
    return result


def _offense_value(combined_rows, name, default=0.0):
    row = _canon_map(combined_rows).get(name)
    if not row:
        return float(default)
    value = row.get("combinedValue")
    if value is None:
        value = row.get("baseValue")
    try:
        return float(value or 0)
    except Exception:
        return float(default)



JOB_COMBAT_PROFILES = {
    "수호성": {"frontRatio": 100.0, "backRatio": 0.0, "directionLabel": "전방 탱킹/딜 기준", "directionConfidence": "high"},
    "살성": {"frontRatio": 0.0, "backRatio": 100.0, "directionLabel": "후방 딜 기준", "directionConfidence": "medium"},
    "검성": {"frontRatio": 0.0, "backRatio": 0.0, "directionLabel": "포지션 가변", "directionConfidence": "manual"},
    "호법성": {"frontRatio": 0.0, "backRatio": 0.0, "directionLabel": "전피/후피 세팅 가변", "directionConfidence": "manual"},
    "치유성": {"frontRatio": 0.0, "backRatio": 0.0, "directionLabel": "원거리/포지션 가변", "directionConfidence": "manual"},
    "궁성": {"frontRatio": 0.0, "backRatio": 0.0, "directionLabel": "원거리/포지션 가변", "directionConfidence": "manual"},
    "마도성": {"frontRatio": 0.0, "backRatio": 0.0, "directionLabel": "원거리/포지션 가변", "directionConfidence": "manual"},
    "정령성": {"frontRatio": 0.0, "backRatio": 0.0, "directionLabel": "원거리/포지션 가변", "directionConfidence": "manual"},
    "권성": {"frontRatio": 0.0, "backRatio": 0.0, "directionLabel": "포지션 가변", "directionConfidence": "manual"},
}

def job_combat_profile(job):
    return dict(JOB_COMBAT_PROFILES.get(
        str(job or "").strip(),
        {"frontRatio": 0.0, "backRatio": 0.0, "directionLabel": "포지션 수동 설정", "directionConfidence": "manual"}
    ))

def damage_index_from_rows(
    combined_rows,
    *,
    critical_rate=50.0,
    hard_hit_rate=0.0,
    back_ratio=0.0,
    front_ratio=0.0,
    boss_resistance=0.0,
    skill_coefficient=1.0,
):
    """
    v45 validated relative PvE damage index.

    Only high-confidence multiplicative relationships are in validatedScore.
    Medium-confidence stats are exposed separately instead of being silently
    forced into the formula.
    """
    atk = _offense_value(combined_rows, "공격력")
    if atk <= 0:
        # If a profile exposes only sub-components, use them as a fallback.
        atk = (
            _offense_value(combined_rows, "추가 공격력")
            + _offense_value(combined_rows, "PVE 공격력")
            + _offense_value(combined_rows, "보스 공격력")
        )
    attack_term = max(1.0, atk)

    # High-confidence additive amplification bucket.
    amp = (
        _offense_value(combined_rows, "피해 증폭")
        + _offense_value(combined_rows, "PVE 피해 증폭")
        + _offense_value(combined_rows, "보스 피해 증폭")
        - float(boss_resistance or 0)
    )
    amp_mult = max(0.01, 1.0 + amp / 100.0)

    # Directional amplification is handled as a separate weighted multiplier.
    back_amp = _offense_value(combined_rows, "후방 피해 증폭")
    front_amp = _offense_value(combined_rows, "전방 피해 증폭")
    back_share = min(100.0, max(0.0, float(back_ratio or 0))) / 100.0
    front_share = min(100.0, max(0.0, float(front_ratio or 0))) / 100.0
    if back_share + front_share > 1.0:
        total = back_share + front_share
        back_share /= total
        front_share /= total
    neutral_share = max(0.0, 1.0 - back_share - front_share)
    directional_mult = (
        neutral_share
        + back_share * (1.0 + back_amp / 100.0)
        + front_share * (1.0 + front_amp / 100.0)
    )

    # Critical expected multiplier: 150% base + critical damage amplification.
    crit_dmg_amp = _offense_value(combined_rows, "치명타 피해 증폭")
    crit_rate = min(100.0, max(0.0, float(critical_rate or 0))) / 100.0
    crit_hit_mult = 1.5 + crit_dmg_amp / 100.0
    crit_expected = (1.0 - crit_rate) + crit_rate * crit_hit_mult

    # PvE Hard Hit: 2x on proc -> E[mult] = 1 + p.
    hard_rate = min(100.0, max(0.0, float(hard_hit_rate or 0))) / 100.0
    hard_expected = 1.0 + hard_rate

    skill_coeff = max(0.01, float(skill_coefficient or 1.0))

    validated_score = (
        attack_term
        * amp_mult
        * directional_mult
        * crit_expected
        * hard_expected
        * skill_coeff
    )

    # Medium-confidence values: returned for analysis, not blindly multiplied.
    weapon_amp = _offense_value(combined_rows, "무기 피해 증폭")
    perfect = _offense_value(combined_rows, "완벽")
    penetration = _offense_value(combined_rows, "관통")
    attack_inc = _offense_value(combined_rows, "공격력 증가율")
    multi_hit = _offense_value(combined_rows, "다단 히트")

    return {
        "score": validated_score,
        "validatedScore": validated_score,
        "model": "validated-relative-pve-v45",
        "attackTerm": attack_term,
        "amplificationPct": amp,
        "amplificationMultiplier": amp_mult,
        "directionMultiplier": directional_mult,
        "criticalMultiplier": crit_expected,
        "hardHitMultiplier": hard_expected,
        "mediumConfidence": {
            "weaponDamageAmplification": weapon_amp,
            "perfect": perfect,
            "penetration": penetration,
            "attackIncreasePct": attack_inc,
            "multiHit": multi_hit,
        },
        "warnings": [
            "무기 피해 증폭은 전체 최종피해에 1:1 곱하지 않음",
            "관통/완벽/다단히트는 스킬별 적용 차이 때문에 검증 딜지수에서 분리",
            "스킬 고정피해와 개별 공격력 계수는 스킬 DB가 연결된 경우 별도 계산 필요",
        ],
    }


def stat_marginal_efficiency(combined_rows, job=None):
    profile = job_combat_profile(job)
    baseline = damage_index_from_rows(
        combined_rows,
        back_ratio=profile["backRatio"],
        front_ratio=profile["frontRatio"],
    )["validatedScore"]

    if baseline <= 0:
        return []

    names = (
        "공격력",
        "피해 증폭",
        "PVE 피해 증폭",
        "보스 피해 증폭",
        "치명타 피해 증폭",
        "후방 피해 증폭",
        "전방 피해 증폭",
    )

    rows = []
    original = [dict(row) for row in (combined_rows or [])]

    for name in names:
        if name == "후방 피해 증폭" and profile["backRatio"] <= 0:
            continue
        if name == "전방 피해 증폭" and profile["frontRatio"] <= 0:
            continue

        modified = [dict(row) for row in original]
        target = next((row for row in modified if row.get("name") == name), None)

        if target is None:
            target = {
                "name": name,
                "baseValue": 0.0,
                "stoneTotal": 0.0,
                "combinedValue": 0.0,
                "unit": "number" if name == "공격력" else "percent",
            }
            modified.append(target)

        cur = target.get("combinedValue")
        if cur is None:
            cur = target.get("baseValue") or 0

        target["combinedValue"] = float(cur) + 1.0

        score = damage_index_from_rows(
            modified,
            back_ratio=profile["backRatio"],
            front_ratio=profile["frontRatio"],
        )["validatedScore"]

        rows.append({
            "name": name,
            "gainPctPer1": ((score / baseline) - 1.0) * 100.0,
            "confidence": "high",
        })

    return sorted(rows, key=lambda x: x["gainPctPer1"], reverse=True)


def build_pro_analysis(a, b):
    ca = a.get("combinedOffense") or []
    cb = b.get("combinedOffense") or []

    ma = _canon_map(ca)
    mb = _canon_map(cb)
    sa = _stone_canon_map(a.get("magicStoneTotals"))
    sb = _stone_canon_map(b.get("magicStoneTotals"))

    gap_rows = []

    for name in OFFENSE_CANONICAL_GROUPS.keys():
        aa = ma.get(name)
        bb = mb.get(name)
        if not aa or not bb:
            continue

        av = aa.get("combinedValue")
        bv = bb.get("combinedValue")
        if av is None:
            av = aa.get("baseValue")
        if bv is None:
            bv = bb.get("baseValue")
        if av is None or bv is None:
            continue

        av = float(av)
        bv = float(bv)
        gap = bv - av
        relative = (gap / abs(bv) * 100.0) if bv else 0.0

        gap_rows.append({
            "name": name,
            "mine": av,
            "target": bv,
            "gap": gap,
            "relativeGapPct": relative,
            "mineStone": float((sa.get(name) or {}).get("total") or 0),
            "targetStone": float((sb.get(name) or {}).get("total") or 0),
        })

    deficits = sorted(
        [r for r in gap_rows if r["gap"] > 0],
        key=lambda r: (r["relativeGapPct"], r["gap"]),
        reverse=True,
    )
    surpluses = sorted(
        [r for r in gap_rows if r["gap"] < 0],
        key=lambda r: abs(r["relativeGapPct"]),
        reverse=True,
    )

    # CP-gap attribution is observational, not an exact CP formula.
    info_a = a.get("info") or {}
    info_b = b.get("info") or {}
    cp_gap = float(info_b.get("combatPower") or 0) - float(info_a.get("combatPower") or 0)

    eq_a = a.get("equipment") or []
    eq_b = b.get("equipment") or []
    arc_a = a.get("arcana") or []
    arc_b = b.get("arcana") or []
    skill_a = a.get("skills") or []
    skill_b = b.get("skills") or []
    pass_a = a.get("passives") or []
    pass_b = b.get("passives") or []

    def avg_level(rows):
        vals = []
        for x in rows:
            try:
                if x.get("level") is not None:
                    vals.append(float(x.get("level")))
            except Exception:
                pass
        return sum(vals) / len(vals) if vals else 0.0

    def avg_enhance(rows):
        vals = []
        for x in rows:
            try:
                if x.get("enhance") is not None:
                    vals.append(float(x.get("enhance")))
            except Exception:
                pass
        return sum(vals) / len(vals) if vals else 0.0

    cp_factors = [
        {
            "name": "장비 강화",
            "mine": avg_enhance(eq_a),
            "target": avg_enhance(eq_b),
        },
        {
            "name": "아르카나 평균 레벨",
            "mine": avg_level(arc_a),
            "target": avg_level(arc_b),
        },
        {
            "name": "스킬 평균 레벨",
            "mine": avg_level(skill_a),
            "target": avg_level(skill_b),
        },
        {
            "name": "패시브 평균 레벨",
            "mine": avg_level(pass_a),
            "target": avg_level(pass_b),
        },
    ]
    for row in cp_factors:
        row["gap"] = row["target"] - row["mine"]

    # Observed ranking entries can provide context.
    rankings_a = a.get("rankings") or []
    rankings_b = b.get("rankings") or []

    job_a = str(info_a.get("job") or "")
    job_b = str(info_b.get("job") or "")
    profile_a = job_combat_profile(job_a)
    profile_b = job_combat_profile(job_b)
    efficiency = stat_marginal_efficiency(ca, job_a)

    medium_confidence_stats = [
        {"name": "무기 피해 증폭", "reason": "기존 프로젝트의 PVE 실측보정 딜상승 모델로 성장 우선순위에 반영"},
        {"name": "관통", "reason": "스킬별 고정 추가피해 성격이라 단순 % 비교에서 분리"},
        {"name": "완벽", "reason": "최대 공격력 적용 계열이며 평균 DPS 기여가 작게 관측"},
        {"name": "다단 히트", "reason": "연쇄 확률과 타수별 추가피해 구조를 별도 계산해야 함"},
    ]

    validated_priority_names = {
        "공격력",
        "피해 증폭",
        "PVE 피해 증폭",
        "보스 피해 증폭",
        "치명타 피해 증폭",
        "무기 피해 증폭",
        "강타",
    }

    if profile_a["backRatio"] > 0:
        validated_priority_names.add("후방 피해 증폭")
    if profile_a["frontRatio"] > 0:
        validated_priority_names.add("전방 피해 증폭")

    baseline_score = damage_index_from_rows(
        ca,
        back_ratio=profile_a["backRatio"],
        front_ratio=profile_a["frontRatio"],
    )["validatedScore"]

    modeled = []

    for row in deficits:
        name = row["name"]

        if name not in validated_priority_names:
            continue

        modified = [dict(x) for x in ca]
        target = next((x for x in modified if x.get("name") == name), None)
        if target is None:
            continue

        current = target.get("combinedValue")
        if current is None:
            current = target.get("baseValue") or 0
        current = float(current or 0)
        gap_value = float(row["gap"] or 0)

        # Use the same option-damage model already used by this project.
        # Previously weapon amp was excluded here, which could incorrectly push
        # PVE amp to #1 even when the existing damage-gain model rated weapon amp higher.
        if name == "무기 피해 증폭":
            weapon_eff_per_1 = 0.477 * ((1.0 + 77.1 / 100.0) / max(0.01, 1.0 + current / 100.0))
            recovery_pct = max(0.0, gap_value * weapon_eff_per_1)
        elif name == "강타":
            # PvE hard hit proc is modeled as 2x damage -> E[mult] = 1 + p.
            cur_mult = max(0.01, 1.0 + current / 100.0)
            tgt_mult = max(0.01, 1.0 + (current + gap_value) / 100.0)
            recovery_pct = max(0.0, (tgt_mult / cur_mult - 1.0) * 100.0)
        else:
            target["combinedValue"] = current + gap_value
            new_score = damage_index_from_rows(
                modified,
                back_ratio=profile_a["backRatio"],
                front_ratio=profile_a["frontRatio"],
            )["validatedScore"]
            recovery_pct = ((new_score / baseline_score) - 1.0) * 100.0 if baseline_score > 0 else 0.0

        item = dict(row)
        item["expectedRecoveryPct"] = recovery_pct
        modeled.append(item)

    modeled.sort(key=lambda x: x.get("expectedRecoveryPct", 0.0), reverse=True)

    priorities = []
    for rank, row in enumerate(modeled[:6], start=1):
        stone_shortage = row["targetStone"] - row["mineStone"]

        if stone_shortage > 0:
            action = (
                f"마석 총합 약 {stone_shortage:.2f} 부족 · "
                f"격차 회복 기대딜 약 +{row['expectedRecoveryPct']:.2f}%"
            )
        else:
            action = (
                f"장비/아르카나/스킬/패시브 쪽 격차 · "
                f"회복 기대딜 약 +{row['expectedRecoveryPct']:.2f}%"
            )

        priorities.append({"rank": rank, **row, "action": action})

    conditional_checks = []
    for row in deficits:
        if row["name"] in validated_priority_names:
            continue
        if row["name"] in {
            "치명타", "명중",
            "완벽", "관통", "공격 속도", "시전 속도",
        }:
            conditional_checks.append({
                **row,
                "reason": "실제 발동률/스킬별 적용계수가 확정되지 않아 메인 우선순위에서 분리",
            })

    swap_candidates = []
    for d in deficits:
        if d["targetStone"] <= d["mineStone"]:
            continue
        for s in surpluses:
            if s["mineStone"] > s["targetStone"]:
                swap_candidates.append({
                    "from": s["name"],
                    "to": d["name"],
                    "fromExcess": s["mineStone"] - s["targetStone"],
                    "toShortage": d["targetStone"] - d["mineStone"],
                })
                break
        if len(swap_candidates) >= 4:
            break

    return {
        "sameJob": str(info_a.get("job") or "") == str(info_b.get("job") or ""),
        "combatPowerGap": cp_gap,
        "priorities": priorities,
        "deficits": deficits[:6],
        "surpluses": surpluses[:6],
        "swapCandidates": swap_candidates,
        "marginalEfficiency": efficiency[:10],
        "mediumConfidenceStats": medium_confidence_stats,
        "researchModel": AION2_RESEARCH_RULES,
        "growthFactors": cp_factors,
        "rankingA": rankings_a[:5],
        "rankingB": rankings_b[:5],
        "jobProfileA": profile_a,
        "jobProfileB": profile_b,
        "conditionalChecks": conditional_checks[:8],
    }



def build_character_option_feedback(character):
    """Calculate option-line damage feedback from the searched character's current stats."""
    rows = character.get("combinedOffense") or []
    cmap = _canon_map(rows)

    def cur(name):
        row = cmap.get(name) or {}
        value = row.get("combinedValue")
        if value is None:
            value = row.get("baseValue")
        try:
            return float(value or 0)
        except Exception:
            return 0.0

    def bucket_gain(current, delta):
        base = max(0.0001, 1.0 + float(current) / 100.0)
        nxt = max(0.0001, 1.0 + (float(current) + float(delta)) / 100.0)
        return (nxt / base - 1.0) * 100.0

    weapon_now = cur("무기 피해 증폭")
    front_now = cur("전방 피해 증폭")
    rear_now = cur("후방 피해 증폭")

    # PVE empirical calibration around 77.1% weapon amplification:
    # +1% weapon amp ~= +0.477% boss damage.
    weapon_eff_per_1 = 0.477 * (
        (1.0 + 77.1 / 100.0) /
        max(0.01, 1.0 + weapon_now / 100.0)
    )

    options = [
        {
            "name": "무피 +0.5",
            "stat": "무기 피해 증폭",
            "current": weapon_now,
            "delta": 0.5,
            "gainPct": 0.5 * weapon_eff_per_1,
            "mode": "PVE 실측보정 추정",
        },
        {
            "name": "전피 +0.9",
            "stat": "전방 피해 증폭",
            "current": front_now,
            "delta": 0.9,
            "gainPct": bucket_gain(front_now, 0.9),
            "mode": "전방 적중 시",
        },
        {
            "name": "후피 +0.9",
            "stat": "후방 피해 증폭",
            "current": rear_now,
            "delta": 0.9,
            "gainPct": bucket_gain(rear_now, 0.9),
            "mode": "후방 적중 시",
        },
    ]

    ranked = sorted(options, key=lambda x: x.get("gainPct", 0.0), reverse=True)

    return {
        "options": options,
        "ranked": ranked,
        "best": ranked[0] if ranked else None,
        "basis": "검색 캐릭터 현재 스탯 기준",
    }



async def _full_profile_for_exact_character(nickname: str, server_name: str):
    """
    v59 self-DB first:
      1) use the last saved detailed profile immediately when present;
      2) otherwise fetch once and persist it;
      3) never discard an existing saved profile because refresh failed.
    """
    target_sid = SERVER_ID_MAP.get(server_name)
    if not target_sid:
        return None, None

    db_row, db_profile = await character_db_get_full_profile(nickname, server_name)

    def db_row_for_profile(row):
        if not row:
            return None
        return {
            "name": row.get("name") or nickname,
            "serverName": row.get("server_name") or server_name,
            "serverId": int(row.get("server_id") or target_sid),
            "characterId": row.get("character_id") or "",
            "className": row.get("job") or "",
            "combatPower": int(row.get("combat_power") or 0),
            "characterLevel": int(row.get("level") or 0),
            "profileImage": row.get("profile_image") or "",
        }

    saved_row = db_row_for_profile(db_row)
    if db_profile:
        return saved_row, db_profile

    row = saved_row
    if not row or not row_character_id(row):
        try:
            candidates = await search_characters_all_servers(nickname)
        except Exception:
            candidates = []

        exact_rows = [r for r in candidates if row_server_id(r) == int(target_sid)]
        if not exact_rows:
            target = server_name.casefold()
            exact_rows = [
                r for r in candidates
                if row_server_name(r) and row_server_name(r).casefold() == target
            ]
        if exact_rows:
            row = exact_rows[0]

    if not row:
        return saved_row, None

    sid = row_server_id(row) or int(target_sid)
    cid = row_character_id(row)
    if not cid:
        return row, None

    cache_key = f"compare-full-profile:{sid}:{cid}"
    cached = cache_get(cache_key, 120)
    if cached is not None:
        await character_db_save_full_profile(nickname, server_name, cached, cid)
        return row, cached

    for attempt in range(3):
        try:
            profile = await get_profile(sid, cid, fast=False)
            has_profile = bool(
                ((profile.get("info") or {}).get("profile"))
                or profile.get("itemDetails")
                or _equipment_rows(profile)
                or extract_profile_stats(profile)
                or extract_arcana(profile)
                or extract_skills(profile)
                or extract_passives(profile)
            )
            if has_profile:
                cache_set(cache_key, profile)
                await character_db_save_full_profile(nickname, server_name, profile, cid)
                return row, profile
        except Exception:
            pass
        await asyncio.sleep(0.35 * (attempt + 1))

    db_row2, db_profile2 = await character_db_get_full_profile(nickname, server_name)
    if db_profile2:
        return db_row_for_profile(db_row2), db_profile2
    return row, None


async def detailed_character_data(nickname: str, server_name: str):
    # Basic DB/NotMeter fallback first.
    basic_resolved = await own_resolve_character(nickname, server_name)

    basic = {}
    if basic_resolved.get("type") == "detail":
        basic = basic_resolved.get("info") or {}

    row, full_profile = await _full_profile_for_exact_character(
        nickname,
        server_name,
    )

    full_info = basic
    profile_available = False

    if row is not None:
        if full_profile:
            full_info = profile_info(
                full_profile,
                nickname,
                server_name,
                row,
            )
            # Only mark detailed when full item profile is actually present.
            profile_available = bool(
                _equipment_rows(full_profile)
                or extract_profile_stats(full_profile)
                or extract_arcana(full_profile)
                or extract_skills(full_profile)
                or extract_passives(full_profile)
            )
        elif not full_info:
            full_info = profile_info(
                {},
                nickname,
                server_name,
                row,
            )

    equipment = _equipment_rows(full_profile) if full_profile else []

    # Compare uses the same profile-wide stone discovery as character lookup.
    # Fall back to equipment-normalized stones only when no profile-wide stones exist.
    stones = _stone_totals_from_profile(full_profile) if full_profile else []
    if not stones:
        stones = _stone_totals_from_equipment(equipment)
    stats = extract_profile_stats(full_profile) if full_profile else []

    combined_offense = build_combined_offense(
        stats,
        stones,
    )

    option_feedback = build_character_option_feedback({
        "combinedOffense": combined_offense,
        "info": full_info,
    })

    arcana = extract_arcana(full_profile) if full_profile else []
    skills = _dedupe_and_filter_level_rows(
        extract_skills(full_profile) if full_profile else [],
        min_level=16,
    )
    passives = _dedupe_and_filter_level_rows(
        extract_passives(full_profile) if full_profile else [],
        min_level=16,
    )

    rankings = []
    try:
        ranking_cache = await fetch_ranking_cache()
        rankings = find_character_rankings(
            ranking_cache,
            full_info,
        )[:8]
    except Exception:
        rankings = []

    return {
        "ok": bool(full_info),
        "profileAvailable": profile_available,
        "info": full_info,
        "equipment": equipment,
        "magicStoneTotals": stones,
        "stats": stats,
        "combinedOffense": combined_offense,
        "optionFeedback": option_feedback,
        "arcana": arcana,
        "skills": skills,
        "passives": passives,
        "rankings": rankings,
        "dataHealth": {
            "profileAvailable": profile_available,
            "equipmentCount": len(equipment),
            "stoneGroupCount": len(stones),
            "statCount": len(stats),
            "arcanaCount": len(arcana),
            "skillCount": len(skills),
            "passiveCount": len(passives),
            "offenseNames": [row.get("name") for row in combined_offense],
        },
    }




COMPARE_SITE_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AION2 세팅 분석기 미리보기</title>
<style>
:root{
  --bg:#08111e;--panel:#101a2b;--panel2:#0c1625;--line:#26374f;
  --txt:#f5f7fb;--muted:#9cafc8;--blue:#78a9ff;--violet:#b995ff;
  --good:#6fd69d;--bad:#ff8792;--gold:#f6ce68;--cyan:#7fddff;
}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#07101b,#0b1422 45%,#111827);color:var(--txt);font-family:Arial,"Noto Sans KR",sans-serif}
.wrap{max-width:1380px;margin:auto;padding:24px 16px 60px}
.hero,.panel{background:rgba(16,26,43,.97);border:1px solid var(--line);border-radius:18px;padding:18px}
h1{margin:0;font-size:30px}.sub{color:var(--muted);margin-top:6px}.section{margin-top:16px}
.search{display:grid;grid-template-columns:1fr 1fr auto;gap:10px;margin-top:18px}
input,button{height:42px;border-radius:10px;border:1px solid var(--line);background:#091422;color:var(--txt);padding:0 12px}
button{background:var(--blue);color:#06101c;border:0;font-weight:900;cursor:pointer}
.topgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.char{display:grid;grid-template-columns:66px 1fr;gap:12px;align-items:center}
.avatar{width:66px;height:66px;border-radius:15px;display:flex;align-items:center;justify-content:center;font-size:25px;font-weight:900;border:1px solid var(--line)}
.a .avatar{background:#16325a;color:#bcd6ff}.b .avatar{background:#40265c;color:#e0c8ff}
.name{font-size:22px;font-weight:900}.meta{font-size:12px;color:var(--muted);margin-top:3px}.cp{font-size:32px;font-weight:900;color:var(--gold);margin-top:4px}
.badges{margin-top:9px;display:flex;flex-wrap:wrap;gap:6px}.badge{font-size:11px;padding:4px 8px;border-radius:999px;background:#17253b;color:#c8d9f4}
.head{font-size:19px;font-weight:900;margin-bottom:12px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.kpi{background:var(--panel2);border:1px solid var(--line);border-radius:13px;padding:14px}
.kpi .label{font-size:12px;color:var(--muted)}.kpi .value{font-size:24px;font-weight:900;margin-top:4px}
.good{color:var(--good)}.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;background:var(--panel2);border-radius:12px;overflow:hidden}
th,td{padding:10px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);text-align:right}th:first-child,td:first-child{text-align:left}td{text-align:right}
.group td{background:#132036!important;color:#8fbaff;font-weight:900;text-align:left!important}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.owner{border:1px solid var(--line);border-radius:14px;padding:12px}
.owner.a{background:linear-gradient(180deg,rgba(47,89,151,.18),rgba(10,18,31,.86));border-color:#365f97}
.owner.b{background:linear-gradient(180deg,rgba(110,64,145,.16),rgba(10,18,31,.86));border-color:#674886}
.ownerhead{font-weight:900;margin-bottom:10px}.a .ownerhead{color:#b9d5ff}.b .ownerhead{color:#dfc8ff}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
.card{background:#0b1524;border:1px solid #253751;border-radius:11px;padding:9px;cursor:pointer}
.rowcard{display:grid;grid-template-columns:36px 1fr;gap:8px;align-items:center}
.icon{width:36px;height:36px;border-radius:8px;background:#1b2a42;display:flex;align-items:center;justify-content:center;font-size:17px}
.ctitle{font-size:13px;font-weight:900}.tiny{font-size:11px;color:var(--muted);margin-top:2px}
.prio{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.pcard{background:#0b1524;border:1px solid var(--line);border-radius:12px;padding:13px}
.rank{font-weight:900;color:var(--gold)}.pct{font-size:21px;font-weight:900;color:var(--cyan);margin-top:5px}
.note{background:#0a1422;border:1px dashed #334764;border-radius:12px;padding:12px;color:var(--muted);font-size:12px;line-height:1.5}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;justify-content:center;padding:18px}
.modal.show{display:flex}.modalbox{width:min(560px,100%);background:#101a2a;border:1px solid #3a5072;border-radius:16px;padding:16px}
.close{float:right;width:auto;height:34px;padding:0 10px;background:#1a2a42;color:#fff}
.compare-search{grid-template-columns:1fr 1fr auto;align-items:stretch}
.search-pair{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(105px,.75fr);gap:8px;padding:8px;border:1px solid var(--line);border-radius:12px;background:#0b1524}
.search-pair.a{border-color:#365f97}.search-pair.b{border-color:#674886}
.search-pair input{width:100%;min-width:0}
@media(max-width:900px){.search,.topgrid,.split,.prio,.kpis{grid-template-columns:1fr}.compare-search{grid-template-columns:1fr}.search-pair{grid-template-columns:1fr 115px}}

.equip-toggle-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}
.mini-btn{border:1px solid var(--line);background:#111d2f;color:#dbeafe;border-radius:10px;padding:9px 12px;font-weight:700;cursor:pointer}
.mini-btn:hover{background:#17263d}
.cards.equip-collapsed{display:none}
@media(max-width:900px){.equip-toggle-row{grid-template-columns:1fr}}


.damage-feedback-panel .feedback-list{display:grid;gap:8px}
.feedback-row{display:grid;grid-template-columns:minmax(130px,1fr) 90px 110px;gap:8px;align-items:center;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#0b1524}
.feedback-row .fname{font-weight:850}
.feedback-row .fcur{font-size:12px;color:var(--muted)}
.feedback-row .fgain{font-size:18px;font-weight:900;text-align:right;color:#34d399}
.feedback-best{border-color:#5f8cff;box-shadow:0 0 0 1px rgba(95,140,255,.18) inset}
.level-table{margin-top:10px}
.level-table th{position:sticky;top:0;background:#111c2e;z-index:2}
.level-table td{padding:12px 10px}
.level-table td:first-child{font-weight:800}
.level-table td:nth-child(2),.level-table td:nth-child(3){font-weight:850;font-size:15px}
.level-table td:last-child{font-weight:900}
.skill-readability-note{display:flex;gap:7px;flex-wrap:wrap;margin-top:8px}
.skill-chip{padding:5px 8px;border:1px solid var(--line);border-radius:999px;font-size:12px;color:#cbd5e1;background:#0b1524}
@media(max-width:900px){
  .feedback-row{grid-template-columns:1fr 82px}
  .feedback-row .fcur{grid-column:1/2}
  .feedback-row .fgain{grid-column:2/3;grid-row:1/3}
}

/* v64 compare readability + visual item cards */
.attack-groups{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.attack-group{background:#0b1524;border:1px solid var(--line);border-radius:14px;overflow:hidden}
.attack-group-title{padding:11px 12px;background:#132036;color:#9fc3ff;font-weight:900;border-bottom:1px solid var(--line)}
.attack-group table{border-radius:0;background:transparent}
.attack-group th{background:#0d1828;font-size:11px}
.attack-group td{padding:11px 9px}
.attack-group td:first-child{font-weight:800}
.visual-compare{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.visual-owner{border:1px solid var(--line);border-radius:14px;padding:12px;background:#0b1524}
.visual-owner.a{border-color:#365f97}.visual-owner.b{border-color:#674886}
.visual-owner-title{font-weight:900;font-size:16px;margin-bottom:10px}
.visual-owner.a .visual-owner-title{color:#b9d5ff}.visual-owner.b .visual-owner-title{color:#dfc8ff}
.visual-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:8px}
.visual-card{display:grid;grid-template-columns:44px minmax(0,1fr);gap:9px;align-items:center;background:#0e1a2b;border:1px solid #263851;border-radius:11px;padding:8px;min-height:62px}
.visual-icon{width:44px;height:44px;border-radius:9px;background:#1b2a42;border:1px solid #31435f;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:18px;font-weight:900;color:#cbd5e1}
.visual-icon img{width:100%;height:100%;object-fit:cover;display:block}
.visual-name{font-size:12px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.visual-meta{font-size:11px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.visual-empty{color:var(--muted);font-size:12px;padding:12px;border:1px dashed #334764;border-radius:10px}
.visual-subhead{font-size:13px;font-weight:900;color:#cbd5e1;margin:13px 0 8px}
.level-table thead th:nth-child(2),.level-table tbody td:nth-child(2){background:rgba(54,95,151,.13)}
.level-table thead th:nth-child(3),.level-table tbody td:nth-child(3){background:rgba(103,72,134,.13)}
.level-table tbody tr:hover td{background-color:rgba(120,169,255,.07)}
@media(max-width:1100px){.attack-groups{grid-template-columns:1fr}.visual-compare{grid-template-columns:1fr}}

/* v69 percent + magic-stone readability */
.stone-compare{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.stone-owner{background:#0b1524;border:1px solid var(--line);border-radius:14px;overflow:hidden}
.stone-owner.a{border-color:#365f97}.stone-owner.b{border-color:#674886}
.stone-owner-head{padding:11px 12px;font-weight:900;background:#111f34;border-bottom:1px solid var(--line)}
.stone-owner.a .stone-owner-head{color:#b9d5ff}.stone-owner.b .stone-owner-head{color:#dfc8ff}
.stone-list{padding:8px 10px 10px}
.stone-row{display:grid;grid-template-columns:minmax(0,1fr) 58px 90px;gap:8px;align-items:center;padding:9px 4px;border-bottom:1px solid rgba(148,163,184,.12)}
.stone-row:last-child{border-bottom:0}
.stone-name{font-weight:800;font-size:13px}.stone-count{text-align:center;color:var(--muted);font-size:12px}.stone-total{text-align:right;font-weight:900;color:var(--cyan)}
.stone-empty{padding:14px 4px;color:var(--muted);font-size:12px}
.final-breakdown{display:block;margin-top:3px;color:var(--muted);font-size:10px;font-weight:500}
@media(max-width:900px){.stone-compare{grid-template-columns:1fr}}

.profile-extra{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
.skill-cell{display:flex;align-items:center;gap:10px;min-width:0}
.skill-icon{width:40px;height:40px;border-radius:10px;border:1px solid #31435f;background:#132036;overflow:hidden;flex:0 0 40px}
.skill-icon img{width:100%;height:100%;display:block;object-fit:cover}
.skill-label{min-width:0}.skill-main{font-size:13px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.skill-sub{font-size:11px;color:var(--muted);margin-top:2px}
.eq-compare{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.eq-owner{border:1px solid var(--line);border-radius:14px;padding:12px;background:#0b1524}
.eq-owner.a{border-color:#365f97}.eq-owner.b{border-color:#674886}
.eq-owner-head{font-weight:900;font-size:16px;margin-bottom:10px}.eq-owner.a .eq-owner-head{color:#b9d5ff}.eq-owner.b .eq-owner-head{color:#dfc8ff}
.eq-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}.eq-card{display:grid;grid-template-columns:42px minmax(0,1fr);gap:9px;align-items:center;background:#0e1a2b;border:1px solid #263851;border-radius:11px;padding:8px;min-height:60px}
.eq-icon{width:42px;height:42px;border-radius:9px;background:#17253b;border:1px solid #31435f;overflow:hidden;display:flex;align-items:center;justify-content:center;color:#cbd5e1;font-weight:900}
.eq-icon img{width:100%;height:100%;display:block;object-fit:cover}.eq-slot{font-size:11px;color:#9fc3ff;font-weight:900}.eq-name{font-size:12px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.eq-meta{font-size:11px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stone-diff-wrap{margin-top:12px}.stone-diff-wrap table td:first-child{font-weight:800}
@media(max-width:900px){.eq-compare{grid-template-columns:1fr}}


/* v75 requested UI: keep original theme, only requested compare blocks changed */
.top-extra{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}.title-badge{background:#14243b;border:1px solid #2f4d73}.wing-badge{display:inline-flex;align-items:center;gap:5px}.wing-badge img{width:18px;height:18px;object-fit:contain}
.skill-grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.skill-panel{background:#0b1524;border:1px solid var(--line);border-radius:14px;overflow:hidden}.skill-panel-head{padding:11px 12px;background:#111f34;border-bottom:1px solid var(--line);font-weight:900}.skill-table{width:100%;border-radius:0}.skill-table th{background:#0d1828;font-size:11px}.skill-table td{padding:8px}.skill-table .skill-icon{width:32px;height:32px;flex-basis:32px;border-radius:7px}.skill-table .skill-main{font-size:12px}.skill-table .skill-sub{display:none}.skill-table td:nth-child(n+2),.skill-table th:nth-child(n+2){text-align:center}.skill-table td:first-child{width:58%}
.equip-detail-compare{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}.equip-detail-owner{border:1px solid var(--line);border-radius:14px;padding:12px;background:#0b1524;min-width:0}.equip-detail-owner.a{border-color:#365f97}.equip-detail-owner.b{border-color:#674886}.equip-detail-head{font-size:16px;font-weight:900;margin-bottom:10px}.equip-section-label{margin:8px 0 5px;font-size:10px;font-weight:900;color:#9fc3ff}.equip-exceed.diff{color:#ff6b7a;font-weight:900}.equip-exceed{color:#ffb36b}.equip-detail-list{display:grid;gap:10px}.equip-folds{display:grid;gap:10px}.equip-fold{border:1px solid #263851;border-radius:12px;background:#0c1727;overflow:hidden}.equip-fold>summary{list-style:none;cursor:pointer;padding:11px 12px;font-size:13px;font-weight:900;color:#dbeafe;background:#111f34;display:flex;align-items:center;justify-content:space-between;gap:10px}.equip-fold>summary::-webkit-details-marker{display:none}.equip-fold>summary::after{content:'펼치기';font-size:10px;color:#7dd3fc;font-weight:800}.equip-fold[open]>summary::after{content:'접기'}.equip-fold-body{padding:10px;display:grid;gap:10px}.equip-detail-card{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(210px,.92fr);gap:14px;border:1px solid #263851;border-radius:12px;background:#0e1a2b;padding:10px;min-height:180px;height:auto;overflow:visible;box-sizing:border-box}.equip-detail-left{display:grid;grid-template-columns:52px minmax(0,1fr);gap:10px;min-width:0}.equip-big-icon{width:52px;height:52px;border-radius:8px;border:1px solid #31435f;background:#142033;overflow:hidden}.equip-big-icon img{width:100%;height:100%;object-fit:cover}.equip-item-title{font-size:13px;font-weight:900;color:#ff7a2f;line-height:1.35;word-break:keep-all}.equip-item-slot{font-size:10px;color:#7fddff;margin-bottom:3px}.equip-mainstats{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;margin-top:4px}.equip-mainstat{font-size:11px;color:#dbe7f7;min-height:22px;display:flex;align-items:center;line-height:1.25}.equip-source{min-height:22px;margin-top:6px;font-size:10px;color:var(--muted);display:flex;align-items:center;white-space:normal;line-height:1.25}.equip-right{border-left:1px solid #2a5267;padding-left:10px;min-width:0}.equip-option-list{display:grid;gap:4px}.equip-substat{display:flex;justify-content:space-between;align-items:center;gap:8px;border:1px solid #20475b;border-radius:6px;padding:4px 7px;font-size:11px;background:#09202b;min-height:24px;overflow:hidden}.equip-substat span{min-width:0;white-space:normal;word-break:keep-all}.equip-substat b{color:#fff;white-space:nowrap}.equip-substat.diff{border-color:#7f2d38;background:#2a1018;color:#ff9aa6}.equip-substat.diff b{color:#ff6b7a}.equip-extra-group{margin-top:8px}.equip-extra-group:first-child{margin-top:0}.equip-extra-title{font-size:10px;font-weight:900;color:#9fc3ff;margin-bottom:4px}.equip-stone{border-color:#3c4f2b;background:#152012}.equip-godstone{border-color:#59472b;background:#211a10}.equip-skillopt{border-color:#5d3f80;background:#21172f}.equip-empty{color:var(--muted);padding:10px}.stone-diff-wrap th,.stone-diff-wrap td{font-size:12px}
@media(max-width:1000px){.skill-grid3,.equip-detail-compare{grid-template-columns:1fr}.equip-detail-card{grid-template-columns:1fr}.equip-right{border-left:0;border-top:1px solid #2a5267;padding-left:0;padding-top:8px}}

</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <h1>AION2 프로 세팅 비교</h1>
    <div class="sub" id="loadStatus">캐릭터명과 서버를 각각 입력</div>
    <div class="search compare-search">
      <div class="search-pair a">
        <input id="nameA" placeholder="캐릭터명">
        <input id="serverA" placeholder="서버명">
      </div>
      <div class="search-pair b">
        <input id="nameB" placeholder="캐릭터명">
        <input id="serverB" placeholder="서버명">
      </div>
      <button id="compareBtn">비교 분석</button>
    </div>
  </div>

  <div class="topgrid section">
    <div class="panel a" id="charA">
      <div class="char"><div class="avatar">A</div><div><div class="name">캐릭터 A</div><div class="meta">아이디 · 서버 입력</div><div class="cp">—</div></div></div>
    </div>
    <div class="panel b" id="charB">
      <div class="char"><div class="avatar">B</div><div><div class="name">캐릭터 B</div><div class="meta">아이디 · 서버 입력</div><div class="cp">—</div></div></div>
    </div>
  </div>

  <div class="panel section">
    <div class="head">마석 세팅 차이</div>
    <div class="note">현재 프로필에서 확인된 마석을 종류별로 합산 · 피해증폭 계열은 %로 표시</div>
    <div class="stone-compare" style="margin-top:10px">
      <div class="stone-owner a">
        <div class="stone-owner-head" id="stoneHeadA">A 마석</div>
        <div class="stone-list" id="stoneListA"><div class="stone-empty">데이터 대기</div></div>
      </div>
      <div class="stone-owner b">
        <div class="stone-owner-head" id="stoneHeadB">B 마석</div>
        <div class="stone-list" id="stoneListB"><div class="stone-empty">데이터 대기</div></div>
      </div>
    </div>
    <div class="stone-diff-wrap">
      <div class="visual-subhead">종류별 마석 차이</div>
      <table id="stoneDiffTable">
        <thead><tr><th>항목</th><th>A</th><th>B</th><th>차이</th></tr></thead>
        <tbody>
          <tr><td>마석 데이터 대기</td><td>—</td><td>—</td><td>—</td></tr>
        </tbody>
      </table>
    </div>
  </div>

<div class="panel section">
    <div class="head">스킬 · 스티그마 · 패시브 비교</div>
    <div class="note">NC 공식 인게임 아이콘 사용 · 각 항목은 레벨 높은 순</div>
    <div class="skill-grid3" style="margin-top:10px">
      <div class="skill-panel"><div class="skill-panel-head" id="activeTitle">액티브 스킬</div><table class="skill-table"><thead><tr><th>스킬</th><th id="activeAName">A</th><th id="activeBName">B</th><th>차이</th></tr></thead><tbody id="activeTable"></tbody></table></div>
      <div class="skill-panel"><div class="skill-panel-head" id="stigmaTitle">스티그마</div><table class="skill-table"><thead><tr><th>스티그마</th><th id="stigmaAName">A</th><th id="stigmaBName">B</th><th>차이</th></tr></thead><tbody id="stigmaTable"></tbody></table></div>
      <div class="skill-panel"><div class="skill-panel-head" id="passiveTitle">패시브 스킬</div><table class="skill-table"><thead><tr><th>패시브</th><th id="passiveAName">A</th><th id="passiveBName">B</th><th>차이</th></tr></thead><tbody id="passiveTable"></tbody></table></div>
    </div>
  </div>

  <div class="panel section">
    <div class="head">장비 상세</div>
    <div class="note">실제 착용 장비의 기본/추가 옵션을 NC 공식 데이터 그대로 표시</div>
    <div class="equip-detail-compare" style="margin-top:10px">
      <div class="equip-detail-owner a"><div class="equip-detail-head" id="equipHeadA">A 장비</div><div class="equip-detail-list" id="equipListA"></div></div>
      <div class="equip-detail-owner b"><div class="equip-detail-head" id="equipHeadB">B 장비</div><div class="equip-detail-list" id="equipListB"></div></div>
    </div>
  </div>

  <div id="modal" class="modal" onclick="if(event.target===this)hide()">
  <div class="modalbox">
    <button class="close" onclick="hide()">닫기</button>
    <div id="m1" class="name"></div>
    <div id="m2" class="meta" style="margin-top:6px"></div>
    <div id="m3" class="note" style="margin-top:12px"></div>
  </div>
</div>

<script>
const $=id=>document.getElementById(id);
const E=s=>String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));
const F=n=>Number(n||0).toLocaleString("ko-KR",{maximumFractionDigits:2});
let DATA=null;

function show(a,b,c){$("m1").textContent=a||"-";$("m2").textContent=b||"";$("m3").textContent=c||"추가 상세정보 없음";$("modal").classList.add("show")}
function hide(){$("modal").classList.remove("show")}
window.show=show; window.hide=hide;

function profileHTML(x,side){
  const i=x?.info||{};
  const p=(DATA?.proAnalysis||{})[side==="a"?"jobProfileA":"jobProfileB"]||{};
  const avatar=i.profileImage
    ? `<img class="avatar" src="${E(i.profileImage)}" style="object-fit:cover">`
    : `<div class="avatar">${E((i.name||"?").slice(0,1))}</div>`;
  const titles=(Array.isArray(i.titles)?i.titles:[]).filter(Boolean).slice(0,3);
  if(!titles.length && String(i.title||'').trim()) titles.push(String(i.title).trim());
  while(titles.length<3) titles.push('—');
  const itemLevel=Number(i.itemLevel||0);
  const wingName=String(i.wingName||'').trim();
  const wingChip=wingName?`<span class="badge wing-badge">${i.wingIcon?`<img src="${E(i.wingIcon)}" alt="">`:''}${E(wingName)}${Number(i.wingEnchantLevel||0)>0?` +${Number(i.wingEnchantLevel)}`:''}</span>`:'';
  return `<div class="char">${avatar}<div>
    <div class="name">${E(i.name||"-")}</div>
    <div class="meta">${E(i.server||"-")} · ${E(i.job||"-")} · Lv.${F(i.level)}</div>
    <div class="cp">${Math.round(Number(i.combatPower||0)/1000)}K</div>
    <div class="top-extra">${titles.map(t=>`<span class="badge title-badge">${E(t)}</span>`).join('')}${itemLevel>0?`<span class="badge">템렙 ${F(itemLevel)}</span>`:''}${wingChip}</div>
    <div class="badges"><span class="badge">${E(p.directionLabel||"포지션 수동")}</span>
    ${Number(p.frontRatio)>0?`<span class="badge">전방 ${F(p.frontRatio)}%</span>`:""}
    ${Number(p.backRatio)>0?`<span class="badge">후방 ${F(p.backRatio)}%</span>`:""}
    </div></div></div>`;
}

function canonMap(x){
  const m={};
  (x?.combinedOffense||[]).forEach(r=>{if(r?.name)m[r.name]=r});
  return m;
}
function valueOf(row){
  if(!row)return null;
  const v=row.combinedValue;
  if(v!==null && v!==undefined && Number.isFinite(Number(v))) return Number(v);
  if(row.baseValue!==null && row.baseValue!==undefined && Number.isFinite(Number(row.baseValue))) return Number(row.baseValue);
  return null;
}
function isPercentStat(key){
  return new Set([
    "공격력 증가율","피해 증폭","무기 피해 증폭","PVE 피해 증폭","보스 피해 증폭",
    "치명타 피해 증폭","전방 피해 증폭","후방 피해 증폭","공격 속도","시전 속도"
  ]).has(key);
}
function pctOrNumber(key,row,value){
  if(value===null) return "데이터 없음";
  return `${F(value)}${isPercentStat(key)?"%":""}`;
}
function breakdownHTML(key,row){
  if(!row) return "";
  const base=Number(row.baseValue||0), stone=Number(row.stoneTotal||0), count=Number(row.stoneCount||0);
  if(!count || !stone) return "";
  const suffix=isPercentStat(key)?"%":"";
  const baseLabel=row?.baseReconstructed?"기본(계산)":"기본";
  return `<span class="final-breakdown">${baseLabel} ${F(base)}${suffix} + 마석 ${F(stone)}${suffix}</span>`;
}
function renderStoneOwner(character, headId, listId){
  const name=character?.info?.name||"캐릭터";
  const head=$(headId), list=$(listId);
  if(head) head.textContent=`${name} 마석`;
  if(!list) return;
  const order=["전방 피해 증폭","후방 피해 증폭","무기 피해 증폭","치명타 피해 증폭","피해 증폭","PVE 피해 증폭","보스 피해 증폭","다단 히트 적중","전투 속도","공격력","추가 공격력","치명타","명중","강타","관통","공격 속도","시전 속도"];
  const allowed=new Set(order);
  const rows=(character?.magicStoneTotals||[])
    .filter(r=>r?.name && allowed.has(String(r.name)) && (Number(r?.count||0)>0 || Number(r?.total||0)!==0))
    .sort((x,y)=>order.indexOf(String(x.name))-order.indexOf(String(y.name)));
  if(!rows.length){list.innerHTML='<div class="stone-empty">확인된 공격 마석 데이터 없음</div>';return;}
  list.innerHTML=rows.map(r=>{
    const key=String(r.name||"마석"), count=Number(r.count||0), total=Number(r.total||0);
    const suffix=isPercentStat(key)?"%":"";
    return `<div class="stone-row"><div class="stone-name">${E(key)}</div><div class="stone-count">× ${count}</div><div class="stone-total">+${F(total)}${suffix}</div></div>`;
  }).join("");
}
function renderStones(a,b){
  renderStoneOwner(a,"stoneHeadA","stoneListA");
  renderStoneOwner(b,"stoneHeadB","stoneListB");
}

function renderStoneDiff(a,b){
  const ta={}, tb={};
  (a?.magicStoneTotals||[]).forEach(r=>{ if(r?.name) ta[r.name]={count:Number(r.count||0), total:Number(r.total||0)}; });
  (b?.magicStoneTotals||[]).forEach(r=>{ if(r?.name) tb[r.name]={count:Number(r.count||0), total:Number(r.total||0)}; });
  const order=["전방 피해 증폭","후방 피해 증폭","무기 피해 증폭","치명타 피해 증폭","피해 증폭","PVE 피해 증폭","보스 피해 증폭","다단 히트 적중","전투 속도","공격력","추가 공격력","치명타","명중","강타","관통","공격 속도","시전 속도"];
  const allowed=new Set(order);
  const names=[...new Set([...Object.keys(ta),...Object.keys(tb)])].filter(x=>allowed.has(x)).sort((x,y)=>{
    const ix=order.indexOf(x), iy=order.indexOf(y);
    return (ix<0?999:ix)-(iy<0?999:iy) || x.localeCompare(y,'ko');
  });
  const head=document.querySelectorAll('#stoneDiffTable thead th');
  if(head.length>=4){head[1].textContent=a?.info?.name||'A'; head[2].textContent=b?.info?.name||'B';}
  const body=$('stoneDiffTable').querySelector('tbody');
  if(!names.length){body.innerHTML='<tr><td>확인된 마석 데이터 없음</td><td>—</td><td>—</td><td>—</td></tr>';return;}
  body.innerHTML=names.map(name=>{
    const av=ta[name]||{count:0,total:0}, bv=tb[name]||{count:0,total:0};
    const diff=av.total-bv.total;
    const cls=diff<0?'bad':diff>0?'good':'';
    const suffix=isPercentStat(name)?'%':'';
    const aTxt=`+${F(av.total)}${suffix} · ${av.count}개`;
    const bTxt=`+${F(bv.total)}${suffix} · ${bv.count}개`;
    const dTxt=`${diff>0?'+':''}${F(diff)}${suffix}`;
    return `<tr><td>${E(name)}</td><td>${aTxt}</td><td>${bTxt}</td><td class="${cls}">${dTxt}</td></tr>`;
  }).join('');
}

function statText(s){
  if(!s||typeof s!=='object') return '';
  const name=String(s.name||s.id||'');
  const min=s.minValue!==undefined&&s.minValue!==null?String(s.minValue):'';
  const value=s.value!==undefined&&s.value!==null?String(s.value):'';
  const extra=s.extra!==undefined&&s.extra!==null&&String(s.extra)!=='0'&&String(s.extra)!=='0%'?String(s.extra):'';
  const val=min&&min!==value?`${min}~${value}`:value;
  return `${name} ${val}${extra?` (+${extra})`:''}`.trim();
}
function tuneValue(x){
  if(!x||typeof x!=='object')return '';
  return String(x.value??x.extra??'').trim();
}
function tuneMap(row){
  const m=new Map();
  (row?.subStats||[]).forEach(x=>{const k=String(x?.name||x?.id||'').trim();if(k)m.set(k,tuneValue(x));});
  return m;
}
function equipPeerMap(character){
  const m=new Map();
  (character?.equipmentDetailed||[]).forEach((r,i)=>m.set(String(r?.slotRaw||`${r?.slot||''}#${i}`),r));
  return m;
}
function renderOptionRows(rows, peerRows, cls=''){
  const peerNames=new Set((peerRows||[]).map(x=>String(x?.name||x?.skillName||x?.id||'').trim()).filter(Boolean));
  if(!Array.isArray(rows)||!rows.length)return '';
  return `<div class="equip-option-list">${rows.map(x=>{
    const key=String(x?.name||x?.skillName||x?.id||'-').trim();
    const value=String(x?.value??x?.level??x?.skillLevel??x?.extra??'').trim();
    const different=!peerNames.has(key); // same option name, value difference alone is not highlighted
    const icon=x?.icon?`<img src="${E(x.icon)}" alt="" style="width:18px;height:18px;border-radius:4px;object-fit:cover;vertical-align:middle;margin-right:5px">`:'';
    return `<div class="equip-substat ${cls}${different?' diff':''}"><span>${icon}${E(key)}</span><b>${E(value||'-')}</b></div>`;
  }).join('')}</div>`;
}
function syncEquipmentCardHeights(){
  requestAnimationFrame(()=>{
    const cards=[...document.querySelectorAll('#equipListA .equip-detail-card, #equipListB .equip-detail-card')];
    cards.forEach(el=>el.style.minHeight='0px');
    const groups=new Map();
    cards.forEach(el=>{const k=el.dataset.slotkey||'';if(!groups.has(k))groups.set(k,[]);groups.get(k).push(el);});
    groups.forEach(arr=>{const h=Math.max(...arr.map(el=>el.scrollHeight));arr.forEach(el=>el.style.minHeight=`${h}px`);});
  });
}
function isAccessorySlot(slot){
  const x=String(slot||'').trim();
  return ['목걸이','귀걸이','반지','팔찌','브로치','아뮬렛','펜던트','허리띠','벨트','룬'].some(k=>x.includes(k));
}
function renderEquipmentCard(r,rowIndex,peer){
  const slotKey=String(r?.slotRaw||`${r?.slot||''}#${rowIndex}`);
  const icon=r?.icon?`<img src="${E(r.icon)}" alt="">`:'';
  const enh=Number(r?.enchantLevel||0)>0?`+${Number(r.enchantLevel)} `:'';
  const myExceed=Number(r?.exceedLevel||0), peerExceed=Number(peer?.exceedLevel||0);
  const exceed=myExceed>0?` · <span class="equip-exceed${peer&&myExceed!==peerExceed?' diff':''}">초월 ${myExceed}</span>`:'';
  const main=Array.isArray(r?.mainStats)?r.mainStats:[];
  const subs=Array.isArray(r?.subStats)?r.subStats:[];
  const skillopts=Array.isArray(r?.skillOptions)?r.skillOptions:[];
  const godstones=Array.isArray(r?.godStoneStat)?r.godStoneStat:[];
  const mainCells=main.length?main.map(x=>`<div class="equip-mainstat">${E(statText(x))}</div>`).join(''):'<div class="equip-mainstat">—</div>';
  const source=(Array.isArray(r?.sources)?r.sources:[]).filter(Boolean).join(' · ');
  const right=[
    `<div class="equip-extra-group"><div class="equip-extra-title">조율 옵션</div>${renderOptionRows(subs,peer?.subStats||[])||'<div class="equip-mainstat">—</div>'}</div>`,
    skillopts.length?`<div class="equip-extra-group"><div class="equip-extra-title">조율 스킬</div>${renderOptionRows(skillopts,peer?.skillOptions||[],'equip-skillopt')}</div>`:'',
    godstones.length?`<div class="equip-extra-group"><div class="equip-extra-title">신석</div>${renderOptionRows(godstones,peer?.godStoneStat||[],'equip-godstone')}</div>`:''
  ].join('');
  return `<div class="equip-detail-card" data-slotkey="${E(slotKey)}"><div class="equip-detail-left"><div class="equip-big-icon">${icon}</div><div><div class="equip-item-slot">${E(r?.slot||'장비')}</div><div class="equip-item-title">${E(enh+(r?.name||'-'))}${exceed}</div><div class="equip-section-label">기본 스탯</div><div class="equip-mainstats">${mainCells}</div><div class="equip-source">${source?`출처 · ${E(source)}`:'출처 · —'}</div></div></div><div class="equip-right">${right}</div></div>`;
}
function renderEquipmentOwner(character, peerCharacter, headId, listId){
  $(headId).textContent=`${character?.info?.name||'캐릭터'} 장비`;
  const rows=Array.isArray(character?.equipmentDetailed)?character.equipmentDetailed:[];
  const peers=equipPeerMap(peerCharacter);
  if(!rows.length){$(listId).innerHTML='<div class="equip-empty">확인된 장비 상세 없음</div>';return;}
  const armor=[], acc=[];
  rows.forEach((r,i)=>(isAccessorySlot(r?.slot)?acc:armor).push([r,i]));
  const section=(title,items)=>`<details class="equip-fold"><summary><span>${E(title)}</span><span>${items.length}개</span></summary><div class="equip-fold-body">${items.length?items.map(([r,i])=>{const key=String(r?.slotRaw||`${r?.slot||''}#${i}`);return renderEquipmentCard(r,i,peers.get(key));}).join(''):'<div class="equip-empty">확인된 항목 없음</div>'}</div></details>`;
  $(listId).innerHTML=`<div class="equip-folds">${section('장비',armor)}${section('악세서리',acc)}</div>`;
  $(listId).querySelectorAll('details.equip-fold').forEach(d=>d.addEventListener('toggle',syncEquipmentCardHeights));
}
function renderEquipment(a,b){renderEquipmentOwner(a,b,'equipHeadA','equipListA');renderEquipmentOwner(b,a,'equipHeadB','equipListB');syncEquipmentCardHeights();}
function skillNameHTML(row, type){
  const name=String(row?.name||'-'), icon=String(row?.icon||'');
  return `<div class="skill-cell"><div class="skill-icon">${icon?`<img src="${E(icon)}" alt="">`:''}</div><div class="skill-label"><div class="skill-main">${E(name)}</div></div></div>`;
}

function renderGap(d){
  const p=d?.proAnalysis||{},a=Number(d?.a?.info?.combatPower||0),b=Number(d?.b?.info?.combatPower||0);
  const deficits=(p.deficits||[]).filter(x=>Number(x?.gap||0)>0);
  const first=deficits[0], second=deficits[1];
  const cards=$("gapKpis").querySelectorAll(".kpi .value");
  const shortageText=r=>r?`${r.name} ${Number(r.relativeGapPct||0).toFixed(1)}%`:"부족 없음";
  if(cards.length>=3){
    cards[0].className="value "+(a>=b?"good":"bad");
    cards[0].textContent=`${a>=b?"+":""}${Math.round((a-b)/1000)}K`;
    cards[1].className="value "+(first?"bad":"good"); cards[1].textContent=shortageText(first);
    cards[2].className="value "+(second?"bad":"good"); cards[2].textContent=shortageText(second);
  }
}

function renderPriority(p){
  const rows=p?.priorities||[], cards=$("priorityCards").querySelectorAll(".pcard");
  cards.forEach((card,i)=>{
    const r=rows[i];
    const rank=card.querySelector(".rank"), pct=card.querySelector(".pct"), tiny=card.querySelector(".tiny");
    if(!r){rank.textContent=`#${i+1} 분석 대기`;pct.textContent="—";tiny.textContent="검증 가능한 데이터 없음";return}
    rank.textContent=`#${i+1} ${r.name||"-"}`;
    pct.textContent=`+${Number(r.expectedRecoveryPct||0).toFixed(2)}%`;
    tiny.textContent=r.action||r.reason||"";
  });
  const c=p?.conditionalChecks||[];
  $("priorityNote").textContent=c.length
    ? `딜상승 계산 반영 · 별도 조건 확인: ${c.slice(0,5).map(x=>x.name).join(" · ")}`
    : "현재 수치와 상대 격차를 기존 딜상승 계산식으로 환산해 정렬.";
}



function filterLevelRows(rows){
  const best=new Map();
  (rows||[]).forEach(x=>{const name=String(x?.name||'').trim(),lv=Number(x?.level);if(!name||!Number.isFinite(lv)||lv<=0)return;const key=name.replace(/\s+/g,' ').toLowerCase();const old=best.get(key);if(!old||Number(old.level||0)<lv)best.set(key,{...x,level:lv});});
  return [...best.values()].sort((a,b)=>Number(b.level)-Number(a.level)||String(a.name).localeCompare(String(b.name),'ko'));
}
function levelMap(rows){const m={};(rows||[]).forEach(x=>{if(x?.name)m[x.name]=x});return m}
function renderSkillCompare(a,b,type){
  const source=type==='passive'?'passives':type==='stigma'?'stigma':'skills';
  const ra=filterLevelRows(a?.[source]||[]),rb=filterLevelRows(b?.[source]||[]),same=(a.info?.job||'')===(b.info?.job||''),ma=levelMap(ra),mb=levelMap(rb);
  const ids={active:['activeTitle','activeTable','activeAName','activeBName'],stigma:['stigmaTitle','stigmaTable','stigmaAName','stigmaBName'],passive:['passiveTitle','passiveTable','passiveAName','passiveBName']}[type];
  const [titleId,bodyId,aNameId,bNameId]=ids;$(aNameId).textContent=a.info?.name||'A';$(bNameId).textContent=b.info?.name||'B';$(titleId).textContent=type==='passive'?'패시브 스킬':type==='stigma'?'스티그마':'액티브 스킬';
  const body=$(bodyId);
  if(same){
    const names=[...new Set([...Object.keys(ma),...Object.keys(mb)])].sort((x,y)=>{const mx=Math.max(Number(ma[x]?.level||0),Number(mb[x]?.level||0)),my=Math.max(Number(ma[y]?.level||0),Number(mb[y]?.level||0));return my-mx||x.localeCompare(y,'ko')});
    body.innerHTML=names.map(n=>{const ar=ma[n],br=mb[n],av=Number(ar?.level),bv=Number(br?.level),va=Number.isFinite(av),vb=Number.isFinite(bv),d=va&&vb?av-bv:null,cls=d<0?'bad':d>0?'good':'';return `<tr><td>${skillNameHTML(ar||br||{name:n},type)}</td><td>${va?av:'—'}</td><td>${vb?bv:'—'}</td><td class="${cls}">${d===null?'—':`${d>0?'+':''}${d}`}</td></tr>`}).join('')||'<tr><td colspan="4">데이터 없음</td></tr>';
  }else{
    const n=Math.max(ra.length,rb.length);body.innerHTML=Array.from({length:n},(_,i)=>{const ar=ra[i],br=rb[i];return `<tr><td>${skillNameHTML(ar||br||{},type)}<div class="tiny">${E(ar?.name||'—')} / ${E(br?.name||'—')}</div></td><td>${ar?.level??'—'}</td><td>${br?.level??'—'}</td><td>—</td></tr>`}).join('')||'<tr><td colspan="4">데이터 없음</td></tr>';
  }
}

function render(d){
  DATA=d;
  $("charA").innerHTML=profileHTML(d.a,"a");
  $("charB").innerHTML=profileHTML(d.b,"b");

  renderStones(d.a,d.b);
  renderStoneDiff(d.a,d.b);
  renderSkillCompare(d.a,d.b,'active');
  renderSkillCompare(d.a,d.b,'stigma');
  renderSkillCompare(d.a,d.b,'passive');
  renderEquipment(d.a,d.b);
}

async function compare(){
  const A={name:$("nameA").value.trim(),server:$("serverA").value.trim()};
  const B={name:$("nameB").value.trim(),server:$("serverB").value.trim()};
  if(!A.name||!A.server||!B.name||!B.server){$("loadStatus").textContent="A/B 캐릭터명과 서버를 모두 입력";return}

  $("loadStatus").textContent="상세 데이터 분석 중...";
  $("compareBtn").disabled=true;
  try{
            const q=new URLSearchParams({name_a:A.name,server_a:A.server,name_b:B.name,server_b:B.server});
    const r=await fetch(`/api/compare?${q}`,{cache:"no-store"});
    const text=await r.text();
    let d;
    try{ d=JSON.parse(text); }catch(_){ throw new Error(`API 응답 오류 HTTP ${r.status}: ${text.slice(0,160)}`); }
    if(!r.ok||!d.ok){$("loadStatus").textContent=d?.message||d?.error||"캐릭터 상세 조회 실패";return}
    render(d);
    const ha=d.a?.dataHealth||{}, hb=d.b?.dataHealth||{};
    $("loadStatus").textContent=`${d.a.info?.name||A.name} ↔ ${d.b.info?.name||B.name} 비교 완료 · 스탯 ${ha.statCount||0}/${hb.statCount||0} · 스킬 ${ha.skillCount||0}/${hb.skillCount||0} · 스티그마 ${ha.stigmaCount||0}/${hb.stigmaCount||0} · 패시브 ${ha.passiveCount||0}/${hb.passiveCount||0}`;
  }catch(e){
    console.error(e); $("loadStatus").textContent=`비교 처리 오류: ${e?.message||e}`;
  }finally{
    $("compareBtn").disabled=false;
  }
}


$("compareBtn").addEventListener("click",compare);
["nameA","serverA","nameB","serverB"].forEach(id=>$(id).addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();compare();}}));
$("modal").addEventListener("click",e=>{if(e.target===$("modal"))hide()});
window.addEventListener("DOMContentLoaded",()=>{$("loadStatus").textContent="캐릭터명과 서버를 각각 입력";});
</script>
</body>
</html>
"""


@app.get("/compare", response_class=HTMLResponse)
async def compare_site():
    return HTMLResponse(
        COMPARE_SITE_HTML,
        media_type="text/html; charset=utf-8"
    )


def _single_character_site_html(nickname: str, server_name: str):
    """Reuse the existing compare UI/data renderer in one-character mode."""
    name_js = json.dumps(str(nickname or ""), ensure_ascii=False)
    server_js = json.dumps(str(server_name or ""), ensure_ascii=False)

    single_css = r"""
<style>
body.single-mode .hero h1{margin-bottom:4px}
body.single-mode .compare-search{grid-template-columns:1fr}
body.single-mode .search-pair.b,
body.single-mode #compareBtn,
body.single-mode #charB,
body.single-mode .stone-owner.b,
body.single-mode .stone-diff-wrap,
body.single-mode .equip-detail-owner.b{display:none!important}
body.single-mode .topgrid,
body.single-mode .stone-compare,
body.single-mode .equip-detail-compare{grid-template-columns:1fr!important}
body.single-mode .skill-table th:nth-child(3),
body.single-mode .skill-table td:nth-child(3),
body.single-mode .skill-table th:nth-child(4),
body.single-mode .skill-table td:nth-child(4){display:none!important}
body.single-mode .skill-table td:first-child{width:76%}
body.single-mode .panel.a,
body.single-mode .stone-owner.a,
body.single-mode .equip-detail-owner.a{border-color:#365f97}
</style>
"""

    single_js = fr"""
<script>
window.addEventListener("DOMContentLoaded", async () => {{
  document.body.classList.add("single-mode");
  const NAME={name_js};
  const SERVER={server_js};
  document.querySelector(".hero h1").textContent="AION2 캐릭터 상세";
  $("nameA").value=NAME;
  $("serverA").value=SERVER;
  $("loadStatus").textContent=`${{NAME}} · ${{SERVER}} 최신 상세정보 불러오는 중...`;

  try {{
    const q=new URLSearchParams({{nickname:NAME,server:SERVER}});
    const r=await fetch(`/api/compare-character?${{q}}`,{{cache:"no-store"}});
    const text=await r.text();
    let d;
    try {{ d=JSON.parse(text); }}
    catch(_) {{ throw new Error(`API 응답 오류 HTTP ${{r.status}}`); }}

    if(!r.ok || !d.ok) {{
      $("loadStatus").textContent=d?.error||"캐릭터 상세 조회 실패";
      return;
    }}

    // Existing compare renderer is reused deliberately; B is the same object
    // and is hidden by single-mode CSS, so all visible A blocks stay identical
    // to the current compare site's data/UI implementation.
    render({{ok:true,a:d,b:d,proAnalysis:{{}}}});
    const h=d.dataHealth||{{}};
    $("loadStatus").textContent=
      `${{d.info?.name||NAME}} · ${{d.info?.server||SERVER}} · `+
      `마석 ${{h.stoneGroupCount||0}}종 · `+
      `스킬 ${{h.skillCount||0}} · `+
      `스티그마 ${{h.stigmaCount||0}} · `+
      `패시브 ${{h.passiveCount||0}}`;
  }} catch(e) {{
    console.error(e);
    $("loadStatus").textContent=`상세 조회 오류: ${{e?.message||e}}`;
  }}
}});
</script>
"""

    html = COMPARE_SITE_HTML.replace("</head>", single_css + "</head>")
    html = html.replace("</body>", single_js + "</body>")
    return html


@app.get("/detail", response_class=HTMLResponse)
async def single_character_site(name: str = "", server: str = ""):
    name = str(name or "").strip()
    server = str(server or "").strip()
    if not name or not server:
        return HTMLResponse(
            "<html><body><h2>캐릭터명과 서버가 필요합니다.</h2></body></html>",
            status_code=400,
        )
    return HTMLResponse(
        _single_character_site_html(name, server),
        media_type="text/html; charset=utf-8",
    )
















@app.get("/api/compare-health")
async def api_compare_health():
    return {
        "ok": True,
        "version": "v53-stable-compare-ui",
        "cors": True,
    }



@app.get("/api/aion2-research")
async def api_aion2_research():
    return {
        "ok": True,
        "version": "v53-stable-compare-ui",
        **AION2_RESEARCH_RULES,
    }


@app.post("/api/damage-index")
async def api_damage_index(request: Request):
    try:
        payload = await request.json()
        rows = payload.get("combinedOffense") or []
        result = damage_index_from_rows(
            rows,
            critical_rate=payload.get("criticalRate", 50),
            hard_hit_rate=payload.get("hardHitRate", 0),
            back_ratio=payload.get("backRatio", 0),
            front_ratio=payload.get("frontRatio", 0),
            boss_resistance=payload.get("bossResistance", 0),
            skill_coefficient=payload.get("skillCoefficient", 1),
        )
        return {
            "ok": True,
            "version": "v53-stable-compare-ui",
            **result,
        }
    except Exception as e:
        return {
            "ok": False,
            "version": "v53-stable-compare-ui",
            "error": f"{type(e).__name__}: {str(e)[:400]}",
        }


@app.get("/api/compare-character")
async def api_compare_character(nickname: str, server: str):
    try:
        # Compare is NC-official-only. Do not fall back to the legacy
        # detailed_character_data() resolver, which can return characterId not found.
        data = await compare_character_data_db_first(nickname, server)
        return {
            "version": "v76-priority-equipment-align",
            **data,
        }
    except Exception as e:
        return {
            "ok": False,
            "version": "v76-priority-equipment-align",
            "error": f"{type(e).__name__}: {str(e)[:400]}",
        }



async def official_compare_character_data(nickname: str, server_name: str):
    """Official NC-only compare loader. Normal character lookup is untouched."""
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip()

    # Compare must reflect the current official character state. Load live first;
    # only use the persistent snapshot when NC is temporarily unavailable.
    src = await debug_official_equipped_stats_v2(nickname=nickname, server=server_name)
    if not isinstance(src, dict) or not src.get("ok"):
        saved = await character_db_get_official_compare(
            nickname, server_name, max_age_seconds=None
        )
        if isinstance(saved, dict) and saved.get("ok"):
            saved = dict(saved)
            health = dict(saved.get("dataHealth") or {})
            health["cache"] = "persistent-stale-fallback"
            saved["dataHealth"] = health
            return saved
        return {"ok": False, "error": (src or {}).get("error") if isinstance(src, dict) else "official load failed"}

    buckets = src.get("aggregatedRawSums") or []
    idx = {}
    for b in buckets:
        if isinstance(b, dict):
            try:
                idx[(str(b.get("source") or ""), str(b.get("id") or ""))] = float(b.get("sum") or 0)
            except Exception:
                pass

    def sv(source, stat_id):
        return float(idx.get((source, stat_id), 0.0))

    def pct_stone(stat_id):
        return sv("magicStone", stat_id) / 100.0

    info_effects = {}
    for st in src.get("infoStatList") or []:
        if not isinstance(st, dict):
            continue
        for text in st.get("statSecondList") or []:
            m = re.search(r'^(.+?)\s*([+-])\s*([0-9.]+)%$', str(text).strip())
            if not m:
                continue
            nm, sign, num = m.groups()
            val = float(num) * (-1 if sign == '-' else 1)
            info_effects[nm.strip()] = info_effects.get(nm.strip(), 0.0) + val

    # Active title effects are returned separately under character/info.title.
    # They are not part of stat.statList or equipment/item, so omitting them
    # under-counts PVE amp, crit, extra attack, combat speed, etc.
    title_effects = {}
    _title_seen = set()
    _title_alias = {
        'PVE 피해 증폭':'PVE 피해 증폭', 'PVE피해 증폭':'PVE 피해 증폭',
        '보스 피해 증폭':'보스 피해 증폭', '피해 증폭':'피해 증폭',
        '무기 피해 증폭':'무기 피해 증폭', '치명타 피해 증폭':'치명타 피해 증폭',
        '후방 피해 증폭':'후방 피해 증폭', '전방 피해 증폭':'전방 피해 증폭',
        '추가 공격력':'추가 공격력', '공격력':'공격력', '치명타':'치명타',
        '추가 명중':'추가 명중', '명중':'명중', '강타':'강타', '관통':'관통',
        '전투 속도':'전투 속도', '공격 속도':'전투 속도', '시전 속도':'시전 속도',
    }
    _title_pat = re.compile(r'(PVE\s*피해\s*증폭|보스\s*피해\s*증폭|무기\s*피해\s*증폭|치명타\s*피해\s*증폭|후방\s*피해\s*증폭|전방\s*피해\s*증폭|피해\s*증폭|추가\s*공격력|추가\s*명중|전투\s*속도|공격\s*속도|시전\s*속도|치명타|강타|관통|명중|공격력)\s*([+-])\s*([0-9.]+)\s*(%)?')
    def _scan_title(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                _scan_title(v)
        elif isinstance(obj, list):
            for v in obj:
                _scan_title(v)
        elif isinstance(obj, str):
            text = obj.strip()
            if not text or text in _title_seen:
                return
            _title_seen.add(text)
            for m in _title_pat.finditer(text):
                raw_name, sign, num, pct = m.groups()
                norm = re.sub(r'\s+', ' ', raw_name).strip()
                # normalize compact spacing variants
                key = None
                for a, canon in _title_alias.items():
                    if re.sub(r'\s+','',a) == re.sub(r'\s+','',norm):
                        key = canon; break
                if not key:
                    continue
                val = float(num) * (-1 if sign == '-' else 1)
                # Keep flat and percent variants distinguishable.
                store_key = key + ('%' if pct else '')
                title_effects[store_key] = title_effects.get(store_key, 0.0) + val
    _scan_title(src.get('titleData'))

    def tv(name, percent=None):
        if percent is True:
            return float(title_effects.get(name+'%', 0.0))
        if percent is False:
            return float(title_effects.get(name, 0.0))
        return float(title_effects.get(name, 0.0)) + float(title_effects.get(name+'%', 0.0))

    attack_flat = sv('main.value','WeaponFixingDamage') + sv('main.extra','WeaponFixingDamage') + sv('sub','WeaponFixingDamage') + sv('magicStone','WeaponFixingDamage') + tv('추가 공격력', False)
    attack_inc = info_effects.get('공격력 증가', 0.0) + sv('sub','DamageRatio')
    accuracy_flat = sv('main.value','WeaponAccuracy') + sv('sub','WeaponAccuracy')
    accuracy_inc = info_effects.get('명중 증가', 0.0)
    additional_accuracy = sv('main.value','Accuracy') + sv('main.extra','Accuracy') + sv('magicStone','Accuracy') + tv('추가 명중', False)
    critical_flat = sv('main.value','Critical') + sv('main.extra','Critical') + sv('sub','Critical') + sv('magicStone','Critical') + tv('치명타', False)
    critical_inc = info_effects.get('치명타 증가', 0.0)

    final_attack = attack_flat * (1.0 + attack_inc / 100.0)
    final_accuracy = accuracy_flat * (1.0 + accuracy_inc / 100.0) + additional_accuracy
    final_critical = critical_flat * (1.0 + critical_inc / 100.0)

    weapon_amp = sv('main.value','AmplifyWeaponDamage') + sv('main.extra','AmplifyWeaponDamage') + sv('sub','AmplifyWeaponDamage') + pct_stone('AmplifyWeaponDamage') + tv('무기 피해 증폭', True)
    damage_amp = sv('main.value','AmplifyAllDamage') + sv('main.extra','AmplifyAllDamage') + sv('sub','AmplifyAllDamage') + pct_stone('AmplifyAllDamage') + tv('피해 증폭', True)
    pve_amp = sv('main.value','PvEAmplifyDamage') + sv('main.extra','PvEAmplifyDamage') + sv('sub','PvEAmplifyDamage') + pct_stone('PvEAmplifyDamage') + tv('PVE 피해 증폭', True)
    boss_amp = sv('main.value','AmplifyBossDamage') + sv('main.extra','AmplifyBossDamage') + sv('sub','AmplifyBossDamage') + pct_stone('AmplifyBossDamage') + tv('보스 피해 증폭', True)
    crit_dmg_amp = sv('main.value','AmplifyCriticalDamage') + sv('main.extra','AmplifyCriticalDamage') + sv('sub','AmplifyCriticalDamage') + pct_stone('AmplifyCriticalDamage') + tv('치명타 피해 증폭', True)
    back_amp = sv('main.value','AmplifyBackAttack') + sv('main.extra','AmplifyBackAttack') + sv('sub','AmplifyBackAttack') + pct_stone('AmplifyBackAttack') + tv('후방 피해 증폭', True)
    front_amp = sv('main.value','AmplifyFrontAttack') + sv('main.extra','AmplifyFrontAttack') + sv('sub','AmplifyFrontAttack') + pct_stone('AmplifyFrontAttack') + tv('전방 피해 증폭', True)
    hard_hit = info_effects.get('강타',0.0) + sv('main.value','HardHit') + sv('main.extra','HardHit') + sv('sub','HardHit') + sv('main.value','Hardhit') + sv('main.extra','Hardhit') + sv('sub','Hardhit') + tv('강타', True)
    combat_speed = info_effects.get('전투 속도',0.0) + sv('main.value','CombatSpeed') + sv('main.extra','CombatSpeed') + sv('sub','CombatSpeed') + tv('전투 속도', True)
    penetration = sv('main.value','DefensePierce') + sv('main.extra','DefensePierce')

    values = {
        '공격력': final_attack,
        '공격력 증가율': attack_inc,
        '피해 증폭': damage_amp,
        '무기 피해 증폭': weapon_amp,
        'PVE 피해 증폭': pve_amp,
        '보스 피해 증폭': boss_amp,
        '전방 피해 증폭': front_amp,
        '후방 피해 증폭': back_amp,
        '치명타 피해 증폭': crit_dmg_amp,
        '치명타': final_critical,
        '명중': final_accuracy,
        '강타': hard_hit,
        '관통': penetration,
        '공격 속도': combat_speed,
    }
    percent_names = {'공격력 증가율','피해 증폭','무기 피해 증폭','PVE 피해 증폭','보스 피해 증폭','전방 피해 증폭','후방 피해 증폭','치명타 피해 증폭','공격 속도','시전 속도'}
    combined = []
    for nm, val in values.items():
        # Do not fabricate absent optional damage categories as measured values.
        if nm in {'보스 피해 증폭','전방 피해 증폭'} and abs(float(val or 0)) < 1e-12:
            continue
        combined.append({
            'name': nm,
            'baseValue': round(float(val or 0), 4),
            'baseRaw': None,
            'unit': 'percent' if nm in percent_names else 'number',
            'stoneCount': 0,
            'stoneTotal': 0.0,
            'stoneAverage': 0.0,
            'combinedValue': round(float(val or 0), 4),
            'baseReconstructed': nm in {'공격력','명중','치명타'},
        })

    # Use the exact same official magic-stone aggregator as the character card.
    magic_stones = _official_magic_stone_totals(src)

    profile_data = src.get('profileData') if isinstance(src.get('profileData'), dict) else {}

    def _extract_title_name(profile_obj, title_obj):
        if isinstance(profile_obj, dict):
            for key in ('titleName','activeTitleName','equippedTitleName','representTitleName','usingTitleName'):
                val = profile_obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            for key in ('title','activeTitle','equippedTitle','representTitle','usingTitle'):
                val = profile_obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
                if isinstance(val, dict):
                    for nk in ('name','titleName','text','label'):
                        nv = val.get(nk)
                        if isinstance(nv, str) and nv.strip():
                            return nv.strip()

        found = []
        def walk(obj):
            if isinstance(obj, dict):
                keys = {str(k).lower() for k in obj.keys()}
                activeish = any(k in keys for k in {'active','equip','equipped','selected','use','using','represent','current'})
                if activeish:
                    flag = False
                    for fk in ('active','equip','equipped','selected','use','using','represent','current'):
                        fv = obj.get(fk)
                        if fv in (True, 1, 'Y', 'y', 'true', 'True'):
                            flag = True
                    if flag:
                        for nk in ('name','titleName','text','label'):
                            nv = obj.get(nk)
                            if isinstance(nv, str) and nv.strip():
                                found.append(nv.strip())
                for nk in ('name','titleName','text','label'):
                    if nk in obj and isinstance(obj.get(nk), str) and obj.get(nk).strip():
                        if activeish:
                            found.append(obj.get(nk).strip())
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)
        walk(title_obj)
        return found[0] if found else ''

    def _extract_equipped_titles(title_obj):
        out, seen = [], set()
        priority_tokens = ('equip','equipped','active','selected','select','using','use','represent','slot','apply')
        def add_name(v):
            if not isinstance(v, str):
                return
            v=v.strip()
            if not v or v in seen:
                return
            seen.add(v); out.append(v)
        def walk(obj, parent_key='', priority=False):
            key_l=str(parent_key or '').lower()
            priority = priority or any(t in key_l for t in priority_tokens)
            if isinstance(obj, dict):
                flag=any(obj.get(k) in (True,1,'1','Y','y','true','True') for k in ('equip','equipped','active','selected','using','use','represent','isEquip','isEquipped','isSelected'))
                local_priority=priority or flag
                if local_priority:
                    for nk in ('titleName','name','text','label'):
                        add_name(obj.get(nk))
                for k,v in obj.items():
                    walk(v,k,local_priority)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v,parent_key,priority)
        walk(title_obj)
        if len(out)<3:
            def fallback(obj):
                if len(out)>=3: return
                if isinstance(obj, dict):
                    for nk in ('titleName','name'):
                        add_name(obj.get(nk))
                    for v in obj.values(): fallback(v)
                elif isinstance(obj, list):
                    for v in obj: fallback(v)
            fallback(title_obj)
        return out[:3]

    item_level = 0
    for st in src.get('infoStatList') or []:
        if not isinstance(st, dict):
            continue
        if str(st.get('type') or '') == 'ItemLevel' or '아이템레벨' in str(st.get('name') or ''):
            try:
                item_level = float(st.get('value') or 0)
                break
            except Exception:
                pass

    def _slot_group_label(slot_name):
        s = str(slot_name or '')
        groups = {
            'MainHand': '주무기', 'SubHand': '보조', 'Torso': '상의', 'Pants': '하의', 'Helmet': '투구',
            'Shoulder': '어깨', 'Gloves': '장갑', 'Boots': '신발', 'Cape': '망토', 'Belt': '허리띠',
            'Necklace': '목걸이', 'Earring1': '귀걸이', 'Earring2': '귀걸이', 'Ring1': '반지', 'Ring2': '반지',
            'Bracelet1': '팔찌', 'Bracelet2': '팔찌', 'Brooch1': '브로치', 'Brooch2': '브로치',
            'Rune1': '룬', 'Rune2': '룬', 'Amulet': '아뮬렛', 'Pendant': '펜던트'
        }
        return groups.get(s, s or '장비')

    grouped_eq = {}
    for item in src.get('items') or []:
        if not isinstance(item, dict) or not item.get('ok'):
            continue
        group = _slot_group_label(item.get('slot'))
        g = grouped_eq.setdefault(group, [])
        g.append({
            'slot': group,
            'name': item.get('name') or '',
            'icon': item.get('icon') or '',
            'grade': item.get('grade') or '',
            'enchantLevel': int(item.get('enchantLevel') or 0),
            'exceedLevel': int(item.get('exceedLevel') or 0),
        })

    equipment_order = ['주무기','보조','상의','망토','목걸이','귀걸이','반지','팔찌','브로치','아뮬렛','펜던트','허리띠']
    equipment_summary = []
    for group in equipment_order:
        items = grouped_eq.get(group) or []
        if not items:
            continue
        first = items[0]
        names = {str(x.get('name') or '') for x in items if x.get('name')}
        display_name = first.get('name') or ''
        if len(items) >= 2 and len(names) == 1:
            display_name = f"{display_name} x{len(items)}"
        elif len(items) >= 2:
            display_name = ' / '.join(str(x.get('name') or '') for x in items[:2])
        equipment_summary.append({
            'slot': group,
            'name': display_name,
            'icon': first.get('icon') or '',
            'grade': first.get('grade') or '',
            'enchantLevel': int(first.get('enchantLevel') or 0),
            'exceedLevel': int(first.get('exceedLevel') or 0),
            'count': len(items),
        })

    equipment_detailed = []
    for item in src.get('items') or []:
        if not isinstance(item, dict) or not item.get('ok'):
            continue
        slot_raw = str(item.get('slot') or '')
        if slot_raw.startswith('Arcana'):
            continue
        equipment_detailed.append({
            'slot': _slot_group_label(slot_raw), 'slotRaw': slot_raw,
            'name': item.get('name') or '', 'icon': item.get('icon') or '', 'grade': item.get('grade') or '',
            'enchantLevel': int(item.get('enchantLevel') or 0), 'exceedLevel': int(item.get('exceedLevel') or 0),
            'mainStats': item.get('mainStats') if isinstance(item.get('mainStats'), list) else [],
            'subStats': item.get('subStats') if isinstance(item.get('subStats'), list) else [],
            'magicStoneStat': item.get('magicStoneStat') if isinstance(item.get('magicStoneStat'), list) else [],
            'godStoneStat': item.get('godStoneStat') if isinstance(item.get('godStoneStat'), list) else [],
            'skillOptions': item.get('skillOptions') if isinstance(item.get('skillOptions'), list) else [],
            'sources': item.get('sources') if isinstance(item.get('sources'), list) else [],
        })

    _detail_slot_order = {name: i for i, name in enumerate(['주무기','보조','상의','하의','투구','어깨','장갑','신발','망토','허리띠','목걸이','귀걸이','반지','팔찌','브로치','룬','아뮬렛','펜던트'])}
    equipment_detailed.sort(key=lambda x: (_detail_slot_order.get(str(x.get('slot') or ''), 999), str(x.get('slotRaw') or ''), str(x.get('name') or '')))

    titles = _extract_equipped_titles(src.get('titleData'))
    petwing = src.get('petwingData') if isinstance(src.get('petwingData'), dict) else {}
    wing = petwing.get('wing') if isinstance(petwing.get('wing'), dict) else {}

    db_rows = await character_db_get(nickname, server_name)
    db = db_rows[0] if db_rows else {}
    info = {
        'name': src.get('name') or nickname,
        'server': src.get('server') or server_name,
        'serverName': src.get('server') or server_name,
        'serverId': int(src.get('serverId') or SERVER_ID_MAP.get(server_name) or 0),
        'characterId': src.get('characterId') or db.get('characterId') or '',
        'job': db.get('job') or profile_data.get('className') or '',
        'className': db.get('job') or profile_data.get('className') or '',
        'combatPower': int(db.get('combatPower') or profile_data.get('combatPower') or 0),
        'level': int(db.get('level') or profile_data.get('characterLevel') or 0),
        'characterLevel': int(db.get('level') or profile_data.get('characterLevel') or 0),
        'profileImage': db.get('profileImage') or profile_data.get('profileImage') or '',
        'title': db.get('title') or profile_data.get('titleName') or _extract_title_name(profile_data, src.get('titleData')) or (titles[0] if titles else ''),
        'titles': titles,
        'itemLevel': item_level,
        'wingName': wing.get('name') or '',
        'wingIcon': wing.get('icon') or '',
        'wingEnchantLevel': wing.get('enchantLevel'),
    }

    active = []
    stigma = []
    passives = []
    for sk in src.get('skills') or []:
        if not isinstance(sk, dict) or not sk.get('acquired'):
            continue
        row = {'code': sk.get('id'), 'name': sk.get('name'), 'level': sk.get('level'), 'category': str(sk.get('category') or '').lower(), 'equip': sk.get('equip'), 'icon': sk.get('icon') or ''}
        if row['category'] == 'passive':
            if int(row.get('level') or 0) > 0:
                passives.append(row)
        elif row['category'] == 'active':
            if int(row.get('level') or 0) > 0:
                active.append(row)
        elif row['category'] == 'dp':
            if int(row.get('level') or 0) > 0:
                stigma.append(row)

    option_feedback = build_character_option_feedback({'combinedOffense': combined, 'info': info})
    result = {
        'ok': True,
        'profileAvailable': True,
        'source': ['plaync-character-info','plaync-character-equipment','plaync-character-equipment-item'],
        'info': info,
        'equipment': equipment_summary,
        'equipmentDetailed': equipment_detailed,
        'magicStoneTotals': magic_stones,
        'stats': [],
        'combinedOffense': combined,
        'optionFeedback': option_feedback,
        'titleEffects': title_effects,
        'arcana': [],
        'skills': active,
        'stigma': stigma,
        'passives': passives,
        'rankings': [],
        'dataHealth': {
            'profileAvailable': True,
            'equipmentCount': int(src.get('equipmentCount') or 0),
            'detailSuccessCount': int(src.get('detailSuccessCount') or 0),
            'stoneGroupCount': len(magic_stones),
            'statCount': len(combined),
            'arcanaCount': 0,
            'skillCount': len(active),
            'stigmaCount': len(stigma),
            'passiveCount': len(passives),
            'offenseNames': [r.get('name') for r in combined],
            'source': 'official-nc-only-title-aware',
        },
    }
    try:
        await character_db_save_official_compare(nickname, server_name, result)
    except Exception:
        pass
    return result

async def compare_character_data_db_first(nickname: str, server_name: str):
    """Compare-only loader.

    Important: normal character lookup is intentionally untouched.
    Compare reads the saved full profile directly from our DB first and does
    not call own_resolve_character(), which used to trigger another external
    refresh and made /compare slow/unreliable even when character lookup worked.
    """
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip()
    if not nickname or server_name not in SERVER_ID_MAP:
        return {"ok": False, "error": "캐릭터명 또는 서버 확인 필요"}

    # v70: compare production source is NC official only. One compare request now
    # resolves identity, equipment, 33 item details, stones and official skill levels internally.
    official = await official_compare_character_data(nickname, server_name)
    if isinstance(official, dict) and official.get("ok"):
        return official

    # If NC is temporarily rate-limiting us, serve the most recent successful
    # official snapshot regardless of age instead of breaking the compare page.
    stale = await character_db_get_official_compare(nickname, server_name, max_age_seconds=None)
    if isinstance(stale, dict) and stale.get("ok"):
        stale = dict(stale)
        health = dict(stale.get("dataHealth") or {})
        health["cache"] = "persistent-stale-fallback"
        stale["dataHealth"] = health
        return stale
    return {"ok": False, "error": (official or {}).get("error") or f"{nickname}[{server_name}] NC 공식 데이터 조회 실패"}

    db_row, full_profile = await character_db_get_full_profile(nickname, server_name)

    # If the user has already looked the character up in v61, this should be
    # available immediately. For a first-ever compare only, allow one bounded
    # fill attempt, then return a clean JSON error instead of hanging the page.
    row = None
    if db_row:
        row = {
            "name": db_row.get("name") or nickname,
            "serverName": db_row.get("server_name") or server_name,
            "serverId": int(db_row.get("server_id") or SERVER_ID_MAP[server_name]),
            "characterId": db_row.get("character_id") or "",
            "className": db_row.get("job") or "",
            "combatPower": int(db_row.get("combat_power") or 0),
            "characterLevel": int(db_row.get("level") or 0),
            "profileImage": db_row.get("profile_image") or "",
        }

    if not full_profile:
        try:
            fetched_row, fetched_profile = await asyncio.wait_for(
                _full_profile_for_exact_character(nickname, server_name),
                timeout=8.0,
            )
            if fetched_row is not None:
                row = fetched_row
            if fetched_profile:
                full_profile = fetched_profile
        except Exception:
            pass

    # Basic identity may still exist in the DB even if no detailed JSON has
    # been saved yet. Return a structured result so the browser never crashes.
    if not full_profile:
        infos = await character_db_get(nickname, server_name)
        basic = infos[0] if infos else {}
        if not basic and not row:
            return {"ok": False, "error": f"{nickname}[{server_name}] 저장 데이터 없음"}
        return {
            "ok": False,
            "error": f"{nickname}[{server_name}] 상세 비교 데이터가 아직 저장되지 않음 · 캐릭터 조회를 한 번 실행 후 다시 비교",
            "info": basic or profile_info({}, nickname, server_name, row or {}),
            "equipment": [], "magicStoneTotals": [], "stats": [],
            "combinedOffense": [], "optionFeedback": {}, "arcana": [],
            "skills": [], "passives": [], "rankings": [],
            "dataHealth": {"profileAvailable": False, "equipmentCount": 0, "stoneGroupCount": 0,
                           "statCount": 0, "arcanaCount": 0, "skillCount": 0, "passiveCount": 0,
                           "offenseNames": []},
        }

    # v68: the real /compare path must use the same broad stat/stone parser.
    # v67 fixed detailed_character_data(), but this compare-only loader still
    # used the old equipment-only stone path, so the browser never saw the fix.
    def parse_compare_profile(profile_obj):
        eq = _equipment_rows(profile_obj) if profile_obj else []
        st = _stone_totals_from_profile(profile_obj) if profile_obj else []
        if not st:
            st = _stone_totals_from_equipment(eq)
        explicit = extract_profile_stats(profile_obj) if profile_obj else []
        reconstructed = extract_visible_base_stats(profile_obj) if profile_obj else []
        ss = merge_explicit_and_reconstructed_stats(explicit, reconstructed)
        return eq, st, ss

    equipment, stones, stats = parse_compare_profile(full_profile)

    # Old DB snapshots can contain skills/passives while missing the final-stat
    # panel.  Do one DIRECT refresh here (bypassing the DB-first helper, which
    # would simply hand the same stale snapshot back) when important offensive
    # data is absent.  Normal character lookup remains untouched.
    current_names = {str(x.get("name") or "") for x in stats}
    important = {
        "무기 피해 증폭", "보스 피해 증폭", "PVE 피해 증폭",
        "전방 피해 증폭", "후방 피해 증폭", "치명타 피해 증폭",
    }
    need_refresh = not current_names.intersection(important)

    if need_refresh and row and row_character_id(row):
        try:
            sid = row_server_id(row) or int(SERVER_ID_MAP[server_name])
            cid = row_character_id(row)
            fresh_profile = await asyncio.wait_for(
                get_profile(sid, cid, fast=False),
                timeout=7.0,
            )
            if isinstance(fresh_profile, dict) and fresh_profile:
                fresh_eq, fresh_stones, fresh_stats = parse_compare_profile(fresh_profile)

                # Prefer a fresh response only when it actually improves the
                # compare dataset.  Never replace a richer saved snapshot with
                # a poorer/throttled response.
                old_score = len(stats) * 20 + len(stones) * 5
                new_score = len(fresh_stats) * 20 + len(fresh_stones) * 5
                if new_score > old_score:
                    full_profile = fresh_profile
                    equipment, stones, stats = fresh_eq, fresh_stones, fresh_stats
                    await character_db_save_full_profile(
                        nickname, server_name, fresh_profile, cid
                    )
        except Exception:
            pass

    full_info = profile_info(full_profile, nickname, server_name, row or {})
    combined_offense = build_combined_offense(stats, stones)
    option_feedback = build_character_option_feedback({
        "combinedOffense": combined_offense,
        "info": full_info,
    })
    arcana = extract_arcana(full_profile)
    skills = _dedupe_and_filter_level_rows(extract_skills(full_profile), min_level=16)
    passives = _dedupe_and_filter_level_rows(extract_passives(full_profile), min_level=16)

    rankings = []
    try:
        ranking_cache = await asyncio.wait_for(fetch_ranking_cache(), timeout=3.0)
        rankings = find_character_rankings(ranking_cache, full_info)[:8]
    except Exception:
        rankings = []

    return {
        "ok": True,
        "profileAvailable": True,
        "info": full_info,
        "equipment": equipment,
        "magicStoneTotals": stones,
        "stats": stats,
        "combinedOffense": combined_offense,
        "optionFeedback": option_feedback,
        "arcana": arcana,
        "skills": skills,
        "passives": passives,
        "rankings": rankings,
        "dataHealth": {
            "profileAvailable": True,
            "equipmentCount": len(equipment),
            "stoneGroupCount": len(stones),
            "statCount": len(stats),
            "arcanaCount": len(arcana),
            "skillCount": len(skills),
            "passiveCount": len(passives),
            "offenseNames": [r.get("name") for r in combined_offense],
        },
    }


@app.get("/api/compare")
async def api_compare(
    name_a: str,
    server_a: str,
    name_b: str,
    server_b: str,
):
    try:
        # DB-first compare. No normal character-lookup path is touched here.
        a, b = await asyncio.gather(
            compare_character_data_db_first(name_a, server_a),
            compare_character_data_db_first(name_b, server_b),
        )

        if not a.get("ok") or not b.get("ok"):
            messages = [x.get("error") for x in (a, b) if not x.get("ok") and x.get("error")]
            return JSONResponse(
                {
                    "ok": False,
                    "version": "v75-ui-requested-final",
                    "message": " / ".join(messages) or "비교 데이터 준비 실패",
                    "a": a,
                    "b": b,
                    "proAnalysis": {},
                },
                status_code=200,
            )

        try:
            pro_analysis = build_pro_analysis(a, b)
        except Exception as e:
            pro_analysis = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

        return {
            "ok": True,
            "version": "v75-ui-requested-final",
            "a": a,
            "b": b,
            "proAnalysis": pro_analysis,
        }
    except Exception as e:
        # Always JSON: prevents fetch().json() from failing on an HTML 500 page.
        return JSONResponse(
            {
                "ok": False,
                "version": "v70-base-stats-reconstructed",
                "error": f"{type(e).__name__}: {str(e)[:400]}",
            },
            status_code=200,
        )


# =========================================================
# NotMeter ranking (server-side; phone never downloads gzip)
# =========================================================
RANKING_URLS = [
    # Current NotMeter-Update published ranking cache
    "https://raw.githubusercontent.com/Not4You-Dev/NotMeter-Update/main/ranking/notmeter-ranking.json",

    # Legacy fallbacks
    "https://notmeter.com/data/notmeter-ranking.json.gz",
    "https://raw.githubusercontent.com/Not4You-Dev/NotMeter-Update/main/docs/data/notmeter-ranking.json.gz",
]
RANKING_CACHE_TTL = 300

async def fetch_ranking_cache():
    cached = cache_get("notmeter-ranking-cache", RANKING_CACHE_TTL)
    if cached is not None:
        return cached
    client = await get_http_client()
    last_error = None
    for url in RANKING_URLS:
        try:
            res = await client.get(
                url,
                headers={**HEADERS, "Accept-Encoding": "identity"},
                timeout=httpx.Timeout(connect=3.0, read=30.0, write=3.0, pool=2.0),
            )
            res.raise_for_status()
            raw = res.content
            if len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B:
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            cache_set("notmeter-ranking-cache", data)
            return data
        except Exception as e:
            last_error = e
    raise last_error or RuntimeError("랭킹 데이터 다운로드 실패")

def _ranking_cp_tier_label(cache, index):
    for item in (cache.get("cpTiers") or []):
        try:
            if int(item.get("index") or 0) == int(index or 0):
                return str(item.get("label") or "")
        except Exception:
            pass
    return ""

def _clean_ranking_boss(value):
    text = str(value or "").strip()
    if not text:
        return "—"
    text = re.sub(r"^\s*\d+\s*(?:네임드|보스)\s*(?:[·:：-]\s*)?", "", text, flags=re.I).strip()
    return text or "—"

def find_character_rankings(cache, info):
    name = str(info.get("name") or "").strip().casefold()
    job = str(info.get("job") or "").strip()
    server_id = int(info.get("serverId") or 0)
    if not name or not job:
        return []

    dungeon_map = {}
    for order, d in enumerate(cache.get("dungeons") or []):
        if isinstance(d, dict) and d.get("key") is not None:
            dungeon_map[str(d.get("key"))] = (d, order)

    metadata_map = {}
    for meta in (cache.get("views") or []):
        if not isinstance(meta, dict):
            continue
        key = f"{meta.get('dungeonKey')}|{int(meta.get('bossIndex') or 0)}|{int(meta.get('cpTierIndex') or 0)}|{meta.get('period')}"
        metadata_map[key] = meta

    results = []
    class_rankings = cache.get("classRankings") or {}
    if not isinstance(class_rankings, dict):
        return []

    for dungeon_key, ranking in class_rankings.items():
        if not isinstance(ranking, dict):
            continue
        for view in (ranking.get("views") or []):
            if not isinstance(view, dict):
                continue
            if str(view.get("period")) != "All":
                continue
            cp_tier_index = int(view.get("cpTierIndex") or 0)
            boss_index = int(view.get("bossIndex") or 0)
            if cp_tier_index <= 0 or boss_index != 0:
                continue

            group = None
            for g in (view.get("rows") or []):
                if isinstance(g, dict) and str(g.get("jobName") or "").strip() == job:
                    group = g
                    break
            if not group:
                continue

            player = None
            for candidate in (group.get("players") or []):
                if not isinstance(candidate, dict):
                    continue
                cname = str(candidate.get("name") or "").strip()
                if not cname or "*" in cname or cname.casefold() != name:
                    continue
                csid = int(candidate.get("serverId") or 0)
                if server_id and csid and csid != server_id:
                    continue
                player = candidate
                break
            if not player:
                continue

            rank = int(player.get("rank") or 0)
            if rank < 1 or rank > 20:
                continue

            meta_key = f"{dungeon_key}|{boss_index}|{cp_tier_index}|{view.get('period')}"
            metadata = metadata_map.get(meta_key) or {}
            dungeon_data, dungeon_order = dungeon_map.get(str(dungeon_key), ({}, 9999))
            recorded_index = int(player.get("B") if player.get("B") is not None else (player.get("bossIndex") or 0))
            boss_names = dungeon_data.get("bossNames") or []
            if recorded_index > 0 and len(boss_names) >= recorded_index:
                recorded_name = boss_names[recorded_index - 1]
            else:
                recorded_name = player.get("bossName") or ""
            cp_label = str(metadata.get("cpTierLabel") or "") or _ranking_cp_tier_label(cache, cp_tier_index)
            results.append({
                "rank": rank,
                "dps": int(player.get("dps") or 0),
                "dungeonKey": str(dungeon_key),
                "dungeonName": str(metadata.get("dungeonName") or dungeon_data.get("displayName") or dungeon_key),
                "bossName": _clean_ranking_boss(recorded_name or metadata.get("bossName") or "—"),
                "cpTierLabel": cp_label,
                "dungeonOrder": dungeon_order,
            })

    best = {}
    for row in results:
        old = best.get(row["dungeonKey"])
        if old is None or row["dps"] > old["dps"] or (row["dps"] == old["dps"] and row["rank"] < old["rank"]):
            best[row["dungeonKey"]] = row
    final = list(best.values())
    final.sort(key=lambda x: (x["dungeonOrder"], -x["dps"]))
    return final

async def ranking_lookup_smart(body: str):
    body = str(body or "").strip()
    if not body:
        return "사용법\n!랭킹 윤이지켈\n!랭킹 지켈윤이"

    nickname, explicit_server = parse_character_query(body)
    server_name = explicit_server

    if not server_name:
        parsed = split_server_and_nickname(body)
        if parsed:
            nickname, server_name = parsed
        else:
            nickname = body

    resolved = await own_resolve_character(
        nickname,
        server_name,
    )

    if resolved["type"] == "none":
        if server_name:
            return f"⚠️ {server_name} 서버에서 '{nickname}' 캐릭터를 찾지 못했습니다."
        return f"🔎 '{nickname}' 캐릭터를 찾지 못했습니다."

    if resolved["type"] == "multiple":
        lines = [
            f"⚠️ '{nickname}' 캐릭터가 여러 서버에 있습니다.",
            "",
            "서버명을 붙여주세요.",
        ]
        for item in resolved["items"][:10]:
            info = item["info"]
            cp = (
                round(int(info.get("combatPower") or 0) / 1000)
                if info.get("combatPower")
                else "-"
            )
            lines.append(
                f"• {info.get('server') or '-'} · "
                f"{info.get('job') or '-'} · {cp}"
            )
        return "\n".join(lines)

    info = resolved["info"]

    # Ranking dataset remains a NotMeter feature.
    # Character identity is resolved from our DB first, then refreshed via NotMeter.
    cache = await fetch_ranking_cache()
    rows = find_character_rankings(cache, info)

    header = f"🏆 {info.get('name')} · {info.get('server')}"

    if not rows:
        return (
            header +
            "\n\nNotMeter 공개 TOP20 기록이 없습니다."
        )

    lines = [header]

    for row in rows[:12]:
        lines += [
            "",
            f"▶ {row['dungeonName']}",
            f"#{row['rank']} · DPS {row['dps']:,}",
        ]

        if row.get("cpTierLabel"):
            lines.append(
                f"CP 구간 : {row['cpTierLabel']}"
            )

        if row.get("bossName") and row.get("bossName") != "—":
            lines.append(
                f"보스 : {row['bossName']}"
            )

    return "\n".join(lines)


# =========================================================
# Field Boss
# =========================================================

# region index must match NotMeterFieldBossCatalog order.
FIELD_BOSS_REGIONS = [
    {
        "key": "verteron",
        "name": "베르테론",
        "bosses": [
            (2100040, "썩은 쿠타르"), (2100076, "광투사 쿠산"),
            (2100003, "동쪽의 네이켈"), (2100050, "서쪽의 케르논"),
            (2100077, "제사장 가르심"), (2100079, "호위병 티간트"),
            (2100141, "만개한 코린"), (2100177, "분노한 사루스"),
            (2100178, "피송곳니 프닌"), (2100582, "배교자 레일라"),
            (2100617, "검은 촉수 라와"), (2100661, "환몽의 카시아"),
            (2100708, "백부장 데미로스"), (2100718, "신성한 안사스"),
            (2100876, "수확관리자 모샤브"), (2100877, "감시병기 크나쉬"),
            (2100988, "학자 라울라"), (2100989, "숲전사 우라무"),
            (2100991, "추격자 타울로"), (2101016, "연구관 세트람"),
            (2101074, "영원의 가르투아"), (2101120, "침묵의 타르탄"),
            (2101122, "영혼 지배자 카샤파"), (2101131, "군단장 라그타"),
        ],
    },
    {
        "key": "altgard",
        "name": "알트가르드",
        "bosses": [
            (2400017, "녹아내린 다나르"), (2400074, "검은 전사 아에드"),
            (2400140, "충실한 라지트"), (2400141, "광전사 발그"),
            (2400212, "포식자 가르산"), (2400223, "혈전사 란나르"),
            (2400274, "기만자 트리드"), (2400335, "푸른물결 켈피나"),
            (2400353, "총감독관 누타"), (2400358, "참모관 르사나"),
            (2400419, "별동대장 링크스"), (2400424, "모독자 노블루드"),
            (2400425, "망혼의 아칸 악시오스"), (2400474, "중독된 하디룬"),
            (2400504, "처형자 바르시엔"), (2400593, "드라칸 부대병기 구루타"),
            (2400607, "백전노장 슈자칸"), (2400608, "비전의 카루카"),
            (2400659, "흑암의 비슈베다"), (2400709, "예리한 쉬라크"),
            (2400800, "불멸의 가르투아"), (2400853, "군단장 라그타"),
            (2400854, "영혼 지배자 카샤파"), (2400855, "침묵의 타르탄"),
        ],
    },
    {
        "key": "eltnen",
        "name": "엘테넨",
        "bosses": [
            (2101217, "응집된 베레놈"), (2101218, "옛 두목 비고르"),
            (2101257, "꺾인 날개 츠바인"), (2101278, "탐욕의 이게티스"),
            (2101279, "생명의 신수 수페르비아"), (2101306, "썩은 뿌리 멜트림"),
            (2101349, "맹목적인 니호그"), (2101350, "최초의 실험체 크티마"),
            (2101415, "세 개의 뿔 마이노"), (2101416, "고통의 람푸스"),
            (2101600, "3부대장 카르코티"), (2101601, "부군단장 비바츠라"),
        ],
    },
    {
        "key": "morheim",
        "name": "모르헤임",
        "bosses": [
            (2406034, "경계의 방랑자 파르곤"), (2406035, "포식의 거수 발라크"),
            (2406071, "핏빛 눈보라 레눌프"), (2406093, "서리갑옷 하르칸"),
            (2406094, "푸른 눈물 글레이시아"), (2406129, "업화의 날개 피오스"),
            (2406131, "용암심장 바투"), (2406132, "정예 심문관 브란트"),
            (2406181, "미쳐버린 파수꾼 불라간"), (2406182, "화산 군주 그림니르"),
            (2406990, "3부대장 미나사라"), (2406991, "부군단장 사르바카"),
        ],
    },
    {
        "key": "abyss-lower",
        "name": "어비스 하층",
        "bosses": [
            (2600068, "정령왕 아그로"), (2600089, "감시자 카이라"),
            (2600084, "수호신장 나흐마"), (2600093, "수호신장 나흐마"),
            (2600094, "수호신장 나흐마"), (2600096, "집행자 타마사"),
            (2600097, "집행자 아그로"), (2600098, "집행자 카이라"),
        ],
    },
    {
        "key": "abyss-middle",
        "name": "어비스 중층",
        "bosses": [
            (2600150, "분노한 수호신장 나흐마"), (2600156, "분노한 수호신장 나흐마"),
            (2600520, "처형관 드라모스"), (2600521, "반역자 듀칼"),
            (2600522, "파멸자 마라카"),
        ],
    },
]

BOSS_BY_CODE = {}
for region_index, region in enumerate(FIELD_BOSS_REGIONS):
    for code, name in region["bosses"]:
        BOSS_BY_CODE[int(code)] = {
            "name": name,
            "region": region["name"],
            "regionIndex": region_index,
        }

async def fetch_field_boss_cache():
    cached = cache_get("field-boss-cache", FIELD_BOSS_CACHE_TTL)
    if cached:
        return cached

    errors = []
    for url in FIELD_BOSS_URLS:
        try:
            data = await http_json(url, params={"v": int(time.time() * 1000)})

            if (
                data.get("schema") != "notmeter-field-boss-public-cache-v1"
                or int(data.get("version") or 0) != 1
                or not isinstance(data.get("servers"), list)
            ):
                raise ValueError("invalid field-boss cache")

            cache_set("field-boss-cache", data)
            return data
        except Exception as e:
            errors.append(type(e).__name__)

    raise RuntimeError("field boss cache unavailable: " + ",".join(errors))

def zikel_boss_entries(cache):
    server = next(
        (
            row for row in cache.get("servers", [])
            if int(row.get("serverId") or 0) == SERVER_ID
        ),
        None,
    )
    if not server:
        return []

    rows = []
    for region in server.get("regions") or []:
        region_index = int(region.get("region") or 0)
        fallback_region_name = (
            FIELD_BOSS_REGIONS[region_index]["name"]
            if 0 <= region_index < len(FIELD_BOSS_REGIONS)
            else f"지역 {region_index}"
        )

        for entry in region.get("entries") or []:
            code = int(entry.get("bossCode") or 0)
            target_at = int(entry.get("targetAt") or 0)
            if not code or not target_at:
                continue

            info = BOSS_BY_CODE.get(code) or {
                "name": f"보스 {code}",
                "region": fallback_region_name,
            }

            rows.append({
                "bossCode": code,
                "name": info["name"],
                "region": info["region"],
                "targetAt": target_at,
            })

    rows.sort(key=lambda x: x["targetAt"])
    return rows

def boss_time_parts(target_at):
    target = datetime.fromtimestamp(target_at / 1000, tz=KST)
    now = datetime.now(KST)
    seconds = int((target - now).total_seconds())
    clock = target.strftime("%H:%M")

    if seconds <= 0:
        ago = abs(seconds)
        if ago < 60:
            status = "시간 도달"
        elif ago < 3600:
            status = f"{ago // 60}분 지남"
        else:
            status = f"{ago // 3600}시간 {(ago % 3600) // 60}분 지남"
        return clock, status

    if seconds < 60:
        status = f"{seconds}초 남음"
    elif seconds < 3600:
        status = f"{seconds // 60}분 남음"
    else:
        status = f"{seconds // 3600}시간 {(seconds % 3600) // 60}분 남음"

    return clock, status

def boss_time_text(target_at):
    clock, status = boss_time_parts(target_at)
    return f"{clock} · {status}"


def format_all_field_bosses(cache):
    rows = zikel_boss_entries(cache)
    if not rows:
        return "🐲 필드보스\n\n출현 시간 정보가 없습니다."

    grouped = {}
    for row in rows:
        grouped.setdefault(row["name"], []).append(row)

    lines = ["🐲 필드보스", ""]

    for name, items in grouped.items():
        lines.append(name)
        for row in items:
            lines.append(f"⏰ {boss_time_text(row['targetAt'])}")
        lines.append("")

        if len("\n".join(lines)) > 900:
            break

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def normalize_boss_query(query):
    return re.sub(r"\s+", "", str(query or "")).casefold()

def format_one_boss(cache, query):
    rows = zikel_boss_entries(cache)
    q = normalize_boss_query(query)

    aliases = {
        "아그로": ("아그로", "정령왕 아그로"),
        "나흐마": ("나흐마",),
    }

    names = aliases.get(q, (query,))
    normalized_names = [normalize_boss_query(x) for x in names]

    matches = []
    for row in rows:
        row_name = normalize_boss_query(row["name"])
        if any(name in row_name for name in normalized_names):
            matches.append(row)

    if not matches:
        return f"🐲 {query}\n\n출현 시간을 찾지 못했습니다."

    display_name = matches[0]["name"]
    lines = [f"🐲 {display_name}", ""]

    seen = set()
    for row in matches:
        target = datetime.fromtimestamp(row["targetAt"] / 1000, tz=KST)
        clock = target.strftime("%H:%M")
        if clock in seen:
            continue
        seen.add(clock)
        lines.append(f"⏰ {clock}")

    return "\n".join(lines)



# =========================================================
# AUTO boss schedule engine
# - Core boss commands do NOT depend on NotMeter boss cache.
# - Every 5 minutes the server re-reads recent official Notice/Update rows.
# - If a recognizable schedule change is found, that rule immediately becomes
#   the source for !필보 / individual boss lookup / 30-minute alerts.
# - If wording is ambiguous, the last confirmed/default rule is preserved.
# =========================================================


# =========================================================
# v48 FIELD-BOSS / CONTENT TIMING POLICY
# =========================================================
#
# Fixed schedules (maintenance does NOT move these):
# - Kaira: 01:00 / 05:00 / 09:00 / 13:00 / 17:00 / 21:00
# - Nahma: Fri / Sun 22:00
#
# Maintenance-based schedules (move when a genuinely NEW maintenance notice is confirmed):
# - Agro / Abyss / Sigong / Gyunyeol / Ati / Field Boss
# - Manual corrections are saved immediately.
# - A later NEW maintenance notice shifts the maintenance-based schedules by the
#   maintenance completion-time change. Old maintenance notices are never reused
#   to roll schedules backward.
# - Agro itself uses the new maintenance END time as its anchor.
#
# Abyss rift/event schedules are separate and must not overwrite the boss schedule.

AGRO_RESPAWN_HOURS = 12
KAIRA_FIXED_HOURS = (1, 5, 9, 13, 17, 21)

def next_agro_from_latest_maintenance(latest_maintenance_end, now_kst):
    if latest_maintenance_end is None:
        return None

    anchor = latest_maintenance_end
    # Move forward in exact 12h increments until future.
    nxt = anchor
    while nxt <= now_kst:
        nxt += timedelta(hours=AGRO_RESPAWN_HOURS)
    return nxt


DEFAULT_BOSS_RULES = {
    "kairaHours": [1, 5, 9, 13, 17, 21],
    "nahmaWeekdays": [4, 6],     # Fri / Sun
    "nahmaHour": 22,
    "nahmaMinute": 0,
    "abyssWeekdays": [2, 5],     # Wed / Sat
    "abyssHour": 22,
    "abyssMinute": 30,
    "agroIntervalHours": 12,
    # Weekly timetable additions from the supplied table.
    "sigongWeekdays": [0, 3, 5],       # Mon / Thu / Sat
    "sigongTimes": [(20, 0), (23, 0)],
    "gyunyeolWeekdays": [1, 3],        # Tue / Thu
    "gyunyeolTimes": [(22, 0)],
    "atiWeekdays": [2, 5],             # Wed / Sat
    "atiTimes": [(22, 0)],
    "fieldBossWeekdays": [2, 5],       # Wed / Sat
    "fieldBossTimes": [(22, 30)],
}

BOSS_RULES = dict(DEFAULT_BOSS_RULES)
BOSS_RULES_META = {
    "updatedAt": None,
    "sources": {},
}

AGRO_FALLBACK_ANCHOR = datetime(2026, 9, 2, 6, 0, tzinfo=KST)

_boss_rule_refresh = {
    "ts": 0.0,
    "lock": asyncio.Lock(),
}

_maintenance_anchor_cache = {
    "value": None,
    "ts": 0.0,
}

# Only Kaira and Nahma are fixed.  These schedules follow maintenance.
MAINTENANCE_DYNAMIC_KEYS = ("abyss", "sigong", "gyunyeol", "ati", "fieldboss")

_WEEKDAY_KO = {
    "월": 0, "월요일": 0,
    "화": 1, "화요일": 1,
    "수": 2, "수요일": 2,
    "목": 3, "목요일": 3,
    "금": 4, "금요일": 4,
    "토": 5, "토요일": 5,
    "일": 6, "일요일": 6,
}

def _next_daily_hours(hours, now=None):
    now = now or datetime.now(KST)
    hours = sorted(set(int(x) for x in hours))
    for hour in hours:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target > now:
            return target
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(
        hour=int(hours[0]),
        minute=0,
        second=0,
        microsecond=0,
    )

def _next_weekly(weekdays, hour, minute=0, now=None):
    now = now or datetime.now(KST)
    best = None
    weekdays = tuple(int(x) for x in weekdays)

    for add_days in range(0, 8):
        day = now + timedelta(days=add_days)
        if day.weekday() not in weekdays:
            continue

        target = day.replace(
            hour=int(hour),
            minute=int(minute),
            second=0,
            microsecond=0,
        )
        if target <= now:
            continue
        if best is None or target < best:
            best = target

    if best is not None:
        return best

    return (now + timedelta(days=7)).replace(
        hour=int(hour),
        minute=int(minute),
        second=0,
        microsecond=0,
    )

def _extract_clock_values(text_value):
    """
    Return unique clock times as (hour, minute).
    Supports 01:00 / 1시 / 1시 30분.
    """
    source = str(text_value or "")
    found = []

    for h, m in re.findall(r"(?<!\d)([01]?\d|2[0-3])\s*:\s*([0-5]\d)", source):
        item = (int(h), int(m))
        if item not in found:
            found.append(item)

    for h, m in re.findall(r"(?<!\d)([01]?\d|2[0-3])\s*시(?:\s*([0-5]?\d)\s*분)?", source):
        item = (int(h), int(m or 0))
        if item not in found:
            found.append(item)

    return found

def _extract_weekdays(text_value):
    source = str(text_value or "")
    result = []

    # Prefer explicit "...요일" tokens.
    for token in re.findall(r"(월요일|화요일|수요일|목요일|금요일|토요일|일요일)", source):
        value = _WEEKDAY_KO.get(token)
        if value is not None and value not in result:
            result.append(value)

    # Also understand compact forms such as 수/토, 금·일.
    if not result:
        for token in re.findall(r"(?<![가-힣])(월|화|수|목|금|토|일)(?![가-힣])", source):
            value = _WEEKDAY_KO.get(token)
            if value is not None and value not in result:
                result.append(value)

    return result


def _flatten_text_values(node, out=None, depth=0):
    if out is None:
        out = []
    if depth > 12:
        return out

    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                # Keep meaningful text-ish fields and any strings containing time/date.
                key_l = str(key).lower()
                if (
                    any(t in key_l for t in ("content", "body", "text", "description", "title", "html"))
                    or re.search(r"\d{1,2}\s*/\s*\d{1,2}", value)
                    or re.search(r"\d{1,2}\s*:\s*\d{2}", value)
                ):
                    out.append(value)
            elif isinstance(value, (dict, list)):
                _flatten_text_values(value, out, depth + 1)

    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                _flatten_text_values(value, out, depth + 1)
            elif isinstance(value, str):
                out.append(value)

    return out


async def fetch_notice_detail_text(row):
    """
    Fetch the actual notice body for maintenance-time parsing.

    The list API often contains only metadata, so a new/edited temporary
    maintenance notice can be newer than the last list row that happens to
    contain a complete time range. We therefore try several public detail
    shapes and the public board page itself.
    """
    row = row or {}
    content_id = str(row.get("id") or "").strip()
    if not content_id:
        return str(row.get("rawText") or "")

    client = await get_http_client()
    alias = BOARD_CONFIGS["공지"]["alias"]

    candidates = [
        (
            f"{COMMUNITY_API}/{alias}/article/{content_id}",
            None,
        ),
        (
            f"{COMMUNITY_API}/{alias}/article/view",
            {"articleId": content_id},
        ),
        (
            f"{COMMUNITY_API}/{alias}/article/view",
            {"contentId": content_id},
        ),
        (
            f"{COMMUNITY_API}/{alias}/article/detail",
            {"articleId": content_id},
        ),
        (
            f"{COMMUNITY_API}/{alias}/article/detail",
            {"contentId": content_id},
        ),
    ]

    pieces = [
        str(row.get("title") or ""),
        str(row.get("rawText") or ""),
    ]

    for url, params in candidates:
        try:
            response = await client.get(
                url,
                params=params,
                headers=PLAYNC_HEADERS,
                timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=2.0),
            )
            if response.status_code != 200:
                continue

            ctype = str(response.headers.get("content-type") or "").lower()
            if "json" in ctype:
                data = response.json()
                pieces.extend(_flatten_text_values(data))
            else:
                body = response.text
                if body:
                    pieces.append(body)

            joined = "\n".join(pieces)
            if _parse_maintenance_end_from_text(joined, row.get("date")) is not None:
                return joined
        except Exception:
            continue

    # Public page fallback. This may work even when another PlayNC endpoint is blocked.
    page_url = str(row.get("link") or "").strip()
    if page_url:
        try:
            response = await client.get(
                page_url,
                headers={
                    **PLAYNC_HEADERS,
                    "accept": "text/html,application/xhtml+xml",
                },
                timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=2.0),
            )
            if response.status_code == 200 and response.text:
                pieces.append(response.text)
        except Exception:
            pass

    return "\n".join(pieces)


def _parse_maintenance_end_from_text(source, posted_date="", allow_extension=True, allow_completion=True):
    """Parse the effective maintenance completion time from a notice.

    Priority is actual completion > extension end > scheduled range.  Edited
    notices may retain old times inside <s>/<del>/<strike>; those superseded
    fragments are removed before parsing.
    """
    raw = unescape(str(source or ""))
    posted = str(posted_date or "").strip()

    # Remove superseded text from edited notices before flattening HTML.
    text = re.sub(
        r"<(?:s|del|strike)\b[^>]*>.*?</(?:s|del|strike)>",
        " ", raw, flags=re.I | re.S,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\u00a0\u200b]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    posted_dt = None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}", posted):
            posted_dt = datetime.fromisoformat(posted[:10]).replace(tzinfo=KST)
    except Exception:
        posted_dt = None
    base_year = posted_dt.year if posted_dt is not None else datetime.now(KST).year

    def _resolve_year(month):
        year = base_year
        if posted_dt is not None:
            # Handle notices posted around New Year for a Jan/Dec maintenance.
            if posted_dt.month == 12 and int(month) == 1:
                year += 1
            elif posted_dt.month == 1 and int(month) == 12:
                year -= 1
        return year

    # Scheduled maintenance range. This also gives the date and start clock.
    range_match = re.search(
        r"(?P<month>\d{1,2})\s*[/.]\s*(?P<day>\d{1,2})"
        r"(?:\s*\([^)]*\))?"
        r".{0,220}?"
        r"(?P<sh>\d{1,2})\s*[:：]\s*(?P<sm>\d{2})"
        r"\s*(?:~|∼|～|–|—|-)\s*"
        r"(?P<eh>\d{1,2})\s*[:：]\s*(?P<em>\d{2})",
        text, re.I | re.S,
    )

    month = day = sh = sm = None
    scheduled_end = None
    if range_match:
        try:
            month = int(range_match.group("month"))
            day = int(range_match.group("day"))
            sh = int(range_match.group("sh"))
            sm = int(range_match.group("sm"))
            eh = int(range_match.group("eh"))
            em = int(range_match.group("em"))
            year = _resolve_year(month)
            start_dt = datetime(year, month, day, sh, sm, tzinfo=KST)
            scheduled_end = datetime(year, month, day, eh, em, tzinfo=KST)
            if scheduled_end <= start_dt:
                scheduled_end += timedelta(days=1)
        except Exception:
            scheduled_end = None

    # If the full range was not found, recover the notice's month/day.
    if month is None or day is None:
        dm = re.search(r"(?<!\d)(\d{1,2})\s*[/.]\s*(\d{1,2})(?!\d)", text)
        if dm:
            try:
                month, day = int(dm.group(1)), int(dm.group(2))
            except Exception:
                month = day = None

    # Use post date only as a last-resort date anchor.
    if (month is None or day is None) and posted_dt is not None:
        month, day = posted_dt.month, posted_dt.day

    clock_pattern = (
        r"(?P<h>[01]?\d|2[0-3])"
        r"(?:\s*[:：]\s*(?P<mc>[0-5]\d)|\s*시(?:\s*(?P<mk>[0-5]?\d)\s*분)?)"
    )

    def _clock_from_match(match):
        if not match:
            return None
        try:
            hour = int(match.group("h"))
            minute = int(match.group("mc") or match.group("mk") or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        return None

    # Actual completion/ending is authoritative, including "점검 종료 7시 30분".
    completion_patterns = [
        r"(?:정기\s*)?점검(?:이|이\s*)?\s*(?:완료|종료)(?:\s*(?:시간|시각))?\s*[:：-]?\s*.{0,45}?" + clock_pattern,
        r"(?:정기\s*)?점검.{0,80}?" + clock_pattern + r"\s*(?:에|부터)?\s*(?:완료|종료)(?:되었습니다|됐습니다|됨|되었)?",
        clock_pattern + r"\s*현재.{0,140}?(?:정상적으로\s*게임\s*이용|점검(?:이)?\s*(?:완료|종료))",
    ]
    completion_clock = None
    if allow_completion:
        for pattern in completion_patterns:
            completion_clock = _clock_from_match(re.search(pattern, text, re.I | re.S))
            if completion_clock is not None:
                break

    # Extension text is authoritative ONLY when the caller already verified
    # that this is an actual extension notice (normally title contains "연장").
    # This prevents boilerplate such as "점검이 연장될 수 있습니다" from
    # changing the official schedule.
    extension_patterns = [
        r"연장.{0,180}?" + clock_pattern + r"\s*(?:에|까지)?\s*(?:종료|완료)(?:\s*예정)?",
        r"연장.{0,180}?(?:종료|완료)(?:\s*예정)?\s*[:：-]?\s*" + clock_pattern,
    ]
    extension_clock = None
    if allow_extension:
        for pattern in extension_patterns:
            extension_clock = _clock_from_match(re.search(pattern, text, re.I | re.S))
            if extension_clock is not None:
                break

    effective_clock = completion_clock or extension_clock
    if effective_clock is not None and month is not None and day is not None:
        try:
            eh, em = effective_clock
            year = _resolve_year(month)
            end_dt = datetime(year, month, day, eh, em, tzinfo=KST)
            if sh is not None and sm is not None:
                start_dt = datetime(year, month, day, sh, sm, tzinfo=KST)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
            return end_dt
        except Exception:
            pass

    return scheduled_end

def _parse_maintenance_end_from_notice(row):
    title = str((row or {}).get("title") or "")
    raw = str((row or {}).get("rawText") or "")
    source = title + "\n" + raw
    return _parse_maintenance_end_from_text(
        source,
        (row or {}).get("date") or "",
    )

def _maintenance_notice_title_kind(title):
    """Classify maintenance news using the TITLE, not boilerplate body text.

    Extension is intentionally strict: a title must actually announce an
    extension. Phrases such as "연장될 수 있습니다" or "연장 가능" are not
    treated as an extension even if they appear in a title.
    """
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    lowered = title_text.casefold()
    is_maintenance = ("점검" in title_text or "maintenance" in lowered)
    if not is_maintenance:
        return None

    if "연장" in title_text:
        possibility = re.search(
            r"연장\s*(?:될\s*)?수\s*있|연장\s*가능|연장\s*가능성",
            title_text,
            re.I,
        )
        if not possibility:
            return "extension"

    if "조기" in title_text and ("종료" in title_text or "완료" in title_text):
        return "early_end"

    if "종료" in title_text or "완료" in title_text:
        return "completion"

    return "schedule"


def _has_definitive_maintenance_completion_text(source):
    """True only for wording that says maintenance actually ended/completed.

    This deliberately rejects generic phrases such as "점검 종료 후" and
    possibility/plan wording. It is used when the original maintenance article
    is edited without changing its title.
    """
    raw = unescape(str(source or ""))
    plain = re.sub(r"<(?:s|del|strike)\b[^>]*>.*?</(?:s|del|strike)>", " ", raw, flags=re.I | re.S)
    plain = re.sub(r"<br\s*/?>", "\n", plain, flags=re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()

    patterns = (
        r"점검(?:이|을)?\s*.{0,80}?(?:종료|완료)\s*(?:되었습니다|됐습니다|되었으며|됐으며|했습니다|하였습니다|됨)",
        r"점검(?:을)?\s*.{0,80}?(?:종료|완료)\s*하였습니다",
        r"(?:조기\s*)?(?:종료|완료)된\s*점검",
    )
    return any(re.search(p, plain, re.I) for p in patterns)


def _parse_maintenance_window_from_text(source, posted_date=""):
    """Return the scheduled maintenance start/end from a notice range."""
    raw = unescape(str(source or ""))
    posted = str(posted_date or "").strip()
    text = re.sub(
        r"<(?:s|del|strike)\b[^>]*>.*?</(?:s|del|strike)>",
        " ", raw, flags=re.I | re.S,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\u00a0\u200b]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    posted_dt = None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}", posted):
            posted_dt = datetime.fromisoformat(posted[:10]).replace(tzinfo=KST)
    except Exception:
        posted_dt = None
    base_year = posted_dt.year if posted_dt is not None else datetime.now(KST).year

    def _resolve_year(month):
        year = base_year
        if posted_dt is not None:
            if posted_dt.month == 12 and int(month) == 1:
                year += 1
            elif posted_dt.month == 1 and int(month) == 12:
                year -= 1
        return year

    match = re.search(
        r"(?P<month>\d{1,2})\s*[/.]\s*(?P<day>\d{1,2})"
        r"(?:\s*\([^)]*\))?"
        r".{0,220}?"
        r"(?P<sh>\d{1,2})\s*[:：]\s*(?P<sm>\d{2})"
        r"\s*(?:~|∼|～|–|—|-)\s*"
        r"(?P<eh>\d{1,2})\s*[:：]\s*(?P<em>\d{2})",
        text, re.I | re.S,
    )
    if not match:
        return {"start": None, "scheduledEnd": None}

    try:
        month = int(match.group("month"))
        day = int(match.group("day"))
        sh = int(match.group("sh"))
        sm = int(match.group("sm"))
        eh = int(match.group("eh"))
        em = int(match.group("em"))
        year = _resolve_year(month)
        start_dt = datetime(year, month, day, sh, sm, tzinfo=KST)
        end_dt = datetime(year, month, day, eh, em, tzinfo=KST)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        return {"start": start_dt, "scheduledEnd": end_dt}
    except Exception:
        return {"start": None, "scheduledEnd": None}

# Manual corrections are intentionally isolated from the rest of the bot.
# They survive normal requests/reloads on the same Render instance.  A full
# With a Render Persistent Disk mounted at /var/data, this survives redeploys/restarts.
BOSS_SCHEDULE_OVERRIDE_FILE = _state_path(
    "BOSS_SCHEDULE_OVERRIDE_FILE",
    "boss_schedule_overrides.json",
    legacy_paths=("/tmp/aion2_boss_schedule_overrides.json",),
)

_BOSS_SCHEDULE_OVERRIDE_CACHE = None


def _load_boss_schedule_overrides():
    global _BOSS_SCHEDULE_OVERRIDE_CACHE
    if isinstance(_BOSS_SCHEDULE_OVERRIDE_CACHE, dict):
        return json.loads(json.dumps(_BOSS_SCHEDULE_OVERRIDE_CACHE, ensure_ascii=False))
    raw = _safe_json_load(BOSS_SCHEDULE_OVERRIDE_FILE, {})
    _BOSS_SCHEDULE_OVERRIDE_CACHE = raw if isinstance(raw, dict) else {}
    return json.loads(json.dumps(_BOSS_SCHEDULE_OVERRIDE_CACHE, ensure_ascii=False))

def _save_boss_schedule_overrides(data):
    global _BOSS_SCHEDULE_OVERRIDE_CACHE
    if not isinstance(data, dict):
        return False
    _BOSS_SCHEDULE_OVERRIDE_CACHE = json.loads(json.dumps(data, ensure_ascii=False))
    return _atomic_json_write(BOSS_SCHEDULE_OVERRIDE_FILE, data)

def _maintenance_runtime_info():
    data = _load_boss_schedule_overrides()
    row = data.get("maintenanceRuntime") if isinstance(data.get("maintenanceRuntime"), dict) else {}
    return {
        "start": _parse_kst_iso(row.get("start")),
        "end": _parse_kst_iso(row.get("end")),
        "sourceId": str(row.get("sourceId") or ""),
        "sourceTitle": str(row.get("sourceTitle") or ""),
        "sourcePostedAt": str(row.get("sourcePostedAt") or ""),
        "changeKind": str(row.get("changeKind") or ""),
        "lastStart": _parse_kst_iso(row.get("lastStart")),
        "lastEnd": _parse_kst_iso(row.get("lastEnd")),
        "updatedAt": str(row.get("updatedAt") or ""),
    }


def _save_maintenance_runtime(start, end, source_id="", source_title="", source_posted_at="", change_kind=""):
    if end is None:
        return False
    now = datetime.now(KST)
    data = _load_boss_schedule_overrides()
    old = data.get("maintenanceRuntime") if isinstance(data.get("maintenanceRuntime"), dict) else {}
    old_start = _parse_kst_iso(old.get("start"))
    old_end = _parse_kst_iso(old.get("end"))
    last_start = _parse_kst_iso(old.get("lastStart"))
    last_end = _parse_kst_iso(old.get("lastEnd"))

    # If a finished maintenance is being replaced by a future maintenance,
    # preserve the completed interval so phones can discard missed lead alerts.
    if old_start is not None and old_end is not None and now >= old_end:
        last_start, last_end = old_start, old_end

    # Extension/completion notices often omit the original start time. Keep the
    # current start only when the end belongs to the same maintenance day.
    chosen_start = start
    if chosen_start is None and old_start is not None and old_end is not None:
        if abs((end.date() - old_end.date()).days) <= 1:
            chosen_start = old_start

    # A new future scheduled maintenance with an explicit start replaces the
    # current interval while retaining the last completed interval.
    row = {
        "start": chosen_start.astimezone(KST).isoformat() if chosen_start is not None else "",
        "end": end.astimezone(KST).isoformat(),
        "sourceId": str(source_id or ""),
        "sourceTitle": str(source_title or ""),
        "sourcePostedAt": str(source_posted_at or ""),
        "changeKind": str(change_kind or ""),
        "lastStart": last_start.astimezone(KST).isoformat() if last_start is not None else "",
        "lastEnd": last_end.astimezone(KST).isoformat() if last_end is not None else "",
        "updatedAt": now.isoformat(),
    }
    data["maintenanceRuntime"] = row
    return _save_boss_schedule_overrides(data)


def _maintenance_runtime_snapshot(now=None, persist_transition=True):
    now = now or datetime.now(KST)
    info = _maintenance_runtime_info()
    start = info.get("start")
    end = info.get("end")
    last_start = info.get("lastStart")
    last_end = info.get("lastEnd")

    active = bool(start is not None and end is not None and start <= now < end)
    scheduled = bool(start is not None and end is not None and now < start)

    # Once the end is reached, remember the exact interval. This is the guard
    # that prevents 30m/10m (or custom lead) catch-up after maintenance.
    if start is not None and end is not None and now >= end:
        if last_start != start or last_end != end:
            last_start, last_end = start, end
            if persist_transition:
                data = _load_boss_schedule_overrides()
                row = data.get("maintenanceRuntime") if isinstance(data.get("maintenanceRuntime"), dict) else {}
                row["lastStart"] = start.astimezone(KST).isoformat()
                row["lastEnd"] = end.astimezone(KST).isoformat()
                row["updatedAt"] = now.isoformat()
                data["maintenanceRuntime"] = row
                _save_boss_schedule_overrides(data)

    return {
        **info,
        "active": active,
        "scheduled": scheduled,
        "lastStart": last_start,
        "lastEnd": last_end,
    }


def _alert_trigger_blocked_by_maintenance(trigger_dt, now=None):
    """Suppress active-maintenance alerts and permanently discard missed leads."""
    snap = _maintenance_runtime_snapshot(now=now, persist_transition=True)
    if snap.get("active"):
        return True
    last_start = snap.get("lastStart")
    last_end = snap.get("lastEnd")
    if trigger_dt is not None and last_start is not None and last_end is not None:
        return last_start <= trigger_dt <= last_end
    return False


# Default automatic alert lead times for every boss/content schedule.
DEFAULT_SCHEDULE_ALERT_LEADS = [30, 10]


def _schedule_key(name):
    key = str(name or "").strip()
    aliases = {
        "아그로": "agro", "정령왕 아그로": "agro",
        "카이라": "kaira", "감시자 카이라": "kaira",
        "나흐마": "nahma", "수호신장 나흐마": "nahma",
        "어비스": "abyss", "어비스보스": "abyss", "어비스 보스": "abyss",
        "시공": "sigong", "시공쟁탈전": "sigong",
        "균영": "gyunyeol", "균열": "gyunyeol", "균열지대": "gyunyeol",
        "아티": "ati", "아티쟁": "ati",
        "필드보스": "fieldboss", "필보": "fieldboss",
    }
    return aliases.get(key)


def _get_schedule_alert_leads(name, room=""):
    """Return alert lead times for this room only.

    Room-specific settings intentionally do not inherit the old global
    alertLeads value. That prevents one chat room from changing another
    room's notifications. Rooms without an override use 30/10 minutes.
    """
    key = _schedule_key(name)
    if not key:
        return list(DEFAULT_SCHEDULE_ALERT_LEADS)

    room_key = _openchat_room_key(room)
    data = _load_boss_schedule_overrides()
    by_room = data.get("alertLeadsByRoom") or {}
    room_rows = by_room.get(room_key) if isinstance(by_room, dict) and room_key else None
    leads = room_rows.get(key) if isinstance(room_rows, dict) else None

    if not isinstance(leads, list) or not leads:
        return list(DEFAULT_SCHEDULE_ALERT_LEADS)

    cleaned = []
    for value in leads:
        try:
            n = int(value)
        except Exception:
            continue
        if 1 <= n <= 180 and n not in cleaned:
            cleaned.append(n)
    return sorted(cleaned, reverse=True) if cleaned else list(DEFAULT_SCHEDULE_ALERT_LEADS)


def _scheduled_alert_key(room, name, target, lead):
    """Stable per-room key for one scheduled alert occurrence.

    The key is based only on the normalized Kakao room name, canonical content
    name, scheduled KST minute, and lead minutes. Recomputing the schedule on
    every poll therefore produces exactly the same key for the same alert.
    """
    room_key = _openchat_room_key(room) or "__global__"
    try:
        target_key = target.astimezone(KST).strftime("%Y%m%d%H%M")
    except Exception:
        target_key = str(target)
    try:
        lead_key = str(int(lead))
    except Exception:
        lead_key = str(lead)
    return "SCHEDULE|{}|{}|{}|{}".format(
        quote(room_key, safe=""),
        quote(str(name or "").strip(), safe=""),
        target_key,
        lead_key,
    )


def _legacy_scheduled_alert_key(name, target, lead):
    """Previous key format, kept only to suppress a one-time redeploy duplicate."""
    return f"{name}|{int(target.timestamp())}|{int(lead)}"


def _clear_schedule_delivery_keys(name, room=""):
    """Drop stale sent/lease keys only for the room whose setting changed."""
    canonical = {
        "agro": "정령왕 아그로",
        "kaira": "감시자 카이라",
        "nahma": "수호신장 나흐마",
        "abyss": "어비스 보스",
        "sigong": "시공쟁탈전",
        "gyunyeol": "균열지대",
        "ati": "아티쟁",
        "fieldboss": "필드보스",
    }.get(_schedule_key(name))
    room_key = _openchat_room_key(room)
    if not canonical or not room_key:
        return
    prefix = canonical + "|"
    try:
        state = _load_openchat_alert_state()
        deliveries = state.get("deliveries") or {}
        delivery_key = _openchat_delivery_key(room_key)
        delivery = deliveries.get(delivery_key)
        if not isinstance(delivery, dict):
            return
        changed = False
        old = list(delivery.get("sentKeys") or [])
        encoded_name = quote(canonical, safe="")

        def _matches_schedule_key(value):
            text = str(value or "")
            # Old format: name|unix_timestamp|lead
            if text.startswith(prefix):
                return True
            # New format: SCHEDULE|encoded_room|encoded_name|YYYYMMDDHHMM|lead
            parts = text.split("|")
            return len(parts) >= 5 and parts[0] == "SCHEDULE" and parts[2] == encoded_name

        new = [k for k in old if not _matches_schedule_key(k)]
        if new != old:
            delivery["sentKeys"] = new
            changed = True
        leases = delivery.get("leases") if isinstance(delivery.get("leases"), dict) else {}
        new_leases = {k: v for k, v in leases.items() if not _matches_schedule_key(k)}
        if new_leases != leases:
            delivery["leases"] = new_leases
            changed = True
        if changed:
            deliveries[delivery_key] = delivery
            state["deliveries"] = deliveries
            _save_openchat_alert_state(state)
    except Exception:
        pass


def _set_schedule_alert_leads(name, leads, room=""):
    key = _schedule_key(name)
    room_key = _openchat_room_key(room)
    if not key or not room_key:
        return False
    cleaned = []
    for value in leads:
        try:
            n = int(value)
        except Exception:
            continue
        if 1 <= n <= 180 and n not in cleaned:
            cleaned.append(n)
    if not cleaned:
        return False

    data = _load_boss_schedule_overrides()
    by_room = data.get("alertLeadsByRoom")
    if not isinstance(by_room, dict):
        by_room = {}
    room_rows = by_room.get(room_key)
    if not isinstance(room_rows, dict):
        room_rows = {}
    room_rows[key] = sorted(cleaned, reverse=True)
    by_room[room_key] = room_rows
    data["alertLeadsByRoom"] = by_room

    ok = _save_boss_schedule_overrides(data)
    if ok:
        _clear_schedule_delivery_keys(name, room_key)
    return ok


def _manual_alert_lead_command(name, value_text, room="", room_label=""):
    # Supports: !아그로 25분전 / !시공 25분전 10분전 / !아그로 알림 25 10
    nums = [int(x) for x in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", str(value_text or ""))]
    nums = [x for x in nums if 1 <= x <= 180]
    if not nums:
        return "⚠️ 알림 시간은 1~180분 사이로 입력해주세요."
    nums = list(dict.fromkeys(nums))
    if not _set_schedule_alert_leads(name, nums, room):
        return "⚠️ 알림 시간 수정 저장 실패"
    shown = " / ".join(f"{x}분 전" for x in sorted(nums, reverse=True))
    room_key = _openchat_room_key(room)
    display_room = _openchat_room_key(room_label) or room_key
    return f"✅ {name} 알림 시간 수정 완료\n\n🏠 {display_room}\n🔔 {shown}"


def _parse_manual_clock(text_value):
    text = str(text_value or "").strip()
    m = re.fullmatch(r"([01]?\d|2[0-3])\s*:\s*([0-5]\d)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"([01]?\d|2[0-3])\s*시(?:\s*([0-5]?\d)\s*분)?", text)
    if m:
        return int(m.group(1)), int(m.group(2) or 0)
    return None


def _weekly_targets(weekdays, times, now=None, limit=6):
    now = now or datetime.now(KST)
    out = []
    day_set = set(int(x) for x in weekdays)
    clean_times = [(int(h), int(m)) for h, m in times]
    for add_days in range(0, 15):
        day = now + timedelta(days=add_days)
        if day.weekday() not in day_set:
            continue
        for hour, minute in clean_times:
            target = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target > now:
                out.append(target)
    out.sort()
    return out[:limit]


def _next_weekly_multi(weekdays, times, now=None):
    targets = _weekly_targets(weekdays, times, now=now, limit=1)
    return targets[0] if targets else None


def _apply_manual_schedule_overrides():
    data = _load_boss_schedule_overrides()

    kaira = data.get("kaira") or {}
    if isinstance(kaira.get("hours"), list) and kaira["hours"]:
        BOSS_RULES["kairaHours"] = sorted(set(int(x) % 24 for x in kaira["hours"]))

    for key, prefix in (("nahma", "nahma"), ("abyss", "abyss")):
        row = data.get(key) or {}
        if "hour" in row and "minute" in row:
            BOSS_RULES[prefix + "Hour"] = int(row["hour"])
            BOSS_RULES[prefix + "Minute"] = int(row["minute"])

    for key, rule_key in (
        ("sigong", "sigongTimes"),
        ("gyunyeol", "gyunyeolTimes"),
        ("ati", "atiTimes"),
        ("fieldboss", "fieldBossTimes"),
    ):
        row = data.get(key) or {}
        times = row.get("times")
        if isinstance(times, list) and times:
            parsed = []
            for item in times:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    parsed.append((int(item[0]), int(item[1])))
            if parsed:
                BOSS_RULES[rule_key] = parsed

    return data


def _parse_kst_iso(value):
    try:
        dt = datetime.fromisoformat(str(value or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return None

def _persist_official_agro_anchor(anchor, source_id="", source_title="", source_posted_at="", change_kind="", force_backward=False):
    if anchor is None:
        return False
    data = _load_boss_schedule_overrides()
    current = data.get("agroOfficial") if isinstance(data.get("agroOfficial"), dict) else {}
    old_anchor = _parse_kst_iso(current.get("anchor"))
    old_source = str(current.get("sourceId") or "")
    new_source = str(source_id or "")
    # Different old notices may never roll the anchor backward. A clearly
    # identified official completion/early-end notice is the one exception.
    if old_anchor is not None and old_anchor > anchor and not force_backward and not (old_source and new_source and old_source == new_source):
        return True
    data["agroOfficial"] = {
        "anchor": anchor.astimezone(KST).isoformat(),
        "sourceId": new_source,
        "sourceTitle": str(source_title or ""),
        "sourcePostedAt": str(source_posted_at or ""),
        "changeKind": str(change_kind or ""),
        "updatedAt": datetime.now(KST).isoformat(),
    }
    return _save_boss_schedule_overrides(data)

def _persisted_official_agro_info():
    data = _load_boss_schedule_overrides()
    row = data.get("agroOfficial") or {}
    return {
        "anchor": _parse_kst_iso(row.get("anchor")),
        "sourceId": str(row.get("sourceId") or ""),
        "sourceTitle": str(row.get("sourceTitle") or ""),
        "sourcePostedAt": str(row.get("sourcePostedAt") or ""),
        "changeKind": str(row.get("changeKind") or ""),
        "updatedAt": str(row.get("updatedAt") or ""),
    }

def _persisted_official_agro_anchor():
    return _persisted_official_agro_info().get("anchor")

def _maintenance_clock_delta_minutes(old_anchor, new_anchor):
    """Clock-time change between two maintenance completions.

    Weekly event weekdays stay intact; only their clock time follows the
    maintenance completion-time shift.  Normalize across midnight so
    23:00 -> 01:00 means +120 minutes rather than -1320 minutes.
    """
    if old_anchor is None or new_anchor is None:
        return 0
    old_m = old_anchor.hour * 60 + old_anchor.minute
    new_m = new_anchor.hour * 60 + new_anchor.minute
    delta = new_m - old_m
    if delta > 720:
        delta -= 1440
    elif delta < -720:
        delta += 1440
    return int(delta)

def _shift_clock(hour, minute, delta_minutes):
    total = (int(hour) * 60 + int(minute) + int(delta_minutes)) % 1440
    return total // 60, total % 60

def _bind_or_rebase_maintenance_schedules(old_anchor, new_anchor, source_id, source_title):
    """Bind current dynamic schedules to the first maintenance source, then
    shift them only when a genuinely newer maintenance source is accepted.

    Kaira and Nahma are intentionally excluded.
    """
    if new_anchor is None:
        return False

    data = _load_boss_schedule_overrides()
    meta = data.get("maintenanceDynamicMeta") if isinstance(data.get("maintenanceDynamicMeta"), dict) else {}
    bound_anchor = _parse_kst_iso(meta.get("anchor"))
    bound_source = str(meta.get("sourceId") or "")
    new_source = str(source_id or "")

    # First binding/migration: preserve every current clock exactly as-is.
    if bound_anchor is None:
        data["maintenanceDynamicMeta"] = {
            "anchor": new_anchor.astimezone(KST).isoformat(),
            "sourceId": new_source,
            "sourceTitle": str(source_title or ""),
            "updatedAt": datetime.now(KST).isoformat(),
        }
        return _save_boss_schedule_overrides(data)

    same_source = bool(new_source and bound_source and new_source == bound_source)
    # Same article can be edited for extension/early completion. Accept a changed
    # anchor for that same source. For a different source, never move backward.
    if new_anchor == bound_anchor:
        return True
    if not same_source and new_anchor < bound_anchor:
        return True

    delta = _maintenance_clock_delta_minutes(bound_anchor, new_anchor)

    # Abyss boss
    row = data.get("abyss") if isinstance(data.get("abyss"), dict) else {}
    ah = int(row.get("hour", BOSS_RULES.get("abyssHour", 22)))
    am = int(row.get("minute", BOSS_RULES.get("abyssMinute", 30)))
    ah, am = _shift_clock(ah, am, delta)
    row.update({"hour": ah, "minute": am})
    data["abyss"] = row

    # Weekly maintenance-based contents / field boss.
    for key, rule_key in (
        ("sigong", "sigongTimes"),
        ("gyunyeol", "gyunyeolTimes"),
        ("ati", "atiTimes"),
        ("fieldboss", "fieldBossTimes"),
    ):
        row = data.get(key) if isinstance(data.get(key), dict) else {}
        times = row.get("times")
        if not isinstance(times, list) or not times:
            times = BOSS_RULES.get(rule_key) or []
        shifted = []
        for item in times:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                h, m = _shift_clock(item[0], item[1], delta)
                shifted.append([h, m])
        if shifted:
            row["times"] = shifted
            data[key] = row

    data["maintenanceDynamicMeta"] = {
        "anchor": new_anchor.astimezone(KST).isoformat(),
        "sourceId": new_source,
        "sourceTitle": str(source_title or ""),
        "previousAnchor": bound_anchor.astimezone(KST).isoformat(),
        "clockShiftMinutes": delta,
        "updatedAt": datetime.now(KST).isoformat(),
    }
    ok = _save_boss_schedule_overrides(data)
    if ok:
        _apply_manual_schedule_overrides()
        for key in ("어비스", "시공", "균열", "아티", "필드보스"):
            _clear_schedule_delivery_keys(key)
    return ok

def _manual_agro_anchor_for_source(source_id=None, official_anchor=None):
    """Keep manual Agro time until an official maintenance confirmation is newer."""
    data = _load_boss_schedule_overrides()
    row = data.get("agro") or {}
    manual = _parse_kst_iso(row.get("anchor"))
    if manual is None:
        return None
    stored_source = str(row.get("sourceId") or "")
    current_source = str(source_id or "")
    set_at = _parse_kst_iso(row.get("setAt"))

    official_info = data.get("agroOfficial") if isinstance(data.get("agroOfficial"), dict) else {}
    official_updated = _parse_kst_iso(official_info.get("updatedAt"))

    # Any official confirmation saved after the manual correction wins. This
    # correctly handles edits/extensions that keep the same article ID.
    if set_at is not None and official_updated is not None and official_updated > set_at:
        return None

    if not current_source:
        return manual
    if stored_source and stored_source == current_source:
        return manual
    if stored_source and stored_source != current_source:
        return None
    return manual


def _set_manual_schedule(name, hour, minute):
    """Set only schedule-related values; every other bot feature remains untouched."""
    data = _load_boss_schedule_overrides()
    now = datetime.now(KST)
    key = str(name or "").strip()

    if key == "아그로":
        # Bind the correction to the currently detected maintenance notice.
        source_id = str(_maintenance_anchor_cache.get("sourceId") or "")
        anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Pick the occurrence closest to now so a correction such as 06:00 works
        # naturally whether entered before or after that clock time.
        candidates = [anchor - timedelta(days=1), anchor, anchor + timedelta(days=1)]
        anchor = min(candidates, key=lambda d: abs((d - now).total_seconds()))
        data["agro"] = {"anchor": anchor.isoformat(), "sourceId": source_id, "setAt": now.isoformat()}

    elif key == "카이라":
        # One corrected occurrence defines the existing four-hour cycle.
        hours = sorted(((hour + 4 * i) % 24) for i in range(6))
        data["kaira"] = {"hours": hours, "minute": minute}
        # Current display/engine is hour-based; preserve minutes by rotating via
        # explicit times only when minute is non-zero.
        if minute:
            data["kaira"]["times"] = [[h, minute] for h in hours]

    elif key == "나흐마":
        data["nahma"] = {"hour": hour, "minute": minute}
    elif key in ("어비스", "어비스보스"):
        data["abyss"] = {"hour": hour, "minute": minute}
    elif key == "시공":
        # Existing timetable is a 3-hour pair: 20:00 / 23:00.
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        second = base + timedelta(hours=3)
        data["sigong"] = {"times": [[hour, minute], [second.hour, second.minute]]}
    elif key in ("균영", "균열", "균열지대"):
        data["gyunyeol"] = {"times": [[hour, minute]]}
    elif key in ("아티", "아티쟁"):
        data["ati"] = {"times": [[hour, minute]]}
    elif key in ("필보", "필드보스"):
        data["fieldboss"] = {"times": [[hour, minute]]}
    else:
        return False

    if not _save_boss_schedule_overrides(data):
        return False
    _apply_manual_schedule_overrides()
    return True

async def latest_maintenance_anchor():
    """Maintenance anchor + live maintenance tracking policy.

    Important rules:
    - Normal notices establish the scheduled maintenance window.
    - EXTENSION is accepted only from a title that actually announces "연장".
      Boilerplate body text such as "연장될 수 있습니다" is ignored.
    - Strong official completion/early-end news may move the end earlier.
    - During an active maintenance the source is rechecked every 60 seconds.
    - Kaira and Nahma stay fixed; maintenance-based schedules keep the existing
      rebase behavior when the confirmed completion time changes.
    """
    now = datetime.now(KST)
    now_ts = time.time()
    runtime_before = _maintenance_runtime_snapshot(now=now, persist_transition=True)
    active_now = bool(runtime_before.get("active"))
    cache_ttl = 60 if active_now else 300

    cached = _maintenance_anchor_cache.get("value")
    cached_ts = float(_maintenance_anchor_cache.get("ts") or 0)
    if cached is not None and now_ts - cached_ts < cache_ttl:
        manual = _manual_agro_anchor_for_source(_maintenance_anchor_cache.get("sourceId"), cached)
        return manual if manual is not None else cached

    persisted_info = _persisted_official_agro_info()
    persisted_anchor = persisted_info.get("anchor")
    persisted_source = str(persisted_info.get("sourceId") or "")
    persisted_posted = _parse_kst_iso(persisted_info.get("sourcePostedAt"))

    runtime = _maintenance_runtime_info()
    runtime_start = runtime.get("start")
    runtime_end = runtime.get("end")

    chosen = None
    try:
        rows = await fetch_board_latest("공지", limit=50)
        candidates = []
        for row in rows:
            title = str(row.get("title") or "")
            title_kind = _maintenance_notice_title_kind(title)
            if title_kind is None:
                continue

            source_id = str(row.get("id") or "")
            source_posted_text = str(row.get("postedAt") or "")
            source_posted = _parse_kst_iso(source_posted_text)

            detail_text = await fetch_notice_detail_text(row)
            window = _parse_maintenance_window_from_text(detail_text, row.get("date") or "")
            scheduled_end = window.get("scheduledEnd")
            start_dt = window.get("start")

            definitive_completion = _has_definitive_maintenance_completion_text(detail_text)
            allow_extension = title_kind == "extension"
            allow_completion = title_kind in ("early_end", "completion") or definitive_completion
            parsed = _parse_maintenance_end_from_text(
                detail_text,
                row.get("date") or "",
                allow_extension=allow_extension,
                allow_completion=allow_completion,
            )
            if parsed is None:
                continue

            effective_kind = title_kind
            if title_kind == "schedule" and definitive_completion and scheduled_end is not None and parsed != scheduled_end:
                effective_kind = "completion"

            candidates.append({
                "end": parsed,
                "start": start_dt,
                "scheduledEnd": scheduled_end,
                "title": title,
                "id": source_id,
                "postedAt": source_posted,
                "postedAtText": source_posted_text,
                "kind": effective_kind,
                "titleKind": title_kind,
            })

        # Board rows are already time-sorted, but make the rule explicit.
        candidates.sort(
            key=lambda c: c.get("postedAt").timestamp() if c.get("postedAt") is not None else 0.0,
            reverse=True,
        )

        # 1) Current-maintenance change news / same-article edits take priority.
        for c in candidates:
            end_dt = c["end"]
            kind = c["kind"]
            same_source = bool(persisted_source and c["id"] and c["id"] == persisted_source)
            same_day = bool(
                persisted_anchor is None
                or abs((end_dt.date() - persisted_anchor.date()).days) <= 1
            )
            newer_source = bool(
                persisted_posted is None
                or c.get("postedAt") is None
                or c.get("postedAt") >= persisted_posted
            )

            # Explicit extension: title must say 연장, and end must actually move later.
            if kind == "extension":
                if persisted_anchor is not None and end_dt <= persisted_anchor:
                    continue
                if persisted_anchor is not None and not same_day:
                    continue
                if not newer_source:
                    continue
                chosen = c
                break

            # Explicit/definitive completion can move earlier or later. A separate
            # article may roll backward only when it belongs to the current date.
            if kind in ("early_end", "completion"):
                if persisted_anchor is not None and end_dt == persisted_anchor:
                    continue
                if persisted_anchor is not None and not same_day:
                    continue
                if not same_source and not newer_source:
                    continue
                chosen = c
                break

            # Same ordinary article with only a changed scheduled range is NOT an
            # extension. This is the user's strict "title must say 연장" rule.
            if same_source:
                continue

        # 2) If there was no current-change notice, accept a genuinely new normal
        # scheduled maintenance only when its completion is later than the last one.
        if chosen is None:
            for c in candidates:
                if c["kind"] != "schedule":
                    continue
                end_dt = c["end"]
                if persisted_anchor is not None and end_dt <= persisted_anchor:
                    continue
                chosen = c
                break

    except Exception:
        chosen = None

    if chosen is not None:
        official_anchor = chosen["end"]
        source_id = chosen["id"]
        source_title = chosen["title"]
        source_posted_text = chosen.get("postedAtText") or ""
        change_kind = chosen.get("kind") or "schedule"

        # Keep the current maintenance start for extension/completion articles
        # that only publish a revised end clock.
        chosen_start = chosen.get("start")
        if chosen_start is None and runtime_start is not None and runtime_end is not None:
            if abs((official_anchor.date() - runtime_end.date()).days) <= 1:
                chosen_start = runtime_start

        _bind_or_rebase_maintenance_schedules(
            persisted_anchor, official_anchor, source_id, source_title
        )
        force_backward = bool(
            persisted_anchor is not None
            and official_anchor < persisted_anchor
            and change_kind in ("early_end", "completion")
        )
        _persist_official_agro_anchor(
            official_anchor,
            source_id,
            source_title,
            source_posted_at=source_posted_text,
            change_kind=change_kind,
            force_backward=force_backward,
        )
        _save_maintenance_runtime(
            chosen_start,
            official_anchor,
            source_id=source_id,
            source_title=source_title,
            source_posted_at=source_posted_text,
            change_kind=change_kind,
        )

        _maintenance_anchor_cache.update({
            "value": official_anchor,
            "ts": now_ts,
            "sourceId": source_id,
            "sourceTitle": source_title,
        })
        BOSS_RULES_META["sources"]["maintenance"] = source_title
        manual = _manual_agro_anchor_for_source(source_id, official_anchor)
        return manual if manual is not None else official_anchor

    base = persisted_anchor or cached or AGRO_FALLBACK_ANCHOR
    source_id = persisted_source or str(_maintenance_anchor_cache.get("sourceId") or "")
    source_title = str(persisted_info.get("sourceTitle") or _maintenance_anchor_cache.get("sourceTitle") or "")

    if base is not None:
        _bind_or_rebase_maintenance_schedules(None, base, source_id, source_title)

    _maintenance_anchor_cache.update({
        "value": base,
        "ts": now_ts,
        "sourceId": source_id,
        "sourceTitle": source_title,
    })
    _maintenance_runtime_snapshot(now=now, persist_transition=True)
    manual = _manual_agro_anchor_for_source(source_id, base)
    return manual if manual is not None else base


def _apply_kaira_rule(source, source_title):
    """
    Accepts either explicit multiple times or a phrase containing a 4-hour cycle
    plus a clear first hour. It only updates when confidence is high.
    """
    if "카이라" not in source:
        return False

    clocks = _extract_clock_values(source)
    zero_minute_hours = sorted(set(h for h, m in clocks if m == 0))

    # Strongest case: six explicit 4-hourly hours.
    if len(zero_minute_hours) >= 6:
        for start in range(0, 4):
            expected = sorted(((start + 4 * i) % 24) for i in range(6))
            if all(h in zero_minute_hours for h in expected):
                BOSS_RULES["kairaHours"] = expected
                BOSS_RULES_META["sources"]["kaira"] = source_title
                return True

    # Wording like "01시부터 4시간마다".
    m = re.search(
        r"(?<!\d)([01]?\d|2[0-3])\s*(?:시|:00).{0,50}?(?:4\s*시간|4시간).{0,20}?(?:마다|간격|주기)",
        source,
        re.S,
    )
    if not m:
        m = re.search(
            r"(?:4\s*시간|4시간).{0,40}?(?:마다|간격|주기).{0,50}?(?<!\d)([01]?\d|2[0-3])\s*(?:시|:00)",
            source,
            re.S,
        )

    if m:
        start_hour = int(m.group(1))
        hours = sorted(((start_hour + 4 * i) % 24) for i in range(6))
        BOSS_RULES["kairaHours"] = hours
        BOSS_RULES_META["sources"]["kaira"] = source_title
        return True

    return False

def _apply_named_weekly_rule(source, source_title, keyword, weekday_key, hour_key, minute_key):
    if keyword not in source:
        return False

    weekdays = _extract_weekdays(source)
    clocks = _extract_clock_values(source)

    if not weekdays or not clocks:
        return False

    # Choose the first explicit time in a compact snippet around the boss name.
    boss_pos = source.find(keyword)
    local = source[max(0, boss_pos - 100): boss_pos + 500]
    local_clocks = _extract_clock_values(local)
    if local_clocks:
        hour, minute = local_clocks[0]
    else:
        hour, minute = clocks[0]

    BOSS_RULES[weekday_key] = sorted(set(weekdays))
    BOSS_RULES[hour_key] = int(hour)
    BOSS_RULES[minute_key] = int(minute)
    BOSS_RULES_META["sources"][keyword] = source_title
    return True

def _apply_agro_interval_rule(source, source_title):
    if "아그로" not in source:
        return False

    m = re.search(r"아그로.{0,120}?(\d{1,2})\s*시간(?:마다|간격|주기)?", source, re.S)
    if not m:
        m = re.search(r"(\d{1,2})\s*시간(?:마다|간격|주기)?.{0,120}?아그로", source, re.S)

    if not m:
        return False

    interval = int(m.group(1))
    if interval < 1 or interval > 48:
        return False

    BOSS_RULES["agroIntervalHours"] = interval
    BOSS_RULES_META["sources"]["agro"] = source_title
    return True

async def refresh_boss_rules(force=False):
    """Apply the saved schedule policy without letting old board text rewrite it.

    Policy:
    - Kaira and Nahma are fixed schedules (manual correction can still be saved).
    - Agro/Abyss/Sigong/Gyunyeol/Ati/Field Boss are maintenance-based.
    - Only latest_maintenance_anchor() may move maintenance-based schedules, and
      only after a genuinely newer maintenance notice is confirmed.
    """
    now_ts = time.time()
    if not force and now_ts - float(_boss_rule_refresh.get("ts") or 0) < 300:
        _apply_manual_schedule_overrides()
        return BOSS_RULES

    async with _boss_rule_refresh["lock"]:
        _apply_manual_schedule_overrides()
        BOSS_RULES_META["updatedAt"] = datetime.now(KST).isoformat()
        _boss_rule_refresh["ts"] = time.time()

    return BOSS_RULES

def next_agro_from_anchor(anchor, now=None):
    now = now or datetime.now(KST)
    interval = timedelta(hours=int(BOSS_RULES["agroIntervalHours"]))

    if now < anchor:
        return anchor

    elapsed = now - anchor
    steps = int(elapsed.total_seconds() // interval.total_seconds()) + 1
    return anchor + (interval * steps)

def agro_targets(anchor, count=4, now=None):
    first = next_agro_from_anchor(anchor, now=now)
    interval = timedelta(hours=int(BOSS_RULES["agroIntervalHours"]))
    return [first + (interval * i) for i in range(count)]

def _kaira_times():
    data = _load_boss_schedule_overrides()
    row = data.get("kaira") or {}
    explicit = row.get("times")
    if isinstance(explicit, list) and explicit:
        out = []
        for item in explicit:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append((int(item[0]), int(item[1])))
        if out:
            return sorted(out)
    return [(int(h), 0) for h in BOSS_RULES["kairaHours"]]

def _next_daily_times(times, now=None):
    now = now or datetime.now(KST)
    clean = sorted((int(h), int(m)) for h, m in times)
    for hour, minute in clean:
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target > now:
            return target
    tomorrow = now + timedelta(days=1)
    hour, minute = clean[0]
    return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)

def format_kaira_schedule():
    lines = ["🐲 감시자 카이라", ""]
    for hour, minute in _kaira_times():
        lines.append(f"⏰ {hour:02d}:{minute:02d}")
    return "\n".join(lines)

def format_nahma_schedule():
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    days = "/".join(weekday_names[int(x)] for x in BOSS_RULES["nahmaWeekdays"])
    return "\n".join([
        "🐲 수호신장 나흐마",
        "",
        f"⏰ {days} {int(BOSS_RULES['nahmaHour']):02d}:{int(BOSS_RULES['nahmaMinute']):02d}",
    ])

def format_abyss_schedule():
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    days = "/".join(weekday_names[int(x)] for x in BOSS_RULES["abyssWeekdays"])
    return "\n".join([
        "🐲 어비스 보스",
        "",
        f"⏰ {days} {int(BOSS_RULES['abyssHour']):02d}:{int(BOSS_RULES['abyssMinute']):02d}",
    ])

def _date_clock(dt):
    return f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}"

async def format_agro_schedule():
    await refresh_boss_rules()
    anchor = await latest_maintenance_anchor()
    targets = agro_targets(anchor, count=4)

    lines = ["🐲 정령왕 아그로", ""]
    for target in targets:
        lines.append(f"⏰ {_date_clock(target)}")

    lines += [
        "",
        f"기준 : 최근 점검 종료 {_date_clock(anchor)}",
        f"주기 : {int(BOSS_RULES['agroIntervalHours'])}시간",
    ]
    return "\n".join(lines)

async def format_all_core_bosses():
    await refresh_boss_rules()

    now = datetime.now(KST)
    anchor = await latest_maintenance_anchor()

    agro = next_agro_from_anchor(anchor, now)
    kaira = _next_daily_times(_kaira_times(), now)
    nahma = _next_weekly(
        BOSS_RULES["nahmaWeekdays"],
        BOSS_RULES["nahmaHour"],
        BOSS_RULES["nahmaMinute"],
        now,
    )
    abyss = _next_weekly(
        BOSS_RULES["abyssWeekdays"],
        BOSS_RULES["abyssHour"],
        BOSS_RULES["abyssMinute"],
        now,
    )

    rows = [
        ("정령왕 아그로", agro),
        ("감시자 카이라", kaira),
        ("수호신장 나흐마", nahma),
        ("어비스 보스", abyss),
    ]
    rows.sort(key=lambda x: x[1])

    lines = ["🐲 필드보스", ""]
    for name, target in rows:
        lines.append(name)
        lines.append(f"⏰ {_date_clock(target)}")
        lines.append("")

    lines.append(f"아그로 기준 점검 종료 : {_date_clock(anchor)}")
    return "\n".join(lines)

def _format_weekly_content(command_name):
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    configs = {
        "시공": ("⚔️ 시공쟁탈전", BOSS_RULES["sigongWeekdays"], BOSS_RULES["sigongTimes"]),
        "균열": ("🟠 균열지대", BOSS_RULES["gyunyeolWeekdays"], BOSS_RULES["gyunyeolTimes"]),
        "아티": ("🟣 아티쟁", BOSS_RULES["atiWeekdays"], BOSS_RULES["atiTimes"]),
        "필드보스": ("🐲 필드보스", BOSS_RULES["fieldBossWeekdays"], BOSS_RULES["fieldBossTimes"]),
    }
    title, weekdays, times = configs[command_name]
    days = "/".join(weekday_names[int(x)] for x in weekdays)
    clocks = " / ".join(f"{int(h):02d}:{int(m):02d}" for h, m in times)
    return f"{title}\n\n⏰ {days} {clocks}"


def _schedule_alert_targets(now):
    """All boss/content schedules that receive 30-minute and 10-minute alerts."""
    targets = [
        ("boss", "정령왕 아그로", None),
        ("boss", "감시자 카이라", _next_daily_times(_kaira_times(), now)),
        ("boss", "수호신장 나흐마", _next_weekly(
            BOSS_RULES["nahmaWeekdays"], BOSS_RULES["nahmaHour"], BOSS_RULES["nahmaMinute"], now
        )),
        ("boss", "어비스 보스", _next_weekly(
            BOSS_RULES["abyssWeekdays"], BOSS_RULES["abyssHour"], BOSS_RULES["abyssMinute"], now
        )),
        ("content", "시공쟁탈전", _next_weekly_multi(BOSS_RULES["sigongWeekdays"], BOSS_RULES["sigongTimes"], now)),
        ("content", "균열지대", _next_weekly_multi(BOSS_RULES["gyunyeolWeekdays"], BOSS_RULES["gyunyeolTimes"], now)),
        ("content", "아티쟁", _next_weekly_multi(BOSS_RULES["atiWeekdays"], BOSS_RULES["atiTimes"], now)),
        ("boss", "필드보스", _next_weekly_multi(BOSS_RULES["fieldBossWeekdays"], BOSS_RULES["fieldBossTimes"], now)),
    ]
    return targets


async def _manual_schedule_command(name, clock_text):
    if name == "필보":
        name = "필드보스"
    parsed = _parse_manual_clock(clock_text)
    if parsed is None:
        return "⚠️ 시간 형식은 21:00 또는 21시처럼 입력해주세요."
    hour, minute = parsed

    # Maintenance-based schedules must bind to the currently confirmed
    # maintenance source before a manual correction is stored.
    if name in ("아그로", "어비스", "어비스보스", "시공", "균영", "균열", "균열지대", "아티", "아티쟁", "필드보스"):
        try:
            await latest_maintenance_anchor()
        except Exception:
            pass

    if not _set_manual_schedule(name, hour, minute):
        return "⚠️ 시간 수정 저장 실패"

    # Return the same normal schedule view immediately after correction.
    if name == "아그로":
        return "✅ 아그로 시간 수정 완료\n\n" + await format_agro_schedule()
    if name == "카이라":
        return "✅ 카이라 시간 수정 완료\n\n" + format_kaira_schedule()
    if name == "나흐마":
        return "✅ 나흐마 시간 수정 완료\n\n" + format_nahma_schedule()
    if name in ("어비스", "어비스보스"):
        return "✅ 어비스 시간 수정 완료\n\n" + format_abyss_schedule()
    if name == "시공":
        return "✅ 시공 시간 수정 완료\n\n" + _format_weekly_content("시공")
    if name in ("균영", "균열", "균열지대"):
        return "✅ 균열지대 시간 수정 완료\n\n" + _format_weekly_content("균열")
    if name in ("아티", "아티쟁"):
        return "✅ 아티쟁 시간 수정 완료\n\n" + _format_weekly_content("아티")
    if name == "필드보스":
        return "✅ 필드보스 시간 수정 완료\n\n" + _format_weekly_content("필드보스")
    return "⚠️ 지원하지 않는 일정입니다."

async def field_boss_lookup(query=None):
    await refresh_boss_rules()

    q = normalize_boss_query(query)

    if not q:
        return await format_all_core_bosses()

    if q in ("카이라", "감시자카이라"):
        return format_kaira_schedule()

    if q in ("나흐마", "수호신장나흐마", "분노한수호신장나흐마"):
        return format_nahma_schedule()

    if q in ("어비스", "어비스보스"):
        return format_abyss_schedule()

    if q in ("아그로", "정령왕아그로", "집행자아그로"):
        return await format_agro_schedule()

    if q in ("시공", "시공쟁탈전"):
        return _format_weekly_content("시공")
    if q in ("균영", "균열", "균열지대"):
        return _format_weekly_content("균열")
    if q in ("아티", "아티쟁"):
        return _format_weekly_content("아티")
    if q == "필드보스":
        return _format_weekly_content("필드보스")

    try:
        cache = await fetch_field_boss_cache()
        return format_one_boss(cache, query)
    except Exception:
        return f"🐲 {query}\n\n현재 자동 일정이 등록되지 않은 보스입니다."


# =========================================================
# Official AION2 boards
# =========================================================

COMMUNITY_API = "https://api-community.plaync.com/aion2/board"

BOARD_CONFIGS = {
    "공지": {
        "alias": "notice_ko",
        "label": "공지",
        "view": "notice",
    },
    "CM": {
        "alias": "cm_story_ko",
        "label": "CM",
        "view": "cm_story",
    },
    "업데이트": {
        "alias": "update_ko",
        "label": "업데이트",
        "view": "update",
    },
}

def _parse_board_post_datetime(value):
    """Best-effort PlayNC post timestamp parser used for pinned-post ordering."""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=KST)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,16}(?:\.\d+)?", text):
        try:
            number = float(text)
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=KST)
        except Exception:
            pass
    iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        pass
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=KST)
        except Exception:
            continue
    return None


async def fetch_board_latest(command: str, limit: int = 5):
    config = BOARD_CONFIGS[command]
    alias = config["alias"]

    url = f"{COMMUNITY_API}/{alias}/article/search/moreArticle"

    # Notices can be preceded by fixed/pinned posts. Always request a wider
    # notice window, then sort by publication time before applying `limit`.
    requested_limit = max(1, int(limit or 5))
    request_size = 50 if command == "공지" else max(18, min(50, requested_limit))

    client = await get_http_client()
    response = await client.get(
        url,
        params={
            "isVote": "true",
            "moreSize": str(request_size),
            "moreDirection": "BEFORE",
            "previousArticleId": "0",
        },
        headers=PLAYNC_HEADERS,
        timeout=httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=2.0),
    )
    response.raise_for_status()
    data = response.json()

    content_list = []
    if isinstance(data, dict):
        if isinstance(data.get("contentList"), list):
            content_list = data.get("contentList") or []
        elif isinstance(data.get("result"), dict) and isinstance(data["result"].get("contentList"), list):
            content_list = data["result"].get("contentList") or []
        elif isinstance(data.get("data"), dict) and isinstance(data["data"].get("contentList"), list):
            content_list = data["data"].get("contentList") or []

    rows = []
    for item in content_list:
        if not isinstance(item, dict):
            continue

        snow = item.get("snow") or {}
        content_id = snow.get("contentId") or item.get("contentId") or item.get("articleId")
        title = str(item.get("title") or "").strip()
        timestamps = item.get("timestamps") or {}
        posted = timestamps.get("postDateTime") or item.get("postDateTime") or ""

        if not content_id or not title:
            continue

        parsed_posted = _parse_board_post_datetime(posted)
        date_text = parsed_posted.strftime("%Y-%m-%d") if parsed_posted is not None else (str(posted)[:10] if posted else "")
        link = (
            f"https://aion2.plaync.com/ko-kr/board/"
            f"{config['view']}/view?articleId={content_id}"
        )

        rows.append({
            "id": str(content_id),
            "title": title,
            "date": date_text,
            "postedAt": parsed_posted.isoformat() if parsed_posted is not None else str(posted or ""),
            "link": link,
            "rawText": json.dumps(item, ensure_ascii=False),
        })

    def _sort_epoch(row):
        dt = _parse_board_post_datetime((row or {}).get("postedAt"))
        if dt is not None:
            return dt.timestamp()
        try:
            return datetime.strptime(str((row or {}).get("date") or "")[:10], "%Y-%m-%d").replace(tzinfo=KST).timestamp()
        except Exception:
            return 0.0

    rows.sort(key=_sort_epoch, reverse=True)
    return rows[:requested_limit]

NOTICE_ALERT_CLASSIFIER_VERSION = "notice-v3-20260908"
NOTICE_RECOVERY_VERSION = "notice-recovery-v3-20260908"


def _classify_notice_kind(title):
    """Return maintenance/live for notice posts we actually alert on."""
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    lowered = title_text.casefold()
    compact = re.sub(r"[^0-9a-z가-힣]+", "", lowered)

    # Maintenance has priority if a title contains both kinds of words.
    if "점검" in title_text or "maintenance" in lowered:
        return "maintenance"

    live_markers = (
        "라이브",
        "생방송",
        "생중계",
        "방송",
        "live",
        "on air",
        "onair",
        "streaming",
        "stream",
        "쇼케이스",
        "showcase",
    )
    for marker in live_markers:
        marker_low = marker.casefold()
        if marker_low in lowered or re.sub(r"[^0-9a-z가-힣]+", "", marker_low) in compact:
            return "live"

    return None


def _notice_header(kind):
    if kind == "maintenance":
        return "🔧 AION2 점검 공지"
    if kind == "live":
        return "🔴 AION2 라이브 공지"
    return "📢 AION2 공지"


def _board_post_is_recent(post, now=None, max_hours=36):
    """Best-effort freshness check used only for one-time alert recovery."""
    if not isinstance(post, dict):
        return False
    current = now or datetime.now(KST)
    posted = _parse_kst_iso(post.get("postedAt"))
    if posted is not None:
        age = (current - posted).total_seconds() / 3600.0
        return -1.0 <= age <= float(max_hours)
    date_text = str(post.get("date") or "").strip()
    try:
        posted_date = datetime.strptime(date_text[:10], "%Y-%m-%d").date()
        delta_days = (current.date() - posted_date).days
        return 0 <= delta_days <= 1
    except Exception:
        return False


def format_board_latest(command: str, rows):
    if command == "공지":
        matched = []
        for row in rows:
            kind = _classify_notice_kind(row.get("title") if isinstance(row, dict) else "")
            if kind:
                matched.append((row, kind))

        if not matched:
            return "📢 AION2 공지\n\n현재 확인되는 점검/라이브 공지가 없습니다."

        row, kind = matched[0]
        return "\n".join([
            _notice_header(kind),
            "",
            row["title"],
            "",
            "🔗 공식 공지",
            row["link"],
        ])

    if command == "CM":
        if not rows:
            return "📢 AION2 CM\n\n최신 CM 글이 없습니다."
        row = rows[0]
        return "\n".join([
            "📢 AION2 CM",
            "",
            row["title"],
            "",
            "🔗 바로 보기",
            row["link"],
        ])

    if command == "업데이트":
        if not rows:
            return "🆕 AION2 업데이트\n\n최신 업데이트가 없습니다."
        row = rows[0]
        return "\n".join([
            "🆕 AION2 업데이트",
            "",
            row["title"],
            "",
            "🔗 바로 보기",
            row["link"],
        ])

    return ""


async def board_lookup(command: str):
    cache_key = f"board:{command}"
    # !공지 must reflect the current notice list immediately; do not serve stale
    # maintenance/live classification from cache. CM/update keep their short cache.
    if command != "공지":
        cached = cache_get(cache_key, 300)
        if cached:
            return cached

    rows = await fetch_board_latest(command, limit=50 if command == "공지" else 5)
    result = format_board_latest(command, rows)
    cache_set(cache_key, result)
    return result




# =========================================================
# Tablet PWA launcher
# =========================================================
PWA_APP_VERSION = "V11 ALERT RETRY FIX"
PWA_HOME_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0c1120">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="AION2 TOOL">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/pwa/icon-192.png">
<title>AION2 TOOL</title>
<style>
*{box-sizing:border-box}
:root{color-scheme:dark;--bg:#0c1120;--card:#151d31;--card2:#10182a;--line:#2a3550;--text:#f4f7ff;--muted:#96a3bb;--blue:#4aa3ff;--purple:#8b78ff;--good:#48d9a7}
html,body{margin:0;min-height:100%;background:radial-gradient(circle at 80% 0,#17234a 0,transparent 34%),var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif;color:var(--text)}
body{padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
.wrap{width:min(1100px,100%);margin:0 auto;padding:22px}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:13px}.logo{width:54px;height:54px;border-radius:16px;background:linear-gradient(145deg,#1d2d56,#171d33);border:1px solid #5f63da;display:grid;place-items:center;font-weight:900;font-size:21px;box-shadow:0 12px 30px #0007}
h1{font-size:24px;margin:0}.sub{font-size:13px;color:var(--muted);margin-top:4px}
.topactions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}.versionbadge{border:1px solid #34415f;background:#10182a;color:#9fb2d3;border-radius:999px;padding:7px 9px;font-size:10px;font-weight:800}.install{border:1px solid #5966a6;background:#1a2440;color:#fff;border-radius:12px;padding:11px 15px;font-weight:700;cursor:pointer}.updatebtn{display:none;border:1px solid #2f8f74;background:#123229;color:#dfffee;border-radius:12px;padding:11px 13px;font-weight:800;cursor:pointer}
.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:16px}.card{background:linear-gradient(180deg,#161f34,#11182a);border:1px solid var(--line);border-radius:18px;padding:17px;box-shadow:0 16px 45px #0004}.card h2{font-size:16px;margin:0 0 13px}.search{display:grid;grid-template-columns:1fr 150px auto;gap:9px}input,select{width:100%;border:1px solid #34415f;background:#0d1424;color:#fff;border-radius:12px;padding:13px;font-size:16px;outline:none}input:focus,select:focus{border-color:var(--blue)}
.dashboard{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}.dashitem{min-height:105px;border:1px solid #2f3b5a;background:linear-gradient(160deg,#141e34,#0f1729);border-radius:15px;padding:13px;cursor:pointer}.dashitem:active{transform:scale(.99)}.dashtitle{font-size:11px;color:#8fa0bc;margin-bottom:8px}.dashvalue{font-size:17px;font-weight:850;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.dashsub{font-size:11px;color:#8493ad;margin-top:7px;line-height:1.35;min-height:28px}.countdown{font-variant-numeric:tabular-nums;letter-spacing:.2px}.favbosschips{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px}.favbosschip{border:1px solid #354363;background:#10192b;color:#cbd7ef;border-radius:999px;padding:4px 7px;font-size:9px;line-height:1.1}.dashgood{color:#74e8bd}.dashwarn{color:#ffd479}.dashbad{color:#ff9aa9}.favtools{display:flex;gap:6px;margin-top:8px}.favmini{flex:1;border:1px solid #354363;background:#111a2e;color:#eaf0ff;border-radius:8px;padding:6px 7px;font-size:10px;font-weight:750;cursor:pointer}
.primary{border:0;background:linear-gradient(135deg,var(--blue),var(--purple));color:white;border-radius:12px;padding:0 17px;font-weight:800;font-size:15px;cursor:pointer}.actions{display:flex;gap:9px;margin-top:10px}.ghost{flex:1;border:1px solid #3a4868;background:#111a2e;color:#eaf0ff;border-radius:11px;padding:11px;font-weight:700;cursor:pointer}
.section{margin-top:16px}.buttons{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.btn{border:1px solid #34415e;background:#121b2e;color:#eef3ff;border-radius:12px;padding:13px 8px;font-size:14px;font-weight:750;cursor:pointer;min-height:48px}.btn:active,.ghost:active,.primary:active{transform:scale(.985)}.btn.feature{border-color:#4d5b93;background:#182341}.btn.news{border-color:#405f64;background:#13282d}
.result{min-height:290px;white-space:pre-wrap;word-break:break-word;background:#0a101d;border:1px solid #28334c;border-radius:14px;padding:15px;color:#e9eefb;font-size:14px;line-height:1.55;overflow:auto}.result a{color:#6bbcff}.status{font-size:12px;color:var(--muted);margin-top:9px}.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#b9c6dc}.dot{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 12px var(--good)}
.notifyrow{display:flex;align-items:center;justify-content:space-between;gap:12px}.notifystate{display:flex;align-items:center;gap:8px;font-size:13px;color:#c9d3e8}.notifydot{width:9px;height:9px;border-radius:50%;background:#6b7280;box-shadow:none}.notifydot.on{background:var(--good);box-shadow:0 0 12px var(--good)}.notifyactions{display:flex;gap:8px;flex-wrap:wrap}.notifybtn{border:1px solid #3d4b6c;background:#121b2f;color:#eef3ff;border-radius:11px;padding:10px 13px;font-weight:750;cursor:pointer}.notifybtn.on{border-color:#2f8f74;background:#123229}.notifybtn.test{border-color:#5365a0;background:#182342}.notifybtn.off{border-color:#70454d;background:#2b171b}.notifyhelp{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.5}
.notifytools{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.notifytools .notifybtn{padding:8px 11px;font-size:12px}.settingsbox{display:none;margin-top:12px;padding:13px;border:1px solid #2d3a58;border-radius:13px;background:#0d1525}.settingsbox.open{display:block}.settingshead{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px}.settingshead b{font-size:13px}.settinglabel{font-size:12px;color:#aebbd1;margin:11px 0 7px}.checkgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.checkchip{display:flex;align-items:center;gap:7px;border:1px solid #34415e;background:#121b2e;border-radius:10px;padding:9px 10px;font-size:12px;color:#eef3ff}.checkchip input{width:auto;margin:0;accent-color:#5a8cff}.saveprefs{width:100%;margin-top:12px;border:0;background:linear-gradient(135deg,var(--blue),var(--purple));color:#fff;border-radius:11px;padding:11px;font-weight:800}.nextline{font-size:11px;color:#8090aa;margin-top:8px}@media(max-width:600px){.checkgrid{grid-template-columns:repeat(2,1fr)}}
.timelinecard{margin-bottom:16px}.timelinehead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:11px}.timelinehead h2{margin:0}.timelineactions{display:flex;gap:6px}.tinybtn{border:1px solid #354363;background:#10192b;color:#dbe5f8;border-radius:9px;padding:7px 10px;font-size:10px;font-weight:800;cursor:pointer}.timeline{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.timelineitem{border:1px solid #2e3b59;background:#0e1728;border-radius:12px;padding:10px 11px;cursor:pointer}.timelineitem.off{opacity:.48}.timelinetop{display:flex;justify-content:space-between;gap:8px;align-items:center}.timelinename{font-size:12px;font-weight:850;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.timelineclock{font-size:10px;color:#8fa0bc}.timelinecount{font-size:15px;font-weight:900;margin-top:7px;font-variant-numeric:tabular-nums}.timelinelead{font-size:9px;color:#7f8da8;margin-top:4px}.backupinput{display:none}@media(max-width:800px){.timeline{grid-template-columns:repeat(2,1fr)}}@media(max-width:480px){.timeline{grid-template-columns:1fr 1fr}.timelineitem{padding:9px}.timelinecount{font-size:13px}}
.footer{text-align:center;color:#6f7e99;font-size:11px;padding:18px 0 3px}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#202a44;border:1px solid #4a587c;border-radius:12px;padding:11px 15px;box-shadow:0 12px 40px #0008;display:none;z-index:50}
@media(max-width:800px){.wrap{padding:15px}.dashboard{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.search{grid-template-columns:1fr 120px}.search .primary{grid-column:1/-1;height:46px}.buttons{grid-template-columns:repeat(3,1fr)}.top{align-items:flex-start}.install{padding:10px 12px}.result{min-height:220px}}
@media(max-width:480px){.dashboard{grid-template-columns:1fr 1fr}.dashitem{min-height:96px}.buttons{grid-template-columns:repeat(2,1fr)}h1{font-size:21px}.logo{width:48px;height:48px}.sub{max-width:230px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand"><div class="logo">A2</div><div><h1>AION2 TOOL</h1><div class="sub">갤럭시탭 + 카카오 통합 · AION2 운영 도구</div></div></div>
    <div class="topactions"><span id="versionBadge" class="versionbadge">V9 FINAL</span><button id="updateBtn" class="updatebtn">새 버전 적용</button><button id="installBtn" class="install">앱 설치</button></div>
  </div>

  <div class="dashboard">
    <div id="dashNext" class="dashitem">
      <div class="dashtitle">⏱️ 다음 일정</div>
      <div id="dashNextName" class="dashvalue">불러오는 중…</div>
      <div id="dashNextTime" class="dashsub">알림 일정을 확인합니다.</div>
    </div>
    <div id="dashFavBossCard" class="dashitem">
      <div class="dashtitle">⭐ 즐겨찾기 보스</div>
      <div id="dashFavBossName" class="dashvalue">불러오는 중…</div>
      <div id="dashFavBossTime" class="dashsub countdown">즐겨찾기 일정을 확인합니다.</div>
      <div id="dashFavBossChips" class="favbosschips"></div>
      <div class="favtools"><button id="favBossCfgBtn" class="favmini">설정</button><button id="favBossOpenBtn" class="favmini">바로 보기</button></div>
    </div>
    <div id="dashHealthCard" class="dashitem">
      <div class="dashtitle">🛡️ 알림 감시</div>
      <div id="dashHealth" class="dashvalue">확인 중…</div>
      <div id="dashHealthSub" class="dashsub">외부 1분 체크 상태</div>
    </div>
    <div id="dashPushCard" class="dashitem">
      <div class="dashtitle">🔔 이 태블릿 알림</div>
      <div id="dashPush" class="dashvalue">확인 중…</div>
      <div id="dashPushSub" class="dashsub">푸시 구독 상태</div>
    </div>
    <div id="dashKakaoCard" class="dashitem">
      <div class="dashtitle">💬 카카오 연동</div>
      <div id="dashKakao" class="dashvalue">확인 중…</div>
      <div id="dashKakaoSub" class="dashsub">카카오 폴링 상태</div>
    </div>
    <div id="dashRecent" class="dashitem">
      <div class="dashtitle">🧾 최근 알림</div>
      <div id="dashRecentTitle" class="dashvalue">아직 없음</div>
      <div id="dashRecentTime" class="dashsub">전송 기록을 확인합니다.</div>
    </div>
    <div class="dashitem">
      <div class="dashtitle">⭐ 즐겨찾기 캐릭터</div>
      <div id="dashFav" class="dashvalue">윤이 · 지켈</div>
      <div class="favtools"><button id="favSaveBtn" class="favmini">현재 저장</button><button id="favOpenBtn" class="favmini">바로 조회</button></div>
    </div>
  </div>

  <div class="card timelinecard">
    <div class="timelinehead"><h2>📅 다가오는 일정</h2><div class="timelineactions"><button id="timelineRefreshBtn" class="tinybtn">새로고침</button></div></div>
    <div id="timelineList" class="timeline"><div class="timelineitem"><div class="timelinename">일정 불러오는 중…</div></div></div>
  </div>

  <div class="grid">
    <div>
      <div class="card">
        <h2>⚔️ 캐릭터</h2>
        <div class="search">
          <input id="charName" placeholder="캐릭터명" autocomplete="off" value="윤이">
          <input id="serverName" placeholder="서버" value="지켈" autocomplete="off">
          <button class="primary" id="detailBtn">상세 조회</button>
        </div>
        <div class="actions">
          <button class="ghost" id="rankingBtn">🏆 랭킹 조회</button>
          <button class="ghost" onclick="location.href='/compare'">⚖️ 캐릭터 비교</button>
        </div>
      </div>

      <div class="card section">
        <div class="notifyrow">
          <div>
            <h2 style="margin-bottom:7px">🔔 앱 알림</h2>
            <div class="notifystate"><span id="notifyDot" class="notifydot"></span><span id="notifyState">상태 확인 중…</span></div>
          </div>
          <div class="notifyactions">
            <button id="notifyOnBtn" class="notifybtn on">알림 켜기</button>
            <button id="notifyTestBtn" class="notifybtn test">테스트</button>
            <button id="notifyOffBtn" class="notifybtn off">끄기</button>
          </div>
        </div>
        <div class="notifyhelp">앱을 닫아도 필보·콘텐츠 30분 전·10분 전, 공지/CM/업데이트, 점검으로 인한 아그로 시간 변경을 갤럭시탭 알림으로 받습니다.</div>
        <div class="notifytools">
          <button id="notifySettingsBtn" class="notifybtn">⚙️ 알림 설정</button>
          <button id="notifyNextBtn" class="notifybtn">⏱️ 다음 알림</button>
          <button id="notifyHistoryBtn" class="notifybtn">🧾 알림 기록</button>
          <button id="notifyHealthBtn" class="notifybtn">🛡️ 시스템 상태</button>
          <button id="test30Btn" class="notifybtn test">30분 테스트</button>
          <button id="test10Btn" class="notifybtn test">10분 테스트</button>
          <button id="testAgroBtn" class="notifybtn test">아그로 변경 테스트</button>
          <button id="fullCheckBtn" class="notifybtn">✅ 전체 자가진단</button>
          <button id="backupBtn" class="notifybtn">💾 설정 백업</button>
          <button id="restoreBtn" class="notifybtn">♻️ 설정 복원</button>
          <input id="restoreFile" class="backupinput" type="file" accept="application/json,.json">
        </div>
        <div id="notifySettingsBox" class="settingsbox">
          <div class="settingshead"><b>알림 세부 설정</b><span style="font-size:11px;color:#7f8da8">이 태블릿용 PWA 설정</span></div>
          <div class="settinglabel">알림 시점</div>
          <div class="checkgrid">
            <label class="checkchip"><input id="lead30" type="checkbox" checked>30분 전</label>
            <label class="checkchip"><input id="lead10" type="checkbox" checked>10분 전</label>
          </div>
          <div class="settinglabel">필보 / 콘텐츠</div>
          <div class="checkgrid">
            <label class="checkchip"><input data-schedule="agro" type="checkbox" checked>아그로</label>
            <label class="checkchip"><input data-schedule="kaira" type="checkbox" checked>카이라</label>
            <label class="checkchip"><input data-schedule="nahma" type="checkbox" checked>나흐마</label>
            <label class="checkchip"><input data-schedule="abyss" type="checkbox" checked>어비스</label>
            <label class="checkchip"><input data-schedule="sigong" type="checkbox" checked>시공</label>
            <label class="checkchip"><input data-schedule="gyunyeol" type="checkbox" checked>균열</label>
            <label class="checkchip"><input data-schedule="ati" type="checkbox" checked>아티</label>
            <label class="checkchip"><input data-schedule="fieldboss" type="checkbox" checked>필드보스</label>
          </div>
          <div class="settinglabel">소식 / 변경</div>
          <div class="checkgrid">
            <label class="checkchip"><input data-board="공지" type="checkbox" checked>공지</label>
            <label class="checkchip"><input data-board="CM" type="checkbox" checked>CM</label>
            <label class="checkchip"><input data-board="업데이트" type="checkbox" checked>업데이트</label>
            <label class="checkchip"><input id="maintenanceAgro" type="checkbox" checked>아그로 변경</label>
          </div>
          <button id="saveNotifyPrefs" class="saveprefs">설정 저장</button>
          <div class="nextline">설정 저장 후부터 새 알림에 적용됩니다.</div>
        </div>
      </div>

      <div class="card section">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px"><h2 style="margin:0">🐲 필드보스 / 콘텐츠</h2><button id="favBossInlineBtn" class="notifybtn" style="padding:7px 10px;font-size:11px">⭐ 즐겨찾기 설정</button></div>
        <div id="favBossSettingsBox" class="settingsbox">
          <div class="settingshead"><b>홈 즐겨찾기 보스</b><span style="font-size:11px;color:#7f8da8">최대 4개</span></div>
          <div class="checkgrid">
            <label class="checkchip"><input data-favboss="agro" type="checkbox">아그로</label>
            <label class="checkchip"><input data-favboss="kaira" type="checkbox">카이라</label>
            <label class="checkchip"><input data-favboss="nahma" type="checkbox">나흐마</label>
            <label class="checkchip"><input data-favboss="abyss" type="checkbox">어비스</label>
            <label class="checkchip"><input data-favboss="sigong" type="checkbox">시공</label>
            <label class="checkchip"><input data-favboss="gyunyeol" type="checkbox">균열</label>
            <label class="checkchip"><input data-favboss="ati" type="checkbox">아티</label>
            <label class="checkchip"><input data-favboss="fieldboss" type="checkbox">필드보스</label>
          </div>
          <button id="saveFavBoss" class="saveprefs">즐겨찾기 저장</button>
          <div class="nextline">선택한 일정 중 가장 가까운 일정이 홈에서 초 단위로 표시됩니다.</div>
        </div>
        <div class="buttons" style="margin-top:13px">
          <button class="btn feature" data-cmd="필보">필보 전체</button>
          <button class="btn" data-cmd="아그로">아그로</button>
          <button class="btn" data-cmd="카이라">카이라</button>
          <button class="btn" data-cmd="나흐마">나흐마</button>
          <button class="btn" data-cmd="어비스">어비스</button>
          <button class="btn" data-cmd="시공">시공</button>
          <button class="btn" data-cmd="균열">균열</button>
          <button class="btn" data-cmd="아티">아티</button>
        </div>
      </div>

      <div class="card section">
        <h2>📢 소식 / 파티편성</h2>
        <div class="buttons">
          <button class="btn news" data-cmd="공지">공지</button>
          <button class="btn news" data-cmd="CM">CM</button>
          <button class="btn news" data-cmd="업데이트">업데이트</button>
          <button class="btn" data-open="/party-card/무스펠">무스펠</button>
          <button class="btn" data-open="/party-card/성역3">성역3</button>
          <button class="btn" data-open="/party-card/성역4">성역4 / 비탄</button>
          <button class="btn" data-cmd="인원">인원표</button>
          <button class="btn" data-cmd="도움">전체 기능</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:13px"><h2 style="margin:0">결과</h2><span class="pill"><span class="dot"></span>서버 연결</span></div>
      <div id="result" class="result">버튼을 누르면 여기에 결과가 표시됩니다.</div>
      <div id="status" class="status">AION2 TOOL · Tablet PWA</div>
    </div>
  </div>
  <div class="footer">PWA 직접 실행 + 카카오 연동 · 동일 AION2 서버/알림 엔진 사용</div>
</div>
<div id="toast" class="toast"></div>
<script>
const $=id=>document.getElementById(id), result=$('result'), statusEl=$('status');
function toast(msg){const el=$('toast');el.textContent=msg;el.style.display='block';setTimeout(()=>el.style.display='none',2400)}
function renderText(text){
  result.textContent='';
  const re=/(https?:\/\/[^\s]+)/g; let pos=0;
  for(const m of text.matchAll(re)){
    result.append(document.createTextNode(text.slice(pos,m.index)));
    const a=document.createElement('a'); a.href=m[0]; a.textContent=m[0]; a.target='_blank'; a.rel='noopener'; result.append(a);
    pos=m.index+m[0].length;
  }
  result.append(document.createTextNode(text.slice(pos)));
}
async function run(cmd){
  statusEl.textContent='조회 중…'; result.textContent='불러오는 중…';
  try{
    const r=await fetch('/openchat?msg='+encodeURIComponent('!'+cmd)+'&room='+encodeURIComponent('AION2 TOOL'),{cache:'no-store'});
    const t=await r.text(); renderText(t); statusEl.textContent='완료 · '+new Date().toLocaleTimeString('ko-KR');
  }catch(e){result.textContent='조회 실패\n'+(e?.message||e);statusEl.textContent='연결 오류'}
}
document.querySelectorAll('[data-cmd]').forEach(b=>b.addEventListener('click',()=>run(b.dataset.cmd)));
document.querySelectorAll('[data-open]').forEach(b=>b.addEventListener('click',()=>location.href=b.dataset.open));
$('detailBtn').onclick=()=>{const n=$('charName').value.trim(),s=$('serverName').value.trim();if(!n||!s)return toast('캐릭터명과 서버를 입력해 주세요.');location.href='/detail?name='+encodeURIComponent(n)+'&server='+encodeURIComponent(s)};
$('rankingBtn').onclick=()=>{const n=$('charName').value.trim(),s=$('serverName').value.trim();if(!n)return toast('캐릭터명을 입력해 주세요.');run('랭킹 '+n+(s||''))};
['charName','serverName'].forEach(id=>$(id).addEventListener('keydown',e=>{if(e.key==='Enter')$('detailBtn').click()}));

const FAV_CHARACTER_KEY='aion2.tool.favorite.character.v1';
const FAV_BOSS_KEY='aion2.tool.favorite.bosses.v1';
const BOSS_META={
  agro:{name:'정령왕 아그로',short:'아그로',cmd:'아그로'},
  kaira:{name:'감시자 카이라',short:'카이라',cmd:'카이라'},
  nahma:{name:'수호신장 나흐마',short:'나흐마',cmd:'나흐마'},
  abyss:{name:'어비스 보스',short:'어비스',cmd:'어비스'},
  sigong:{name:'시공쟁탈전',short:'시공',cmd:'시공'},
  gyunyeol:{name:'균열지대',short:'균열',cmd:'균열'},
  ati:{name:'아티쟁',short:'아티',cmd:'아티'},
  fieldboss:{name:'필드보스',short:'필드보스',cmd:'필보'}
};
let latestNextItems=[];
let dashNextTargetIso='';
let dashFavBossTargetIso='';
let timelineRows=[];
let dashFavBossKey='';
function scheduleKeyFromName(name){for(const [k,v] of Object.entries(BOSS_META)){if(v.name===name)return k}return ''}
function getFavoriteBosses(){
  try{const x=JSON.parse(localStorage.getItem(FAV_BOSS_KEY)||'null');if(Array.isArray(x)&&x.length)return x.filter(k=>BOSS_META[k]).slice(0,4)}catch(e){}
  return ['agro','fieldboss','kaira'];
}
function renderFavBossChecks(){const selected=new Set(getFavoriteBosses());document.querySelectorAll('[data-favboss]').forEach(el=>el.checked=selected.has(el.dataset.favboss));renderFavBossChips();}
function renderFavBossChips(){const el=$('dashFavBossChips');if(!el)return;el.innerHTML='';for(const k of getFavoriteBosses()){const chip=document.createElement('span');chip.className='favbosschip';chip.textContent=BOSS_META[k].short;el.append(chip)}}
function saveFavoriteBosses(){
  const selected=[...document.querySelectorAll('[data-favboss]:checked')].map(el=>el.dataset.favboss);
  if(!selected.length)return toast('즐겨찾기 보스를 1개 이상 선택해 주세요.');
  if(selected.length>4)return toast('즐겨찾기는 최대 4개까지 가능합니다.');
  localStorage.setItem(FAV_BOSS_KEY,JSON.stringify(selected));renderFavBossChips();updateFavoriteBossDashboard();$('favBossSettingsBox').classList.remove('open');toast('즐겨찾기 보스를 저장했습니다.');
}
function formatCountdown(iso){
  if(!iso)return '';
  const ms=new Date(iso).getTime()-Date.now(); if(!Number.isFinite(ms))return '';
  const total=Math.max(0,Math.floor(ms/1000)),d=Math.floor(total/86400),h=Math.floor(total%86400/3600),m=Math.floor(total%3600/60),sec=total%60;
  if(d>0)return d+'일 '+String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0');
  return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0');
}
function updateFavoriteBossDashboard(){
  const fav=new Set(getFavoriteBosses());
  const rows=latestNextItems.filter(x=>x.enabled!==false&&fav.has(scheduleKeyFromName(x.name))).sort((a,b)=>(a.targetTs||0)-(b.targetTs||0));
  const x=rows[0]; renderFavBossChips();
  if(!x){$('dashFavBossName').textContent='예정 없음';$('dashFavBossTime').textContent='즐겨찾기 일정이 없습니다.';dashFavBossTargetIso='';dashFavBossKey='';return}
  dashFavBossKey=scheduleKeyFromName(x.name);dashFavBossTargetIso=x.targetIso||'';$('dashFavBossName').textContent=BOSS_META[dashFavBossKey]?.short||x.name;
  $('dashFavBossTime').textContent=(x.time||'')+(dashFavBossTargetIso?' · '+formatCountdown(dashFavBossTargetIso):'');
}
function renderTimeline(){
  const box=$('timelineList'); if(!box)return;
  timelineRows=latestNextItems.slice().sort((a,b)=>(a.targetTs||0)-(b.targetTs||0)).slice(0,6);
  if(!timelineRows.length){box.innerHTML='<div class="timelineitem"><div class="timelinename">예정된 일정 없음</div></div>';return;}
  box.innerHTML=timelineRows.map((x,i)=>{
    const key=x.key||scheduleKeyFromName(x.name);
    const leads=(x.leads||[]).map(v=>v+'분').join('·');
    return '<div class="timelineitem '+(x.enabled===false?'off':'')+'" data-ti="'+i+'" data-key="'+(key||'')+'"><div class="timelinetop"><div class="timelinename">'+(x.enabled===false?'🔕 ':'')+(BOSS_META[key]?.short||x.name||'일정')+'</div><div class="timelineclock">'+(x.time||'')+'</div></div><div class="timelinecount" data-target="'+(x.targetIso||'')+'">'+formatCountdown(x.targetIso||'')+'</div><div class="timelinelead">'+(leads?('알림 '+leads):'알림 설정 없음')+'</div></div>';
  }).join('');
  box.querySelectorAll('[data-ti]').forEach(el=>el.onclick=()=>{const x=timelineRows[Number(el.dataset.ti)||0];const key=el.dataset.key||scheduleKeyFromName(x?.name);if(BOSS_META[key])run(BOSS_META[key].cmd);else showNextAlerts();});
}
function tickDashboardCountdowns(){
  if(dashNextTargetIso){const x=latestNextItems.find(v=>v.targetIso===dashNextTargetIso);$('dashNextTime').textContent=(x?.time||'')+' · '+formatCountdown(dashNextTargetIso)+' 후'+((x?.leads||[]).length?' · '+x.leads.map(v=>v+'분 전').join(' / '):'')}
  if(dashFavBossTargetIso){const x=latestNextItems.find(v=>v.targetIso===dashFavBossTargetIso);$('dashFavBossTime').textContent=(x?.time||'')+' · '+formatCountdown(dashFavBossTargetIso)+' 후'}
  document.querySelectorAll('.timelinecount[data-target]').forEach(el=>{el.textContent=formatCountdown(el.dataset.target)+' 후';});
}

function getFavoriteCharacter(){
  try{const x=JSON.parse(localStorage.getItem(FAV_CHARACTER_KEY)||'null');if(x&&x.name&&x.server)return x}catch(e){}
  return {name:$('charName').value.trim()||'윤이',server:$('serverName').value.trim()||'지켈'};
}
function renderFavoriteCharacter(){
  const x=getFavoriteCharacter();$('dashFav').textContent=x.name+' · '+x.server;return x;
}
$('favSaveBtn').onclick=()=>{
  const name=$('charName').value.trim(),server=$('serverName').value.trim();
  if(!name||!server)return toast('캐릭터명과 서버를 입력해 주세요.');
  localStorage.setItem(FAV_CHARACTER_KEY,JSON.stringify({name,server}));renderFavoriteCharacter();toast('즐겨찾기 캐릭터를 저장했습니다.');
};
$('favOpenBtn').onclick=()=>{const x=getFavoriteCharacter();location.href='/detail?name='+encodeURIComponent(x.name)+'&server='+encodeURIComponent(x.server)};
$('favBossCfgBtn').onclick=()=>{$('favBossSettingsBox').classList.toggle('open');renderFavBossChecks();$('favBossSettingsBox').scrollIntoView({behavior:'smooth',block:'center'});};
$('favBossInlineBtn').onclick=()=>{$('favBossSettingsBox').classList.toggle('open');renderFavBossChecks();};
$('saveFavBoss').onclick=saveFavoriteBosses;
$('favBossOpenBtn').onclick=()=>{if(dashFavBossKey&&BOSS_META[dashFavBossKey])run(BOSS_META[dashFavBossKey].cmd);else run('필보')};
$('dashFavBossCard').onclick=e=>{if(e.target.closest('button'))return;if(dashFavBossKey&&BOSS_META[dashFavBossKey])run(BOSS_META[dashFavBossKey].cmd)};
renderFavBossChips();

async function refreshDashboard(){
  try{
    const [nextRes,healthRes,historyRes,kakaoRes]=await Promise.all([
      fetch('/api/push/next',{cache:'no-store'}).then(r=>r.json()).catch(()=>({})),
      fetch('/api/push/diagnostics',{cache:'no-store'}).then(r=>r.json()).catch(()=>({})),
      fetch('/api/push/history',{cache:'no-store'}).then(r=>r.json()).catch(()=>({})),
      fetch('/api/kakao/status',{cache:'no-store'}).then(r=>r.json()).catch(()=>({}))
    ]);
    latestNextItems=(nextRes.items||[]).map(x=>Object.assign({},x,{targetTs:x.targetIso?new Date(x.targetIso).getTime():Date.now()+(Number(x.minutes)||0)*60000}));
    const next=latestNextItems.find(x=>x.enabled!==false)||latestNextItems[0];
    if(next){
      $('dashNextName').textContent=next.name||'다음 일정';
      dashNextTargetIso=next.targetIso||'';
      const leads=(next.leads||[]).map(v=>v+'분 전').join(' / ');
      $('dashNextTime').textContent=(next.time||'')+(dashNextTargetIso?' · '+formatCountdown(dashNextTargetIso)+' 후':(next.minutes!=null?' · '+next.minutes+'분 후':''))+(leads?' · '+leads:'');
    }else{
      dashNextTargetIso='';$('dashNextName').textContent='예정 없음';$('dashNextTime').textContent='현재 예정된 알림 일정이 없습니다.';
    }
    updateFavoriteBossDashboard();
    renderTimeline();
    const ok=!!healthRes.externalCronHealthy;
    $('dashHealth').textContent=ok?'정상 감시':'점검 필요';
    $('dashHealth').className='dashvalue '+(ok?'dashgood':'dashwarn');
    const ext=healthRes.lastExternalMinutes;
    $('dashHealthSub').textContent=(ext==null?'외부 체크 기록 없음':'외부 체크 '+ext+'분 전')+' · 등록 '+(healthRes.subscriptions||0)+'대';
    const activeKakao=Number(kakaoRes.activeRooms||0), totalKakao=Number(kakaoRes.rooms||0);
    if(activeKakao>0){$('dashKakao').textContent='연결';$('dashKakao').className='dashvalue dashgood';$('dashKakaoSub').textContent='활성 '+activeKakao+'방 · 등록 '+totalKakao+'방';}
    else if(totalKakao>0){$('dashKakao').textContent='대기';$('dashKakao').className='dashvalue dashwarn';$('dashKakaoSub').textContent='등록 '+totalKakao+'방 · 폰 봇 폴링 확인';}
    else{$('dashKakao').textContent='미등록';$('dashKakao').className='dashvalue dashwarn';$('dashKakaoSub').textContent='카카오 방에서 !로컬확인 실행';}
    const recent=(historyRes.items||[])[0];
    if(recent){$('dashRecentTitle').textContent=recent.title||'알림';$('dashRecentTime').textContent=(recent.time||'')+' · '+String(recent.body||'').slice(0,35)}
    else{$('dashRecentTitle').textContent='아직 없음';$('dashRecentTime').textContent='첫 자동 알림 전입니다.'}
  }catch(e){}
}
$('dashNext').onclick=()=>showNextAlerts();
$('dashHealthCard').onclick=()=>showPushHealth();
$('dashRecent').onclick=()=>showPushHistory();
$('dashPushCard').onclick=()=>{$('notifySettingsBox').classList.add('open');$('notifySettingsBox').scrollIntoView({behavior:'smooth',block:'center'});};
$('dashKakaoCard').onclick=()=>showKakaoStatus();
async function showKakaoStatus(){
  statusEl.textContent='카카오 연동 상태 확인 중…';
  try{
    const d=await fetch('/api/kakao/status',{cache:'no-store'}).then(r=>r.json());
    const lines=['💬 카카오 연동 상태','','등록 방: '+(d.rooms||0),'최근 60초 활성 방: '+(d.activeRooms||0),'기본 일정 알림: 30분 전 / 10분 전','공지 자동알림: 점검·라이브','CM / 업데이트: 새 글 전체','아그로: 점검 시간 변경 시 1회 알림',''];
    (d.roomStates||[]).slice(0,8).forEach(x=>lines.push('• '+(x.alias||x.room||'방')+' · '+(x.active?'연결':'대기')+(x.lastPollAt?' · '+x.lastPollAt:'')));
    lines.push('','카카오 명령: !봇상태 / !알림진단 / !알림테스트 / !알림기본 / !앱');
    renderText(lines.join('\n')); statusEl.textContent='카카오 상태 확인 완료';
  }catch(e){renderText('카카오 상태 조회 실패\n'+(e?.message||e));statusEl.textContent='조회 오류';}
}
renderFavoriteCharacter();

function b64ToU8(base64String){
  const padding='='.repeat((4-base64String.length%4)%4);
  const base64=(base64String+padding).replace(/-/g,'+').replace(/_/g,'/');
  const raw=atob(base64), out=new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++) out[i]=raw.charCodeAt(i);
  return out;
}
async function getPushSubscription(){
  if(!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
  const reg=await navigator.serviceWorker.ready;
  return await reg.pushManager.getSubscription();
}
async function updateNotifyState(){
  const state=$('notifyState'), dot=$('notifyDot'), dash=$('dashPush'), dashSub=$('dashPushSub');
  if(!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)){
    state.textContent='이 기기에서 푸시 알림 미지원'; dot.classList.remove('on');dash.textContent='미지원';dash.className='dashvalue dashbad';dashSub.textContent='이 기기에서 Web Push 미지원';return;
  }
  const sub=await getPushSubscription().catch(()=>null);
  if(Notification.permission==='granted' && sub){
    fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subscription:sub.toJSON(),label:'Galaxy Tablet PWA'})}).catch(()=>{});
    state.textContent='알림 ON · 이 태블릿 등록됨'; dot.classList.add('on');dash.textContent='ON';dash.className='dashvalue dashgood';dashSub.textContent='30분·10분 전 푸시 수신';
  }else if(Notification.permission==='denied'){
    state.textContent='알림 차단됨 · Android 설정에서 허용 필요'; dot.classList.remove('on');dash.textContent='차단됨';dash.className='dashvalue dashbad';dashSub.textContent='Android 알림 권한 확인 필요';
  }else{
    state.textContent='알림 OFF'; dot.classList.remove('on');dash.textContent='OFF';dash.className='dashvalue dashwarn';dashSub.textContent='알림 켜기를 눌러 등록';
  }
}
async function loadNotifyPrefs(){
  try{
    const d=await fetch('/api/push/settings',{cache:'no-store'}).then(r=>r.json());
    if(!d.ok)return;
    const p=d.settings||{};
    const leads=p.leads||[30,10];
    $('lead30').checked=leads.includes(30); $('lead10').checked=leads.includes(10);
    document.querySelectorAll('[data-schedule]').forEach(el=>{el.checked=(p.schedule||{})[el.dataset.schedule]!==false});
    document.querySelectorAll('[data-board]').forEach(el=>{el.checked=(p.boards||{})[el.dataset.board]!==false});
    $('maintenanceAgro').checked=p.maintenanceAgro!==false;
  }catch(e){}
}
async function saveNotifyPrefs(){
  try{
    const leads=[]; if($('lead30').checked)leads.push(30); if($('lead10').checked)leads.push(10);
    if(!leads.length)return toast('30분 전 또는 10분 전 중 하나는 선택해 주세요.');
    const schedule={}; document.querySelectorAll('[data-schedule]').forEach(el=>schedule[el.dataset.schedule]=el.checked);
    const boards={}; document.querySelectorAll('[data-board]').forEach(el=>boards[el.dataset.board]=el.checked);
    const body={leads,schedule,boards,maintenanceAgro:$('maintenanceAgro').checked};
    const r=await fetch('/api/push/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json(); if(!d.ok)throw new Error(d.error||'저장 실패');
    toast('알림 설정을 저장했습니다.');
  }catch(e){toast('설정 저장 실패: '+(e?.message||e));}
}
async function showNextAlerts(){
  statusEl.textContent='다음 알림 계산 중…';
  try{
    const d=await fetch('/api/push/next',{cache:'no-store'}).then(r=>r.json());
    const rows=d.items||[];
    let t='⏱️ 다음 알림 예정\n\n';
    for(const x of rows){t+=(x.enabled?'🔔 ':'🔕 ')+x.name+'\n   '+x.time+' · '+x.minutes+'분 후 · '+(x.leads||[]).map(v=>v+'분 전').join(' / ')+'\n\n';}
    renderText(t.trim()||'예정된 일정이 없습니다.'); statusEl.textContent='알림 일정 · '+new Date().toLocaleTimeString('ko-KR');
  }catch(e){renderText('다음 알림 조회 실패\n'+(e?.message||e));}
}
async function showPushHistory(){
  statusEl.textContent='알림 기록 조회 중…';
  try{
    const d=await fetch('/api/push/history',{cache:'no-store'}).then(r=>r.json());
    const rows=d.items||[];
    let t='🧾 최근 앱 알림 기록\n\n';
    for(const x of rows){t+=(x.time||'')+'  '+(x.title||'')+'\n'+(x.body||'')+'\n\n';}
    renderText(t.trim()||'아직 전송된 알림 기록이 없습니다.'); statusEl.textContent='최근 '+rows.length+'건';
  }catch(e){renderText('알림 기록 조회 실패\n'+(e?.message||e));}
}
async function showPushHealth(){
  statusEl.textContent='알림 시스템 점검 중…';
  try{
    const d=await fetch('/api/push/diagnostics',{cache:'no-store'}).then(r=>r.json());
    const ext=d.lastExternalMinutes;
    const bg=d.lastBackgroundMinutes;
    let t='🛡️ 앱 알림 시스템 상태\n\n';
    t+='푸시 키 : '+(d.configured?'정상':'설정 필요')+'\n';
    t+='등록 기기 : '+(d.subscriptions||0)+'대\n';
    t+='외부 1분 체크 : '+(d.externalCronHealthy?'정상':'미연결/지연')+(ext==null?'':' · '+ext+'분 전')+'\n';
    t+='내부 체크 : '+(d.backgroundHealthy?'정상':'대기/지연')+(bg==null?'':' · '+bg+'분 전')+'\n';
    t+='서버 저장 : '+(d.persistentStorage?'영구 저장':'임시 저장(Render 무료 서버)')+'\n\n';
    t+=(d.recommendation||'');
    renderText(t); statusEl.textContent=d.externalCronHealthy?'알림 감시 정상':'외부 체크 연결 권장';
  }catch(e){renderText('알림 시스템 점검 실패\n'+(e?.message||e));}
}
$('notifySettingsBtn').onclick=()=>{$('notifySettingsBox').classList.toggle('open');loadNotifyPrefs();};
$('saveNotifyPrefs').onclick=saveNotifyPrefs;
$('notifyNextBtn').onclick=showNextAlerts;
$('notifyHistoryBtn').onclick=showPushHistory;
$('notifyHealthBtn').onclick=showPushHealth;
async function enablePush(){
  try{
    if(!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)){
      return toast('이 기기에서는 웹 푸시를 지원하지 않습니다.');
    }
    const perm=await Notification.requestPermission();
    if(perm!=='granted'){await updateNotifyState();return toast('알림 권한이 허용되지 않았습니다.');}
    const reg=await navigator.serviceWorker.ready;
    let sub=await reg.pushManager.getSubscription();
    if(!sub){
      const k=await fetch('/api/push/public-key',{cache:'no-store'}).then(r=>r.json());
      if(!k.enabled || !k.publicKey) return toast('서버 푸시 키 설정이 아직 안 됐습니다.');
      sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64ToU8(k.publicKey)});
    }
    const r=await fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subscription:sub.toJSON(),label:'Galaxy Tablet PWA'})});
    const d=await r.json();
    if(!d.ok) throw new Error(d.error||'등록 실패');
    await updateNotifyState();
    toast('앱 알림이 켜졌습니다.');
  }catch(e){toast('알림 설정 실패: '+(e?.message||e));}
}
async function testPush(){
  try{
    const sub=await getPushSubscription();
    if(!sub) return toast('먼저 알림 켜기를 눌러주세요.');
    const r=await fetch('/api/push/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:sub.endpoint})});
    const d=await r.json();
    if(!d.ok) throw new Error(d.error||'테스트 실패');
    toast('테스트 알림을 보냈습니다.');
  }catch(e){toast('테스트 실패: '+(e?.message||e));}
}
async function sendScenarioTest(kind){
  try{
    const sub=await getPushSubscription();
    if(!sub)return toast('먼저 알림 켜기를 눌러주세요.');
    const r=await fetch('/api/push/test-scenario',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:sub.endpoint,kind})});
    const d=await r.json();
    if(!d.ok)throw new Error(d.error||'테스트 실패');
    const label=kind==='lead30'?'30분 전':kind==='lead10'?'10분 전':'아그로 시간 변경';
    toast(label+' 테스트 알림을 보냈습니다.');
  }catch(e){toast('시나리오 테스트 실패: '+(e?.message||e));}
}
async function fullSelfCheck(){
  statusEl.textContent='전체 자가진단 중…';
  try{
    const [v,d,s]=await Promise.all([
      fetch('/api/app/version',{cache:'no-store'}).then(r=>r.json()),
      fetch('/api/push/diagnostics',{cache:'no-store'}).then(r=>r.json()),
      getPushSubscription().catch(()=>null)
    ]);
    const perm=('Notification' in window)?Notification.permission:'unsupported';
    let t='✅ AION2 TOOL 전체 자가진단\n\n';
    t+='앱 버전 : '+(v.version||'?')+'\n';
    t+='푸시 서버 : '+(d.configured?'정상':'설정 필요')+'\n';
    t+='이 태블릿 권한 : '+perm+'\n';
    t+='푸시 구독 : '+(s?'등록됨':'없음')+'\n';
    t+='cron 1분 체크 : '+(d.externalCronHealthy?'정상':'점검 필요')+(d.lastExternalMinutes==null?'':' · '+d.lastExternalMinutes+'분 전')+'\n';
    t+='등록 기기 : '+(d.subscriptions||0)+'대\n';
    t+='최근 체크 오류 : '+((d.scheduler||{}).lastError||'없음')+'\n\n';
    t+=(d.configured&&s&&d.externalCronHealthy?'🟢 자동알림 시스템 정상':'🟡 위 항목 중 점검 필요');
    renderText(t);statusEl.textContent='자가진단 완료';
  }catch(e){renderText('자가진단 실패\n'+(e?.message||e));statusEl.textContent='자가진단 오류';}
}
async function backupSettings(){
  try{
    const prefs=await fetch('/api/push/settings',{cache:'no-store'}).then(r=>r.json()).catch(()=>({}));
    const data={format:'AION2_TOOL_BACKUP_V1',createdAt:new Date().toISOString(),appVersion:'V9 FINAL',favoriteCharacter:getFavoriteCharacter(),favoriteBosses:getFavoriteBosses(),pushSettings:prefs.settings||null};
    const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download='AION2_TOOL_settings_'+new Date().toISOString().slice(0,10)+'.json';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('설정 백업 파일을 만들었습니다.');
  }catch(e){toast('설정 백업 실패');}
}
async function restoreSettingsFile(file){
  try{
    const data=JSON.parse(await file.text());if(data.format!=='AION2_TOOL_BACKUP_V1')throw new Error('지원하지 않는 백업 파일');
    if(data.favoriteCharacter?.name&&data.favoriteCharacter?.server)localStorage.setItem(FAV_CHARACTER_KEY,JSON.stringify(data.favoriteCharacter));
    if(Array.isArray(data.favoriteBosses))localStorage.setItem(FAV_BOSS_KEY,JSON.stringify(data.favoriteBosses.slice(0,4)));
    if(data.pushSettings){const r=await fetch('/api/push/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data.pushSettings)});if(!r.ok)throw new Error('알림 설정 복원 실패');}
    renderFavoriteCharacter();renderFavBossChips();renderFavBossChecks();await loadNotifyPrefs();await refreshDashboard();toast('설정을 복원했습니다.');
  }catch(e){toast('복원 실패: '+(e?.message||e));}
}
$('fullCheckBtn').onclick=fullSelfCheck;
$('backupBtn').onclick=backupSettings;
$('restoreBtn').onclick=()=>$('restoreFile').click();
$('restoreFile').onchange=e=>{const f=e.target.files?.[0];if(f)restoreSettingsFile(f);e.target.value='';};
$('timelineRefreshBtn').onclick=async()=>{await refreshDashboard();toast('일정을 새로고침했습니다.');};

async function disablePush(){
  try{
    const sub=await getPushSubscription();
    if(sub){
      await fetch('/api/push/unsubscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:sub.endpoint})}).catch(()=>{});
      await sub.unsubscribe().catch(()=>{});
    }
    await updateNotifyState();
    toast('앱 알림을 껐습니다.');
  }catch(e){toast('알림 해제 실패: '+(e?.message||e));}
}
$('notifyOnBtn').onclick=enablePush;
$('notifyTestBtn').onclick=testPush;
$('notifyOffBtn').onclick=disablePush;
$('test30Btn').onclick=()=>sendScenarioTest('lead30');
$('test10Btn').onclick=()=>sendScenarioTest('lead10');
$('testAgroBtn').onclick=()=>sendScenarioTest('agrochange');
window.addEventListener('load',()=>{setTimeout(updateNotifyState,700);setTimeout(loadNotifyPrefs,900);setTimeout(refreshDashboard,1100);setTimeout(renderFavBossChecks,1200);setTimeout(()=>{const q=new URLSearchParams(location.search).get('cmd');if(q){run(q);history.replaceState({},'',location.pathname);}},1350)});
setInterval(refreshDashboard,30000);
setInterval(tickDashboardCountdowns,1000);

let deferredPrompt=null;const installBtn=$('installBtn');
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferredPrompt=e;installBtn.style.display='block'});
installBtn.onclick=async()=>{if(deferredPrompt){deferredPrompt.prompt();await deferredPrompt.userChoice;deferredPrompt=null}else{toast('Chrome 메뉴(⋮) → 앱 설치 또는 홈 화면에 추가')}};
window.addEventListener('appinstalled',()=>{installBtn.textContent='설치됨';toast('AION2 TOOL 설치 완료')});

let reloadingForSW=false;
const updateBtn=$('updateBtn');
function showUpdateButton(reg){
  updateBtn.style.display='inline-block';
  updateBtn.onclick=()=>{if(reg&&reg.waiting){reg.waiting.postMessage({type:'SKIP_WAITING'});}else{location.reload();}};
}
async function setupServiceWorker(){
  if(!('serviceWorker' in navigator))return;
  try{
    const reg=await navigator.serviceWorker.register('/sw.js',{updateViaCache:'none'});
    window.__aion2SW=reg;
    if(reg.waiting)showUpdateButton(reg);
    reg.addEventListener('updatefound',()=>{
      const w=reg.installing;
      if(!w)return;
      w.addEventListener('statechange',()=>{
        if(w.state==='installed'&&navigator.serviceWorker.controller)showUpdateButton(reg);
      });
    });
    await reg.update().catch(()=>{});
  }catch(e){}
}
navigator.serviceWorker?.addEventListener('controllerchange',()=>{
  if(reloadingForSW)return;
  reloadingForSW=true;
  location.reload();
});
window.addEventListener('load',()=>{
  setupServiceWorker();
  fetch('/api/app/version',{cache:'no-store'}).then(r=>r.json()).then(d=>{if(d.version)$('versionBadge').textContent=d.version;}).catch(()=>{});
});
</script>
</body>
</html>"""

PWA_MANIFEST = {
    "name": "AION2 TOOL",
    "short_name": "AION2",
    "description": "AION2 character, ranking, field boss and party tool",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0c1120",
    "theme_color": "#0c1120",
    "orientation": "any",
    "icons": [
        {"src": "/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
    ]
}

PWA_SW = r"""const CACHE='aion2-tool-shell-v8-pro';
const SHELL=['/','/manifest.webmanifest','/pwa/icon-192.png','/pwa/icon-512.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)));self.skipWaiting()});
async function registerExistingSubscription(){try{const sub=await self.registration.pushManager.getSubscription();if(sub)await fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subscription:sub.toJSON(),label:'Galaxy Tablet PWA · SW'})});}catch(e){}}
function b64ToU8SW(s){const pad='='.repeat((4-s.length%4)%4);const b=(s+pad).replace(/-/g,'+').replace(/_/g,'/');const raw=atob(b),out=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)out[i]=raw.charCodeAt(i);return out;}
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()).then(()=>registerExistingSubscription()))});
self.addEventListener('pushsubscriptionchange',event=>{event.waitUntil((async()=>{try{const k=await fetch('/api/push/public-key',{cache:'no-store'}).then(r=>r.json());if(!k.enabled||!k.publicKey)return;const sub=await self.registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64ToU8SW(k.publicKey)});await fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subscription:sub.toJSON(),label:'Galaxy Tablet PWA · renewed'})});}catch(e){}})())});
self.addEventListener('message',e=>{if(e.data&&e.data.type==='SKIP_WAITING')self.skipWaiting()});
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  const u=new URL(e.request.url);
  if(u.origin!==location.origin)return;
  if(u.pathname.startsWith('/api/')||u.pathname.startsWith('/openchat')||u.pathname.startsWith('/alerts/')||u.pathname.startsWith('/c/')||u.pathname.startsWith('/detail'))return;
  if(e.request.mode==='navigate'){
    e.respondWith(fetch(e.request).then(r=>{const copy=r.clone();caches.open(CACHE).then(c=>c.put('/',copy));return r}).catch(()=>caches.match('/')));return;
  }
  e.respondWith(caches.match(e.request).then(cached=>{
    const network=fetch(e.request).then(r=>{if(r&&r.ok)caches.open(CACHE).then(c=>c.put(e.request,r.clone()));return r});
    return cached||network;
  }));
});
self.addEventListener('push',event=>{
  let d={title:'AION2 TOOL',body:'새 알림이 있습니다.',url:'/'};
  try{if(event.data)d=Object.assign(d,event.data.json())}catch(e){try{d.body=event.data.text()}catch(_){}}
  const options={
    body:d.body||'',
    icon:'/pwa/icon-192.png',
    badge:'/pwa/icon-192.png',
    tag:d.tag||('aion2-'+Date.now()),
    renotify:true,
    data:{url:d.url||'/'},
    vibrate:[180,80,180]
  };
  event.waitUntil(self.registration.showNotification(d.title||'AION2 TOOL',options));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const target=(event.notification.data&&event.notification.data.url)||'/';
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list=>{
    for(const c of list){if('focus' in c){c.navigate(target);return c.focus()}}
    if(clients.openWindow)return clients.openWindow(target);
  }));
});"""

@app.get("/api/app/version")
async def pwa_app_version():
    return {"ok": True, "version": PWA_APP_VERSION, "build": "2026-09-07-v11-alert-retry-fix"}


@app.get("/manifest.webmanifest")
async def pwa_manifest():
    return JSONResponse(PWA_MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

@app.get("/sw.js")
async def pwa_service_worker():
    return Response(PWA_SW, media_type="application/javascript; charset=utf-8", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

@app.get("/pwa/icon-{size}.png")
async def pwa_icon(size: int):
    if size not in (192, 512):
        return Response(status_code=404)
    icon_path = Path(__file__).resolve().parent / "pwa_assets" / f"icon-{size}.png"
    try:
        return Response(icon_path.read_bytes(), media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        return Response(status_code=404)


# =========================================================
# Routes
# =========================================================


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(PWA_HOME_HTML, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

@app.get("/api/status")
async def api_status():
    return {
        "ok": True,
        "service": "AION2 Server v23 FullServerFix + Tablet PWA",
        "server": "전 서버 캐릭터 검색 / 지켈 필드보스",
        "character": "Own DB + NotMeter refresh",
        "fieldBoss": "NotMeter public cache",
        "officialBoards": ["공지", "CM", "업데이트"],
    }

@app.get("/health")
async def health():
    return {
        "ok": True,
        "storage": str(AION2_DATA_DIR),
        "persistent": AION2_STORAGE_PERSISTENT,
    }













# =========================================================
# Pretty OpenChat article cards + MessengerBotR alerts
# =========================================================

OPENCHAT_ALERT_STATE_FILE = _state_path(
    "OPENCHAT_ALERT_STATE_FILE",
    "openchat_alert_state.json",
    legacy_paths=("/tmp/aion2_openchat_alert_state.json",),
)
_openchat_alert_lock = asyncio.Lock()

_openchat_alert_source_refresh = {
    "ts": 0.0,
    "task": None,
}

async def _refresh_openchat_alert_sources():
    """Refresh official boss/maintenance sources without blocking alert delivery."""
    try:
        await refresh_boss_rules()
    except Exception:
        pass
    try:
        await latest_maintenance_anchor()
    except Exception:
        pass
    _openchat_alert_source_refresh["ts"] = time.time()

def _kick_openchat_alert_source_refresh():
    """Refresh every minute during maintenance, otherwise every 5 minutes."""
    try:
        task = _openchat_alert_source_refresh.get("task")
        if task is not None and not task.done():
            return
        active = bool(_maintenance_runtime_snapshot(now=datetime.now(KST), persist_transition=True).get("active"))
        interval = 60 if active else 300
        if time.time() - float(_openchat_alert_source_refresh.get("ts") or 0) < interval:
            return
        task = asyncio.create_task(_refresh_openchat_alert_sources())
        _openchat_alert_source_refresh["task"] = task
    except Exception:
        pass

def _cached_alert_agro_anchor():
    """Return the best Agro anchor immediately, without network I/O."""
    cached = _maintenance_anchor_cache.get("value")
    source_id = _maintenance_anchor_cache.get("sourceId")
    base = cached or _persisted_official_agro_anchor() or AGRO_FALLBACK_ANCHOR
    manual = _manual_agro_anchor_for_source(source_id, base)
    return manual if manual is not None else base


def _default_openchat_delivery_state():
    return {
        "initialized": False,
        "lastSeen": {"공지": None, "CM": None, "업데이트": None},
        "testQueue": [],
        "boardPending": [],
        "sentKeys": [],
        "leases": {},
        "lastPollAt": "",
        "lastAlias": "",
        "lastAckAt": "",
        "noticeClassifierVersion": "",
        "noticeRecoveryVersion": "",
        "maintenanceSourceId": "",
        "maintenanceAnchor": "",
        "maintenancePending": None,
    }

def _default_openchat_alert_state():
    return {"enabled": True, "rooms": {}, "deliveries": {}}


def _alert_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)

def _normalize_openchat_delivery(raw):
    out = _default_openchat_delivery_state()
    if not isinstance(raw, dict):
        return out
    out["initialized"] = bool(raw.get("initialized", False))
    if isinstance(raw.get("lastSeen"), dict):
        out["lastSeen"].update(raw["lastSeen"])
    if isinstance(raw.get("testQueue"), list):
        out["testQueue"] = [x for x in raw["testQueue"][-20:] if isinstance(x, dict)]
    if isinstance(raw.get("boardPending"), list):
        out["boardPending"] = [x for x in raw["boardPending"][-100:] if isinstance(x, dict)]
    sent = []
    if isinstance(raw.get("sentKeys"), list):
        sent.extend(str(x) for x in raw["sentKeys"] if str(x))
    for field in ("boardSent", "bossSent"):
        if isinstance(raw.get(field), list):
            sent.extend(str(x) for x in raw[field] if str(x))
    out["sentKeys"] = list(dict.fromkeys(sent))[-1000:]
    if isinstance(raw.get("leases"), dict):
        clean_leases = {}
        now_epoch = time.time()
        for key, expiry in raw["leases"].items():
            try:
                expiry_f = float(expiry)
            except Exception:
                continue
            if str(key) and expiry_f > now_epoch - 60:
                clean_leases[str(key)] = expiry_f
        out["leases"] = clean_leases
    for field in ("lastPollAt", "lastAlias", "lastAckAt", "noticeClassifierVersion", "noticeRecoveryVersion", "maintenanceSourceId", "maintenanceAnchor"):
        if field in raw:
            out[field] = str(raw.get(field) or "")
    if isinstance(raw.get("maintenancePending"), dict):
        out["maintenancePending"] = dict(raw.get("maintenancePending") or {})
    return out

def _load_openchat_alert_state():
    raw = _safe_json_load(OPENCHAT_ALERT_STATE_FILE, {})
    state = _default_openchat_alert_state()
    if not isinstance(raw, dict):
        return state
    if "enabled" in raw:
        state["enabled"] = bool(raw.get("enabled"))
    if isinstance(raw.get("rooms"), dict):
        state["rooms"] = {str(k): bool(v) for k, v in raw["rooms"].items() if str(k).strip()}
    if isinstance(raw.get("deliveries"), dict):
        state["deliveries"] = {
            str(k): _normalize_openchat_delivery(v)
            for k, v in raw["deliveries"].items() if str(k).strip()
        }
    if any(k in raw for k in ("initialized", "lastSeen", "boardSent", "bossSent")):
        state["deliveries"].setdefault("__global__", _normalize_openchat_delivery(raw))
    return state

def _save_openchat_alert_state(state):
    return _atomic_json_write(OPENCHAT_ALERT_STATE_FILE, state)


def _openchat_room_key(room):
    return re.sub(r"\s+", " ", str(room or "")).strip()

def _migrate_openchat_room_alias(room, room_alias=""):
    """Copy legacy room-name state to a stable room ID key once.

    The phone can send room="CID:<channelId>" and room_alias="visible room name".
    Existing room-name settings are copied only when the new ID bucket does
    not already exist, so later ID-based changes remain authoritative.
    """
    new_key = _openchat_room_key(room)
    old_key = _openchat_room_key(room_alias)
    if not new_key or not old_key or new_key == old_key:
        return False

    changed = False

    # Room-specific alert lead settings.
    try:
        data = _load_boss_schedule_overrides()
        by_room = data.get("alertLeadsByRoom")
        if not isinstance(by_room, dict):
            by_room = {}
        if new_key not in by_room and isinstance(by_room.get(old_key), dict):
            by_room[new_key] = json.loads(json.dumps(by_room[old_key], ensure_ascii=False))
            data["alertLeadsByRoom"] = by_room
            if _save_boss_schedule_overrides(data):
                changed = True
    except Exception:
        pass

    # ON/OFF state and delivery history. Transform scheduled keys so an alert
    # already sent just before migration is still recognized under the ID key.
    try:
        state = _load_openchat_alert_state()
        state_changed = False
        rooms = state.setdefault("rooms", {})
        if new_key not in rooms and old_key in rooms:
            rooms[new_key] = bool(rooms.get(old_key))
            state_changed = True

        deliveries = state.setdefault("deliveries", {})
        if new_key not in deliveries and isinstance(deliveries.get(old_key), dict):
            delivery = json.loads(json.dumps(deliveries[old_key], ensure_ascii=False))
            old_encoded = quote(old_key, safe="")
            new_encoded = quote(new_key, safe="")

            def _rewrite_key(value):
                text = str(value or "")
                prefix = "SCHEDULE|" + old_encoded + "|"
                if text.startswith(prefix):
                    return "SCHEDULE|" + new_encoded + "|" + text[len(prefix):]
                return text

            delivery["sentKeys"] = [
                _rewrite_key(x) for x in (delivery.get("sentKeys") or []) if str(x)
            ]
            leases = delivery.get("leases") if isinstance(delivery.get("leases"), dict) else {}
            delivery["leases"] = {_rewrite_key(k): v for k, v in leases.items()}
            deliveries[new_key] = _normalize_openchat_delivery(delivery)
            state_changed = True

        if state_changed and _save_openchat_alert_state(state):
            changed = True
    except Exception:
        pass

    return changed

def _openchat_delivery_key(room=None):
    return _openchat_room_key(room) or "__global__"

def _openchat_alert_enabled(state, room=None):
    room_key = _openchat_room_key(room)
    if room_key:
        return bool(state.get("rooms", {}).get(room_key, state.get("enabled", True)))
    return bool(state.get("enabled", True))

def _openchat_get_delivery(state, room=None):
    key = _openchat_delivery_key(room)
    deliveries = state.setdefault("deliveries", {})
    delivery = _normalize_openchat_delivery(deliveries.get(key))
    deliveries[key] = delivery
    return key, delivery

def _set_openchat_alert_enabled(enabled, room=None):
    state = _load_openchat_alert_state()
    room_key = _openchat_room_key(room)
    if room_key:
        state.setdefault("rooms", {})[room_key] = bool(enabled)
        # Create a separate delivery bucket immediately, but keep it uninitialized
        # so the first poll establishes a clean baseline and sends no old alerts.
        state.setdefault("deliveries", {}).setdefault(
            room_key, _default_openchat_delivery_state()
        )
    else:
        state["enabled"] = bool(enabled)
        state.setdefault("deliveries", {}).setdefault(
            "__global__", _default_openchat_delivery_state()
        )
    _save_openchat_alert_state(state)
    return bool(enabled), room_key


def board_card_url(board_name, post_id):
    return (
        "https://aion2-kakao-bot.onrender.com/p/"
        + quote(str(board_name), safe="")
        + "/"
        + quote(str(post_id), safe="")
    )

def board_card_label(board_name, post_title=""):
    if board_name == "공지":
        return _notice_header(_classify_notice_kind(post_title))
    if board_name == "CM":
        return "📢 AION2 CM"
    return "🆕 AION2 업데이트"

async def _official_page_og_image(url: str):
    cache_key = "ogimg:" + url
    cached = cache_get(cache_key, 3600)
    if cached is not None:
        return cached

    try:
        client = await get_http_client()
        res = await client.get(
            url,
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=httpx.Timeout(connect=1.0, read=3.0, write=1.0, pool=1.0),
        )
        text = res.text

        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]
        image = ""
        for pat in patterns:
            m = re.search(pat, text, flags=re.I)
            if m:
                image = m.group(1).strip()
                break

        cache_set(cache_key, image)
        return image
    except Exception:
        cache_set(cache_key, "")
        return ""

@app.get("/p/{board_name}/{post_id}")
async def pretty_board_card(board_name: str, post_id: str):
    normalized = "CM" if board_name.lower() == "cm" else board_name
    if normalized not in BOARD_CONFIGS:
        return HTMLResponse("<h2>잘못된 게시판입니다.</h2>", status_code=404)

    rows = await fetch_board_latest(normalized, limit=50 if normalized == "공지" else 18)
    post = next((x for x in rows if str(x["id"]) == str(post_id)), None)

    # Old post may not be in latest 18; still make a valid redirect card.
    if not post:
        config = BOARD_CONFIGS[normalized]
        official = (
            f"https://aion2.plaync.com/ko-kr/board/"
            f"{config['view']}/view?articleId={post_id}"
        )
        title_text = board_card_label(normalized)
        desc_text = "AION2 공식 게시글 보기"
    else:
        official = post["link"]
        title_text = board_card_label(normalized, post.get("title") or "")
        desc_text = post["title"]

    og_image = await _official_page_og_image(official)

    safe_title = escape(title_text)
    safe_desc = escape(desc_text)
    safe_official = escape(official, quote=True)
    safe_img = escape(og_image, quote=True)

    image_meta = (
        f'<meta property="og:image" content="{safe_img}">'
        if safe_img else ""
    )

    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:type" content="website">
<meta property="og:title" content="{safe_title}">
<meta property="og:description" content="{safe_desc}">
{image_meta}
<meta name="twitter:card" content="summary_large_image">
<meta http-equiv="refresh" content="0;url={safe_official}">
<title>{safe_title}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif;
margin:0;background:#111827;color:#fff;display:grid;place-items:center;min-height:100vh}}
.card{{width:min(520px,90vw);padding:28px;border-radius:22px;background:#1f2937}}
h2{{margin:0 0 12px}}p{{color:#d1d5db;line-height:1.6}}a{{color:#93c5fd}}
</style>
</head>
<body>
<div class="card">
<h2>{safe_title}</h2>
<p>{safe_desc}</p>
<a href="{safe_official}">공식 게시글로 이동</a>
</div>
<script>location.replace({json.dumps(official)});</script>
</body>
</html>"""
    return HTMLResponse(html)


# =========================================================
# YUNBOT V8 common schedule snapshot
#
# MessengerBotR V8 keeps one server bridge (YUNBOT), while
# each registered Kakao room keeps its own local schedule offset
# and alert lead times.  This endpoint exposes only the COMMON
# server schedule.  Room-specific corrections never mutate it.
# =========================================================

YUNBOT_SNAPSHOT_VERSION = "v8-room-schedule-2026-09-07"


def _yunbot_v8_occurrence(dt):
    if dt is None:
        return None
    try:
        local = dt.astimezone(KST)
    except Exception:
        local = dt
    return {
        "epochMs": int(local.timestamp() * 1000),
        "iso": local.isoformat(),
        "date": local.strftime("%m/%d"),
        "time": local.strftime("%H:%M"),
        "hour": int(local.hour),
        "minute": int(local.minute),
    }


def _yunbot_v8_daily_occurrences(times, now=None):
    now = now or datetime.now(KST)
    out = []
    seen = set()
    clean = []
    for item in (times or []):
        try:
            h, m = int(item[0]), int(item[1])
        except Exception:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            clean.append((h, m))
    for day_delta in (-1, 0, 1, 2):
        day = now + timedelta(days=day_delta)
        for h, m in clean:
            dt = day.replace(hour=h, minute=m, second=0, microsecond=0)
            key = int(dt.timestamp())
            if key in seen:
                continue
            seen.add(key)
            out.append(dt)
    out.sort()
    return out


def _yunbot_v8_weekly_occurrences(weekdays, times, now=None):
    now = now or datetime.now(KST)
    day_set = set()
    for value in (weekdays or []):
        try:
            day_set.add(int(value))
        except Exception:
            pass
    clean_times = []
    for item in (times or []):
        try:
            h, m = int(item[0]), int(item[1])
        except Exception:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            clean_times.append((h, m))

    out = []
    seen = set()
    # One week behind + slightly more than one week ahead.  The past
    # occurrence is needed when a room correction shifts an event later
    # than the common clock (e.g. 20:00 common -> 20:12 room-specific).
    for day_delta in range(-8, 10):
        day = now + timedelta(days=day_delta)
        if int(day.weekday()) not in day_set:
            continue
        for h, m in clean_times:
            dt = day.replace(hour=h, minute=m, second=0, microsecond=0)
            key = int(dt.timestamp())
            if key in seen:
                continue
            seen.add(key)
            out.append(dt)
    out.sort()
    return out


def _yunbot_v8_agro_occurrences(anchor, now=None):
    now = now or datetime.now(KST)
    if anchor is None:
        return []
    try:
        interval_hours = max(1, int(BOSS_RULES.get("agroIntervalHours", 4)))
    except Exception:
        interval_hours = 4
    interval = timedelta(hours=interval_hours)
    interval_seconds = interval.total_seconds()
    try:
        steps = int((now - anchor).total_seconds() // interval_seconds)
    except Exception:
        steps = 0

    out = []
    seen = set()
    # Keep several previous/future occurrences so local room offsets can
    # cross the common event clock without disappearing after that clock.
    for i in range(steps - 3, steps + 10):
        dt = anchor + interval * i
        key = int(dt.timestamp())
        if key in seen:
            continue
        seen.add(key)
        out.append(dt)
    out.sort()
    return out


def _yunbot_v8_pack_schedule(key, name, item_type, occurrences):
    packed = []
    for dt in occurrences or []:
        row = _yunbot_v8_occurrence(dt)
        if row:
            packed.append(row)
    return {
        "key": str(key),
        "name": str(name),
        "type": str(item_type),
        "occurrences": packed,
    }


@app.get("/openchat/schedule-snapshot")
async def openchat_schedule_snapshot(room: str = "YUNBOT", room_alias: str = "윤이봇"):
    """Return the common schedule used by YUNBOT V8.

    This route deliberately contains no per-Kakao-room override state.
    MessengerBotR stores room-specific clock offsets and alert lead times
    locally and resets only those clock offsets when `generation` changes.
    """
    now = datetime.now(KST)
    try:
        await refresh_boss_rules()
    except Exception:
        pass

    try:
        common_agro_anchor = await latest_maintenance_anchor()
    except Exception:
        common_agro_anchor = _persisted_official_agro_anchor() or AGRO_FALLBACK_ANCHOR

    official_info = _persisted_official_agro_info()
    official_anchor = official_info.get("anchor")
    source_id = str(official_info.get("sourceId") or "")
    source_title = str(official_info.get("sourceTitle") or "")

    generation_anchor = official_anchor or common_agro_anchor or AGRO_FALLBACK_ANCHOR
    generation = source_id
    if generation_anchor is not None:
        generation = (generation + "|" if generation else "") + generation_anchor.astimezone(KST).strftime("%Y%m%d%H%M")
    if not generation:
        generation = "fallback"

    schedules = []

    schedules.append(_yunbot_v8_pack_schedule(
        "agro", "정령왕 아그로", "boss",
        _yunbot_v8_agro_occurrences(common_agro_anchor, now),
    ))

    schedules.append(_yunbot_v8_pack_schedule(
        "kaira", "감시자 카이라", "boss",
        _yunbot_v8_daily_occurrences(_kaira_times(), now),
    ))

    schedules.append(_yunbot_v8_pack_schedule(
        "nahma", "수호신장 나흐마", "boss",
        _yunbot_v8_weekly_occurrences(
            BOSS_RULES.get("nahmaWeekdays") or [],
            [(BOSS_RULES.get("nahmaHour", 0), BOSS_RULES.get("nahmaMinute", 0))],
            now,
        ),
    ))

    schedules.append(_yunbot_v8_pack_schedule(
        "abyss", "어비스 보스", "boss",
        _yunbot_v8_weekly_occurrences(
            BOSS_RULES.get("abyssWeekdays") or [],
            [(BOSS_RULES.get("abyssHour", 0), BOSS_RULES.get("abyssMinute", 0))],
            now,
        ),
    ))

    schedules.append(_yunbot_v8_pack_schedule(
        "sigong", "시공쟁탈전", "content",
        _yunbot_v8_weekly_occurrences(
            BOSS_RULES.get("sigongWeekdays") or [],
            BOSS_RULES.get("sigongTimes") or [],
            now,
        ),
    ))

    schedules.append(_yunbot_v8_pack_schedule(
        "gyunyeol", "균열지대", "content",
        _yunbot_v8_weekly_occurrences(
            BOSS_RULES.get("gyunyeolWeekdays") or [],
            BOSS_RULES.get("gyunyeolTimes") or [],
            now,
        ),
    ))

    schedules.append(_yunbot_v8_pack_schedule(
        "ati", "아티쟁", "content",
        _yunbot_v8_weekly_occurrences(
            BOSS_RULES.get("atiWeekdays") or [],
            BOSS_RULES.get("atiTimes") or [],
            now,
        ),
    ))

    schedules.append(_yunbot_v8_pack_schedule(
        "fieldboss", "필드보스", "boss",
        _yunbot_v8_weekly_occurrences(
            BOSS_RULES.get("fieldBossWeekdays") or [],
            BOSS_RULES.get("fieldBossTimes") or [],
            now,
        ),
    ))

    maintenance_runtime = _maintenance_runtime_snapshot(now=now, persist_transition=True)
    maintenance_start = maintenance_runtime.get("start")
    maintenance_end = maintenance_runtime.get("end")
    maintenance_last_start = maintenance_runtime.get("lastStart")
    maintenance_last_end = maintenance_runtime.get("lastEnd")

    return {
        "ok": True,
        "version": YUNBOT_SNAPSHOT_VERSION,
        "bridge": _openchat_room_key(room) or "YUNBOT",
        "alias": _openchat_room_key(room_alias) or "윤이봇",
        "nowEpochMs": int(now.timestamp() * 1000),
        "nowIso": now.isoformat(),
        "generation": generation,
        "maintenance": {
            "sourceId": source_id,
            "sourceTitle": source_title,
            "officialAnchor": official_anchor.astimezone(KST).isoformat() if official_anchor else "",
            "commonAgroAnchor": common_agro_anchor.astimezone(KST).isoformat() if common_agro_anchor else "",
            "active": bool(maintenance_runtime.get("active")),
            "scheduled": bool(maintenance_runtime.get("scheduled")),
            "changeKind": str(maintenance_runtime.get("changeKind") or ""),
            "startIso": maintenance_start.astimezone(KST).isoformat() if maintenance_start else "",
            "endIso": maintenance_end.astimezone(KST).isoformat() if maintenance_end else "",
            "startEpochMs": int(maintenance_start.timestamp() * 1000) if maintenance_start else 0,
            "endEpochMs": int(maintenance_end.timestamp() * 1000) if maintenance_end else 0,
            "lastStartEpochMs": int(maintenance_last_start.timestamp() * 1000) if maintenance_last_start else 0,
            "lastEndEpochMs": int(maintenance_last_end.timestamp() * 1000) if maintenance_last_end else 0,
        },
        "schedules": schedules,
    }


@app.get("/openchat/alerts")
async def openchat_alerts(room: str = "", room_alias: str = ""):
    """Single-delivery polling endpoint.

    Each room+alert key is claimed atomically on the server before it is
    returned, so duplicate phone pollers cannot send the same alert repeatedly.
    """
    room_key = _openchat_room_key(room)
    _kick_openchat_alert_source_refresh()
    now = datetime.now(KST)
    now_epoch = time.time()

    # ---------------- FAST LOCAL PATH ----------------
    async with _openchat_alert_lock:
        _migrate_openchat_room_alias(room, room_alias)
        state = _load_openchat_alert_state()
        if not _openchat_alert_enabled(state, room):
            return {"ok": True, "enabled": False, "room": room_key, "baseline": False, "items": []}

        _, delivery = _openchat_get_delivery(state, room)
        delivery["lastPollAt"] = now.isoformat()
        delivery["lastAlias"] = _openchat_room_key(room_alias) or room_key
        first_run = not delivery.get("initialized")
        items = []

        # Maintenance-driven Agro change, kept pending until phone ACK.
        try:
            maint = _persisted_official_agro_info()
            current_source = str(maint.get("sourceId") or "")
            current_anchor = maint.get("anchor")
            seen_source = str(delivery.get("maintenanceSourceId") or "")
            seen_anchor = _parse_kst_iso(delivery.get("maintenanceAnchor"))
            pending = delivery.get("maintenancePending") if isinstance(delivery.get("maintenancePending"), dict) else None
            if not seen_source and current_source and current_anchor is not None:
                delivery["maintenanceSourceId"] = current_source
                delivery["maintenanceAnchor"] = current_anchor.astimezone(KST).isoformat()
            elif current_source and current_anchor is not None and (
                current_source != seen_source or seen_anchor is None or current_anchor != seen_anchor
            ):
                pending_anchor = current_anchor.astimezone(KST).strftime("%Y%m%d%H%M")
                pending_key = (
                    "MAINT|"
                    + re.sub(r"[^0-9A-Za-z_-]", "", current_source)[:100]
                    + "|" + pending_anchor
                )
                if not pending or str(pending.get("key") or "") != pending_key:
                    delta = _maintenance_clock_delta_minutes(seen_anchor, current_anchor) if seen_anchor else 0
                    if delta:
                        nxt = next_agro_from_anchor(current_anchor, now)
                        pending = {
                            "key": pending_key,
                            "sourceId": current_source,
                            "anchor": current_anchor.astimezone(KST).isoformat(),
                            "oldTime": seen_anchor.strftime("%H:%M") if seen_anchor else "기존",
                            "newTime": current_anchor.strftime("%H:%M"),
                            "shiftMinutes": int(delta),
                            "nextAgro": nxt.strftime("%m/%d %H:%M") if nxt else "",
                            "title": str(maint.get("sourceTitle") or "점검 일정 변경"),
                            "changeKind": str(maint.get("changeKind") or ""),
                        }
                        delivery["maintenancePending"] = pending
                    else:
                        delivery["maintenanceSourceId"] = current_source
                        delivery["maintenanceAnchor"] = current_anchor.astimezone(KST).isoformat()
                        delivery["maintenancePending"] = None
            pending = delivery.get("maintenancePending") if isinstance(delivery.get("maintenancePending"), dict) else None
            if pending and str(pending.get("key") or ""):
                shift = int(pending.get("shiftMinutes") or 0)
                sign = "+" if shift > 0 else ""
                old_time = str(pending.get("oldTime") or "")
                new_time = str(pending.get("newTime") or "")
                next_agro_text = str(pending.get("nextAgro") or "")
                notice_title = str(pending.get("title") or "")
                change_kind = str(pending.get("changeKind") or "")
                if change_kind == "extension" or (shift > 0 and "연장" in notice_title):
                    change_header = "🔧 점검 연장"
                elif change_kind in ("early_end", "completion") and shift < 0:
                    change_header = "✅ 점검 조기 종료"
                else:
                    change_header = "🔧 아그로 시간 변경"
                maintenance_message = (
                    change_header + "\n\n"
                    f"점검 종료: {old_time} → {new_time}\n"
                    f"변경폭: {sign}{shift}분\n"
                    f"다음 아그로: {next_agro_text}"
                )
                if notice_title:
                    maintenance_message += f"\n\n공지: {notice_title}"
                items.append({
                    "type": "maintenance_change",
                    "title": "아그로 시간 변경",
                    "message": maintenance_message,
                    "oldTime": old_time,
                    "newTime": new_time,
                    "shiftMinutes": shift,
                    "shiftText": f"{sign}{shift}분",
                    "nextAgro": next_agro_text,
                    "noticeTitle": notice_title,
                    "key": str(pending.get("key") or ""),
                })
        except Exception:
            pass

        # Test alerts stay retryable for 5 minutes, but only the newest one
        # for this room is ever exposed. This also cleans old duplicate state.
        valid_tests = []
        for row in (delivery.get("testQueue") or []):
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            try:
                created = float(row.get("created") or 0)
            except Exception:
                created = 0.0
            if key and now_epoch - created <= 300:
                valid_tests.append((created, row))
        valid_tests.sort(key=lambda x: x[0], reverse=True)
        newest_test = valid_tests[0][1] if valid_tests else None
        delivery["testQueue"] = [newest_test] if newest_test else []
        if newest_test:
            scenario = str(newest_test.get("scenario") or "generic").strip().lower()
            test_key = str(newest_test.get("key") or "")
            if scenario == "boss30":
                test_target = now + timedelta(minutes=30)
                items.append({
                    "type": "boss",
                    "boss": "정령왕 아그로 (30분 테스트)",
                    "time": test_target.strftime("%H:%M"),
                    "alertMinutes": 30,
                    "remainingMinutes": 30,
                    "key": test_key,
                })
            elif scenario == "boss10":
                test_target = now + timedelta(minutes=10)
                items.append({
                    "type": "boss",
                    "boss": "정령왕 아그로 (10분 테스트)",
                    "time": test_target.strftime("%H:%M"),
                    "alertMinutes": 10,
                    "remainingMinutes": 10,
                    "key": test_key,
                })
            elif scenario == "content30":
                test_target = now + timedelta(minutes=30)
                items.append({
                    "type": "content",
                    "content": "시공쟁탈전 (30분 테스트)",
                    "time": test_target.strftime("%H:%M"),
                    "alertMinutes": 30,
                    "remainingMinutes": 30,
                    "key": test_key,
                })
            elif scenario == "content10":
                test_target = now + timedelta(minutes=10)
                items.append({
                    "type": "content",
                    "content": "시공쟁탈전 (10분 테스트)",
                    "time": test_target.strftime("%H:%M"),
                    "alertMinutes": 10,
                    "remainingMinutes": 10,
                    "key": test_key,
                })
            elif scenario == "agrochange":
                items.append({
                    "type": "maintenance_change",
                    "title": "아그로 시간 변경 테스트",
                    "message": (
                        "🔧 아그로 시간 변경 테스트\n\n"
                        "점검 종료: 07:00 → 08:00\n"
                        "변경폭: +60분\n"
                        "다음 아그로: 테스트 일정"
                    ),
                    "oldTime": "07:00",
                    "newTime": "08:00",
                    "shiftMinutes": 60,
                    "shiftText": "+60분",
                    "nextAgro": "테스트 일정",
                    "noticeTitle": "테스트 점검 공지",
                    "key": test_key,
                })
            else:
                items.append({
                    "type": "test",
                    "title": "알림 시스템 테스트",
                    "time": now.strftime("%H:%M"),
                    "alertMinutes": 0,
                    "key": test_key,
                })

        # Scheduled alerts are recomputed every poll and are NOT consumed.
        # During maintenance they are completely suppressed. After maintenance,
        # any lead whose trigger time fell inside the maintenance interval is
        # discarded instead of being catch-up delivered.
        maintenance_runtime = _maintenance_runtime_snapshot(now=now, persist_transition=True)
        targets = [] if maintenance_runtime.get("active") else _schedule_alert_targets(now)
        if targets:
            targets[0] = (
                "boss", "정령왕 아그로",
                next_agro_from_anchor(_cached_alert_agro_anchor(), now),
            )

        for item_type, name, target in targets:
            if target is None:
                continue
            minutes = (target - now).total_seconds() / 60.0
            for lead in _get_schedule_alert_leads(name, room):
                trigger_dt = target - timedelta(minutes=int(lead))
                if _alert_trigger_blocked_by_maintenance(trigger_dt, now=now):
                    continue
                # Up to 3 minutes of retry time. For a 2m test lead, retry
                # until just before the event instead of disappearing after one GET.
                # PWA push gets a wider retry/catch-up window so a brief
                # Render cold start or one missed external tick does not lose
                # the 30m/10m alert. Messenger rooms keep the tighter 3m window.
                window = min(6 if room_key == PWA_PUSH_ROOM else 3, lead)
                if max(0, lead - window) < minutes <= lead:
                    items.append({
                        "type": item_type,
                        "boss": name if item_type == "boss" else None,
                        "content": name if item_type == "content" else None,
                        "time": target.strftime("%H:%M"),
                        "alertMinutes": lead,
                        "remainingMinutes": max(0, int(round(minutes))),
                        "key": _scheduled_alert_key(room, name, target, lead),
                        "_legacyKey": _legacy_scheduled_alert_key(name, target, lead),
                    })

        # Short delivery lease: prevent simultaneous duplicate pollers, but retry
        # quickly when the phone fetched an item and failed to send/ACK it.
        # MessengerBotR polls every 15 seconds, so an 8-second lease guarantees
        # the next poll can retry. ACK is the only permanent delivery confirmation.
        sent_keys = set(str(x) for x in (delivery.get("sentKeys") or []) if str(x))
        leases = delivery.get("leases") if isinstance(delivery.get("leases"), dict) else {}
        leases = {
            str(k): float(v)
            for k, v in leases.items()
            if str(k) and _alert_float(v, 0.0) > now_epoch
        }
        fresh_items = []
        for item in items:
            key = str(item.get("key") or "").strip()
            legacy_key = str(item.get("_legacyKey") or "").strip()
            if not key or key in sent_keys or (legacy_key and legacy_key in sent_keys):
                continue
            if float(leases.get(key) or 0) > now_epoch:
                continue
            if legacy_key and float(leases.get(legacy_key) or 0) > now_epoch:
                continue
            lease_seconds = 8.0
            leases[key] = now_epoch + lease_seconds
            item.pop("_legacyKey", None)
            fresh_items.append(item)

        delivery["leases"] = leases
        state.setdefault("deliveries", {})[_openchat_delivery_key(room)] = delivery
        _save_openchat_alert_state(state)

        if fresh_items:
            return {"ok": True, "enabled": True, "room": room_key, "baseline": first_run, "items": fresh_items}

    # ---------------- BOARD PATH ----------------
    async def _fetch_board(board_name):
        try:
            board_limit = 50 if board_name == "공지" else 18
            return await asyncio.wait_for(fetch_board_latest(board_name, limit=board_limit), timeout=4.0)
        except Exception:
            return []

    boards = ("공지", "CM", "업데이트")
    results = await asyncio.gather(*[_fetch_board(b) for b in boards])
    latest_by_board = dict(zip(boards, results))

    async with _openchat_alert_lock:
        state = _load_openchat_alert_state()
        if not _openchat_alert_enabled(state, room):
            return {"ok": True, "enabled": False, "room": room_key, "baseline": False, "items": []}

        _, delivery = _openchat_get_delivery(state, room)
        first_run = not delivery.get("initialized")
        pending = []

        # Keep discovered board items retryable until the phone ACKs them.
        # The list is bounded to 100 below, so a transient send/ACK failure cannot
        # silently discard a notice after an arbitrary 30-minute timeout.
        for row in (delivery.get("boardPending") or []):
            if not isinstance(row, dict):
                continue
            item = row.get("item")
            if not isinstance(item, dict) or not str(item.get("key") or "").strip():
                continue
            pending.append(row)

        if first_run:
            for board, rows in latest_by_board.items():
                if rows:
                    delivery["lastSeen"][board] = rows[0]["id"]
            delivery["initialized"] = True
        else:
            known_pending = {str(x.get("item", {}).get("key") or "") for x in pending}
            for board, rows in latest_by_board.items():
                if not rows:
                    continue
                previous_id = delivery["lastSeen"].get(board)
                new_rows = []
                for row in rows:
                    if previous_id is not None and str(row["id"]) == str(previous_id):
                        break
                    new_rows.append(row)
                delivery["lastSeen"][board] = rows[0]["id"]

                for post in reversed(new_rows):
                    kind = None
                    if board == "공지":
                        kind = _classify_notice_kind(post.get("title") or "")
                        if not kind:
                            continue
                    key = f"{board}:{post['id']}"
                    if key in known_pending:
                        continue
                    card_url = board_card_url(board, post["id"])
                    item = {
                        # Keep this out of the phone's old "board" text branch.
                        # The V8 phone code falls through to item.message and sends the card URL.
                        "type": "board_card",
                        "board": board,
                        "kind": kind,
                        "id": post["id"],
                        "title": post["title"],
                        "message": card_url,
                        "cardUrl": card_url,
                        "officialUrl": str(post.get("link") or ""),
                        "key": key,
                    }
                    pending.append({"created": now_epoch, "item": item})
                    known_pending.add(key)

        # Classifier-upgrade recovery.
        # Important: do NOT consume the recovery merely because one poll ran.
        # It is considered complete only after the phone actually sends it and ACKs.
        # This also uses its own key so a legacy pre-ACK boardSent/sentKeys entry
        # cannot suppress the one recovery alert the user explicitly missed.
        if str(delivery.get("noticeRecoveryVersion") or "") != NOTICE_RECOVERY_VERSION:
            sent_keys_for_recovery = set(str(x) for x in (delivery.get("sentKeys") or []) if str(x))
            known_pending = {str(x.get("item", {}).get("key") or "") for x in pending}
            notice_rows = latest_by_board.get("공지") or []
            for post in notice_rows:
                kind = _classify_notice_kind(post.get("title") or "")
                if not kind or not _board_post_is_recent(post, now=now, max_hours=36):
                    continue
                recovery_key = f"NOTICE_RECOVERY|{NOTICE_RECOVERY_VERSION}|{post['id']}"
                if recovery_key in sent_keys_for_recovery or recovery_key in known_pending:
                    break
                card_url = board_card_url("공지", post["id"])
                item = {
                    "type": "board_card",
                    "board": "공지",
                    "kind": kind,
                    "id": post["id"],
                    "title": post["title"],
                    "message": card_url,
                    "cardUrl": card_url,
                    "officialUrl": str(post.get("link") or ""),
                    "key": recovery_key,
                }
                pending.append({"created": now_epoch, "item": item})
                break

        # Record the classifier version only when the notice source was actually fetched.
        # This is diagnostic only; recovery completion itself is ACK-gated above.
        if latest_by_board.get("공지"):
            delivery["noticeClassifierVersion"] = NOTICE_ALERT_CLASSIFIER_VERSION

        delivery["boardPending"] = pending[-100:]

        sent_keys = set(str(x) for x in (delivery.get("sentKeys") or []) if str(x))
        leases = delivery.get("leases") if isinstance(delivery.get("leases"), dict) else {}
        leases = {
            str(k): float(v)
            for k, v in leases.items()
            if str(k) and _alert_float(v, 0.0) > now_epoch
        }
        board_items = []
        for row in pending:
            item = row.get("item") if isinstance(row, dict) else None
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key or key in sent_keys:
                continue
            if float(leases.get(key) or 0) > now_epoch:
                continue
            # Board alerts remain pending until the phone ACKs them. The lease is
            # intentionally short. If the phone fetches but cannot send/ACK, the
            # next MessengerBotR poll retries instead of losing the alert.
            leases[key] = now_epoch + 8.0
            board_items.append(item)

        delivery["leases"] = leases
        state.setdefault("deliveries", {})[_openchat_delivery_key(room)] = delivery
        _save_openchat_alert_state(state)

        return {
            "ok": True,
            "enabled": True,
            "room": room_key,
            "baseline": first_run,
            "items": board_items,
        }


@app.get("/openchat/alerts/ack")
async def openchat_alert_ack(room: str = "", key: str = "", room_alias: str = ""):
    room_key = _openchat_room_key(room)
    alert_key = str(key or "").strip()
    if not alert_key:
        return {"ok": False, "room": room_key, "key": "", "error": "MissingKey"}
    async with _openchat_alert_lock:
        _migrate_openchat_room_alias(room, room_alias)
        state = _load_openchat_alert_state()
        _, delivery = _openchat_get_delivery(state, room)
        sent_keys = list(dict.fromkeys(
            [str(x) for x in (delivery.get("sentKeys") or []) if str(x)] + [alert_key]
        ))[-1000:]
        # A recovery notice is complete only here, after the phone has sent it.
        recovery_prefix = f"NOTICE_RECOVERY|{NOTICE_RECOVERY_VERSION}|"
        if alert_key.startswith(recovery_prefix):
            recovered_id = alert_key[len(recovery_prefix):].strip()
            if recovered_id:
                canonical_key = f"공지:{recovered_id}"
                sent_keys = list(dict.fromkeys(sent_keys + [canonical_key]))[-1000:]
            delivery["noticeRecoveryVersion"] = NOTICE_RECOVERY_VERSION
        delivery["sentKeys"] = sent_keys
        delivery["lastAckAt"] = datetime.now(KST).isoformat()
        pending_maintenance = delivery.get("maintenancePending") if isinstance(delivery.get("maintenancePending"), dict) else None
        if pending_maintenance and str(pending_maintenance.get("key") or "") == alert_key:
            delivery["maintenanceSourceId"] = str(pending_maintenance.get("sourceId") or "")
            delivery["maintenanceAnchor"] = str(pending_maintenance.get("anchor") or "")
            delivery["maintenancePending"] = None
        leases = delivery.get("leases") if isinstance(delivery.get("leases"), dict) else {}
        leases.pop(alert_key, None)
        delivery["leases"] = leases
        delivery["testQueue"] = [
            x for x in (delivery.get("testQueue") or [])
            if str((x or {}).get("key") or "") != alert_key
        ]
        delivery["boardPending"] = [
            x for x in (delivery.get("boardPending") or [])
            if str(((x or {}).get("item") or {}).get("key") or "") != alert_key
        ]
        state.setdefault("deliveries", {})[_openchat_delivery_key(room)] = delivery
        _save_openchat_alert_state(state)
    return {"ok": True, "room": room_key, "key": alert_key, "acked": True}


@app.get("/api/kakao/status")
async def kakao_bridge_status():
    state = _load_openchat_alert_state()
    deliveries = state.get("deliveries") if isinstance(state.get("deliveries"), dict) else {}
    room_states = []
    now = datetime.now(KST)
    for key, raw in deliveries.items():
        if key == "__global__":
            continue
        d = _normalize_openchat_delivery(raw)
        last = _parse_kst_iso(d.get("lastPollAt"))
        age = None
        active = False
        if last is not None:
            age = max(0.0, (now - last).total_seconds())
            active = age <= 60
        room_states.append({
            "room": str(key),
            "alias": str(d.get("lastAlias") or key),
            "enabled": _openchat_alert_enabled(state, key),
            "active": active,
            "lastPollAgeSeconds": round(age, 1) if age is not None else None,
            "lastPollAt": last.strftime("%H:%M:%S") if last is not None else "",
            "lastAckAt": str(d.get("lastAckAt") or ""),
        })
    room_states.sort(key=lambda x: (not x["active"], x["alias"]))
    return {"ok": True, "version": PWA_APP_VERSION, "rooms": len(room_states),
            "activeRooms": sum(1 for x in room_states if x["active"]),
            "defaultLeads": list(DEFAULT_SCHEDULE_ALERT_LEADS), "roomStates": room_states[:30]}


# =========================================================
# PWA Web Push notifications
# =========================================================

PWA_PUSH_ROOM = "PWA:AION2 TOOL"
PWA_PUSH_SUBSCRIPTIONS_FILE = _state_path(
    "PWA_PUSH_SUBSCRIPTIONS_FILE",
    "pwa_push_subscriptions.json",
    legacy_paths=("/tmp/aion2_pwa_push_subscriptions.json",),
)
PWA_VAPID_PUBLIC_KEY = str(os.getenv("VAPID_PUBLIC_KEY") or "").strip()
PWA_VAPID_PRIVATE_KEY = str(os.getenv("VAPID_PRIVATE_KEY") or "").strip()
PWA_VAPID_SUBJECT = str(os.getenv("VAPID_SUBJECT") or "https://aion2-kakao-bot.onrender.com").strip()
PWA_PUSH_CHECK_LOCK = asyncio.Lock()
PWA_PUSH_BACKGROUND_TASK = None
PWA_PUSH_HISTORY_FILE = _state_path(
    "PWA_PUSH_HISTORY_FILE",
    "pwa_push_history.json",
    legacy_paths=("/tmp/aion2_pwa_push_history.json",),
)

PWA_PUSH_SCHEDULER_FILE = _state_path(
    "PWA_PUSH_SCHEDULER_FILE",
    "pwa_push_scheduler.json",
    legacy_paths=("/tmp/aion2_pwa_push_scheduler.json",),
)

def _default_pwa_scheduler_state():
    return {
        "lastCheckAt": "",
        "lastSuccessAt": "",
        "lastExternalAt": "",
        "lastBackgroundAt": "",
        "lastDurationMs": 0,
        "lastSent": 0,
        "lastFailed": 0,
        "lastItems": 0,
        "lastError": "",
        "checks": 0,
    }

def _load_pwa_scheduler_state():
    raw = _safe_json_load(PWA_PUSH_SCHEDULER_FILE, _default_pwa_scheduler_state())
    state = _default_pwa_scheduler_state()
    if isinstance(raw, dict):
        state.update({k: raw.get(k, v) for k, v in state.items()})
    return state

def _save_pwa_scheduler_state(state):
    return _atomic_json_write(PWA_PUSH_SCHEDULER_FILE, state)

def _scheduler_stamp(source="background", *, ok=None, sent=None, failed=None, items=None, duration_ms=None, error=""):
    now = datetime.now(KST)
    state = _load_pwa_scheduler_state()
    state["lastCheckAt"] = now.isoformat()
    if ok is None:
        state["checks"] = int(state.get("checks") or 0) + 1
        if str(source) == "external":
            state["lastExternalAt"] = now.isoformat()
        elif str(source) == "background":
            state["lastBackgroundAt"] = now.isoformat()
    if ok is True:
        state["lastSuccessAt"] = now.isoformat()
        state["lastError"] = ""
    elif ok is False:
        state["lastError"] = str(error or "UNKNOWN")[:300]
    if sent is not None:
        state["lastSent"] = int(sent or 0)
    if failed is not None:
        state["lastFailed"] = int(failed or 0)
    if items is not None:
        state["lastItems"] = int(items or 0)
    if duration_ms is not None:
        state["lastDurationMs"] = int(duration_ms or 0)
    _save_pwa_scheduler_state(state)
    return state

def _minutes_since_iso(value):
    dt = _parse_kst_iso(value)
    if dt is None:
        return None
    return max(0.0, (datetime.now(KST) - dt).total_seconds() / 60.0)

PWA_SCHEDULE_NAMES = {
    "agro": "정령왕 아그로",
    "kaira": "감시자 카이라",
    "nahma": "수호신장 나흐마",
    "abyss": "어비스 보스",
    "sigong": "시공쟁탈전",
    "gyunyeol": "균열지대",
    "ati": "아티쟁",
    "fieldboss": "필드보스",
}

def _default_pwa_alert_settings():
    return {
        "leads": [30, 10],
        "schedule": {key: True for key in PWA_SCHEDULE_NAMES},
        "boards": {"공지": True, "CM": True, "업데이트": True},
        "maintenanceAgro": True,
    }

def _get_pwa_alert_settings():
    data = _load_boss_schedule_overrides()
    raw = data.get("pwaAlertSettings") if isinstance(data.get("pwaAlertSettings"), dict) else {}
    out = _default_pwa_alert_settings()
    leads = raw.get("leads") if isinstance(raw, dict) else None
    if isinstance(leads, list):
        clean=[]
        for value in leads:
            try: n=int(value)
            except Exception: continue
            if 1 <= n <= 180 and n not in clean: clean.append(n)
        if clean: out["leads"] = sorted(clean, reverse=True)
    for group in ("schedule", "boards"):
        rows = raw.get(group) if isinstance(raw, dict) and isinstance(raw.get(group), dict) else {}
        for key in out[group]:
            if key in rows: out[group][key] = bool(rows.get(key))
    if isinstance(raw, dict) and "maintenanceAgro" in raw:
        out["maintenanceAgro"] = bool(raw.get("maintenanceAgro"))
    return out

def _save_pwa_alert_settings(settings):
    data = _load_boss_schedule_overrides()
    data["pwaAlertSettings"] = settings
    ok = _save_boss_schedule_overrides(data)
    if ok:
        for name in PWA_SCHEDULE_NAMES.values():
            _set_schedule_alert_leads(name, settings.get("leads") or [30, 10], PWA_PUSH_ROOM)
    return ok

def _pwa_item_enabled(item, settings=None):
    settings = settings or _get_pwa_alert_settings()
    typ = str((item or {}).get("type") or "")
    if typ == "board":
        return bool((settings.get("boards") or {}).get(str(item.get("board") or ""), True))
    if typ in ("boss", "content"):
        name = str(item.get("boss") or item.get("content") or "")
        key = _schedule_key(name)
        return bool((settings.get("schedule") or {}).get(key, True)) if key else True
    return True

def _load_pwa_push_history():
    raw = _safe_json_load(PWA_PUSH_HISTORY_FILE, {"items": []})
    rows = raw.get("items") if isinstance(raw, dict) else []
    return [x for x in rows if isinstance(x, dict)][-100:]

def _record_pwa_push_history(payload):
    rows = _load_pwa_push_history()
    rows.append({
        "time": datetime.now(KST).strftime("%m/%d %H:%M"),
        "title": str((payload or {}).get("title") or ""),
        "body": str((payload or {}).get("body") or ""),
        "tag": str((payload or {}).get("tag") or ""),
    })
    return _atomic_json_write(PWA_PUSH_HISTORY_FILE, {"items": rows[-100:]})


def _load_pwa_push_subscriptions():
    raw = _safe_json_load(PWA_PUSH_SUBSCRIPTIONS_FILE, {"subscriptions": []})
    rows = raw.get("subscriptions") if isinstance(raw, dict) else []
    clean = []
    seen = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        sub = row.get("subscription") if isinstance(row.get("subscription"), dict) else {}
        endpoint = str(sub.get("endpoint") or "").strip()
        keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
        if not endpoint or not keys.get("p256dh") or not keys.get("auth") or endpoint in seen:
            continue
        seen.add(endpoint)
        clean.append({
            "subscription": {
                "endpoint": endpoint,
                "expirationTime": sub.get("expirationTime"),
                "keys": {
                    "p256dh": str(keys.get("p256dh") or ""),
                    "auth": str(keys.get("auth") or ""),
                },
            },
            "label": str(row.get("label") or "AION2 TOOL")[:80],
            "created": float(row.get("created") or time.time()),
            "updated": float(row.get("updated") or time.time()),
        })
    return clean


def _save_pwa_push_subscriptions(rows):
    return _atomic_json_write(PWA_PUSH_SUBSCRIPTIONS_FILE, {"subscriptions": rows})


def _pwa_push_configured():
    return bool(PWA_VAPID_PUBLIC_KEY and PWA_VAPID_PRIVATE_KEY)


def _pwa_push_payload(item):
    item = item or {}
    typ = str(item.get("type") or "")
    key = str(item.get("key") or "")
    if typ == "board":
        board = str(item.get("board") or "공지")
        icon = "📢" if board in ("공지", "CM") else "🆕"
        return {
            "title": f"{icon} AION2 {board}",
            "body": str(item.get("title") or "새 글이 등록되었습니다."),
            "url": f"/p/{quote(board, safe='')}/{quote(str(item.get('id') or ''), safe='')}",
            "tag": "board-" + key,
        }
    if typ in ("boss", "content"):
        name = str(item.get("boss") or item.get("content") or "AION2 콘텐츠")
        lead = int(item.get("alertMinutes") or 0)
        remaining = int(item.get("remainingMinutes") if item.get("remainingMinutes") is not None else lead)
        when = str(item.get("time") or "")
        late_note = ""
        if lead and remaining < lead - 1:
            late_note = f" · 현재 약 {remaining}분 전"
        return {
            "title": f"🐲 {name} {lead}분 전",
            "body": f"{when} 예정{late_note} · AION2 TOOL에서 확인하세요.",
            "url": "/?cmd=" + quote(({"agro":"아그로","kaira":"카이라","nahma":"나흐마","abyss":"어비스","sigong":"시공","gyunyeol":"균열","ati":"아티","fieldboss":"필보"}.get(_schedule_key(name)) or "필보"), safe=""),
            "tag": "schedule-" + key,
        }
    return {
        "title": "🔔 AION2 TOOL",
        "body": str(item.get("title") or "알림 시스템 테스트"),
        "url": "/",
        "tag": "test-" + key,
    }


async def _pwa_send_one(subscription, payload):
    if not _pwa_push_configured():
        return {"ok": False, "status": 0, "error": "VAPID_NOT_CONFIGURED"}
    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        return {"ok": False, "status": 0, "error": f"PYWEBPUSH_MISSING:{type(e).__name__}"}

    def _send():
        try:
            response = webpush(
                subscription_info=subscription,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=PWA_VAPID_PRIVATE_KEY,
                vapid_claims={"sub": PWA_VAPID_SUBJECT},
                headers={"TTL": "600"},
                timeout=12,
            )
            status = int(getattr(response, "status_code", 201) or 201)
            return {"ok": 200 <= status < 300, "status": status}
        except WebPushException as e:
            status = int(getattr(e, "status_code", 0) or 0)
            return {"ok": False, "status": status, "error": str(e)[:240]}
        except Exception as e:
            return {"ok": False, "status": 0, "error": f"{type(e).__name__}:{str(e)[:220]}"}

    return await asyncio.to_thread(_send)


async def _pwa_send_payload_to_all(payload, only_endpoint=""):
    rows = _load_pwa_push_subscriptions()
    if only_endpoint:
        rows = [
            row for row in rows
            if str((row.get("subscription") or {}).get("endpoint") or "") == only_endpoint
        ]
    if not rows:
        return {"ok": False, "sent": 0, "failed": 0, "removed": 0, "error": "NO_SUBSCRIPTIONS"}

    sent = 0
    failed = 0
    removed = 0
    dead = set()
    errors = []
    for row in rows:
        sub = row.get("subscription") or {}
        endpoint = str(sub.get("endpoint") or "")
        result = await _pwa_send_one(sub, payload)
        if result.get("ok"):
            sent += 1
        else:
            failed += 1
            status = int(result.get("status") or 0)
            if status in (404, 410):
                dead.add(endpoint)
                removed += 1
            if result.get("error"):
                errors.append(str(result.get("error"))[:160])

    if dead:
        all_rows = _load_pwa_push_subscriptions()
        _save_pwa_push_subscriptions([
            row for row in all_rows
            if str((row.get("subscription") or {}).get("endpoint") or "") not in dead
        ])

    return {
        "ok": sent > 0,
        "sent": sent,
        "failed": failed,
        "removed": removed,
        "errors": errors[:3],
    }


def _pwa_alert_ack_key(room, alert_key):
    state = _load_openchat_alert_state()
    _, delivery = _openchat_get_delivery(state, room)
    sent_keys = list(dict.fromkeys(
        [str(x) for x in (delivery.get("sentKeys") or []) if str(x)] + [alert_key]
    ))[-1000:]
    delivery["sentKeys"] = sent_keys
    leases = delivery.get("leases") if isinstance(delivery.get("leases"), dict) else {}
    leases.pop(alert_key, None)
    delivery["leases"] = leases
    delivery["testQueue"] = [
        x for x in (delivery.get("testQueue") or [])
        if str((x or {}).get("key") or "") != alert_key
    ]
    delivery["boardPending"] = [
        x for x in (delivery.get("boardPending") or [])
        if str(((x or {}).get("item") or {}).get("key") or "") != alert_key
    ]
    state.setdefault("deliveries", {})[_openchat_delivery_key(room)] = delivery
    _save_openchat_alert_state(state)


def _pwa_alert_release_lease(room, alert_key):
    state = _load_openchat_alert_state()
    _, delivery = _openchat_get_delivery(state, room)
    leases = delivery.get("leases") if isinstance(delivery.get("leases"), dict) else {}
    leases.pop(alert_key, None)
    delivery["leases"] = leases
    state.setdefault("deliveries", {})[_openchat_delivery_key(room)] = delivery
    _save_openchat_alert_state(state)



def _agro_phase_shift_minutes(old_anchor, new_anchor):
    """Return the actual Agro cycle shift in minutes (-360..360).

    Agro repeats every 12 hours, so a 12-hour clock difference produces the
    same spawn cycle and should not be announced as a schedule change.
    """
    if old_anchor is None or new_anchor is None:
        return 0
    old_m = old_anchor.hour * 60 + old_anchor.minute
    new_m = new_anchor.hour * 60 + new_anchor.minute
    delta = (new_m - old_m) % (12 * 60)
    if delta > 6 * 60:
        delta -= 12 * 60
    return int(delta)


def _pwa_maintenance_change_candidate(now=None):
    """Return one pending maintenance-driven Agro schedule change.

    The first observed maintenance source is only a baseline. A notification is
    created only when a genuinely newer maintenance source changes the 12-hour
    Agro phase. The marker is advanced only after a successful push, so a
    transient push failure is retried on the next check.
    """
    now = now or datetime.now(KST)
    data = _load_boss_schedule_overrides()
    current = data.get("agroOfficial") if isinstance(data.get("agroOfficial"), dict) else {}
    source_id = str(current.get("sourceId") or "").strip()
    source_title = str(current.get("sourceTitle") or "").strip()
    new_anchor = _parse_kst_iso(current.get("anchor"))
    if not source_id or new_anchor is None:
        return None

    marker = data.get("pwaMaintenanceAlert") if isinstance(data.get("pwaMaintenanceAlert"), dict) else {}
    marker_source = str(marker.get("sourceId") or "").strip()
    old_anchor = _parse_kst_iso(marker.get("anchor"))

    # First run after this feature is installed: establish a baseline without
    # sending an old maintenance notice as if it were new.
    if not marker.get("initialized"):
        data["pwaMaintenanceAlert"] = {
            "initialized": True,
            "sourceId": source_id,
            "sourceTitle": source_title,
            "anchor": new_anchor.isoformat(),
            "updatedAt": now.isoformat(),
        }
        _save_boss_schedule_overrides(data)
        return None

    if marker_source == source_id:
        return None

    phase_delta = _agro_phase_shift_minutes(old_anchor, new_anchor)

    # A new maintenance notice with the same 12-hour Agro phase does not change
    # Agro spawn times. Advance the marker silently so it is not reconsidered.
    if old_anchor is not None and phase_delta == 0:
        data["pwaMaintenanceAlert"] = {
            "initialized": True,
            "sourceId": source_id,
            "sourceTitle": source_title,
            "anchor": new_anchor.isoformat(),
            "updatedAt": now.isoformat(),
        }
        _save_boss_schedule_overrides(data)
        return None

    next_agro = next_agro_from_anchor(new_anchor, now)
    old_clock = old_anchor.strftime("%H:%M") if old_anchor is not None else "기존"
    new_clock = new_anchor.strftime("%H:%M")
    if phase_delta > 0:
        shift_text = f"+{phase_delta // 60}시간 {phase_delta % 60}분" if phase_delta % 60 else f"+{phase_delta // 60}시간"
    elif phase_delta < 0:
        mins = abs(phase_delta)
        shift_text = f"-{mins // 60}시간 {mins % 60}분" if mins % 60 else f"-{mins // 60}시간"
    else:
        shift_text = "변경"

    body = f"점검 종료 {old_clock} → {new_clock} · 아그로 {shift_text} · 다음 {next_agro.strftime('%m/%d %H:%M')}"
    if source_title:
        body += f"\n{source_title[:70]}"

    return {
        "sourceId": source_id,
        "sourceTitle": source_title,
        "anchor": new_anchor,
        "payload": {
            "title": "⚠️ 아그로 시간 변경",
            "body": body,
            "url": "/?cmd=" + quote("아그로", safe=""),
            "tag": "agro-maintenance-" + re.sub(r"[^0-9A-Za-z_-]", "", source_id)[:80],
        },
    }


def _pwa_mark_maintenance_change_sent(candidate, now=None):
    if not isinstance(candidate, dict):
        return False
    anchor = candidate.get("anchor")
    if anchor is None:
        return False
    now = now or datetime.now(KST)
    data = _load_boss_schedule_overrides()
    data["pwaMaintenanceAlert"] = {
        "initialized": True,
        "sourceId": str(candidate.get("sourceId") or ""),
        "sourceTitle": str(candidate.get("sourceTitle") or ""),
        "anchor": anchor.astimezone(KST).isoformat(),
        "updatedAt": now.isoformat(),
    }
    return _save_boss_schedule_overrides(data)


async def _run_pwa_push_alert_check(source="background"):
    started = time.perf_counter()
    _scheduler_stamp(source)
    if not _pwa_push_configured():
        _scheduler_stamp(source, ok=False, duration_ms=int((time.perf_counter()-started)*1000), error="VAPID_NOT_CONFIGURED")
        return {"ok": False, "configured": False, "error": "VAPID_NOT_CONFIGURED"}
    if not _load_pwa_push_subscriptions():
        _scheduler_stamp(source, ok=True, sent=0, failed=0, items=0, duration_ms=int((time.perf_counter()-started)*1000))
        return {"ok": True, "configured": True, "subscriptions": 0, "items": 0, "sent": 0}

    async with PWA_PUSH_CHECK_LOCK:
        settings = _get_pwa_alert_settings()
        # Refresh the official maintenance anchor first. The normal source layer
        # refreshes every minute while maintenance is active, five minutes otherwise.
        try:
            await latest_maintenance_anchor()
        except Exception:
            pass

        maintenance_result = None
        maintenance_candidate = _pwa_maintenance_change_candidate(datetime.now(KST))
        if maintenance_candidate:
            if settings.get("maintenanceAgro", True):
                maintenance_result = await _pwa_send_payload_to_all(maintenance_candidate["payload"])
                if maintenance_result.get("ok"):
                    _record_pwa_push_history(maintenance_candidate["payload"])
                    _pwa_mark_maintenance_change_sent(maintenance_candidate)
            else:
                # Silenced changes are still consumed so an old maintenance shift
                # does not fire later merely because the toggle was re-enabled.
                _pwa_mark_maintenance_change_sent(maintenance_candidate)

        body = await openchat_alerts(room=PWA_PUSH_ROOM)
        items = body.get("items") if isinstance(body, dict) else []
        if not isinstance(items, list):
            items = []

        sent_total = int((maintenance_result or {}).get("sent") or 0)
        failed_total = int((maintenance_result or {}).get("failed") or 0)
        results = []
        if maintenance_result is not None:
            results.append({"key": "maintenance-agro-change", **maintenance_result})
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not _pwa_item_enabled(item, settings):
                if key:
                    _pwa_alert_ack_key(PWA_PUSH_ROOM, key)
                continue
            payload = _pwa_push_payload(item)
            delivery = await _pwa_send_payload_to_all(payload)
            sent_total += int(delivery.get("sent") or 0)
            failed_total += int(delivery.get("failed") or 0)
            results.append({"key": key, **delivery})
            if key:
                if delivery.get("ok"):
                    _record_pwa_push_history(payload)
                    _pwa_alert_ack_key(PWA_PUSH_ROOM, key)
                else:
                    _pwa_alert_release_lease(PWA_PUSH_ROOM, key)

        duration_ms = int((time.perf_counter() - started) * 1000)
        _scheduler_stamp(
            source, ok=True, sent=sent_total, failed=failed_total,
            items=len(items), duration_ms=duration_ms
        )
        return {
            "ok": True,
            "configured": True,
            "subscriptions": len(_load_pwa_push_subscriptions()),
            "items": len(items),
            "sent": sent_total,
            "failed": failed_total,
            "durationMs": duration_ms,
            "source": source,
            "results": results,
        }


@app.get("/api/push/settings")
async def pwa_push_settings_get():
    return {"ok": True, "settings": _get_pwa_alert_settings()}


@app.post("/api/push/settings")
async def pwa_push_settings_set(request: Request):
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    current = _get_pwa_alert_settings()
    leads = raw.get("leads") if isinstance(raw, dict) else None
    if isinstance(leads, list):
        clean=[]
        for value in leads:
            try: n=int(value)
            except Exception: continue
            if n in (10, 30) and n not in clean: clean.append(n)
        if clean: current["leads"] = sorted(clean, reverse=True)
    for group in ("schedule", "boards"):
        rows = raw.get(group) if isinstance(raw, dict) and isinstance(raw.get(group), dict) else None
        if rows is not None:
            for key in current[group]:
                if key in rows: current[group][key] = bool(rows.get(key))
    if isinstance(raw, dict) and "maintenanceAgro" in raw:
        current["maintenanceAgro"] = bool(raw.get("maintenanceAgro"))
    ok = _save_pwa_alert_settings(current)
    return {"ok": bool(ok), "settings": current}


@app.get("/api/push/next")
async def pwa_push_next():
    now = datetime.now(KST)
    try:
        await latest_maintenance_anchor()
    except Exception:
        pass
    targets = _schedule_alert_targets(now)
    if targets:
        targets[0] = ("boss", "정령왕 아그로", next_agro_from_anchor(_cached_alert_agro_anchor(), now))
    settings = _get_pwa_alert_settings()
    out=[]
    for typ, name, target in targets:
        if target is None: continue
        minutes=max(0, int(round((target-now).total_seconds()/60.0)))
        key=_schedule_key(name)
        out.append({
            "type": typ, "key": key, "name": name, "time": target.strftime("%m/%d %H:%M"),
            "minutes": minutes, "targetIso": target.astimezone(KST).isoformat(),
            "enabled": bool((settings.get("schedule") or {}).get(key, True)),
            "leads": settings.get("leads") or [30,10],
        })
    out.sort(key=lambda x: x["minutes"])
    return {"ok": True, "items": out}


@app.get("/api/push/history")
async def pwa_push_history():
    rows = list(reversed(_load_pwa_push_history()[-30:]))
    return {"ok": True, "items": rows}


@app.get("/api/push/public-key")
async def pwa_push_public_key():
    return {
        "ok": True,
        "enabled": _pwa_push_configured(),
        "publicKey": PWA_VAPID_PUBLIC_KEY if _pwa_push_configured() else "",
    }


@app.get("/api/push/status")
async def pwa_push_status():
    scheduler = _load_pwa_scheduler_state()
    return {
        "ok": True,
        "configured": _pwa_push_configured(),
        "appVersion": PWA_APP_VERSION,
        "subscriptions": len(_load_pwa_push_subscriptions()),
        "persistentStorage": AION2_STORAGE_PERSISTENT,
        "scheduler": scheduler,
    }


@app.get("/api/push/diagnostics")
async def pwa_push_diagnostics():
    scheduler = _load_pwa_scheduler_state()
    ext_m = _minutes_since_iso(scheduler.get("lastExternalAt"))
    bg_m = _minutes_since_iso(scheduler.get("lastBackgroundAt"))
    any_m = _minutes_since_iso(scheduler.get("lastCheckAt"))
    return {
        "ok": True,
        "configured": _pwa_push_configured(),
        "subscriptions": len(_load_pwa_push_subscriptions()),
        "persistentStorage": AION2_STORAGE_PERSISTENT,
        "dataDir": str(AION2_DATA_DIR),
        "lastCheckMinutes": None if any_m is None else round(any_m, 1),
        "lastExternalMinutes": None if ext_m is None else round(ext_m, 1),
        "lastBackgroundMinutes": None if bg_m is None else round(bg_m, 1),
        "externalCronHealthy": ext_m is not None and ext_m <= 3.0,
        "backgroundHealthy": bg_m is not None and bg_m <= 2.5,
        "scheduler": scheduler,
        "recommendation": (
            "정상" if ext_m is not None and ext_m <= 3.0
            else "외부 1분 체크를 연결하면 Render 절전/지연에도 30분·10분 알림이 안정적입니다."
        ),
    }


@app.post("/api/push/subscribe")
async def pwa_push_subscribe(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    sub = data.get("subscription") if isinstance(data, dict) else None
    if not isinstance(sub, dict):
        return JSONResponse({"ok": False, "error": "BAD_SUBSCRIPTION"}, status_code=400)

    endpoint = str(sub.get("endpoint") or "").strip()
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return JSONResponse({"ok": False, "error": "BAD_SUBSCRIPTION"}, status_code=400)

    normalized = {
        "endpoint": endpoint,
        "expirationTime": sub.get("expirationTime"),
        "keys": {
            "p256dh": str(keys.get("p256dh") or ""),
            "auth": str(keys.get("auth") or ""),
        },
    }
    now_epoch = time.time()
    rows = _load_pwa_push_subscriptions()
    found = False
    for row in rows:
        if str((row.get("subscription") or {}).get("endpoint") or "") == endpoint:
            row["subscription"] = normalized
            row["label"] = str(data.get("label") or row.get("label") or "AION2 TOOL")[:80]
            row["updated"] = now_epoch
            found = True
            break
    if not found:
        rows.append({
            "subscription": normalized,
            "label": str(data.get("label") or "AION2 TOOL")[:80],
            "created": now_epoch,
            "updated": now_epoch,
        })
    ok = _save_pwa_push_subscriptions(rows[-20:])
    return {"ok": bool(ok), "subscriptions": len(rows[-20:]), "configured": _pwa_push_configured()}


@app.post("/api/push/unsubscribe")
async def pwa_push_unsubscribe(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    endpoint = str((data or {}).get("endpoint") or "").strip()
    rows = _load_pwa_push_subscriptions()
    kept = [
        row for row in rows
        if str((row.get("subscription") or {}).get("endpoint") or "") != endpoint
    ]
    _save_pwa_push_subscriptions(kept)
    return {"ok": True, "subscriptions": len(kept)}


@app.post("/api/push/test")
async def pwa_push_test(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    endpoint = str((data or {}).get("endpoint") or "").strip()
    if not endpoint:
        return JSONResponse({"ok": False, "error": "NO_ENDPOINT"}, status_code=400)
    payload = {
        "title": "🔔 AION2 TOOL",
        "body": "앱 푸시 알림 연결 완료 · 앱을 닫아도 알림을 받을 수 있습니다.",
        "url": "/",
        "tag": "aion2-push-test-" + str(int(time.time())),
    }
    result = await _pwa_send_payload_to_all(payload, only_endpoint=endpoint)
    if result.get("ok"):
        _record_pwa_push_history(payload)
    status = 200 if result.get("ok") else 500
    return JSONResponse(result, status_code=status)


@app.post("/api/push/test-scenario")
async def pwa_push_test_scenario(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    endpoint = str((data or {}).get("endpoint") or "").strip()
    kind = str((data or {}).get("kind") or "").strip().lower()
    if not endpoint:
        return JSONResponse({"ok": False, "error": "NO_ENDPOINT"}, status_code=400)

    now = datetime.now(KST)
    if kind == "lead30":
        payload = {
            "title": "🐲 [테스트] 아그로 30분 전",
            "body": "정령왕 아그로 출현까지 30분 · 실제 자동알림 형식 점검",
            "url": "/",
            "tag": "aion2-test-lead30-" + str(int(time.time())),
        }
    elif kind == "lead10":
        payload = {
            "title": "🚨 [테스트] 아그로 10분 전",
            "body": "정령왕 아그로 출현까지 10분 · 준비하세요.",
            "url": "/",
            "tag": "aion2-test-lead10-" + str(int(time.time())),
        }
    elif kind == "agrochange":
        nxt = now + timedelta(hours=2)
        payload = {
            "title": "⚠️ [테스트] 아그로 시간 변경",
            "body": f"점검 종료 06:00 → 08:00 · 아그로 +2시간 · 다음 {nxt.strftime('%m/%d %H:%M')}",
            "url": "/",
            "tag": "aion2-test-agrochange-" + str(int(time.time())),
        }
    else:
        return JSONResponse({"ok": False, "error": "BAD_TEST_KIND"}, status_code=400)

    result = await _pwa_send_payload_to_all(payload, only_endpoint=endpoint)
    if result.get("ok"):
        _record_pwa_push_history(payload)
    status = 200 if result.get("ok") else 500
    return JSONResponse({"ok": bool(result.get("ok")), "kind": kind, **result}, status_code=status)


@app.get("/alerts/check")
async def alerts_check(secret: str = ""):

    expected = str(os.getenv("ALERT_CRON_SECRET") or "").strip()
    if expected and str(secret or "") != expected:
        return JSONResponse({"ok": False, "error": "UNAUTHORIZED"}, status_code=403)
    try:
        return await _run_pwa_push_alert_check(source="external")
    except Exception as e:
        _scheduler_stamp("external", ok=False, error=f"{type(e).__name__}:{str(e)[:220]}")
        return JSONResponse({"ok": False, "error": "ALERT_CHECK_FAILED"}, status_code=500)


async def _pwa_push_background_loop():
    await asyncio.sleep(20)
    while True:
        try:
            if _pwa_push_configured() and _load_pwa_push_subscriptions():
                await _run_pwa_push_alert_check(source="background")
        except Exception as e:
            _scheduler_stamp("background", ok=False, error=f"{type(e).__name__}:{str(e)[:220]}")
        await asyncio.sleep(60)


@app.on_event("startup")
async def _start_pwa_push_background():
    global PWA_PUSH_BACKGROUND_TASK
    if PWA_PUSH_BACKGROUND_TASK is None or PWA_PUSH_BACKGROUND_TASK.done():
        PWA_PUSH_BACKGROUND_TASK = asyncio.create_task(_pwa_push_background_loop())


async def character_card_data_fast(nickname: str, server_name: str):
    """Fast OG-card snapshot. Never perform the expensive equipment-item crawl here."""
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip()
    if not nickname or server_name not in SERVER_ID_MAP:
        return None

    info = None
    stones = []

    # The direct chat lookup saves current official basic info before returning
    # the card URL, so this normally resolves from the persistent DB immediately.
    try:
        rows = await character_db_get(nickname, server_name)
        if rows:
            info = dict(rows[0])
    except Exception:
        info = None

    # Reuse the last successful official compare stone totals when available.
    # This keeps OG rendering deterministic and fast; detail/compare endpoints
    # continue to do their own live refresh.
    try:
        saved = await character_db_get_official_compare(
            nickname, server_name, max_age_seconds=None
        )
        for row in (saved or {}).get("magicStoneTotals") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            total = row.get("total")
            try:
                total = float(total or 0)
            except Exception:
                continue
            pct = any(x in name for x in ("증폭", "피해", "강타", "완벽"))
            suffix = "%" if pct else ""
            stones.append((name, "+" + pretty_number(total) + suffix))
    except Exception:
        pass

    # If compare has never been opened, use the detailed profile already saved
    # by the normal character lookup. This is a local DB read only, so the card
    # still renders immediately while showing the mounted magic-stone total.
    if not stones:
        try:
            _, saved_profile = await character_db_get_full_profile(
                nickname, server_name
            )
            if saved_profile:
                stones = aggregate_magic_stones(saved_profile)
        except Exception:
            pass

    # If the DB is empty (direct URL / first-ever hit), allow only a short basic
    # resolver attempt. Card rendering must not time out into a bare Kakao URL.
    if not info:
        try:
            resolved = await asyncio.wait_for(
                own_resolve_character(nickname, server_name), timeout=2.8
            )
            if isinstance(resolved, dict) and resolved.get("type") == "detail":
                info = dict(resolved.get("info") or {})
        except Exception:
            pass

    if not info:
        info = {
            "name": nickname,
            "server": server_name,
            "serverId": int(SERVER_ID_MAP.get(server_name) or 0),
            "characterId": "",
            "job": "AION2 캐릭터",
            "combatPower": 0,
            "level": 0,
            "profileImage": "",
        }

    info.setdefault("name", nickname)
    info.setdefault("server", server_name)
    info.setdefault("job", "AION2 캐릭터")
    info.setdefault("combatPower", 0)
    info.setdefault("profileImage", "")
    return {"info": info, "stones": stones}




@app.get("/api/card-stones")
async def character_card_stones_latest(name: str = "", server: str = ""):
    """Live mounted-stone refresh using the exact same NC official source as detail/compare."""
    nickname = str(name or "").strip()
    server_name = str(server or "").strip()
    if not nickname or server_name not in SERVER_ID_MAP:
        return {"ok": False, "fresh": False, "stones": []}

    try:
        # IMPORTANT: use the same official equipment + item-detail pipeline as
        # detail/compare. Do not use get_profile(fast=True) or an old full-profile
        # snapshot here; those can briefly show the right value and then overwrite
        # it with stale stone totals.
        src = await debug_official_equipped_stats_v2(
            nickname=nickname,
            server=server_name,
        )
        if isinstance(src, dict) and src.get("ok"):
            fresh_stones = _official_magic_stones_for_card(src)
            if fresh_stones:
                return {
                    "ok": True,
                    "fresh": True,
                    "stones": [
                        {"name": n, "value": v}
                        for n, v in fresh_stones
                    ],
                }
    except Exception:
        pass

    # Never overwrite the already-rendered card with an older DB snapshot.
    return {"ok": False, "fresh": False, "stones": []}


@app.get("/c/{nickname}/{server_name}")
async def character_card(nickname: str, server_name: str):
    try:
        data = await asyncio.wait_for(
            character_card_data_fast(nickname, server_name),
            timeout=3.2,
        )
    except Exception:
        data = None

    if not data:
        return HTMLResponse(
            "<html><body><h2>캐릭터 정보를 찾지 못했습니다.</h2></body></html>",
            status_code=404,
        )

    info = data["info"]
    stones = data["stones"]
    cp_short = round(info["combatPower"] / 1000) if info["combatPower"] else 0

    profile_image = escape(info.get("profileImage") or "")
    title = escape(f"{info['name']} · {info['server']} · {info['job']}")
    desc = escape(f"전투력 {cp_short} · AION2 캐릭터 정보")
    detail_url = (
        "/detail?name=" + quote(str(info.get("name") or nickname), safe="")
        + "&server=" + quote(str(info.get("server") or server_name), safe="")
    )

    stones_html = "".join(
        f'<div class="stone"><span>{escape(name)}</span><b>{escape(value)}</b></div>'
        for name, value in stones
    )
    if not stones_html:
        stones_html = '<div class="muted">캐릭터 기본정보: AION2 공식 정보실 기준</div>'

    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:type" content="website">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{profile_image}">
<meta name="twitter:card" content="summary_large_image">
<title>{title}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#0c1018;color:#f5f7fb;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif}}
.wrap{{min-height:100vh;padding:24px;display:flex;justify-content:center;align-items:flex-start}}
.card{{width:min(520px,100%);background:linear-gradient(145deg,#171e2b,#10151f);border:1px solid #2b3444;border-radius:24px;overflow:hidden;box-shadow:0 22px 70px rgba(0,0,0,.45)}}
.cardtop{{display:flex;justify-content:flex-end;padding:14px 18px 0}}
.detailbtn{{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;color:#eaf2ff;background:#172b47;border:1px solid #355d8f;border-radius:10px;padding:8px 12px;font-size:13px;font-weight:800}}
.detailbtn:active{{transform:translateY(1px)}}
.hero{{padding:18px 24px 24px;display:flex;gap:18px;align-items:center;background:radial-gradient(circle at 10% 10%,rgba(100,150,255,.25),transparent 50%)}}
.avatar{{width:96px;height:96px;border-radius:22px;object-fit:cover;background:#252d3b;border:1px solid #3a465b}}
.name{{font-size:28px;font-weight:800;margin-bottom:6px}}
.meta{{color:#aeb9cb;font-size:15px}}
.cp{{margin-top:8px;font-size:18px;font-weight:700}}
.section{{padding:20px 24px 24px}}
.section h3{{margin:0 0 14px;font-size:17px}}
.stone{{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid #252e3c}}
.stone span{{color:#c7cfdb}}
.stone b{{color:#fff}}
.muted{{color:#8e99aa}}
.stone-status{{margin:-4px 0 8px;color:#93a4bd;font-size:13px}}
.badge{{display:inline-block;margin-top:8px;padding:5px 9px;border-radius:999px;background:#202b3c;color:#b8c9e8;font-size:12px}}
</style>
</head>
<body>
<div class="wrap"><div class="card">
  <div class="cardtop"><a class="detailbtn" href="{detail_url}">상세보기</a></div>
  <div class="hero">
    <img class="avatar" src="{profile_image}" alt="">
    <div>
      <div class="name">{escape(info['name'])}</div>
      <div class="meta">{escape(info['server'])} · {escape(info['job'])}</div>
      <div class="cp">전투력 {cp_short}</div>
      <div class="badge">AION2 CHARACTER</div>
    </div>
  </div>
  <div class="section">
    <h3>💎 장착 마석 총합</h3>
    <div id="stoneStatus" class="stone-status">최신 마석 조회중...</div>
    <div id="stoneList">{stones_html}</div>
  </div>
</div></div>
<script>
(function(){{
  var box=document.getElementById("stoneList");
  var status=document.getElementById("stoneStatus");
  if(!box) return;
  var name={nickname!r};
  var server={server_name!r};
  function esc(v){{
    return String(v==null?"":v)
      .replace(/&/g,"&amp;")
      .replace(/</g,"&lt;")
      .replace(/>/g,"&gt;")
      .replace(/\"/g,"&quot;");
  }}
  fetch("/api/card-stones?name="+encodeURIComponent(name)+"&server="+encodeURIComponent(server),{{cache:"no-store"}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(status) status.style.display="none";
      if(!d || d.fresh !== true) return;
      var rows=(d&&d.stones)||[];
      if(!rows.length) return;
      box.innerHTML=rows.map(function(x){{
        return '<div class="stone"><span>'+esc(x.name)+'</span><b>'+esc(x.value)+'</b></div>';
      }}).join("");
    }})
    .catch(function(){{
      if(status) status.style.display="none";
    }});
}})();
</script>
</body>
</html>"""
    return HTMLResponse(html)





















# =========================================================
# OWN_STATS_V1 debug - official PlayNC character/info payload
# =========================================================

def _own_stats_collect_numeric(node, out=None, path="", depth=0):
    """Collect numeric-looking leaves from the official payload for schema discovery."""
    if out is None:
        out = []
    if depth > 12:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(value, (dict, list)):
                _own_stats_collect_numeric(value, out, child_path, depth + 1)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append({"path": child_path, "key": str(key), "value": value})
            elif isinstance(value, str):
                text = value.strip().replace(",", "")
                if re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", text):
                    num_text = text[:-1] if text.endswith("%") else text
                    try:
                        num = float(num_text)
                    except Exception:
                        continue
                    out.append({
                        "path": child_path,
                        "key": str(key),
                        "value": num,
                        "raw": value,
                        "unit": "percent" if text.endswith("%") else "number",
                    })
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _own_stats_collect_numeric(value, out, f"{path}[{i}]", depth + 1)
    return out


def _own_stats_v1_from_official_payload(payload, nickname, server_name, server_id, character_id):
    rows = _own_stats_collect_numeric(payload)

    # Canonical V4-like output keys.  Values are filled only when an exact
    # official key/label is present; no equipment reconstruction or guessing.
    aliases = {
        "attack": {"attack", "공격력"},
        "additionalAttack": {"additionalattack", "추가공격력", "추가 공격력"},
        "maximumAttack": {"maximumattack", "maxattack", "최대공격력", "최대 공격력"},
        "minimumAttack": {"minimumattack", "minattack", "최소공격력", "최소 공격력"},
        "attackIncreasePercent": {"attackincreasepercent", "공격력증가율", "공격력 증가율"},
        "accuracy": {"accuracy", "명중"},
        "weaponAccuracy": {"weaponaccuracy", "무기명중", "무기 명중"},
        "accuracyIncreasePercent": {"accuracyincreasepercent", "명중증가율", "명중 증가율"},
        "pveAccuracy": {"pveaccuracy", "pve명중", "pve 명중"},
        "critical": {"critical", "치명타"},
        "criticalIncreasePercent": {"criticalincreasepercent", "치명타증가율", "치명타 증가율"},
        "defense": {"defense", "방어력"},
        "armorDefense": {"armordefense", "장비방어력", "장비 방어력"},
        "defenseIncreasePercent": {"defenseincreasepercent", "방어력증가율", "방어력 증가율"},
        "penetration": {"penetration", "관통"},
        "pveAttack": {"pveattack", "pve공격력", "pve 공격력"},
        "bossAttack": {"bossattack", "보스공격력", "보스 공격력"},
        "frontAttack": {"frontattack", "전방공격력", "전방 공격력"},
        "backAttack": {"backattack", "rearattack", "후방공격력", "후방 공격력"},
        "damageAmplificationPercent": {"damageamplificationpercent", "피해증폭", "피해 증폭"},
        "weaponDamageAmplificationPercent": {"weapondamageamplificationpercent", "무기피해증폭", "무기 피해 증폭"},
        "pveDamageAmplificationPercent": {"pvedamageamplificationpercent", "pve피해증폭", "pve 피해 증폭"},
        "bossDamageAmplificationPercent": {"bossdamageamplificationpercent", "보스피해증폭", "보스 피해 증폭"},
        "criticalDamageAmplificationPercent": {"criticaldamageamplificationpercent", "치명타피해증폭", "치명타 피해 증폭"},
        "frontDamageAmplificationPercent": {"frontdamageamplificationpercent", "전방피해증폭", "전방 피해 증폭"},
        "backDamageAmplificationPercent": {"backdamageamplificationpercent", "reardamageamplificationpercent", "후방피해증폭", "후방 피해 증폭"},
        "hardHitPercent": {"hardhitpercent", "강타"},
        "perfectPercent": {"perfectpercent", "완벽"},
        "additionalHitAccuracyPercent": {"additionalhitaccuracypercent", "추가명중률", "추가 명중률"},
        "combatSpeedPercent": {"combatspeedpercent", "전투속도", "전투 속도"},
        "cooldownTimePercent": {"cooldowntimepercent", "쿨다운", "재사용시간", "재사용 시간"},
    }

    result = {
        "schema": "OWN_STATS_V1",
        "source": "plaync-character-info",
        "name": nickname,
        "server": server_name,
        "serverId": int(server_id),
        "characterId": str(character_id),
        "stats": {},
    }

    for row in rows:
        raw_key = re.sub(r"[^a-z0-9가-힣]", "", str(row.get("key") or "").lower())
        if not raw_key:
            continue
        for canonical, names in aliases.items():
            norm_names = {re.sub(r"[^a-z0-9가-힣]", "", n.lower()) for n in names}
            if raw_key in norm_names and canonical not in result["stats"]:
                result["stats"][canonical] = {
                    "value": row.get("value"),
                    "raw": row.get("raw", row.get("value")),
                    "path": row.get("path"),
                }
                break

    result["statCount"] = len(result["stats"])
    return result, rows






















# =========================================================
# Party composition image - fixed Google Sheet ranges
# Commands: !무스펠 / !성역3 / !성역4
# =========================================================

PARTY_SHEET_ID = "1TDkZojKWuHNfu5cl1lpuqVZvTKF6W9-c9WLjga8ihIc"
PARTY_CONFIGS = {
    "무스펠": {"sheet": "파티편성", "gid": "234073660", "range": "A1:D31"},
    "성역3": {"sheet": "파티편성", "gid": "234073660", "range": "A18:D31"},
    "성역4": {"sheet": "성역4", "gid": None, "range": "A1:D31"},
}


def _party_normalize(value: str) -> str:
    text = unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("!", "")
    return re.sub(r"\s+", "", text).strip().casefold()


def _party_get_config(section_name: str):
    key = _party_normalize(section_name)
    for name, cfg in PARTY_CONFIGS.items():
        if _party_normalize(name) == key:
            return name, cfg
    return None, None


async def _party_fetch_range(cfg):
    """Fetch exactly the configured A1 range as CSV; no section-name searching."""
    sheet_name = str(cfg.get("sheet") or "").strip()
    gid = str(cfg.get("gid") or "").strip()
    a1 = str(cfg.get("range") or "").strip()
    cache_key = f"party-range-v5:{sheet_name}:{gid}:{a1}"
    cached = cache_get(cache_key, 30)
    if cached is not None:
        return cached

    client = await get_http_client()
    attempts = [
        (
            f"https://docs.google.com/spreadsheets/d/{PARTY_SHEET_ID}/gviz/tq",
            {"tqx": "out:csv", "sheet": sheet_name, "range": a1},
        ),
    ]
    if gid:
        attempts.append((
            f"https://docs.google.com/spreadsheets/d/{PARTY_SHEET_ID}/gviz/tq",
            {"tqx": "out:csv", "gid": gid, "range": a1},
        ))

    last_error = None
    for url, params in attempts:
        try:
            res = await client.get(
                url,
                params=params,
                headers={
                    "Accept": "text/csv,text/plain,*/*;q=0.8",
                    "User-Agent": HEADERS["User-Agent"],
                },
                timeout=httpx.Timeout(connect=4.0, read=15.0, write=4.0, pool=3.0),
            )
            res.raise_for_status()
            raw = (res.text or "").lstrip("\ufeff").strip()
            if not raw or raw[:120].lower().startswith(("<!doctype", "<html", "<head")):
                raise RuntimeError("Google Sheet CSV 대신 HTML 응답")
            rows = [list(row) for row in csv.reader(io.StringIO(raw))]
            if not rows:
                raise RuntimeError("Google Sheet 범위 데이터 없음")
            # The requested range is 5 columns. Preserve blank cells/rows as much as CSV permits.
            width = 5
            normalized = []
            for row in rows:
                row = list(row) + [""] * max(0, width - len(row))
                normalized.append([str(v or "").strip() for v in row[:width]])
            cache_set(cache_key, normalized)
            return normalized
        except Exception as e:
            last_error = e

    raise last_error or RuntimeError("Google Sheet 범위 조회 실패")


def _party_svg(display_name: str, rows):
    """Pure-SVG renderer: no Pillow/font package dependency on Render."""
    if not rows:
        rows = [["데이터 없음", "", "", "", ""]]

    cols = 5
    rows = [list(r) + [""] * max(0, cols - len(r)) for r in rows]
    rows = [r[:cols] for r in rows]

    # Fixed proportions matching the sheet: party / name / class / power / role.
    col_widths = [150, 220, 150, 180, 240]
    row_h = 54
    title_h = 70
    margin = 18
    width = sum(col_widths) + margin * 2
    height = title_h + row_h * len(rows) + margin

    def sx(v):
        return escape(str(v or ""))

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<style>text{font-family:"Noto Sans KR","Apple SD Gothic Neo","Malgun Gothic",sans-serif}</style>',
        f'<text x="{margin}" y="44" font-size="30" font-weight="700" fill="#111827">{sx(display_name)} 파티편성</text>',
    ]

    y0 = title_h
    for r_idx, row in enumerate(rows):
        row_text = " ".join(str(x or "") for x in row)
        is_header = ("1파티" in row_text or "2파티" in row_text or "1공대" in row_text or "2공대" in row_text)
        # In the screenshots, the top summary rows are blue-ish and blank separators are white.
        is_summary = r_idx < 2 and any(str(x or "").strip() for x in row)
        fill = "#6d9eea" if is_header else ("#d9e8fb" if is_summary else "#ffffff")
        text_fill = "#ffffff" if is_header else "#111827"
        weight = "700" if (is_header or is_summary) else "500"

        x = margin
        y = y0 + r_idx * row_h
        for c_idx in range(cols):
            w = col_widths[c_idx]
            out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{row_h}" fill="{fill}" stroke="#202020" stroke-width="1"/>')
            value = str(row[c_idx] or "")
            if value:
                # Keep long role notes readable without breaking the SVG.
                max_chars = [10, 15, 10, 12, 18][c_idx]
                shown = value if len(value) <= max_chars else value[:max_chars-1] + "…"
                out.append(
                    f'<text x="{x + w/2}" y="{y + 34}" text-anchor="middle" font-size="20" font-weight="{weight}" fill="{text_fill}">{sx(shown)}</text>'
                )
            x += w

    out.append('</svg>')
    return "".join(out)


@app.get("/party-image/{section_name}")
async def party_image(section_name: str):
    display_name, cfg = _party_get_config(section_name)
    if not cfg:
        return PlainTextResponse("지원하지 않는 파티편성입니다.", status_code=404)

    try:
        # ONE pipeline only:
        # Google Sheet fixed range CSV -> server-side PNG -> image/png
        rows = await _party_fetch_range(cfg)

        # Keep the exact configured row count even when trailing rows are blank.
        m = re.fullmatch(r"[A-Za-z]+(\d+):[A-Za-z]+(\d+)", str(cfg.get("range") or "").strip())
        expected_rows = (int(m.group(2)) - int(m.group(1)) + 1) if m else len(rows)
        while len(rows) < expected_rows:
            rows.append(["", "", "", "", ""])
        rows = rows[:expected_rows]

        from PIL import Image, ImageDraw, ImageFont

        def load_font(size, bold=False):
            paths = []
            if bold:
                paths += [
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
                    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
                    "/usr/share/fonts/truetype/nanum/NanumGothicCoding-Bold.ttf",
                ]
            paths += [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                "/usr/share/fonts/truetype/nanum/NanumGothicCoding.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
            for fp in paths:
                try:
                    return ImageFont.truetype(fp, size=size)
                except Exception:
                    continue
            return ImageFont.load_default()

        title_font = load_font(30, True)
        header_font = load_font(21, True)
        body_font = load_font(20, False)

        # Fixed 5-column sheet layout: 파티 / 이름 / 직업 / 전투력 / 비고
        col_widths = [145, 225, 150, 185, 265]
        row_h = 52
        title_h = 68
        margin = 18
        width = sum(col_widths) + margin * 2
        height = title_h + len(rows) * row_h + margin

        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)
        draw.text((margin, 16), f"{display_name} 파티편성", fill="black", font=title_font)

        y0 = title_h
        for r_idx, row in enumerate(rows):
            row = list(row) + [""] * max(0, 5 - len(row))
            row = row[:5]
            joined = " ".join(str(x or "") for x in row)

            party_header = any(x in joined for x in ("1파티", "2파티", "1공대", "2공대"))
            summary = r_idx in (0, 1) and any(str(x or "").strip() for x in row)

            fill = "#6d9eea" if party_header else ("#dbeafe" if summary else "white")
            font = header_font if (party_header or summary) else body_font
            text_fill = "white" if party_header else "black"

            x = margin
            y = y0 + r_idx * row_h
            for c, cell in enumerate(row):
                w = col_widths[c]
                draw.rectangle(
                    [x, y, x + w, y + row_h],
                    fill=fill,
                    outline="#1f2937",
                    width=1,
                )
                value = str(cell or "").strip()
                if value:
                    # Keep long notes within the cell.
                    max_chars = [10, 16, 10, 12, 20][c]
                    shown = value if len(value) <= max_chars else value[:max_chars - 1] + "…"
                    bbox = draw.textbbox((0, 0), shown, font=font)
                    tw = bbox[2] - bbox[0]
                    th = bbox[3] - bbox[1]
                    tx = x + max(8, (w - tw) / 2)
                    ty = y + max(5, (row_h - th) / 2 - 2)
                    draw.text((tx, ty), shown, fill=text_fill, font=font)
                x += w

        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return Response(
            content=out.getvalue(),
            media_type="image/png",
            headers={
                "Cache-Control": "no-store, max-age=0",
            },
        )

    except ImportError as e:
        return PlainTextResponse(
            "파티편성 PNG 생성 실패: Pillow(PIL)가 서버에 설치되어 있지 않습니다. "
            f"({type(e).__name__}: {str(e)[:180]})",
            status_code=500,
        )
    except Exception as e:
        # No SVG/other fallback: show the real error immediately.
        return PlainTextResponse(
            f"파티편성 PNG 생성 실패: {type(e).__name__}: {str(e)[:300]}",
            status_code=500,
        )


@app.get("/party-card/{section_name}")
async def party_card(section_name: str):
    display_name, cfg = _party_get_config(section_name)
    if not cfg:
        return HTMLResponse("<h2>지원하지 않는 파티편성입니다.</h2>", status_code=404)

    encoded = quote(display_name, safe="")
    image_url = f"https://aion2-kakao-bot.onrender.com/party-image/{encoded}"
    title = escape(f"{display_name} 파티편성")
    desc = escape("1파티 · 2파티")
    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:type" content="website">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{image_url}">
<meta name="twitter:card" content="summary_large_image">
<title>{title}</title>
<style>
body{{margin:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif}}
.wrap{{max-width:1200px;margin:24px auto;padding:16px}}
.card{{background:#fff;border-radius:16px;padding:16px;box-shadow:0 8px 24px rgba(0,0,0,.08)}}
h2{{margin:0 0 12px}}img{{display:block;width:100%;height:auto;border-radius:10px}}
</style>
</head>
<body><div class="wrap"><div class="card"><h2>{title}</h2><img src="{image_url}" alt="{title}"></div></div></body>
</html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})



async def _format_openchat_alert_diagnostic(room: str = "", room_label: str = ""):
    """Read-only alert diagnostics for the exact room name received from Kakao."""
    now = datetime.now(KST)
    state = _load_openchat_alert_state()
    room_key = _openchat_room_key(room)
    display_room = _openchat_room_key(room_label) or room_key
    enabled = _openchat_alert_enabled(state, room)
    _delivery_key, delivery = _openchat_get_delivery(state, room)
    sent = set(str(x) for x in delivery.get("sentKeys", []))

    try:
        await refresh_boss_rules()
    except Exception:
        pass

    try:
        maintenance_anchor = await latest_maintenance_anchor()
    except Exception:
        maintenance_anchor = _persisted_official_agro_anchor() or AGRO_FALLBACK_ANCHOR

    targets = _schedule_alert_targets(now)
    if targets:
        targets[0] = ("boss", "정령왕 아그로", next_agro_from_anchor(maintenance_anchor, now))

    lines = [
        "🔎 알림 진단",
        "",
        f"방: {display_room or '(빈 방이름)'}",
        f"상태: {'ON' if enabled else 'OFF'}",
        f"서버시간: {now.strftime('%Y-%m-%d %H:%M:%S')} KST",
        f"방 초기화: {'완료' if delivery.get('initialized') else '미완료'}",
        "",
    ]

    for item_type, name, target in targets:
        if target is None:
            lines.append(f"• {name}: 다음 일정 계산 실패")
            continue

        minutes = (target - now).total_seconds() / 60.0
        leads = _get_schedule_alert_leads(name, room)
        eligible = None
        already = False
        for lead in leads:
            key = _scheduled_alert_key(room, name, target, lead)
            legacy_key = _legacy_scheduled_alert_key(name, target, lead)
            window = min(3, lead)
            if max(0, lead - window) < minutes <= lead:
                eligible = lead
                already = key in sent or legacy_key in sent
                break

        lead_text = "/".join(str(x) for x in leads) + "분 전"
        if eligible is not None:
            status = f"지금 {eligible}분 알림 대상"
            if already:
                status += " (ACK 확인됨)"
            else:
                status += " (전송 가능)"
        elif minutes <= 0:
            status = "이미 시작/종료"
        else:
            status = "현재 알림 구간 아님"

        lines.extend([
            f"• {name}",
            f"  다음: {target.strftime('%a %H:%M')} / {minutes:.1f}분 남음",
            f"  설정: {lead_text}",
            f"  판정: {status}",
        ])

    return "\n".join(lines)


def _queue_openchat_test_alert(room: str, scenario: str = "generic"):
    room_key = _openchat_room_key(room)
    if not room_key:
        return False
    try:
        state = _load_openchat_alert_state()
        _, delivery = _openchat_get_delivery(state, room)
        nonce = int(time.time() * 1000)
        scenario_key = re.sub(r"[^0-9A-Za-z_-]", "", str(scenario or "generic"))[:40] or "generic"
        key = f"TEST|{room_key}|{scenario_key}|{nonce}"
        # Keep only the newest test alert for this room.
        # Re-running a test replaces any still-retryable older test.
        delivery["testQueue"] = [{
            "key": key,
            "created": time.time(),
            "scenario": scenario_key,
        }]
        state.setdefault("deliveries", {})[_openchat_delivery_key(room)] = delivery
        return _save_openchat_alert_state(state)
    except Exception:
        return False


@app.get("/openchat")
async def openchat(msg: str = "", room: str = "", room_alias: str = ""):
    _migrate_openchat_room_alias(room, room_alias)
    display_room = _openchat_room_key(room_alias) or _openchat_room_key(room)
    command = clean_command(msg)
    if not command.startswith(("!", ".")):
        return PlainTextResponse("명령어 앞에 ! 또는 . 을 붙여주세요.", media_type="text/plain; charset=utf-8")

    body = command[1:].strip()
    if not body:
        return PlainTextResponse("!윤이 / !랭킹 윤이지켈 / !필보 / !인원 / !공지 / !CM", media_type="text/plain; charset=utf-8")

    # 봇 전용 명령은 반드시 여기서 끝난다. 캐릭터 검색으로 fall-through 금지.
    if body in ("설명", "도움", "명령어", "사용법"):
        return PlainTextResponse(
            "📘 AION2 봇 사용법\n(! 또는 . 둘 다 사용 가능)\n\n"
            "⚔️ 캐릭터\n!윤이\n!윤이지켈 / !지켈윤이\n!윤이 지켈\n\n"
            "🏆 랭킹\n!랭킹 윤이지켈\n\n"
            "🐲 필드보스\n!필보 / !아그로 / !카이라 / !나흐마 / !어비스 / !필드보스\n"
            "⚔️ 콘텐츠\n!시공 / !균열 / !아티\n\n"
            "📢 소식\n!공지 / !CM / !업데이트\n\n"
            "👥 파티편성\n!무스펠 / !성역3 / !성역4 / !비탄\n\n"
            "👥 기타\n!인원\n!비교\n!앱\n\n"
            "🔔 알림\n!알림켜기 / !알림끄기 / !알림상태\n"
            "!방이름 / !봇상태 / !알림진단 / !알림큐 / !알림테스트 / !알림기본\n"
            "!알림30테스트 / !알림10테스트 / !콘텐츠30테스트 / !콘텐츠10테스트 / !아그로변경테스트\n"
            "기본: 모든 일정 30분 전 + 10분 전\n"
            "시간 수정: !아그로 06:00 / !시공 20:00\n"
            "알림 수정: !아그로 25분전 / !시공 25분전 10분전",
            media_type="text/plain; charset=utf-8",
        )

    if body == "앱":
        return PlainTextResponse(
            "📱 AION2 TOOL\nhttps://aion2-kakao-bot.onrender.com",
            media_type="text/plain; charset=utf-8",
        )

    if body == "봇상태":
        state = _load_openchat_alert_state()
        _key, delivery = _openchat_get_delivery(state, room)
        last = _parse_kst_iso(delivery.get("lastPollAt"))
        age = None if last is None else max(0, int((datetime.now(KST) - last).total_seconds()))
        leads = _get_schedule_alert_leads("아그로", room)
        text = (
            "💬 AION2 카카오 연동\n\n"
            f"방: {display_room or _openchat_room_key(room) or '(미확인)'}\n"
            f"알림: {'ON' if _openchat_alert_enabled(state, room) else 'OFF'}\n"
            f"자동 폴링: {'정상' if age is not None and age <= 60 else '확인 필요'}"
            + (f" ({age}초 전)" if age is not None else "") + "\n"
            f"일정 알림: {' / '.join(str(x)+'분 전' for x in leads)}\n"
            "점검 아그로 변경: 자동 1회 알림\n"
            f"서버: {PWA_APP_VERSION}"
        )
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    if body == "알림기본":
        _set_openchat_alert_enabled(True, room)
        for _name in ("아그로", "카이라", "나흐마", "어비스", "시공", "균열", "아티", "필드보스"):
            _set_schedule_alert_leads(_name, [30, 10], room)
        return PlainTextResponse(
            f"✅ [{display_room or '현재 방'}] 알림 기본값 적용\n모든 일정: 30분 전 + 10분 전\n점검 아그로 변경: 자동",
            media_type="text/plain; charset=utf-8",
        )

    if body == "비교":
        return PlainTextResponse(
            "https://aion2-kakao-bot.onrender.com/compare",
            media_type="text/plain; charset=utf-8",
        )

    if body == "인원":
        return PlainTextResponse(
            f"https://docs.google.com/spreadsheets/d/{PARTY_SHEET_ID}/edit?gid=0#gid=0",
            media_type="text/plain; charset=utf-8",
        )

    if body == "방이름":
        room_key = _openchat_room_key(room)
        text = f"🏠 봇이 인식한 방\n\n{display_room or '(빈 방이름)'}"
        if room_key and room_key != display_room:
            text += f"\nID: {room_key}"
        return PlainTextResponse(
            text,
            media_type="text/plain; charset=utf-8",
        )

    if body in ("알림테스트", "알림30테스트", "알림10테스트", "콘텐츠30테스트", "콘텐츠10테스트", "아그로변경테스트"):
        scenario_map = {
            "알림테스트": "generic",
            "알림30테스트": "boss30",
            "알림10테스트": "boss10",
            "콘텐츠30테스트": "content30",
            "콘텐츠10테스트": "content10",
            "아그로변경테스트": "agrochange",
        }
        scenario = scenario_map.get(body, "generic")
        ok = _queue_openchat_test_alert(room, scenario=scenario)
        if ok:
            labels = {
                "generic": "일반 자동알림",
                "boss30": "필드보스 30분 전",
                "boss10": "필드보스 10분 전",
                "content30": "콘텐츠 30분 전",
                "content10": "콘텐츠 10분 전",
                "agrochange": "아그로 점검 시간 변경",
            }
            text = (
                f"🧪 {labels.get(scenario, '자동알림')} 테스트 등록 완료\n\n"
                "메신저봇R 자동 폴링이 정상이라면 30초 안에 별도 테스트 알림이 1개 옵니다. "
                "전송/ACK가 실패해도 15초 주기로 다시 시도합니다."
            )
        else:
            text = "⚠️ 알림 테스트 등록 실패"
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    if body == "알림큐":
        state = _load_openchat_alert_state()
        _key, delivery = _openchat_get_delivery(state, room)
        now_epoch = time.time()
        tests = [x for x in (delivery.get("testQueue") or []) if isinstance(x, dict)]
        leases = delivery.get("leases") if isinstance(delivery.get("leases"), dict) else {}
        active_leases = {str(k): max(0, int(float(v) - now_epoch)) for k, v in leases.items() if _alert_float(v, 0.0) > now_epoch}
        sent_count = len([x for x in (delivery.get("sentKeys") or []) if str(x)])
        lines = [
            "🧪 자동알림 큐 진단",
            "",
            f"방: {display_room or _openchat_room_key(room) or '(미확인)'}",
            f"테스트 대기: {len(tests)}개",
            f"활성 lease: {len(active_leases)}개",
            f"ACK 완료 누적: {sent_count}개",
            f"마지막 폴링: {str(delivery.get('lastPollAt') or '없음')}",
        ]
        if tests:
            newest = tests[-1]
            lines.append(f"최근 테스트: {str(newest.get('scenario') or 'generic')} / {str(newest.get('key') or '')[-18:]}")
        if active_leases:
            sample_key = next(iter(active_leases.keys()))
            lines.append(f"lease 남음: {active_leases[sample_key]}초")
        return PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")

    if body == "알림진단":
        try:
            text = await asyncio.wait_for(_format_openchat_alert_diagnostic(room, display_room), timeout=12.0)
        except asyncio.TimeoutError:
            text = "⚠️ 알림 진단 지연\n서버 일정 조회가 12초를 초과했습니다."
        except Exception as e:
            text = f"⚠️ 알림 진단 실패\n{type(e).__name__}"
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    if body in ("알림켜기", "알림끄기", "알림상태", "테스트"):
        if body == "테스트":
            text = f"✅ AION2 {PWA_APP_VERSION} 서버 정상"
        elif body == "알림켜기":
            _enabled, room_key = _set_openchat_alert_enabled(True, room)
            target = f"[{display_room}] " if display_room else ""
            text = f"🔔 {target}알림 ON"
        elif body == "알림끄기":
            _enabled, room_key = _set_openchat_alert_enabled(False, room)
            target = f"[{display_room}] " if display_room else ""
            text = f"🔕 {target}알림 OFF"
        else:
            state = _load_openchat_alert_state()
            room_key = _openchat_room_key(room)
            enabled = _openchat_alert_enabled(state, room)
            target = f"[{display_room}] " if display_room else ""
            text = f"🔔 {target}알림 상태 : {'ON' if enabled else 'OFF'}"
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    # 파티편성 전용 명령: 캐릭터 검색으로 절대 내려가지 않는다.
    # !비탄은 !성역4와 같은 파티편성으로 연결한다.
    if body in ("무스펠", "성역3", "성역4", "비탄"):
        section_name = "성역4" if body == "비탄" else body
        url = "https://aion2-kakao-bot.onrender.com/party-card/" + quote(section_name, safe="")
        return PlainTextResponse(url, media_type="text/plain; charset=utf-8")

    if body == "랭킹" or body.startswith("랭킹 ") or (body.startswith("랭킹") and len(body) > 2):
        query = body[2:].strip()
        try:
            result = await asyncio.wait_for(ranking_lookup_smart(query), timeout=35.0)
        except asyncio.TimeoutError:
            result = "⚠️ 랭킹 조회 지연"
        except Exception:
            result = "⚠️ 랭킹 정보를 불러오지 못했습니다."
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    # Boss/content alert lead correction:
    # !아그로 25분전 / !시공 25분전 10분전 / !아그로 알림 25 10
    m_alert_lead = re.fullmatch(
        r"(아그로|카이라|나흐마|어비스|어비스보스|시공|균영|균열|균열지대|아티|아티쟁|필보|필드보스)\s+(?:(?:알림)\s+)?((?:\d{1,3}\s*(?:분전|분\s*전)?)(?:\s+\d{1,3}\s*(?:분전|분\s*전)?)*?)",
        body,
    )
    if m_alert_lead and ("분" in body or "알림" in body):
        result = _manual_alert_lead_command(m_alert_lead.group(1), m_alert_lead.group(2), room, display_room)
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    # Boss/content schedule correction: !카이라 02:00 / !시공 21:00 / !아그로 06:00
    m_schedule = re.fullmatch(
        r"(아그로|카이라|나흐마|어비스|어비스보스|시공|균영|균열|균열지대|아티|아티쟁|필보|필드보스)\s+(.+)",
        body,
    )
    if m_schedule:
        try:
            result = await asyncio.wait_for(
                _manual_schedule_command(m_schedule.group(1), m_schedule.group(2)),
                timeout=12.0,
            )
        except Exception:
            result = "⚠️ 시간 수정 실패"
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    if body in ("시공", "시공쟁탈전", "균영", "균열", "균열지대", "아티", "아티쟁", "필드보스"):
        try:
            result = await asyncio.wait_for(field_boss_lookup(body), timeout=8.0)
        except Exception:
            result = "⚠️ 일정 조회 실패"
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    if body in ("아그로", "카이라", "나흐마", "어비스", "어비스보스"):
        try:
            result = await asyncio.wait_for(field_boss_lookup(body), timeout=10.0)
        except Exception:
            result = "⚠️ 필드보스 조회 실패"
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    if body == "필보":
        try:
            result = await asyncio.wait_for(field_boss_lookup(), timeout=10.0)
        except Exception:
            result = "⚠️ 필드보스 조회 실패"
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    board_command = "CM" if body.casefold() == "cm" else body
    if board_command in ("공지", "CM", "업데이트"):
        try:
            result = await asyncio.wait_for(board_lookup(board_command), timeout=5.0)
        except Exception:
            result = f"⚠️ {board_command} 조회 실패"
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    boss_query = normalize_boss_query(body)
    if any(boss_query in normalize_boss_query(info["name"]) for info in BOSS_BY_CODE.values()):
        try:
            result = await asyncio.wait_for(field_boss_lookup(body), timeout=5.0)
        except Exception:
            result = "⚠️ 필드보스 조회 실패"
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    # 위 전용 명령 어느 것도 아닐 때만 캐릭터 검색.
    try:
        result = await asyncio.wait_for(character_lookup_smart(body), timeout=12.0)
    except asyncio.TimeoutError:
        result = "⚠️ 캐릭터 조회 지연"
    except Exception:
        result = "⚠️ 캐릭터 조회 실패"
    if not result:
        result = "캐릭터를 찾지 못했습니다."
    return PlainTextResponse(result, media_type="text/plain; charset=utf-8")









@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    try:
        payload = await request.json()
        command = clean_command(
            payload.get("userRequest", {}).get("utterance") or ""
        )

        if not command.startswith(("!", ".")):
            return JSONResponse(
                kakao_text("명령어 앞에 ! 또는 . 을 붙여주세요.\n예: !윤이 / .윤이 / !필보 / .필보")
            )

        body = command[1:].strip()

        if not body:
            return JSONResponse(
                kakao_text("사용법\n!윤이\n!필보\n!가르투아\n!아그로")
            )

        # ---------- Field boss ----------
        if body == "필보":
            try:
                result = await asyncio.wait_for(
                    field_boss_lookup(),
                    timeout=4.3,
                )
                return JSONResponse(kakao_text(result))
            except asyncio.TimeoutError:
                return JSONResponse(
                    kakao_text("⚠️ 필드보스 조회가 지연되고 있습니다.")
                )
            except Exception:
                return JSONResponse(
                    kakao_text("⚠️ 필드보스 정보를 불러오지 못했습니다.")
                )

        # If input matches a known boss name fragment, handle as boss lookup.
        boss_query = normalize_boss_query(body)
        known_boss_match = any(
            boss_query in normalize_boss_query(info["name"])
            for info in BOSS_BY_CODE.values()
        )

        if known_boss_match:
            try:
                result = await asyncio.wait_for(
                    field_boss_lookup(body),
                    timeout=4.3,
                )
                return JSONResponse(kakao_text(result))
            except asyncio.TimeoutError:
                return JSONResponse(
                    kakao_text("⚠️ 필드보스 조회가 지연되고 있습니다.")
                )
            except Exception:
                return JSONResponse(
                    kakao_text("⚠️ 필드보스 정보를 불러오지 못했습니다.")
                )

        # ---------- Official boards ----------
        board_command = "CM" if body.lower() == "cm" else body
        if board_command in BOARD_CONFIGS:
            try:
                result = await asyncio.wait_for(
                    board_lookup(board_command),
                    timeout=4.3,
                )
                return JSONResponse(kakao_text(result))
            except asyncio.TimeoutError:
                return JSONResponse(
                    kakao_text(f"⚠️ {board_command} 조회가 지연되고 있습니다.")
                )
            except Exception:
                return JSONResponse(
                    kakao_text(f"⚠️ {board_command} 정보를 불러오지 못했습니다.")
                )

        # ---------- Character ----------
        try:
            result = await asyncio.wait_for(
                character_lookup_smart(body),
                timeout=4.35,
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                kakao_text(
                    "⚠️ 캐릭터 조회가 지연되고 있습니다.\n"
                    "한 번 더 입력해 주세요."
                )
            )

        if not result:
            return JSONResponse(
                kakao_text(
                    f"🔎 {body}\n"
                    "전 서버에서 캐릭터를 찾지 못했습니다."
                )
            )

        return JSONResponse(kakao_text(result))

    except Exception:
        return JSONResponse(
            kakao_text("⚠️ 조회 중 오류가 발생했습니다.")
        )










async def _official_resolve_character_strict(nickname: str, server: str):
    """Resolve official NC character identity for compare/debug without legacy resolver."""
    nickname = str(nickname or '').strip()
    server = str(server or '').strip()
    server_id = int(SERVER_ID_MAP.get(server) or 0)
    if not nickname or not server_id:
        return None

    # 1) Existing DB identity, accepting both DB column names and normalized keys.
    try:
        db_rows = await character_db_get(nickname, server)
        for d in db_rows or []:
            cid = d.get('characterId') or d.get('character_id') or d.get('charId') or d.get('char_id')
            sid = d.get('serverId') or d.get('server_id') or server_id
            nm = d.get('name') or nickname
            if cid and int(sid or 0) == server_id and str(nm).casefold() == nickname.casefold():
                # Do not trust an old persisted characterId blindly. A deleted/recreated
                # or otherwise stale identity used to make compare fail before re-search.
                try:
                    live = await _official_get_json_live(
                        OFFICIAL_CHARACTER_INFO_API,
                        params={
                            'lang': 'ko',
                            'characterId': str(cid),
                            'serverId': server_id,
                        },
                        timeout=httpx.Timeout(connect=1.5, read=2.5, write=1.5, pool=1.5),
                    )
                    profile = live.get('profile') if isinstance(live, dict) else {}
                    live_name = _strip_html(
                        (profile or {}).get('name')
                        or (profile or {}).get('characterName')
                        or (live or {}).get('name')
                    )
                    if live_name and live_name.casefold() == nickname.casefold():
                        return {
                            'name': live_name,
                            'serverName': d.get('serverName') or d.get('server_name') or d.get('server') or server,
                            'serverId': server_id,
                            'characterId': str(cid),
                            'identitySource': 'db-live-validated',
                        }
                except Exception:
                    # Continue into official search below instead of failing compare
                    # on a stale DB identity.
                    pass
    except Exception:
        pass

    # 2) Existing official search helper. Clear only this identity search cache
    # so compare can recover immediately after a stale characterId.
    try:
        _drop_character_live_caches(nickname, server)
    except Exception:
        pass
    try:
        rows = await official_search_characters(nickname, server)
        for r in rows or []:
            cid = r.get('characterId') or r.get('character_id') or r.get('charId') or r.get('id')
            sid = r.get('serverId') or r.get('server_id') or server_id
            nm = _strip_html(r.get('name')) or nickname
            if cid and int(sid or 0) == server_id and str(nm).casefold() == nickname.casefold():
                return {**r, 'name': nm, 'serverId': server_id, 'characterId': unquote(str(cid))}
    except Exception:
        pass

    # 3) Raw NC search API fallback. Recursively scan response so schema changes
    #    (list/result/items/characters etc.) do not break compare identity lookup.
    def walk(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)

    for race in (2, 1):
        try:
            payload = await _official_get_json(
                OFFICIAL_CHARACTER_SEARCH_API,
                params={'keyword': nickname, 'race': race, 'serverId': server_id},
                timeout=httpx.Timeout(connect=4.0, read=12.0, write=4.0, pool=4.0),
            )
            for item in walk(payload):
                nm = _strip_html(item.get('name') or item.get('characterName') or item.get('nickname'))
                if not nm or nm.casefold() != nickname.casefold():
                    continue
                sid = item.get('serverId') or item.get('server_id') or server_id
                try:
                    sid = int(sid)
                except Exception:
                    continue
                if sid != server_id:
                    continue
                cid = item.get('characterId') or item.get('character_id') or item.get('charId') or item.get('char_id')
                # Generic 'id' is accepted only when it looks like the opaque character id,
                # not a small numeric search/result id.
                if not cid:
                    raw_id = item.get('id')
                    if isinstance(raw_id, str) and len(raw_id) >= 16:
                        cid = raw_id
                if cid:
                    return {
                        'name': nm,
                        'serverName': _strip_html(item.get('serverName') or item.get('server_name')) or server,
                        'serverId': sid,
                        'characterId': unquote(str(cid).strip()),
                        'identitySource': 'plaync-search',
                    }
        except Exception:
            continue

    # Final identity-only fallback: use the exact same resolver that powers the
    # normal character lookup. This is deliberately identity-only; compare stats
    # still come exclusively from the official NC info/equipment/item APIs.
    try:
        resolved = await own_resolve_character(nickname, server)
        if isinstance(resolved, dict) and resolved.get('type') == 'detail':
            r = resolved.get('row') or {}
            info = resolved.get('info') or {}
            cid = row_character_id(r) or str(info.get('characterId') or '').strip()
            sid = row_server_id(r) or int(info.get('serverId') or 0) or server_id
            nm = row_name(r) or str(info.get('name') or nickname).strip() or nickname
            sname = row_server_name(r) or str(info.get('server') or server).strip() or server
            if cid and int(sid or 0) == server_id and str(nm).casefold() == nickname.casefold():
                return {
                    'name': nm,
                    'serverName': sname,
                    'serverId': server_id,
                    'characterId': unquote(str(cid).strip()),
                    'identitySource': 'normal-resolver-id-only',
                }
    except Exception:
        pass

    # Last direct search fallback for cases where the normal resolver returns none.
    try:
        rows = await search_character_on_server(nickname, server)
        for r in rows or []:
            cid = row_character_id(r)
            sid = row_server_id(r) or server_id
            nm = row_name(r) or nickname
            if cid and int(sid or 0) == server_id and str(nm).casefold() == nickname.casefold():
                return {
                    'name': nm,
                    'serverName': row_server_name(r) or server,
                    'serverId': server_id,
                    'characterId': unquote(str(cid).strip()),
                    'identitySource': 'legacy-search-id-only',
                }
    except Exception:
        pass
    return None





