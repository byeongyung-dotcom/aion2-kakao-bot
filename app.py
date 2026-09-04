
import asyncio
import os
import json
import gzip
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
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
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
        lowered = title.lower()
        if "점검" in title:
            header = "🔧 AION2 점검 공지"
        elif "라이브" in title or "live" in lowered:
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
    "AmplifyWeaponDamage",
    "AmplifyCriticalDamage",
    "AmplifyBackAttack",
    "AmplifyFrontAttack",
}

STONE_ORDER = [
    "무기 피해 증폭",
    "치명타 피해 증폭",
    "후방 피해 증폭",
    "전방 피해 증폭",
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
    name = str(info.get("name") or "").strip()
    server = str(info.get("server") or "").strip()
    job = str(info.get("job") or "").strip()
    cp = int(info.get("combatPower") or 0)

    if not name:
        return None

    cp_short = round(cp / 1000) if cp else 0

    line2 = " · ".join(
        [x for x in (server, job, str(cp_short) if cp_short else "") if x]
    )

    lines = [f"⚔️ {name}"]
    if line2:
        lines.append(line2)

    # v61: restore magic-stone totals in normal character lookup.
    clean_stones = []
    for item in (stones or []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            sname = str(item[0] or "").strip()
            sval = str(item[1] or "").strip()
            if sname and sval:
                clean_stones.append((sname, sval))

    if clean_stones:
        lines += ["", "💎 마석 합계"]
        lines.extend(f"• {sname} {sval}" for sname, sval in clean_stones)

    if server:
        lines += [
            "",
            "🖼 프로필 보기",
            character_card_url(name, server),
        ]

    return "\n".join(lines)


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
    env_path = str(os.getenv("CHARACTER_DB_PATH") or "").strip()
    if env_path:
        return env_path

    if os.path.isdir("/var/data"):
        return "/var/data/aion2_characters.db"

    # Works immediately, but /tmp is not persistent across Render replacement.
    return "/tmp/aion2_characters.db"


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


async def own_resolve_character(nickname, server_name=None):
    """
    Resolution order:
      1. Read our own DB.
      2. Try NotMeter to refresh/fill.
      3. If NotMeter fails or returns nothing, keep using our DB.
    """
    nickname = str(nickname or "").strip()
    server_name = str(server_name or "").strip() or None

    db_infos = await character_db_get(
        nickname,
        server_name,
    )

    db_resolved = _db_resolved_from_infos(
        db_infos
    )

    # v59: DB hit is authoritative for normal lookup; do not block on upstream refresh.
    if db_infos:
        return db_resolved

    try:
        fresh = await asyncio.wait_for(
            resolve_character(nickname, server_name),
            timeout=6.0,
        )
        if fresh and fresh.get("type") != "none":
            await _save_notmeter_resolved(fresh)
            return fresh
    except Exception:
        pass

    return db_resolved


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

    # v61: enrich single-character lookup with the same saved detailed profile
    # used by /compare, so magic stones and compare stay in sync.
    if resolved.get("type") == "detail":
        lookup_server = explicit_server if explicit_server else (parsed[1] if 'parsed' in locals() and parsed else None)
        resolved = await _lookup_detail_with_saved_stones(resolved, nickname, lookup_server)

    if resolved.get("type") == "none":
        return None

    if resolved.get("type") == "multiple":
        return format_character_multiple(
            nickname,
            resolved.get("items") or [],
        )

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
    client = await get_http_client()

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

    response.raise_for_status()
    return response.json()


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


async def character_card_data(nickname: str, server_name: str):
    resolved = await own_resolve_character(
        nickname,
        server_name,
    )

    if resolved.get("type") != "detail":
        return None

    resolved = await _lookup_detail_with_saved_stones(
        resolved, nickname, server_name
    )

    return {
        "info": resolved.get("info") or {},
        "stones": resolved.get("stones") or [],
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
        {"name": "무기 피해 증폭", "reason": "무기/최대공격력 계열 영향은 확인됐지만 전체 스킬 최종배율 1:1 공식은 미확정"},
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

        target["combinedValue"] = float(current) + float(row["gap"])

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
            "무기 피해 증폭", "치명타", "명중", "강타",
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

</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <h1>AION2 프로 세팅 비교</h1>
    <div class="sub" id="loadStatus">캐릭터명과 서버를 각각 입력</div>
    <div class="search compare-search">
      <div class="search-pair a">
        <input id="nameA" value="윤이" placeholder="A 캐릭터명">
        <input id="serverA" value="지켈" placeholder="A 서버">
      </div>
      <div class="search-pair b">
        <input id="nameB" value="쵸비" placeholder="B 캐릭터명">
        <input id="serverB" value="지켈" placeholder="B 서버">
      </div>
      <button id="compareBtn">비교 분석</button>
    </div>
  </div>

  <div class="topgrid section">
    <div class="panel a" id="charA">
      <div class="char">
        <div class="avatar">윤</div>
        <div>
          <div class="name">윤이</div>
          <div class="meta">지켈 · 마도성 · Lv.50</div>
          <div class="cp">922K</div>
          <div class="badges"><span class="badge">원거리</span><span class="badge">포지션 수동</span></div>
        </div>
      </div>
    </div>
    <div class="panel b" id="charB">
      <div class="char">
        <div class="avatar">수</div>
        <div>
          <div class="name">쵸비</div>
          <div class="meta">지켈 · 마도성 · Lv.50</div>
          <div class="cp">968K</div>
          <div class="badges"><span class="badge">원거리</span><span class="badge">포지션 수동</span></div>
        </div>
      </div>
    </div>
  </div>

  <div class="panel section">
    <div class="head">핵심 격차</div>
    <div class="kpis" id="gapKpis">
      <div class="kpi"><div class="label">전투력 차이</div><div class="value bad">-46K</div></div>
      <div class="kpi"><div class="label">예상 딜 차이</div><div class="value bad">-7.4%</div></div>
      <div class="kpi"><div class="label">가장 큰 부족</div><div class="value">무피</div></div>
      <div class="kpi"><div class="label">가장 빠른 개선</div><div class="value">핵심 액티브</div></div>
    </div>
  </div>

  <div class="panel section">
    <div class="head">공격 스탯 비교</div>
    <div class="attack-groups" id="attackStats">
      <div class="attack-group">
        <div class="attack-group-title">공격 기반</div>
        <table><thead><tr><th>항목</th><th>A</th><th>B</th><th>차이</th></tr></thead><tbody>
          <tr data-stat="공격력"><td>공격력</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="추가 공격력"><td>추가 공격력</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="PVE 공격력"><td>PVE 공격력</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="보스 공격력"><td>보스 공격력</td><td>—</td><td>—</td><td>—</td></tr>
        </tbody></table>
      </div>
      <div class="attack-group">
        <div class="attack-group-title">피해 증폭</div>
        <table><thead><tr><th>항목</th><th>A</th><th>B</th><th>차이</th></tr></thead><tbody>
          <tr data-stat="피해 증폭"><td>일반 피해증폭</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="무기 피해 증폭"><td>무피</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="PVE 피해 증폭"><td>PVE 피해증폭</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="보스 피해 증폭"><td>보스 피해증폭</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="전방 피해 증폭"><td>전피</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="후방 피해 증폭"><td>후피</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="치명타 피해 증폭"><td>치피증</td><td>—</td><td>—</td><td>—</td></tr>
        </tbody></table>
      </div>
      <div class="attack-group">
        <div class="attack-group-title">조건 · 확률</div>
        <table><thead><tr><th>항목</th><th>A</th><th>B</th><th>차이</th></tr></thead><tbody>
          <tr data-stat="치명타"><td>치명타</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="강타"><td>강타</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="명중"><td>명중</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="관통"><td>관통</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="공격 속도"><td>공격 속도</td><td>—</td><td>—</td><td>—</td></tr>
          <tr data-stat="시전 속도"><td>시전 속도</td><td>—</td><td>—</td><td>—</td></tr>
        </tbody></table>
      </div>
    </div>
  </div>

  <div class="panel section">
    <div class="head">마석 세팅 비교</div>
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
  </div>

  <div class="panel section">
    <div class="head">성장 우선순위</div>
    <div class="prio" id="priorityCards">
      <div class="pcard"><div class="rank">#1 무기 피해증폭</div><div class="pct">+2.31%</div><div class="tiny">무피 11.4% → 14.1% 보완 시 예상 딜 회복</div></div>
      <div class="pcard"><div class="rank">#2 핵심 액티브 평균</div><div class="pct">+1.74%</div><div class="tiny">평균 Lv.16.3 → 17.5 수준 보완 기준</div></div>
      <div class="pcard"><div class="rank">#3 PVE 피해증폭</div><div class="pct">+1.12%</div><div class="tiny">9.8% → 10.9% 보완 기준</div></div>
    </div>
    <div class="note" id="priorityNote" style="margin-top:10px">치명·강타·명중은 보스 조건과 실제 발동률이 필요한 항목이라 단순 수치차만으로 1순위에 올리지 않는 구조.</div>
  </div>

    <div class="panel section damage-feedback-panel">
    <div class="head">캐릭터별 옵션 딜상승 피드백</div>
    <div class="note">검색된 현재 스탯으로 옵션 한 줄 추가 시 예상 상승률 계산</div>
    <div class="split feedback-split" style="margin-top:10px">
      <div class="owner a">
        <div class="ownerhead" id="feedbackHeadA">A</div>
        <div class="feedback-list" id="feedbackA"></div>
      </div>
      <div class="owner b">
        <div class="ownerhead" id="feedbackHeadB">B</div>
        <div class="feedback-list" id="feedbackB"></div>
      </div>
    </div>
  </div>

<div class="panel section">
    <div class="head" id="passiveTitle">마도성 패시브 1:1 비교</div>
    <div class="skill-readability-note"><span class="skill-chip">Lv.16 이상만</span><span class="skill-chip">중복 제거</span><span class="skill-chip">패시브만</span></div>
    <table class="level-table">
      <thead>
        <tr><th>마도성 패시브</th><th id="passiveAName">윤이</th><th id="passiveBName">쵸비</th><th>차이</th></tr>
      </thead>
      <tbody id="passiveTable">
        <tr><td>불의 표식</td><td>35</td><td>36</td><td class="bad">-1</td></tr>
        <tr><td>대지의 로브</td><td>32</td><td>34</td><td class="bad">-2</td></tr>
        <tr><td>냉기 소환</td><td>31</td><td>31</td><td>0</td></tr>
        <tr><td>불꽃의 로브</td><td>36</td><td>38</td><td class="bad">-2</td></tr>
        <tr><td>정기 흡수</td><td>34</td><td>35</td><td class="bad">-1</td></tr>
        <tr><td>저항의 은혜</td><td>24</td><td>24</td><td>0</td></tr>
        <tr><td>냉기의 로브</td><td>25</td><td>27</td><td class="bad">-2</td></tr>
        <tr><td>강화의 은혜</td><td>35</td><td>36</td><td class="bad">-1</td></tr>
        <tr><td>회생의 계약</td><td>22</td><td>22</td><td>0</td></tr>
        <tr><td>생기 증발</td><td>30</td><td>33</td><td class="bad">-3</td></tr>
      </tbody>
    </table>

    <div class="kpis" id="passiveKpis" style="margin-top:10px">
      <div class="kpi"><div class="label">20레벨+ 패시브 수</div><div class="value">10 / 10</div></div>
      <div class="kpi"><div class="label">평균 패시브 레벨</div><div class="value">29.4 / 30.6</div></div>
      <div class="kpi"><div class="label">가장 큰 차이</div><div class="value bad">생기 증발 -3</div></div>
      <div class="kpi"><div class="label">핵심 패시브 차이</div><div class="value bad">-1.4 평균</div></div>
    </div>
  </div>

  <div class="panel section">
    <div class="head" id="activeTitle">마도성 핵심 액티브 스킬 1:1 비교</div>
    <div class="note">같은 직업이므로 스킬명 자체를 맞춰서 비교. 레벨 차이뿐 아니라 최종본에서는 <b>레벨 차이 → 스킬 피해 차이 → 전체 사이클 DPS 영향</b>까지 계산.</div>
    <table class="level-table">
      <thead>
        <tr><th>마도성 핵심 액티브</th><th id="activeAName">윤이</th><th id="activeBName">쵸비</th><th>차이</th></tr>
      </thead>
      <tbody id="activeTable">
        <tr><td>불꽃 화살</td><td>20</td><td>20</td><td>0</td></tr>
        <tr><td>작렬</td><td>18</td><td>20</td><td class="bad">-2</td></tr>
        <tr><td>열화</td><td>16</td><td>18</td><td class="bad">-2</td></tr>
        <tr><td>얼음 사슬</td><td>14</td><td>14</td><td>0</td></tr>
        <tr><td>냉기 파동</td><td>16</td><td>16</td><td>0</td></tr>
        <tr><td>불꽃 작살</td><td>20</td><td>20</td><td>0</td></tr>
        <tr><td>혹한의 바람</td><td>18</td><td>20</td><td class="bad">-2</td></tr>
        <tr><td>불꽃 폭발</td><td>20</td><td>20</td><td>0</td></tr>
        <tr><td>화염 난사</td><td>16</td><td>18</td><td class="bad">-2</td></tr>
        <tr><td>빙결</td><td>12</td><td>12</td><td>0</td></tr>
        <tr><td>겨울의 속박</td><td>14</td><td>16</td><td class="bad">-2</td></tr>
        <tr><td>빙결 폭발</td><td>16</td><td>18</td><td class="bad">-2</td></tr>
        <tr><td>집중의 기원</td><td>20</td><td>20</td><td>0</td></tr>
        <tr><td>지옥의 화염</td><td>18</td><td>20</td><td class="bad">-2</td></tr>
        <tr><td>충격 해제</td><td>10</td><td>10</td><td>0</td></tr>
        <tr><td>저주: 고목</td><td>12</td><td>12</td><td>0</td></tr>
      </tbody>
    </table>

    <div class="kpis" id="activeKpis" style="margin-top:10px">
      <div class="kpi"><div class="label">20레벨 핵심 액티브 수</div><div class="value">4 / 6</div></div>
      <div class="kpi"><div class="label">평균 핵심 액티브 레벨</div><div class="value">16.3 / 17.5</div></div>
      <div class="kpi"><div class="label">주력 딜스킬 평균</div><div class="value bad">18.7 / 19.7</div></div>
      <div class="kpi"><div class="label">가장 큰 레벨 격차</div><div class="value bad">-2</div></div>
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
  return `<div class="char">${avatar}<div>
    <div class="name">${E(i.name||"-")}</div>
    <div class="meta">${E(i.server||"-")} · ${E(i.job||"-")} · Lv.${F(i.level)}</div>
    <div class="cp">${Math.round(Number(i.combatPower||0)/1000)}K</div>
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
function renderAttack(a,b){
  const A=canonMap(a),B=canonMap(b);
  document.querySelectorAll("#attackStats table").forEach(table=>{
    const th=table.querySelectorAll("thead th");
    if(th.length>=4){th[1].textContent=a?.info?.name||"A";th[2].textContent=b?.info?.name||"B";}
  });
  document.querySelectorAll("#attackStats tr[data-stat]").forEach(tr=>{
    const key=tr.dataset.stat, ar=A[key], br=B[key], av=valueOf(ar), bv=valueOf(br);
    const tds=tr.querySelectorAll("td");
    tds[1].innerHTML=av===null?"데이터 없음":`${pctOrNumber(key,ar,av)}${breakdownHTML(key,ar)}`;
    tds[2].innerHTML=bv===null?"데이터 없음":`${pctOrNumber(key,br,bv)}${breakdownHTML(key,br)}`;
    tds[3].className="";
    if(av===null||bv===null){tds[3].textContent="—";return}
    const d=av-bv;
    if(d<0)tds[3].classList.add("bad"); else if(d>0)tds[3].classList.add("good");
    tds[3].textContent=`${d>0?"+":""}${F(d)}${isPercentStat(key)?"%p":""}`;
  });
}

function renderStoneOwner(character, headId, listId){
  const name=character?.info?.name||"캐릭터";
  $(headId).textContent=`${name} 마석`;
  const rows=(character?.magicStoneTotals||[]).filter(x=>Number(x?.count||0)>0 || Number(x?.total||0)!==0);
  if(!rows.length){$(listId).innerHTML='<div class="stone-empty">확인된 마석 데이터 없음</div>';return;}
  const order=["무기 피해 증폭","PVE 피해 증폭","보스 피해 증폭","전방 피해 증폭","후방 피해 증폭","치명타 피해 증폭","피해 증폭","공격력","추가 공격력","치명타","명중","강타","관통","공격 속도","시전 속도"];
  const rank=x=>{const i=order.indexOf(String(x?.name||""));return i<0?999:i};
  rows.sort((x,y)=>rank(x)-rank(y)||String(x?.name||"").localeCompare(String(y?.name||"")));
  $(listId).innerHTML=rows.map(r=>{
    const key=String(r?.name||"마석"), count=Number(r?.count||0), total=Number(r?.total||0);
    const suffix=isPercentStat(key)?"%":"";
    return `<div class="stone-row"><div class="stone-name">${esc(key)}</div><div class="stone-count">× ${count}</div><div class="stone-total">+${F(total)}${suffix}</div></div>`;
  }).join("");
}
function renderStones(a,b){
  renderStoneOwner(a,"stoneHeadA","stoneListA");
  renderStoneOwner(b,"stoneHeadB","stoneListB");
}

function renderGap(d){
  const p=d?.proAnalysis||{},a=Number(d?.a?.info?.combatPower||0),b=Number(d?.b?.info?.combatPower||0);
  const pr=p.priorities||[], first=pr[0], second=pr[1];
  const cards=$("gapKpis").querySelectorAll(".kpi .value");
  if(cards.length>=4){
    cards[0].className="value "+(a>=b?"good":"bad");
    cards[0].textContent=`${a>=b?"+":""}${Math.round((a-b)/1000)}K`;
    cards[1].className="value "+(Number(p.expectedDamageGapPct||0)>=0?"good":"bad");
    cards[1].textContent=p.expectedDamageGapPct==null?"계산 대기":`${Number(p.expectedDamageGapPct).toFixed(2)}%`;
    cards[2].className="value"; cards[2].textContent=first?.name||"데이터 없음";
    cards[3].className="value"; cards[3].textContent=second?.name||first?.name||"데이터 없음";
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
    ? `조건부 확인: ${c.slice(0,5).map(x=>x.name).join(" · ")} — 단순 수치차만으로 우선순위를 정하지 않음.`
    : "검증 가능한 딜 회복량 기준으로 정렬.";
}



function filterLevelRows(rows){
  const best=new Map();
  (rows||[]).forEach(x=>{
    const name=String(x?.name||"").trim();
    const lv=Number(x?.level);
    if(!name || !Number.isFinite(lv) || lv<16) return;
    const key=name.replace(/\s+/g," ").toLowerCase();
    const old=best.get(key);
    if(!old || Number(old.level||0)<lv) best.set(key,{...x,level:lv});
  });
  return [...best.values()].sort((a,b)=>Number(b.level)-Number(a.level)||String(a.name).localeCompare(String(b.name),"ko"));
}
function avg(rows){
  const vals=(rows||[]).map(x=>Number(x.level)).filter(Number.isFinite);
  return vals.length?vals.reduce((a,b)=>a+b,0)/vals.length:0;
}
function levelMap(rows){const m={};(rows||[]).forEach(x=>{if(x?.name)m[x.name]=x});return m}
function renderLevelTable(a,b,type){
  const passive=type==="passive";
  const ra=filterLevelRows(passive?(a.passives||[]):(a.skills||[]));
  const rb=filterLevelRows(passive?(b.passives||[]):(b.skills||[]));
  const same=(a.info?.job||"")===(b.info?.job||"");
  const ma=levelMap(ra),mb=levelMap(rb);

  const title=$(passive?"passiveTitle":"activeTitle");
  const body=$(passive?"passiveTable":"activeTable");
  const table=body.closest("table");
  const th=table.querySelectorAll("thead th");

  title.textContent=same
    ? `${a.info?.job||""} ${passive?"패시브":"액티브 스킬"} 비교`
    : `${passive?"패시브":"액티브 스킬"} 직업별 비교`;

  if(same){
    th[0].textContent=passive?"패시브":"액티브 스킬";
    th[1].textContent=a.info?.name||"A";
    th[2].textContent=b.info?.name||"B";
    th[3].textContent="차이";

    const names=[...new Set([...Object.keys(ma),...Object.keys(mb)])].sort((x,y)=>{
      const mx=Math.max(Number(ma[x]?.level||0),Number(mb[x]?.level||0));
      const my=Math.max(Number(ma[y]?.level||0),Number(mb[y]?.level||0));
      return my-mx || x.localeCompare(y,"ko");
    });

    body.innerHTML=names.map(n=>{
      const av=Number(ma[n]?.level),bv=Number(mb[n]?.level);
      const validA=Number.isFinite(av),validB=Number.isFinite(bv);
      const d=validA&&validB?av-bv:null;
      const cls=d<0?"bad":d>0?"good":"";
      return `<tr><td>${E(n)}</td><td>${validA?av:"—"}</td><td>${validB?bv:"—"}</td><td class="${cls}">${d===null?"—":`${d>0?"+":""}${d}`}</td></tr>`;
    }).join("")||`<tr><td colspan="4">Lv.16 이상 데이터 없음</td></tr>`;
  }else{
    th[0].textContent=`${a.info?.name||"A"} ${passive?"패시브":"스킬"}`;
    th[1].textContent="Lv";
    th[2].textContent=`${b.info?.name||"B"} ${passive?"패시브":"스킬"}`;
    th[3].textContent="Lv";

    const n=Math.max(ra.length,rb.length);
    body.innerHTML=Array.from({length:n},(_,i)=>`
      <tr>
        <td>${E(ra[i]?.name||"—")}</td>
        <td>${ra[i]?.level??"—"}</td>
        <td>${E(rb[i]?.name||"—")}</td>
        <td>${rb[i]?.level??"—"}</td>
      </tr>
    `).join("")||`<tr><td colspan="4">Lv.16 이상 데이터 없음</td></tr>`;
  }

  const k=$(passive?"passiveKpis":"activeKpis").querySelectorAll(".kpi .value");
  const a20=ra.filter(x=>Number(x.level)>=20).length,b20=rb.filter(x=>Number(x.level)>=20).length;
  const aa=avg(ra),bb=avg(rb);

  if(k.length>=4){
    k[0].textContent=`${a20} / ${b20}`;
    k[1].textContent=`${aa.toFixed(1)} / ${bb.toFixed(1)}`;
    k[2].className="value "+(aa-bb<0?"bad":aa-bb>0?"good":"");
    k[2].textContent=`${aa-bb>0?"+":""}${(aa-bb).toFixed(1)}`;
    k[3].className="value";
    k[3].textContent=same?"이름 기준 1:1":"직업별 별도";
  }
}

function renderOptionFeedback(side, character){
  const key=String(side||"").toUpperCase();
  const head=$(key==="A"?"feedbackHeadA":"feedbackHeadB");
  const body=$(key==="A"?"feedbackA":"feedbackB");
  if(!head||!body) return;
  head.textContent=character?.info?.name||key;
  const fb=character?.optionFeedback||{};
  const rows=(Array.isArray(fb.ranked)&&fb.ranked.length)?fb.ranked:(Array.isArray(fb.options)?fb.options:[]);
  if(!rows.length){body.innerHTML='<div class="note">저장된 상세 스탯 데이터 없음</div>';return;}
  const bestName=fb?.best?.name||rows[0]?.name||"";
  body.innerHTML=rows.map(row=>{
    const current=Number(row?.current), gain=Number(row?.gainPct), delta=Number(row?.delta);
    const curText=Number.isFinite(current)?current.toFixed(2).replace(/\.00$/,""):"—";
    const gainText=Number.isFinite(gain)?`+${gain.toFixed(3)}%`:"—";
    const deltaText=Number.isFinite(delta)?`+${delta}`:"—";
    const best=String(row?.name||"")===String(bestName)?" feedback-best":"";
    return `<div class="feedback-row${best}"><div><div class="fname">${E(row?.name||"—")}</div><div class="fcur">현재 ${E(row?.stat||"")} ${curText}${row?.mode?` · ${E(row.mode)}`:""}</div></div><div class="fcur">${deltaText}</div><div class="fgain">${gainText}</div></div>`;
  }).join("");
}

function render(d){
  DATA=d;
  $("charA").innerHTML=profileHTML(d.a,"a");
  $("charB").innerHTML=profileHTML(d.b,"b");

  renderGap(d);
  renderAttack(d.a,d.b);
  renderStones(d.a,d.b);
  renderPriority(d.proAnalysis||{});
  renderOptionFeedback("A",d.a);
  renderOptionFeedback("B",d.b);
  renderLevelTable(d.a,d.b,"passive");
  renderLevelTable(d.a,d.b,"active");
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
    $("loadStatus").textContent=`${d.a.info?.name||A.name} ↔ ${d.b.info?.name||B.name} 비교 완료 · 스탯 ${ha.statCount||0}/${hb.statCount||0} · 스킬 ${ha.skillCount||0}/${hb.skillCount||0} · 패시브 ${ha.passiveCount||0}/${hb.passiveCount||0}`;
  }catch(e){
    console.error(e); $("loadStatus").textContent=`비교 처리 오류: ${e?.message||e}`;
  }finally{
    $("compareBtn").disabled=false;
  }
}


$("compareBtn").addEventListener("click",compare);
["nameA","serverA","nameB","serverB"].forEach(id=>$(id).addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();compare();}}));
$("modal").addEventListener("click",e=>{if(e.target===$("modal"))hide()});
window.addEventListener("DOMContentLoaded",compare);
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


@app.get("/debug/v48")
async def debug_v48():
    anchor = await latest_maintenance_anchor()
    now = datetime.now(KST)
    return {
        "ok": True,
        "version": "v59-selfdb-compare",
        "maintenanceAnchor": anchor.isoformat(),
        "maintenanceSourceTitle": _maintenance_anchor_cache.get("sourceTitle"),
        "maintenanceSourceId": _maintenance_anchor_cache.get("sourceId"),
        "nextAgro": next_agro_from_anchor(anchor, now).isoformat(),
        "kairaHours": BOSS_RULES.get("kairaHours"),
        "nahma": {
            "weekdays": BOSS_RULES.get("nahmaWeekdays"),
            "hour": BOSS_RULES.get("nahmaHour"),
            "minute": BOSS_RULES.get("nahmaMinute"),
        },
        "abyss": {
            "weekdays": BOSS_RULES.get("abyssWeekdays"),
            "hour": BOSS_RULES.get("abyssHour"),
            "minute": BOSS_RULES.get("abyssMinute"),
        },
        "noticeAlert": "title-only",
    }


@app.get("/debug/v53")
async def debug_v53():
    return {
        "ok": True,
        "version": "v53-stable-compare-ui",
        "compare": "/compare",
        "searchInputs": "separate-name-server",
        "equipmentArcanaModal": True,
        "layoutReloadOnSearch": False,
    }


@app.get("/debug/v54")
async def debug_v54():
    return {
        "ok": True,
        "version": "v54-locked-approved-ui",
        "ui": "approved-preview-locked",
        "attackRowsNeverDisappear": True,
        "separateNameServerInputs": True,
        "equipmentArcanaDynamic": True,
    }


@app.get("/debug/v55")
async def debug_v55(nickname: str = "윤이", server: str = "지켈"):
    data = await detailed_character_data(nickname, server)
    return {
        "ok": data.get("ok"),
        "version": "v55-real-data-recovery",
        "info": data.get("info"),
        "dataHealth": data.get("dataHealth"),
        "stats": data.get("stats"),
        "equipment": data.get("equipment"),
        "arcana": data.get("arcana"),
    }


@app.get("/debug/v56")
async def debug_v56():
    return {
        "ok": True,
        "version": "v56-equip-fold-skill-filter",
        "equipmentDefault": "collapsed",
        "skillMinLevel": 16,
        "passiveMinLevel": 16,
        "skillDedup": True,
        "passiveDedup": True,
    }


@app.get("/debug/v57")
async def debug_v57(nickname: str = "윤이", server: str = "지켈"):
    data = await detailed_character_data(nickname, server)
    return {
        "ok": data.get("ok"),
        "version": "v59-selfdb-compare",
        "info": data.get("info"),
        "dataHealth": data.get("dataHealth"),
        "optionFeedback": data.get("optionFeedback"),
        "stats": data.get("stats"),
        "skills": data.get("skills"),
        "passives": data.get("passives"),
    }


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
        data = await detailed_character_data(nickname, server)
        return {
            "version": "v53-stable-compare-ui",
            **data,
        }
    except Exception as e:
        return {
            "ok": False,
            "version": "v53-stable-compare-ui",
            "error": f"{type(e).__name__}: {str(e)[:400]}",
        }


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
                    "version": "v70-base-stats-reconstructed",
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
            "version": "v70-base-stats-reconstructed",
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
# v47 FIELD-BOSS TIMING POLICY
# =========================================================
#
# Agro:
# - Always use the most recent parsed maintenance END time as the anchor.
# - Refresh whenever a newer maintenance notice is found.
# - 12-hour cycle from that anchor.
#
# Kaira:
# - Fixed daily times: 01:00 / 05:00 / 09:00 / 13:00 / 17:00 / 21:00
#
# Nahma:
# - Fri / Sun 22:00
#
# Abyss boss:
# - Wed / Sat 22:30
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
}

BOSS_RULES = dict(DEFAULT_BOSS_RULES)
BOSS_RULES_META = {
    "updatedAt": None,
    "sources": {},
}

AGRO_FALLBACK_ANCHOR = datetime(2026, 9, 2, 7, 0, tzinfo=KST)

_boss_rule_refresh = {
    "ts": 0.0,
    "lock": asyncio.Lock(),
}

_maintenance_anchor_cache = {
    "value": None,
    "ts": 0.0,
}

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


def _parse_maintenance_end_from_text(source, posted_date=""):
    source = str(source or "")
    posted = str(posted_date or "")

    # Flexible: 9/4 04:30 ~ 06:00, 9.4 04:30 ~ 06:00,
    # "점검 일시 : 9/4(금) 04:30 ~ 06:00" etc.
    m = re.search(
        r"(?P<month>\d{1,2})\s*[/.]\s*(?P<day>\d{1,2})"
        r"(?:\s*\([^)]*\))?"
        r".{0,160}?"
        r"(?P<sh>\d{1,2})\s*:\s*(?P<sm>\d{2})"
        r"\s*(?:~|∼|～|–|—|-)\s*"
        r"(?P<eh>\d{1,2})\s*:\s*(?P<em>\d{2})",
        source,
        re.S,
    )
    if not m:
        return None

    year = datetime.now(KST).year
    if re.match(r"^\d{4}-\d{2}-\d{2}$", posted):
        try:
            year = int(posted[:4])
        except Exception:
            pass

    try:
        month = int(m.group("month"))
        day = int(m.group("day"))
        sh = int(m.group("sh"))
        sm = int(m.group("sm"))
        eh = int(m.group("eh"))
        em = int(m.group("em"))

        start = datetime(year, month, day, sh, sm, tzinfo=KST)
        end = datetime(year, month, day, eh, em, tzinfo=KST)

        if end <= start:
            end += timedelta(days=1)

        return end
    except Exception:
        return None


def _parse_maintenance_end_from_notice(row):
    title = str((row or {}).get("title") or "")
    raw = str((row or {}).get("rawText") or "")
    source = title + "\n" + raw
    return _parse_maintenance_end_from_text(
        source,
        (row or {}).get("date") or "",
    )

async def latest_maintenance_anchor():
    """
    Canonical Agro anchor:
    newest official maintenance notice whose actual body contains a complete
    maintenance time range. The article body is fetched, not just the list row.
    """
    now_ts = time.time()
    cached = _maintenance_anchor_cache.get("value")
    cached_ts = float(_maintenance_anchor_cache.get("ts") or 0)

    if cached is not None and now_ts - cached_ts < 300:
        return cached

    parsed_rows = []

    try:
        rows = await fetch_board_latest("공지", limit=30)

        maintenance_rows = [
            row for row in rows
            if "점검" in str(row.get("title") or "")
        ]

        # Inspect newest posts first. If the newest one is an edited temporary
        # maintenance notice, its body determines the new anchor.
        for row in maintenance_rows:
            detail_text = await fetch_notice_detail_text(row)
            parsed = _parse_maintenance_end_from_text(
                detail_text,
                row.get("date") or "",
            )

            if parsed is not None:
                parsed_rows.append({
                    "end": parsed,
                    "title": str(row.get("title") or ""),
                    "id": str(row.get("id") or ""),
                })

    except Exception:
        parsed_rows = []

    if parsed_rows:
        # Use the chronologically newest maintenance END, regardless of list
        # metadata order or which older row had an easy-to-parse rawText.
        parsed_rows.sort(key=lambda x: x["end"], reverse=True)
        chosen = parsed_rows[0]
        anchor = chosen["end"]

        _maintenance_anchor_cache["value"] = anchor
        _maintenance_anchor_cache["ts"] = now_ts
        _maintenance_anchor_cache["sourceId"] = chosen["id"]
        _maintenance_anchor_cache["sourceTitle"] = chosen["title"]

        BOSS_RULES_META["sources"]["maintenance"] = chosen["title"]
        return anchor

    anchor = cached if cached is not None else AGRO_FALLBACK_ANCHOR
    _maintenance_anchor_cache["value"] = anchor
    _maintenance_anchor_cache["ts"] = now_ts
    return anchor

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
    """
    Re-scan official Notice + Update every 5 minutes.
    Newer rows win because board APIs return newest first.
    """
    now_ts = time.time()
    if not force and now_ts - float(_boss_rule_refresh.get("ts") or 0) < 300:
        return BOSS_RULES

    async with _boss_rule_refresh["lock"]:
        now_ts = time.time()
        if not force and now_ts - float(_boss_rule_refresh.get("ts") or 0) < 300:
            return BOSS_RULES

        # Start from the current rule set, not defaults, so an ambiguous post
        # never wipes a previously confirmed schedule.
        try:
            rows = []
            for board_name in ("공지", "업데이트"):
                try:
                    rows.extend(await fetch_board_latest(board_name, limit=18))
                except Exception:
                    pass

            # Sort by date descending when available; original order is already
            # newest-first within each board.
            seen_sources = set()
            for row in rows:
                source_title = str(row.get("title") or "")
                source = (
                    source_title + "\n" +
                    str(row.get("rawText") or "")
                )

                if not source_title or source_title in seen_sources:
                    continue
                seen_sources.add(source_title)

                _apply_kaira_rule(source, source_title)
                _apply_agro_interval_rule(source, source_title)
                _apply_named_weekly_rule(
                    source, source_title,
                    "나흐마",
                    "nahmaWeekdays", "nahmaHour", "nahmaMinute",
                )
                _apply_named_weekly_rule(
                    source, source_title,
                    "어비스",
                    "abyssWeekdays", "abyssHour", "abyssMinute",
                )

            BOSS_RULES_META["updatedAt"] = datetime.now(KST).isoformat()
        finally:
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

def format_kaira_schedule():
    hours = list(BOSS_RULES["kairaHours"])
    lines = ["🐲 감시자 카이라", ""]
    for hour in hours:
        lines.append(f"⏰ {int(hour):02d}:00")
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
    kaira = _next_daily_hours(BOSS_RULES["kairaHours"], now)
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

async def fetch_board_latest(command: str, limit: int = 5):
    config = BOARD_CONFIGS[command]
    alias = config["alias"]

    url = f"{COMMUNITY_API}/{alias}/article/search/moreArticle"

    client = await get_http_client()
    response = await client.get(
        url,
        params={
            "isVote": "true",
            "moreSize": "18",
            "moreDirection": "BEFORE",
            "previousArticleId": "0",
        },
        headers=PLAYNC_HEADERS,
        timeout=httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=2.0),
    )
    response.raise_for_status()
    data = response.json()

    # PlayNC payload has normally been {"contentList":[...]}, but tolerate wrappers.
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

        date_text = str(posted)[:10] if posted else ""
        link = (
            f"https://aion2.plaync.com/ko-kr/board/"
            f"{config['view']}/view?articleId={content_id}"
        )

        rows.append({
            "id": str(content_id),
            "title": title,
            "date": date_text,
            "link": link,
            "rawText": json.dumps(item, ensure_ascii=False),
        })

        if len(rows) >= limit:
            break

    return rows

def format_board_latest(command: str, rows):
    if command == "공지":
        rows = [
            r for r in rows
            if "점검" in str(r.get("title") or "")
        ]
        if not rows:
            return "🔧 AION2 점검 공지\n\n현재 확인되는 점검 공지가 없습니다."

        row = rows[0]
        return "\n".join([
            "🔧 AION2 점검 공지",
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
    cached = cache_get(cache_key, 300)
    if cached:
        return cached

    rows = await fetch_board_latest(command, limit=18 if command == "공지" else 5)
    result = format_board_latest(command, rows)
    cache_set(cache_key, result)
    return result


# =========================================================
# New-post alerts
# =========================================================

# Filled later in Render Environment.
KAKAO_BOT_ID = os.getenv("KAKAO_BOT_ID", "").strip()
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_EVENT_NAME = os.getenv("KAKAO_EVENT_NAME", "aion2_new_post").strip()
ALERT_CRON_SECRET = os.getenv("ALERT_CRON_SECRET", "").strip()

# Optional permanent recipients:
# ALERT_BOT_USER_KEYS=key1,key2,key3
ENV_ALERT_USER_KEYS = [
    x.strip()
    for x in os.getenv("ALERT_BOT_USER_KEYS", "").split(",")
    if x.strip()
]

# State is kept on the web service. The 1-minute cron ping keeps a free web
# service awake. On a redeploy/restart the first check becomes a fresh baseline
# and sends ZERO old posts.
ALERT_STATE_FILE = Path(os.getenv("ALERT_STATE_FILE", "/tmp/aion2_alert_state.json"))

NOTICE_ALERT_KEYWORDS = ("점검", "라이브")

_alert_check_lock = asyncio.Lock()

def _default_alert_state():
    return {
        "initialized": False,
        "lastSeen": {"공지": None, "CM": None, "업데이트": None},
        "runtimeSubscribers": {},
        "sent": [],
    }

def _load_alert_state():
    try:
        if not ALERT_STATE_FILE.exists():
            return _default_alert_state()
        raw = json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
        state = _default_alert_state()
        state["initialized"] = bool(raw.get("initialized", False))
        if isinstance(raw.get("lastSeen"), dict):
            state["lastSeen"].update(raw["lastSeen"])
        if isinstance(raw.get("runtimeSubscribers"), dict):
            state["runtimeSubscribers"] = raw["runtimeSubscribers"]
        if isinstance(raw.get("sent"), list):
            state["sent"] = raw["sent"][-500:]
        return state
    except Exception:
        return _default_alert_state()

def _save_alert_state(state):
    try:
        ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ALERT_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(ALERT_STATE_FILE)
        return True
    except Exception:
        return False

def _extract_user_key(payload):
    user = ((payload.get("userRequest") or {}).get("user") or {})
    props = user.get("properties") or {}

    key = str(props.get("botUserKey") or "").strip()
    if key:
        return "botUserKey", key

    key = str(props.get("plusfriendUserKey") or "").strip()
    if key:
        return "plusfriendUserKey", key

    # Kakao skill payload generally exposes the bot user key as user.id.
    key = str(user.get("id") or "").strip()
    if key:
        return "botUserKey", key

    return None, None

def _register_alert_user(payload):
    user_type, user_id = _extract_user_key(payload)
    if not user_id:
        return False, "알림 등록용 사용자 키를 확인하지 못했습니다."

    state = _load_alert_state()
    state["runtimeSubscribers"][user_id] = {
        "type": user_type,
        "registeredAt": int(time.time()),
    }
    _save_alert_state(state)
    return True, (
        "🔔 새글 알림 등록 완료\n"
        "공지 : 새 공지 전체 자동알림\n"
        "CM : 새 글 전체\n"
        "업데이트 : 새 글 전체"
    )

def _unregister_alert_user(payload):
    _user_type, user_id = _extract_user_key(payload)
    if not user_id:
        return False, "알림 해제용 사용자 키를 확인하지 못했습니다."

    state = _load_alert_state()
    state["runtimeSubscribers"].pop(user_id, None)
    _save_alert_state(state)
    return True, "🔕 새글 알림을 해제했습니다."

def _alert_recipients(state):
    recipients = {}

    # Environment recipients survive web redeploys.
    for key in ENV_ALERT_USER_KEYS:
        recipients[key] = {"type": "botUserKey", "id": key}

    # Runtime registration is convenient while testing.
    for key, info in state.get("runtimeSubscribers", {}).items():
        recipients[key] = {
            "type": str(info.get("type") or "botUserKey"),
            "id": key,
        }

    return list(recipients.values())

def _should_alert(board_name, post):
    if board_name == "공지":
        title = str(post.get("title") or "")
        return any(keyword in title for keyword in NOTICE_ALERT_KEYWORDS)
    return board_name in ("CM", "업데이트")

async def _send_kakao_event(recipients, board_name, post):
    if not recipients:
        return {"status": "NO_RECIPIENTS"}

    if not KAKAO_BOT_ID or not KAKAO_REST_API_KEY:
        return {
            "status": "NOT_CONFIGURED",
            "message": "KAKAO_BOT_ID / KAKAO_REST_API_KEY missing",
        }

    url = f"https://bot-api.kakao.com/v2/bots/{KAKAO_BOT_ID}/talk"
    headers = {
        "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}",
        "Content-Type": "application/json",
    }

    results = []
    for start in range(0, len(recipients), 100):
        users = recipients[start:start + 100]
        body = {
            "event": {
                "name": KAKAO_EVENT_NAME,
                "data": {
                    "board": str(board_name),
                    "title": str(post.get("title") or ""),
                    "date": str(post.get("date") or ""),
                    "url": str(post.get("link") or ""),
                },
            },
            "user": users,
        }

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=body)

        try:
            response_json = response.json()
        except Exception:
            response_json = {"raw": response.text[:500]}

        results.append({
            "httpStatus": response.status_code,
            "response": response_json,
        })

    return {"status": "REQUESTED", "results": results}

async def _initialize_alert_baseline(state=None):
    """
    Safety rule:
    The first monitoring check stores the current latest IDs and sends NOTHING.
    Therefore old posts never fire merely because monitoring was enabled.
    """
    if state is None:
        state = _load_alert_state()

    for board_name in ("공지", "CM", "업데이트"):
        rows = await fetch_board_latest(board_name, limit=18)
        state["lastSeen"][board_name] = rows[0]["id"] if rows else None

    state["initialized"] = True
    _save_alert_state(state)
    return state

async def check_new_posts_and_alert():
    async with _alert_check_lock:
        state = _load_alert_state()

        if not state.get("initialized"):
            await _initialize_alert_baseline(state)
            return {
                "ok": True,
                "baseline": True,
                "alerts": 0,
                "message": "현재 최신 글을 기준점으로 저장했습니다. 과거 글은 발송하지 않았습니다.",
            }

        recipients = _alert_recipients(state)
        sent_keys = set(str(x) for x in state.get("sent", []))
        summary = []
        alert_count = 0

        for board_name in ("공지", "CM", "업데이트"):
            rows = await fetch_board_latest(board_name, limit=18)
            if not rows:
                continue

            previous_id = state["lastSeen"].get(board_name)

            # API is time-descending. Collect only rows that appeared before
            # the previous marker, then send oldest->newest.
            new_rows = []
            for row in rows:
                if previous_id is not None and int(row["id"]) == int(previous_id):
                    break
                new_rows.append(row)

            # Always advance marker to current latest, even if notice filters out.
            state["lastSeen"][board_name] = rows[0]["id"]

            for post in reversed(new_rows):
                sent_key = f"{board_name}:{post['id']}"
                if sent_key in sent_keys:
                    continue
                if not _should_alert(board_name, post):
                    continue

                send_result = await _send_kakao_event(
                    recipients,
                    board_name,
                    post,
                )

                summary.append({
                    "board": board_name,
                    "id": post["id"],
                    "title": post["title"],
                    "send": send_result,
                })

                # Mark only after the send request has been made. This prevents
                # duplicate requests on the next minute.
                if send_result.get("status") in ("REQUESTED", "NO_RECIPIENTS", "NOT_CONFIGURED"):
                    sent_keys.add(sent_key)
                    alert_count += 1

        state["sent"] = list(sent_keys)[-500:]
        _save_alert_state(state)

        return {
            "ok": True,
            "baseline": False,
            "alerts": alert_count,
            "recipients": len(recipients),
            "items": summary,
        }

# =========================================================
# Routes
# =========================================================

@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "AION2 Server v23 FullServerFix",
        "server": "전 서버 캐릭터 검색 / 지켈 필드보스",
        "character": "Own DB + NotMeter refresh",
        "fieldBoss": "NotMeter public cache",
        "officialBoards": ["공지", "CM", "업데이트"],
    }

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/debug/character-server/{server_name}/{nickname}")
async def debug_character_server(server_name: str, nickname: str):
    try:
        result = await asyncio.wait_for(
            character_lookup_server_fast(nickname, server_name),
            timeout=7.0,
        )
        return {
            "ok": bool(result),
            "server": server_name,
            "nickname": nickname,
            "knownServerId": SERVER_ID_CACHE.get(server_name),
            "result": result,
        }
    except Exception as e:
        return {
            "ok": False,
            "server": server_name,
            "nickname": nickname,
            "error": type(e).__name__,
            "message": str(e)[:300],
        }

@app.get("/debug/server-parse/{text}")
async def debug_server_parse(text: str):
    parsed = split_server_and_nickname(text)

    if not parsed:
        return {
            "ok": False,
            "input": text,
            "parsed": None,
        }

    nickname, server_name = parsed

    return {
        "ok": True,
        "input": text,
        "nickname": nickname,
        "server": server_name,
        "serverId": SERVER_ID_MAP.get(server_name),
    }


@app.get("/debug/server-character/{server_name}/{nickname}")
async def debug_server_character(server_name: str, nickname: str):
    try:
        rows = await search_character_on_server(
            nickname,
            server_name,
        )

        return {
            "ok": bool(rows),
            "server": server_name,
            "serverId": SERVER_ID_MAP.get(server_name),
            "nickname": nickname,
            "count": len(rows),
            "rows": [
                {
                    "name": row_name(row),
                    "serverName": row_server_name(row),
                    "serverId": row_server_id(row),
                    "characterId": row_character_id(row),
                    "combatPower": row.get("combatPower"),
                    "className": row.get("className"),
                }
                for row in rows[:10]
            ],
        }
    except Exception as e:
        return {
            "ok": False,
            "server": server_name,
            "nickname": nickname,
            "error": type(e).__name__,
            "message": str(e)[:300],
        }


@app.get("/debug/zikel/{nickname}")
async def debug_zikel_character(nickname: str):
    try:
        result = await asyncio.wait_for(
            character_lookup_server_fast(nickname, "지켈"),
            timeout=6.5,
        )
        return {
            "ok": bool(result),
            "result": result,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:300],
        }

@app.get("/debug/character/{nickname}")
async def debug_character(nickname: str):
    try:
        result = await asyncio.wait_for(character_lookup(nickname), timeout=4.35)
        return {"ok": bool(result), "result": result}
    except Exception as e:
        return {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:300],
        }

@app.get("/debug/field-boss")
async def debug_field_boss():
    try:
        result = await asyncio.wait_for(field_boss_lookup(), timeout=4.35)
        return {"ok": True, "result": result}
    except Exception as e:
        return {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:300],
        }

@app.get("/debug/field-boss/{boss_name}")
async def debug_one_field_boss(boss_name: str):
    try:
        result = await asyncio.wait_for(
            field_boss_lookup(boss_name),
            timeout=4.35,
        )
        return {"ok": True, "result": result}
    except Exception as e:
        return {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:300],
        }


@app.get("/debug/board/{board_name}")
async def debug_board(board_name: str):
    key = "CM" if board_name.lower() == "cm" else board_name
    if key not in BOARD_CONFIGS:
        return {
            "ok": False,
            "error": "UnknownBoard",
            "message": "공지 / CM / 업데이트 중 하나를 입력하세요.",
        }
    try:
        result = await asyncio.wait_for(board_lookup(key), timeout=4.35)
        return {"ok": True, "result": result}
    except Exception as e:
        return {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:300],
        }


@app.get("/alerts/status")
async def alerts_status():
    state = _load_alert_state()
    return {
        "ok": True,
        "initialized": state.get("initialized", False),
        "lastSeen": state.get("lastSeen", {}),
        "runtimeSubscribers": len(state.get("runtimeSubscribers", {})),
        "envSubscribers": len(ENV_ALERT_USER_KEYS),
        "kakaoConfigured": bool(KAKAO_BOT_ID and KAKAO_REST_API_KEY and KAKAO_EVENT_NAME),
    }

@app.get("/alerts/check")
async def alerts_check(secret: str = ""):
    if ALERT_CRON_SECRET and secret != ALERT_CRON_SECRET:
        return JSONResponse(
            {"ok": False, "error": "Unauthorized"},
            status_code=401,
        )
    try:
        result = await asyncio.wait_for(
            check_new_posts_and_alert(),
            timeout=20,
        )
        return result
    except Exception as e:
        return JSONResponse(
            {
                "ok": False,
                "error": type(e).__name__,
                "message": str(e)[:500],
            },
            status_code=500,
        )




# =========================================================
# Pretty OpenChat article cards + MessengerBotR alerts
# =========================================================

OPENCHAT_ALERT_STATE_FILE = Path(
    os.getenv("OPENCHAT_ALERT_STATE_FILE", "/tmp/aion2_openchat_alert_state.json")
)
_openchat_alert_lock = asyncio.Lock()

def _default_openchat_alert_state():
    return {
        "initialized": False,
        "lastSeen": {"공지": None, "CM": None, "업데이트": None},
        "bossSent": [],
    }

def _load_openchat_alert_state():
    try:
        if not OPENCHAT_ALERT_STATE_FILE.exists():
            return _default_openchat_alert_state()
        raw = json.loads(OPENCHAT_ALERT_STATE_FILE.read_text(encoding="utf-8"))
        state = _default_openchat_alert_state()
        state["initialized"] = bool(raw.get("initialized", False))
        if isinstance(raw.get("lastSeen"), dict):
            state["lastSeen"].update(raw["lastSeen"])
        if isinstance(raw.get("bossSent"), list):
            state["bossSent"] = [str(x) for x in raw["bossSent"]]
        return state
    except Exception:
        return _default_openchat_alert_state()

def _save_openchat_alert_state(state):
    try:
        OPENCHAT_ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OPENCHAT_ALERT_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

def board_card_url(board_name, post_id):
    return (
        "https://aion2-kakao-bot.onrender.com/p/"
        + quote(str(board_name), safe="")
        + "/"
        + quote(str(post_id), safe="")
    )

def board_card_label(board_name):
    if board_name == "공지":
        return "📢 AION2 공지"
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
async def pretty_board_card(board_name: str, post_id: int):
    normalized = "CM" if board_name.lower() == "cm" else board_name
    if normalized not in BOARD_CONFIGS:
        return HTMLResponse("<h2>잘못된 게시판입니다.</h2>", status_code=404)

    rows = await fetch_board_latest(normalized, limit=18)
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
        title_text = board_card_label(normalized)
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

@app.get("/openchat/alerts")
async def openchat_alerts():
    async with _openchat_alert_lock:
        state = _load_openchat_alert_state()

        if not isinstance(state.get("bossSent"), list):
            state["bossSent"] = []

        items = []

        # ---- Board alerts ----
        latest_by_board = {}
        for board in ("공지", "CM", "업데이트"):
            try:
                rows = await fetch_board_latest(board, limit=18)
            except Exception:
                rows = []
            latest_by_board[board] = rows

        first_run = not state.get("initialized")

        if first_run:
            for board, rows in latest_by_board.items():
                if rows:
                    state["lastSeen"][board] = rows[0]["id"]
            state["initialized"] = True
        else:
            for board, rows in latest_by_board.items():
                if not rows:
                    continue

                previous_id = state["lastSeen"].get(board)
                new_rows = []

                for row in rows:
                    if previous_id is not None and str(row["id"]) == str(previous_id):
                        break
                    new_rows.append(row)

                state["lastSeen"][board] = rows[0]["id"]

                for post in reversed(new_rows):
                    # Notice alert: ALL new notice posts.
                    # "점검" / "라이브" are only used to choose the alert header.
                    if board == "공지":
                        title = str(post.get("title") or "")

                    notice_kind = None
                    if board == "공지":
                        title_text = str(post.get("title") or "")
                        if "라이브" in title_text:
                            notice_kind = "live"
                        elif "점검" in title_text:
                            notice_kind = "maintenance"
                        else:
                            notice_kind = "general"

                    items.append({
                        "type": "board",
                        "board": board,
                        "kind": notice_kind,
                        "id": post["id"],
                        "title": post["title"],
                    })

        # ---- Boss alerts: 30 minutes before, once ----
        now = datetime.now(KST)

        sent = set(str(x) for x in state.get("bossSent", []))
        keep = set()

        try:
            await refresh_boss_rules()
        except Exception:
            pass

        try:
            maintenance_anchor = await latest_maintenance_anchor()
        except Exception:
            maintenance_anchor = AGRO_FALLBACK_ANCHOR

        targets = [
            ("정령왕 아그로", next_agro_from_anchor(maintenance_anchor, now)),
            ("감시자 카이라", _next_daily_hours(BOSS_RULES["kairaHours"], now)),
            ("수호신장 나흐마", _next_weekly(
                BOSS_RULES["nahmaWeekdays"],
                BOSS_RULES["nahmaHour"],
                BOSS_RULES["nahmaMinute"],
                now,
            )),
            ("어비스 보스", _next_weekly(
                BOSS_RULES["abyssWeekdays"],
                BOSS_RULES["abyssHour"],
                BOSS_RULES["abyssMinute"],
                now,
            )),
        ]

        for name, target in targets:
            minutes = (target - now).total_seconds() / 60.0
            key = f"{name}|{int(target.timestamp())}"

            if minutes > -180:
                keep.add(key)

            # 1-minute phone polling + Render wake-up delay tolerance.
            if 27 <= minutes <= 33 and key not in sent:
                items.append({
                    "type": "boss",
                    "boss": name,
                    "time": target.strftime("%H:%M"),
                    "key": key,
                })
                sent.add(key)
                keep.add(key)

        state["bossSent"] = sorted(sent.intersection(keep))
        _save_openchat_alert_state(state)

        return {
            "ok": True,
            "baseline": first_run,
            "items": items,
        }


@app.get("/c/{nickname}/{server_name}")
async def character_card(nickname: str, server_name: str):
    try:
        data = await asyncio.wait_for(
            character_card_data(nickname, server_name),
            timeout=7.5,
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
.hero{{padding:24px;display:flex;gap:18px;align-items:center;background:radial-gradient(circle at 10% 10%,rgba(100,150,255,.25),transparent 50%)}}
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
.badge{{display:inline-block;margin-top:8px;padding:5px 9px;border-radius:999px;background:#202b3c;color:#b8c9e8;font-size:12px}}
</style>
</head>
<body>
<div class="wrap"><div class="card">
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
    {stones_html}
  </div>
</div></div>
</body>
</html>"""
    return HTMLResponse(html)



@app.get("/debug/upstreams")
async def debug_upstreams():
    result = {}

    # Character
    try:
        data = await http_json(
            NOTMETER_API + "/character/v1/search",
            params={"name": "윤이", "region": "kr", "lang": "ko", "fast": "1"},
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0),
        )
        result["character"] = {
            "ok": True,
            "count": len(data.get("results") or data.get("characters") or []),
        }
    except Exception as e:
        result["character"] = {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:250],
        }

    # Field boss
    try:
        fb = await fetch_field_boss_cache()
        result["fieldBoss"] = {
            "ok": True,
            "type": type(fb).__name__,
        }
    except Exception as e:
        result["fieldBoss"] = {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:250],
        }

    # Board
    try:
        rows = await fetch_board_latest("공지", limit=1)
        result["notice"] = {
            "ok": True,
            "count": len(rows),
        }
    except Exception as e:
        result["notice"] = {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:250],
        }

    result["ok"] = all(
        isinstance(v, dict) and v.get("ok")
        for k, v in result.items()
        if k != "ok"
    )
    return result

@app.get("/debug/character-search/{nickname}")
async def debug_character_search(nickname: str):
    try:
        rows = await search_characters_all_servers(nickname)
        return {
            "ok": True,
            "count": len(rows),
            "results": [
                {
                    "name": row_name(r),
                    "serverId": row_server_id(r),
                    "serverName": row_server_name(r),
                    "characterId": row_character_id(r),
                    "combatPower": r.get("combatPower"),
                    "className": r.get("className"),
                }
                for r in rows[:20]
            ],
        }
    except Exception as e:
        return {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:400],
        }









@app.get("/debug/notmeter-character")
async def debug_notmeter_character(nickname: str):
    result = {
        "ok": False,
        "version": "v53-stable-compare-ui",
        "api": NOTMETER_CHARACTER_API,
        "nickname": nickname,
    }

    try:
        data = await character_api_get(
            "/character/v1/search",
            {
                "name": nickname,
                "region": "kr",
                "lang": "ko",
                "fast": "1",
            },
            timeout=httpx.Timeout(
                connect=3.0,
                read=8.0,
                write=3.0,
                pool=2.0,
            ),
        )

        rows = data.get("results") or data.get("characters") or []
        result["ok"] = True
        result["count"] = len(rows)
        result["results"] = [
            {
                "name": row_name(row),
                "serverId": row_server_id(row),
                "serverName": row_server_name(row),
                "characterId": row_character_id(row),
                "combatPower": row.get("combatPower"),
                "className": row.get("className"),
            }
            for row in rows[:20]
            if isinstance(row, dict)
        ]

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:400]}"

    return result


@app.get("/debug/character-db")
async def debug_character_db(
    nickname: str | None = None,
    server: str | None = None,
):
    stats = await character_db_stats()

    result = {
        "ok": True,
        "version": "v53-stable-compare-ui",
        "database": stats,
    }

    if nickname:
        result["lookup"] = await character_db_get(
            nickname,
            server,
        )

    return result


@app.get("/debug/character-resolve")
async def debug_character_resolve(
    nickname: str,
    server: str | None = None,
):
    before = await character_db_get(
        nickname,
        server,
    )

    resolved = await own_resolve_character(
        nickname,
        server,
    )

    after = await character_db_get(
        nickname,
        server,
    )

    return {
        "ok": True,
        "version": "v53-stable-compare-ui",
        "dbPath": CHARACTER_DB_PATH,
        "dbBefore": before,
        "resolvedType": resolved.get("type"),
        "resolvedInfo": (
            resolved.get("info")
            if resolved.get("type") == "detail"
            else [
                item.get("info") or {}
                for item in resolved.get("items") or []
            ]
        ),
        "dbAfter": after,
    }




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


@app.get("/debug/own-stats-v1")
async def debug_own_stats_v1(nickname: str = "윤이", server: str = "지켈"):
    nickname = str(nickname or "").strip()
    server = str(server or "").strip()
    server_id = int(SERVER_ID_MAP.get(server) or 0)
    if not nickname or not server_id:
        return {"ok": False, "error": "nickname/server 확인 필요"}

    row = None

    # Reuse the exact character id already stored by the normal !윤이지켈 path.
    try:
        db_rows = await character_db_get(nickname, server)
        if db_rows:
            d = db_rows[0]
            if d.get("characterId"):
                row = {
                    "name": d.get("name") or nickname,
                    "serverName": d.get("server") or server,
                    "serverId": int(d.get("serverId") or server_id),
                    "characterId": d.get("characterId") or "",
                }
    except Exception:
        pass

    # First-use fallback: use the existing character search only to resolve id.
    if not row or not row.get("characterId"):
        try:
            rows = await search_character_on_server(nickname, server)
            if rows:
                row = rows[0]
        except Exception as e:
            return {"ok": False, "error": f"character id 조회 실패: {type(e).__name__}: {str(e)[:200]}"}

    if not row or not row_character_id(row):
        return {"ok": False, "error": f"{nickname}[{server}] characterId 없음"}

    sid = int(row_server_id(row) or server_id)
    cid = row_character_id(row)

    try:
        official = await _official_get_json(
            OFFICIAL_CHARACTER_INFO_API,
            params={"lang": "ko", "characterId": cid, "serverId": sid},
            timeout=httpx.Timeout(connect=3.0, read=12.0, write=3.0, pool=2.0),
        )
    except Exception as e:
        return {
            "ok": False,
            "error": f"official character/info 실패: {type(e).__name__}: {str(e)[:300]}",
            "serverId": sid,
            "characterId": cid,
        }

    own, candidates = _own_stats_v1_from_official_payload(
        official, nickname, server, sid, cid
    )

    raw_stat_list = []
    raw_stat = {}
    if isinstance(official, dict):
        raw_stat = official.get("stat") if isinstance(official.get("stat"), dict) else {}
        if isinstance(raw_stat.get("statList"), list):
            raw_stat_list = raw_stat.get("statList") or []

    return {
        "ok": True,
        **own,
        "officialTopKeys": list(official.keys()) if isinstance(official, dict) else [],
        "rawStat": raw_stat,
        "rawStatList": raw_stat_list,
        "rawStatListCount": len(raw_stat_list),
        "candidateNumericCount": len(candidates),
        "candidateNumericSample": candidates[:120],
    }



@app.get("/debug/official-equipment-v1")
async def debug_official_equipment_v1(nickname: str = "윤이", server: str = "지켈"):
    """Return the official NC equipment payload and recursively surface stat/skill-like fields."""
    nickname = str(nickname or "").strip()
    server = str(server or "").strip()
    server_id = int(SERVER_ID_MAP.get(server) or 0)
    if not nickname or not server_id:
        return {"ok": False, "error": "nickname/server 확인 필요"}

    row = None
    try:
        db_rows = await character_db_get(nickname, server)
        if db_rows:
            d = db_rows[0]
            if d.get("characterId"):
                row = {
                    "name": d.get("name") or nickname,
                    "serverName": d.get("server") or server,
                    "serverId": int(d.get("serverId") or server_id),
                    "characterId": d.get("characterId") or "",
                }
    except Exception:
        pass

    if not row or not row.get("characterId"):
        try:
            rows = await search_character_on_server(nickname, server)
            if rows:
                row = rows[0]
        except Exception as e:
            return {"ok": False, "error": f"character id 조회 실패: {type(e).__name__}: {str(e)[:200]}"}

    if not row or not row_character_id(row):
        return {"ok": False, "error": f"{nickname}[{server}] characterId 없음"}

    sid = int(row_server_id(row) or server_id)
    cid = row_character_id(row)
    page_url = f"{OFFICIAL_CHARACTER_BASE}/ko-kr/characters/{sid}/{quote(str(cid), safe='')}"
    headers = dict(OFFICIAL_API_HEADERS)
    headers.setdefault("Referer", page_url)
    client = await get_http_client()

    params = {"serverId": sid, "characterId": cid}
    try:
        r = await client.get(
            f"{OFFICIAL_CHARACTER_BASE}/api/character/equipment",
            params=params,
            headers=headers,
            timeout=httpx.Timeout(connect=4.0, read=15.0, write=4.0, pool=3.0),
        )
    except Exception as e:
        return {"ok": False, "error": f"equipment 호출 실패: {type(e).__name__}: {str(e)[:300]}"}

    try:
        data = r.json()
    except Exception:
        return {
            "ok": False,
            "status": r.status_code,
            "contentType": r.headers.get("content-type"),
            "bodySample": (r.text or "")[:2000],
        }

    interesting = []
    keywords = (
        "attack", "damage", "critical", "accuracy", "penetr", "speed", "combat",
        "boss", "pve", "front", "back", "perfect", "hard", "ampl", "weapon",
        "stat", "skill", "level", "option", "effect", "hit", "defense", "hp",
        "공격", "피해", "치명", "명중", "관통", "속도", "보스", "전방", "후방",
        "강타", "완벽", "증폭", "스킬", "레벨", "옵션", "효과", "방어", "생명"
    )

    def walk(obj, path="", depth=0):
        if depth > 12 or len(interesting) >= 500:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                pth = f"{path}.{k}" if path else str(k)
                ks = str(k).lower()
                value_text = str(v)[:500] if isinstance(v, (str, int, float, bool)) or v is None else ""
                hay = (ks + " " + value_text.lower())
                if any(x in hay for x in keywords):
                    interesting.append({"path": pth, "key": str(k), "value": v if isinstance(v, (str, int, float, bool)) or v is None else type(v).__name__})
                walk(v, pth, depth + 1)
        elif isinstance(obj, list):
            for i, v in enumerate(obj[:300]):
                walk(v, f"{path}[{i}]", depth + 1)

    walk(data)

    return {
        "ok": r.status_code == 200,
        "schema": "OFFICIAL_EQUIPMENT_V1",
        "source": "plaync-character-equipment",
        "name": nickname,
        "server": server,
        "serverId": sid,
        "characterId": cid,
        "status": r.status_code,
        "topKeys": list(data.keys()) if isinstance(data, dict) else [],
        "interestingCount": len(interesting),
        "interesting": interesting,
        "raw": data,
    }


@app.get("/debug/official-endpoints-v1")
async def debug_official_endpoints_v1(nickname: str = "윤이", server: str = "지켈"):
    """Discover official AION2 character-page API paths without changing production lookup logic."""
    nickname = str(nickname or "").strip()
    server = str(server or "").strip()
    server_id = int(SERVER_ID_MAP.get(server) or 0)
    if not nickname or not server_id:
        return {"ok": False, "error": "nickname/server 확인 필요"}

    row = None
    try:
        db_rows = await character_db_get(nickname, server)
        if db_rows:
            d = db_rows[0]
            if d.get("characterId"):
                row = {
                    "name": d.get("name") or nickname,
                    "serverName": d.get("server") or server,
                    "serverId": int(d.get("serverId") or server_id),
                    "characterId": d.get("characterId") or "",
                }
    except Exception:
        pass

    if not row or not row.get("characterId"):
        try:
            rows = await search_character_on_server(nickname, server)
            if rows:
                row = rows[0]
        except Exception as e:
            return {"ok": False, "error": f"character id 조회 실패: {type(e).__name__}: {str(e)[:200]}"}

    if not row or not row_character_id(row):
        return {"ok": False, "error": f"{nickname}[{server}] characterId 없음"}

    sid = int(row_server_id(row) or server_id)
    cid = row_character_id(row)
    encoded_cid = quote(str(cid), safe="")
    page_url = f"{OFFICIAL_CHARACTER_BASE}/ko-kr/characters/{sid}/{encoded_cid}"

    client = await get_http_client()
    headers = dict(OFFICIAL_API_HEADERS)
    headers.setdefault("Referer", page_url)

    result = {
        "ok": True,
        "schema": "OFFICIAL_ENDPOINTS_V1",
        "name": nickname,
        "server": server,
        "serverId": sid,
        "characterId": cid,
        "pageUrl": page_url,
        "page": {},
        "discoveredApiPaths": [],
        "discoveredCharacterApiPaths": [],
        "scriptCount": 0,
        "scannedScripts": 0,
        "scriptMatches": [],
        "probes": [],
    }

    page_text = ""
    script_srcs = []
    try:
        r = await client.get(
            page_url,
            headers=headers,
            timeout=httpx.Timeout(connect=4.0, read=15.0, write=4.0, pool=3.0),
        )
        page_text = r.text or ""
        result["page"] = {
            "status": r.status_code,
            "contentType": r.headers.get("content-type"),
            "length": len(page_text),
        }
        script_srcs = re.findall(r'<script[^>]+src=["\\\']([^"\\\']+)["\\\']', page_text, flags=re.I)
        result["scriptCount"] = len(script_srcs)
    except Exception as e:
        result["page"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}

    api_paths = set()
    char_paths = set()

    def collect_paths(text: str):
        if not text:
            return
        # Raw strings and escaped strings from HTML/Next/webpack bundles.
        variants = [text, text.replace('\\/', '/')]
        for blob in variants:
            for m in re.findall(r'(?:(?:https?:)?//aion2\\.plaync\\.com)?(/(?:ko-kr/)?api/[A-Za-z0-9_./?=&%{}:\\-]+)', blob):
                clean = m.rstrip('"\\\'`),;]}>')
                if clean:
                    api_paths.add(clean)
                    if "/character" in clean.lower():
                        char_paths.add(clean)

    collect_paths(page_text)

    # Scan a bounded number of same-origin JS bundles for hidden API route strings.
    seen_scripts = set()
    for src in script_srcs[:24]:
        if not src or src in seen_scripts:
            continue
        seen_scripts.add(src)
        if src.startswith("//"):
            url = "https:" + src
        elif src.startswith("/"):
            url = OFFICIAL_CHARACTER_BASE + src
        elif src.startswith("http://") or src.startswith("https://"):
            url = src
        else:
            url = OFFICIAL_CHARACTER_BASE + "/" + src.lstrip("/")
        if "aion2.plaync.com" not in url:
            continue
        try:
            sr = await client.get(
                url,
                headers=headers,
                timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=2.0),
            )
            txt = sr.text or ""
            before = len(char_paths)
            collect_paths(txt)
            new_count = len(char_paths) - before
            if new_count > 0:
                result["scriptMatches"].append({
                    "url": url,
                    "status": sr.status_code,
                    "length": len(txt),
                    "newCharacterPathCount": new_count,
                })
            result["scannedScripts"] += 1
        except Exception:
            continue

    result["discoveredApiPaths"] = sorted(api_paths)[:300]
    result["discoveredCharacterApiPaths"] = sorted(char_paths)[:200]

    # Probe only a small read-only candidate set. These are diagnostics, not assumed-valid endpoints.
    candidates = [
        "/api/character/info",
        "/api/character/equipment",
        "/api/character/skill",
        "/api/character/skills",
        "/api/character/stat",
        "/api/character/stats",
        "/api/character/detail",
        "/api/character/profile",
        "/api/character/combat",
    ]
    # Add discovered character endpoints that look callable and are not templates/assets.
    for path in sorted(char_paths):
        if path not in candidates and "{" not in path and "}" not in path and len(candidates) < 20:
            candidates.append(path.split("?")[0])

    for path in candidates:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = OFFICIAL_CHARACTER_BASE + path
        probe = {"path": path}
        try:
            pr = await client.get(
                url,
                params={"lang": "ko", "characterId": cid, "serverId": sid},
                headers=headers,
                timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=2.0),
            )
            probe["status"] = pr.status_code
            probe["contentType"] = pr.headers.get("content-type")
            text = pr.text or ""
            probe["length"] = len(text)
            try:
                data = pr.json()
                probe["jsonType"] = type(data).__name__
                if isinstance(data, dict):
                    probe["topKeys"] = list(data.keys())[:80]
                    # Search key names that may indicate final combat stats.
                    interesting = []
                    def walk_keys(obj, prefix=""):
                        if len(interesting) >= 120:
                            return
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                pth = f"{prefix}.{k}" if prefix else str(k)
                                lk = str(k).lower()
                                if any(t in lk for t in (
                                    "attack", "damage", "critical", "accuracy", "penetration",
                                    "speed", "perfect", "hardhit", "pve", "boss", "front", "back",
                                    "공격", "피해", "치명", "명중", "관통", "속도", "강타"
                                )):
                                    interesting.append({"path": pth, "value": v if isinstance(v, (str, int, float, bool, type(None))) else type(v).__name__})
                                walk_keys(v, pth)
                                if len(interesting) >= 120:
                                    break
                        elif isinstance(obj, list):
                            for i, v in enumerate(obj[:30]):
                                walk_keys(v, f"{prefix}[{i}]")
                                if len(interesting) >= 120:
                                    break
                    walk_keys(data)
                    probe["interestingKeys"] = interesting
                elif isinstance(data, list):
                    probe["itemCount"] = len(data)
                    if data and isinstance(data[0], dict):
                        probe["firstItemKeys"] = list(data[0].keys())[:80]
            except Exception:
                probe["bodySample"] = text[:600]
        except Exception as e:
            probe["error"] = f"{type(e).__name__}: {str(e)[:240]}"
        result["probes"].append(probe)

    return result


@app.get("/debug/official-raw")
async def debug_official_raw(nickname: str, server: str):
    server_id = SERVER_ID_MAP.get(server)
    if not server_id:
        return {"ok": False, "error": "unknown server"}

    race = _official_server_race(server_id)
    client = await get_http_client()
    result = {
        "version": "v53-stable-compare-ui",
        "nickname": nickname,
        "server": server,
        "serverId": server_id,
        "race": race,
    }

    # Raw JSON API response
    try:
        api_res = await client.get(
            OFFICIAL_CHARACTER_SEARCH_API,
            params={
                "keyword": nickname,
                "race": race,
                "serverId": server_id,
            },
            headers=OFFICIAL_API_HEADERS,
            timeout=httpx.Timeout(connect=3.0, read=12.0, write=3.0, pool=2.0),
        )
        result["api"] = {
            "status": api_res.status_code,
            "contentType": api_res.headers.get("content-type"),
            "body": (api_res.text or "")[:2000],
        }
    except Exception as e:
        result["api"] = {
            "error": f"{type(e).__name__}: {str(e)[:300]}"
        }

    # Raw official search-page response
    try:
        page_res = await client.get(
            "https://aion2.plaync.com/ko-kr/characters/index",
            params={
                "keyword": nickname,
                "race": race,
                "serverId": server_id,
            },
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ko-KR,ko;q=0.9",
                "User-Agent": HEADERS["User-Agent"],
            },
            timeout=httpx.Timeout(connect=3.0, read=15.0, write=3.0, pool=2.0),
        )

        raw = page_res.text or ""
        normalized = (
            raw
            .replace("\\/", "/")
            .replace("\\u002F", "/")
            .replace("\\u003D", "=")
        )
        links = re.findall(
            r"/ko-kr/characters/(\d+)/([^\"'<>\s?&]+)",
            normalized,
        )

        result["page"] = {
            "status": page_res.status_code,
            "bytes": len(raw.encode("utf-8")),
            "containsNickname": nickname in raw,
            "characterLinks": links[:20],
        }
    except Exception as e:
        result["page"] = {
            "error": f"{type(e).__name__}: {str(e)[:300]}"
        }

    return result


@app.get("/debug/official-api-search")
async def debug_official_api_search(nickname: str, server: str | None = None):
    try:
        rows = await official_search_characters(nickname, server)

        details = []
        for row in rows[:10]:
            info = await official_load_detail(row)
            details.append({
                "search": row,
                "detail": info,
            })

        return {
            "ok": True,
            "version": "v53-stable-compare-ui",
            "count": len(rows),
            "results": details,
        }

    except Exception as e:
        return {
            "ok": False,
            "version": "v53-stable-compare-ui",
            "error": f"{type(e).__name__}: {str(e)[:400]}",
        }


@app.get("/debug/official-character")
async def debug_official_character(nickname: str, server: str | None = None):
    try:
        resolved = await official_resolve_character(nickname, server)
        if resolved.get("type") == "detail":
            info = resolved.get("info") or {}
            return {
                "ok": True,
                "version": "v53-stable-compare-ui",
                "type": "detail",
                "info": info,
            }

        if resolved.get("type") == "multiple":
            return {
                "ok": True,
                "version": "v53-stable-compare-ui",
                "type": "multiple",
                "items": [
                    item.get("info") or {}
                    for item in resolved.get("items") or []
                ],
            }

        return {
            "ok": True,
            "version": "v53-stable-compare-ui",
            "type": "none",
        }
    except Exception as e:
        return {
            "ok": False,
            "version": "v53-stable-compare-ui",
            "error": f"{type(e).__name__}: {str(e)[:300]}",
        }


@app.get("/debug/boss-schedule")
async def debug_boss_schedule():
    await refresh_boss_rules(force=True)

    now = datetime.now(KST)
    anchor = await latest_maintenance_anchor()
    agro = next_agro_from_anchor(anchor, now)
    kaira = _next_daily_hours(BOSS_RULES["kairaHours"], now)
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

    return {
        "version": "v53-stable-compare-ui",
        "nowKST": now.isoformat(),
        "maintenanceAnchor": anchor.isoformat(),
        "rules": BOSS_RULES,
        "ruleMeta": BOSS_RULES_META,
        "next": {
            "agro": agro.isoformat(),
            "kaira": kaira.isoformat(),
            "nahma": nahma.isoformat(),
            "abyss": abyss.isoformat(),
        },
    }


@app.get("/debug/ranking")
async def debug_ranking():
    client = await get_http_client()
    attempts = []

    for url in RANKING_URLS:
        try:
            res = await client.get(
                url,
                headers={**HEADERS, "Accept-Encoding": "identity"},
                timeout=httpx.Timeout(connect=3.0, read=30.0, write=3.0, pool=2.0),
            )
            raw = res.content
            row = {
                "url": url,
                "status": res.status_code,
                "bytes": len(raw),
                "gzip": bool(len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B),
            }

            if 200 <= res.status_code < 300:
                try:
                    decoded = gzip.decompress(raw) if row["gzip"] else raw
                    data = json.loads(decoded.decode("utf-8"))
                    row["json"] = True
                    row["topKeys"] = list(data.keys())[:12] if isinstance(data, dict) else []
                    row["classRankings"] = (
                        len(data.get("classRankings") or {})
                        if isinstance(data, dict) and isinstance(data.get("classRankings"), dict)
                        else 0
                    )
                except Exception as parse_error:
                    row["json"] = False
                    row["parseError"] = f"{type(parse_error).__name__}: {str(parse_error)[:180]}"
            attempts.append(row)
        except Exception as e:
            attempts.append({
                "url": url,
                "error": f"{type(e).__name__}: {str(e)[:180]}",
            })

    try:
        cache = await fetch_ranking_cache()
        active = {
            "ok": True,
            "topKeys": list(cache.keys())[:12] if isinstance(cache, dict) else [],
            "classRankings": (
                len(cache.get("classRankings") or {})
                if isinstance(cache, dict) and isinstance(cache.get("classRankings"), dict)
                else 0
            ),
        }
    except Exception as e:
        active = {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:180]}",
        }

    return {
        "version": "v26-ranking-fix",
        "attempts": attempts,
        "activeCache": active,
    }


@app.get("/debug/v25")
async def debug_v25():
    result = {
        "ok": True,
        "version": "v25-alert-board-fix",
        "timeKST": datetime.now(KST).isoformat(),
        "boards": {},
        "alerts": {},
    }

    for board in ("공지", "CM", "업데이트"):
        try:
            rows = await fetch_board_latest(board, limit=18 if board == "공지" else 3)
            result["boards"][board] = {
                "ok": True,
                "count": len(rows),
                "latest": rows[0] if rows else None,
            }
        except Exception as e:
            result["boards"][board] = {
                "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:180]}",
            }

    try:
        now = datetime.now(KST)
        target = next_kaira_spawn(now)
        result["alerts"]["nextKaira"] = target.isoformat()
        result["alerts"]["minutesToKaira"] = round((target - now).total_seconds() / 60, 1)
    except Exception as e:
        result["alerts"]["kairaError"] = f"{type(e).__name__}: {str(e)[:180]}"

    try:
        state = _load_openchat_alert_state()
        result["alerts"]["state"] = state
    except Exception as e:
        result["alerts"]["stateError"] = f"{type(e).__name__}: {str(e)[:180]}"

    return result


@app.get("/openchat")
async def openchat(msg: str = ""):
    command = clean_command(msg)
    if not command.startswith("!"):
        return PlainTextResponse("명령어 앞에 !를 붙여주세요.", media_type="text/plain; charset=utf-8")

    body = command[1:].strip()
    if not body:
        return PlainTextResponse("!윤이 / !랭킹 윤이지켈 / !필보 / !인원 / !공지 / !CM", media_type="text/plain; charset=utf-8")

    # 봇 전용 명령은 반드시 여기서 끝난다. 캐릭터 검색으로 fall-through 금지.
    if body in ("설명", "도움", "명령어", "사용법"):
        return PlainTextResponse(
            "📘 AION2 봇 사용법\n\n"
            "⚔️ 캐릭터\n!윤이\n!윤이지켈 / !지켈윤이\n!윤이 지켈\n\n"
            "🏆 랭킹\n!랭킹 윤이지켈\n\n"
            "🐲 필드보스\n!필보 / !아그로 / !카이라 / !나흐마 / !어비스\n\n"
            "📢 소식\n!공지 / !CM / !업데이트\n\n"
            "👥 기타\n!인원\n!비교\n\n"
            "🔔 알림\n!알림켜기 / !알림끄기 / !알림상태",
            media_type="text/plain; charset=utf-8",
        )

    if body == "비교":
        return PlainTextResponse(
            "⚔️ AION2 캐릭터 비교\n\nhttps://aion2-kakao-bot.onrender.com/compare",
            media_type="text/plain; charset=utf-8",
        )

    if body == "인원":
        return PlainTextResponse(
            "👥 인원표\n\nhttps://docs.google.com/spreadsheets/d/1TDkZojKWuHNfu5cl1lpuqVZvTKF6W9-c9WLjga8ihIc/edit?gid=0#gid=0",
            media_type="text/plain; charset=utf-8",
        )

    if body in ("알림켜기", "알림끄기", "알림상태", "테스트"):
        text = "✅ AION2 v57 서버 정상" if body == "테스트" else "📱 알림 설정은 휴대폰 봇에서 처리됩니다."
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    if body == "랭킹" or body.startswith("랭킹 ") or (body.startswith("랭킹") and len(body) > 2):
        query = body[2:].strip()
        try:
            result = await asyncio.wait_for(ranking_lookup_smart(query), timeout=35.0)
        except asyncio.TimeoutError:
            result = "⚠️ 랭킹 조회 지연"
        except Exception:
            result = "⚠️ 랭킹 정보를 불러오지 못했습니다."
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    if body in ("아그로", "카이라", "나흐마", "어비스", "어비스보스"):
        try:
            result = await asyncio.wait_for(field_boss_lookup(body), timeout=5.0)
        except Exception:
            result = "⚠️ 필드보스 조회 실패"
        return PlainTextResponse(result, media_type="text/plain; charset=utf-8")

    if body == "필보":
        try:
            result = await asyncio.wait_for(field_boss_lookup(), timeout=5.0)
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
        result = await asyncio.wait_for(character_lookup_smart(body), timeout=7.0)
    except asyncio.TimeoutError:
        result = "⚠️ 캐릭터 조회 지연"
    except Exception:
        result = "⚠️ 캐릭터 조회 실패"
    if not result:
        result = "캐릭터를 찾지 못했습니다."
    return PlainTextResponse(result, media_type="text/plain; charset=utf-8")



@app.get("/debug/final-stats-hunt-v1")
async def debug_final_stats_hunt_v1(nickname: str = "윤이", server: str = "지켈"):
    """Probe likely official read-only character endpoints and score responses for final combat-stat fields."""
    nickname = str(nickname or "").strip()
    server = str(server or "").strip()
    server_id = int(SERVER_ID_MAP.get(server) or 0)
    if not nickname or not server_id:
        return {"ok": False, "error": "nickname/server 확인 필요"}

    row = None
    try:
        db_rows = await character_db_get(nickname, server)
        if db_rows:
            d = db_rows[0]
            if d.get("characterId"):
                row = {
                    "name": d.get("name") or nickname,
                    "serverName": d.get("server") or server,
                    "serverId": int(d.get("serverId") or server_id),
                    "characterId": d.get("characterId") or "",
                }
    except Exception:
        pass

    if not row or not row.get("characterId"):
        try:
            rows = await search_character_on_server(nickname, server)
            if rows:
                row = rows[0]
        except Exception as e:
            return {"ok": False, "error": f"character id 조회 실패: {type(e).__name__}: {str(e)[:200]}"}

    if not row or not row_character_id(row):
        return {"ok": False, "error": f"{nickname}[{server}] characterId 없음"}

    sid = int(row_server_id(row) or server_id)
    cid = row_character_id(row)
    encoded_cid = quote(str(cid), safe="")
    page_url = f"{OFFICIAL_CHARACTER_BASE}/ko-kr/characters/{sid}/{encoded_cid}"
    headers = dict(OFFICIAL_API_HEADERS)
    headers.setdefault("Referer", page_url)
    client = await get_http_client()

    names = [
        "stat", "stats", "status", "combat", "combat-stat", "combat-stats", "combatstat", "combatstats",
        "battle", "battle-stat", "battle-stats", "battlestat", "battlestats",
        "detail-stat", "detail-stats", "detailstat", "detailstats",
        "stat-detail", "stat-details", "statdetail", "statdetails",
        "ability", "abilities", "attribute", "attributes", "spec", "specs",
        "power", "powers", "battlepower", "combatpower", "summary", "detail", "profile",
        "character-stat", "character-stats", "characterstat", "characterstats",
        "offense", "offensive", "attack", "damage", "battle-info", "combat-info",
    ]
    paths = [f"/api/character/{n}" for n in names]
    # Also try v1/v2 style variants seen in NC services.
    paths += [f"/api/character/v1/{n}" for n in names[:24]]
    paths += [f"/api/character/v2/{n}" for n in names[:24]]

    key_terms = (
        "attack", "additionalattack", "maximumattack", "minimumattack", "damage", "amplification",
        "critical", "accuracy", "penetration", "combatspeed", "hardhit", "perfect",
        "pve", "boss", "front", "back", "weapon", "공격", "피해", "치명", "명중", "관통", "강타", "속도"
    )

    def inspect_json(data):
        hits = []
        def walk(obj, prefix=""):
            if len(hits) >= 200:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    pth = f"{prefix}.{k}" if prefix else str(k)
                    lk = str(k).lower().replace("_", "").replace("-", "")
                    sv = str(v)[:180].lower() if isinstance(v, (str, int, float, bool)) or v is None else ""
                    hay = lk + " " + sv
                    if any(t.replace("_", "").replace("-", "") in hay for t in key_terms):
                        hits.append({"path": pth, "value": v if isinstance(v, (str, int, float, bool)) or v is None else type(v).__name__})
                    walk(v, pth)
                    if len(hits) >= 200:
                        break
            elif isinstance(obj, list):
                for i, v in enumerate(obj[:250]):
                    walk(v, f"{prefix}[{i}]")
                    if len(hits) >= 200:
                        break
        walk(data)
        return hits

    async def probe(path):
        url = OFFICIAL_CHARACTER_BASE + path
        out = {"path": path}
        param_sets = [
            {"lang": "ko", "characterId": cid, "serverId": sid},
            {"characterId": cid, "serverId": sid},
            {"lang": "ko", "serverId": sid, "id": cid},
        ]
        best = None
        for idx, params in enumerate(param_sets):
            try:
                r = await client.get(
                    url, params=params, headers=headers,
                    timeout=httpx.Timeout(connect=2.5, read=5.0, write=2.5, pool=2.0),
                )
                item = {"status": r.status_code, "contentType": r.headers.get("content-type"), "length": len(r.text or ""), "paramSet": idx}
                try:
                    data = r.json()
                    item["jsonType"] = type(data).__name__
                    if isinstance(data, dict):
                        item["topKeys"] = list(data.keys())[:80]
                    hits = inspect_json(data)
                    item["hitCount"] = len(hits)
                    item["hits"] = hits[:80]
                except Exception:
                    item["hitCount"] = 0
                    item["textSample"] = (r.text or "")[:220]
                if best is None or (item.get("hitCount",0), item.get("status")==200, item.get("length",0)) > (best.get("hitCount",0), best.get("status")==200, best.get("length",0)):
                    best = item
                if r.status_code == 200 and item.get("hitCount", 0) > 0:
                    break
            except Exception as e:
                if best is None:
                    best = {"error": f"{type(e).__name__}: {str(e)[:160]}", "hitCount": 0}
        out.update(best or {})
        return out

    sem = asyncio.Semaphore(8)
    async def limited(path):
        async with sem:
            return await probe(path)

    results = await asyncio.gather(*(limited(p) for p in paths))
    useful = [r for r in results if r.get("status") == 200 or r.get("hitCount", 0) > 0]
    useful.sort(key=lambda x: (x.get("hitCount",0), x.get("status")==200, x.get("length",0)), reverse=True)

    return {
        "ok": True,
        "schema": "FINAL_STATS_HUNT_V1",
        "name": nickname,
        "server": server,
        "serverId": sid,
        "characterId": cid,
        "tested": len(results),
        "usefulCount": len(useful),
        "useful": useful[:30],
        "status200Paths": [r.get("path") for r in results if r.get("status") == 200],
    }




@app.get("/debug/frontend-api-hunt-v2")
async def debug_frontend_api_hunt_v2():
    """Scan official PlayNC frontend JS bundles for real character API paths."""
    client = await get_http_client()
    headers = dict(OFFICIAL_API_HEADERS)
    headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://aion2.plaync.com/ko-kr/",
    })
    seed_urls = [
        "https://aion2.plaync.com/",
        "https://aion2.plaync.com/ko-kr/",
        "https://aion2.plaync.com/ko-kr/characters",
    ]
    pages = []
    script_urls = []
    import re as _re
    from urllib.parse import urljoin as _urljoin

    for u in seed_urls:
        try:
            r = await client.get(u, headers=headers, timeout=12.0, follow_redirects=True)
            text = r.text or ""
            pages.append({"url": u, "status": r.status_code, "length": len(text), "contentType": r.headers.get("content-type")})
            for src in _re.findall(r'<script[^>]+src=["\\\']([^"\\\']+)["\\\']', text, flags=_re.I):
                su = _urljoin(str(r.url), src)
                if su not in script_urls:
                    script_urls.append(su)
        except Exception as e:
            pages.append({"url": u, "error": f"{type(e).__name__}: {str(e)[:180]}"})

    # Known Next.js manifests can reveal all chunk names even if a route HTML is protected.
    extra = [
        "https://aion2.plaync.com/_next/static/BUILD_ID",
        "https://aion2.plaync.com/_next/static/chunks/webpack.js",
        "https://aion2.plaync.com/_next/static/chunks/main.js",
    ]
    for x in extra:
        if x not in script_urls:
            script_urls.append(x)

    api_paths = set()
    character_contexts = []
    fetched = []
    # limit to avoid a slow debug request
    for su in script_urls[:45]:
        try:
            rr = await client.get(su, headers={**OFFICIAL_API_HEADERS, "Referer": "https://aion2.plaync.com/ko-kr/"}, timeout=10.0, follow_redirects=True)
            txt = rr.text or ""
            fetched.append({"url": su, "status": rr.status_code, "length": len(txt), "contentType": rr.headers.get("content-type")})
            if rr.status_code != 200 or len(txt) < 20:
                continue
            for pat in [
                r'["\\\'](/api/character/[^"\\\'?#`\\s]+)',
                r'["\\\'](https://aion2\\.plaync\\.com/api/character/[^"\\\'?#`\\s]+)',
                r'["\\\'](/ko-kr/api/[^"\\\'?#`\\s]+)',
            ]:
                for m in _re.finditer(pat, txt, flags=_re.I):
                    val = m.group(1)
                    if val.startswith("https://aion2.plaync.com"):
                        val = val.split("aion2.plaync.com",1)[1]
                    api_paths.add(val)
                    if len(character_contexts) < 80:
                        a=max(0,m.start()-220); b=min(len(txt),m.end()+320)
                        ctx=txt[a:b]
                        character_contexts.append({"path": val, "context": ctx})
            # Also capture likely endpoint construction around characterId/serverId.
            for term in ("characterId", "serverId", "equipment", "statList", "combatPower"):
                pos=0
                while len(character_contexts) < 120:
                    i=txt.find(term,pos)
                    if i<0: break
                    a=max(0,i-260); b=min(len(txt),i+420)
                    frag=txt[a:b]
                    if "/api/" in frag or "character" in frag.lower():
                        character_contexts.append({"term": term, "context": frag})
                    pos=i+len(term)
        except Exception as e:
            fetched.append({"url": su, "error": f"{type(e).__name__}: {str(e)[:160]}"})

    return {
        "ok": True,
        "schema": "FRONTEND_API_HUNT_V2",
        "pages": pages,
        "scriptCount": len(script_urls),
        "scripts": script_urls[:60],
        "fetched": fetched[:60],
        "apiPaths": sorted(api_paths),
        "contexts": character_contexts[:120],
    }


@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    try:
        payload = await request.json()
        command = clean_command(
            payload.get("userRequest", {}).get("utterance") or ""
        )

        if not command.startswith("!"):
            return JSONResponse(
                kakao_text("명령어 앞에 !를 붙여주세요.\n예: !윤이 / !필보")
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

        # ---------- Alert subscription ----------
        if body == "알림등록":
            _ok, message = _register_alert_user(payload)
            return JSONResponse(kakao_text(message))

        if body == "알림해제":
            _ok, message = _unregister_alert_user(payload)
            return JSONResponse(kakao_text(message))

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
@app.get('/debug/api-probe-v3')
async def debug_api_probe_v3(nickname: str = '윤이', server: str = '지켈'):
    import urllib.parse
    server_id = int(SERVER_ID_MAP.get(server) or 2002)
    row = None
    try:
        db_rows = await character_db_get(nickname, server)
        if db_rows:
            d = db_rows[0]
            row = {
                'name': d.get('name') or nickname,
                'serverName': d.get('server') or server,
                'serverId': int(d.get('serverId') or server_id),
                'characterId': d.get('characterId') or '',
            }
    except Exception:
        pass
    if not row or not row.get('characterId'):
        try:
            rows = await search_character_on_server(nickname, server)
            if rows:
                row = rows[0]
        except Exception:
            pass
    character_id = row_character_id(row) if row else None
    if not character_id:
        return {'ok': False, 'error': f'{nickname}[{server}] characterId not found'}
    server_id = int(row_server_id(row) or server_id)

    candidates = [
        'stat','stats','status','stat-info','statinfo','stat-detail','statdetail',
        'detail-stat','detail-stats','detailstat','detailstats',
        'combat','combat-stat','combat-stats','combatstat','combatstats',
        'battle','battle-stat','battle-stats','battlestat','battlestats',
        'ability','abilities','attribute','attributes','spec','specs',
        'power','combat-power','combatpower','profile-stat','profile-stats',
        'summary','detail','character-stat','character-stats',
        'equipment-stat','equipment-stats','option','options',
        'collection','collections','growth','growth-stat','growth-stats',
        'potential','potential-stat','potential-stats','additional-stat','additional-stats'
    ]
    qs_variants = [
        {'lang':'ko','characterId':character_id,'serverId':server_id},
        {'characterId':character_id,'serverId':server_id},
        {'lang':'ko','characterId':character_id},
    ]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': f'https://aion2.plaync.com/ko-kr/characters/{server_id}/{urllib.parse.quote(character_id, safe="")}',
        'Origin': 'https://aion2.plaync.com',
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
    }
    results = []
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=headers) as client:
        for name in candidates:
            path = f'/api/character/{name}'
            best = None
            for params in qs_variants:
                try:
                    r = await client.get('https://aion2.plaync.com' + path, params=params)
                    ct = r.headers.get('content-type','')
                    item = {'path': path, 'status': r.status_code, 'length': len(r.content), 'contentType': ct}
                    if 'json' in ct:
                        try:
                            j = r.json()
                            item['jsonType'] = type(j).__name__
                            if isinstance(j, dict):
                                item['topKeys'] = list(j.keys())[:30]
                                txt = json.dumps(j, ensure_ascii=False)
                                needles = ['attack','damage','critical','accuracy','penetration','boss','back','front','combatSpeed','hardHit','perfect','공격','피해','치명','명중','관통','후방','전방','강타']
                                hits = [x for x in needles if x.lower() in txt.lower()]
                                if hits:
                                    item['hits'] = hits[:30]
                            elif isinstance(j, list):
                                item['listCount'] = len(j)
                        except Exception:
                            pass
                    if r.status_code == 200:
                        best = item
                        break
                    if best is None or r.status_code not in (404,):
                        best = item
                except Exception as e:
                    best = {'path': path, 'error': str(e)[:180]}
            if best and (best.get('status') == 200 or best.get('status') not in (404, None)):
                results.append(best)
    return {
        'ok': True,
        'schema': 'API_PROBE_V3',
        'name': nickname,
        'server': server,
        'serverId': server_id,
        'characterId': character_id,
        'tested': len(candidates),
        'results': results,
    }

@app.get('/debug/official-combined-v1')
async def debug_official_combined_v1(nickname: str = '윤이', server: str = '지켈'):
    nickname = str(nickname or '').strip()
    server = str(server or '').strip()
    server_id = int(SERVER_ID_MAP.get(server) or 0)
    if not nickname or not server_id:
        return {'ok': False, 'error': 'nickname/server 확인 필요'}

    row = None
    try:
        db_rows = await character_db_get(nickname, server)
        if db_rows:
            d = db_rows[0]
            if d.get('characterId'):
                row = {
                    'name': d.get('name') or nickname,
                    'serverName': d.get('server') or server,
                    'serverId': int(d.get('serverId') or server_id),
                    'characterId': d.get('characterId') or '',
                }
    except Exception:
        pass

    if not row or not row.get('characterId'):
        try:
            rows = await search_character_on_server(nickname, server)
            if rows:
                row = rows[0]
        except Exception:
            pass

    cid = row_character_id(row) if row else None
    if not cid:
        return {'ok': False, 'error': f'{nickname}[{server}] characterId not found'}
    sid = int(row_server_id(row) or server_id)

    params = {'lang': 'ko', 'characterId': cid, 'serverId': sid}
    try:
        info = await _official_get_json(
            OFFICIAL_CHARACTER_INFO_API,
            params=params,
            timeout=httpx.Timeout(connect=4.0, read=15.0, write=4.0, pool=3.0),
        )
        equipment = await _official_get_json(
            f'{OFFICIAL_CHARACTER_BASE}/api/character/equipment',
            params=params,
            timeout=httpx.Timeout(connect=4.0, read=15.0, write=4.0, pool=3.0),
        )
    except Exception as e:
        return {'ok': False, 'error': f'official API 실패: {type(e).__name__}: {str(e)[:300]}'}

    stat_obj = info.get('stat') if isinstance(info, dict) and isinstance(info.get('stat'), dict) else {}
    stat_list = stat_obj.get('statList') if isinstance(stat_obj.get('statList'), list) else []

    skill_obj = equipment.get('skill') if isinstance(equipment, dict) and isinstance(equipment.get('skill'), dict) else {}
    skill_list = skill_obj.get('skillList') if isinstance(skill_obj.get('skillList'), list) else []
    normalized_skills = []
    for s in skill_list:
        if not isinstance(s, dict):
            continue
        normalized_skills.append({
            'id': s.get('id'),
            'name': s.get('name'),
            'category': s.get('category'),
            'level': s.get('skillLevel'),
            'acquired': s.get('acquired'),
            'equip': s.get('equip'),
            'needLevel': s.get('needLevel'),
        })

    eq_obj = equipment.get('equipment') if isinstance(equipment, dict) and isinstance(equipment.get('equipment'), dict) else {}
    eq_list = eq_obj.get('equipmentList') if isinstance(eq_obj.get('equipmentList'), list) else []
    normalized_equipment = []
    for x in eq_list:
        if not isinstance(x, dict):
            continue
        normalized_equipment.append({
            'slot': x.get('slotPosName'),
            'id': x.get('id'),
            'name': x.get('name'),
            'grade': x.get('grade'),
            'enchantLevel': x.get('enchantLevel'),
            'exceedLevel': x.get('exceedLevel'),
        })

    # Search only explicit named final-combat-stat fields. Do not infer or add equipment options.
    wanted = {
        'attack','additionalattack','maximumattack','minimumattack','attackincreasepercent',
        'accuracy','weaponaccuracy','accuracyincreasepercent','pveaccuracy','critical',
        'criticalincreasepercent','penetration','pveattack','bossattack','frontattack','backattack',
        'frontcritical','backcritical','damageamplificationpercent','weapondamageamplificationpercent',
        'pvedamageamplificationpercent','bossdamageamplificationpercent','criticaldamageamplificationpercent',
        'additionalhitaccuracypercent','perfectpercent','hardhitpercent','cooldowntimepercent',
        'combatspeedpercent','frontdamageamplificationpercent','backdamageamplificationpercent'
    }
    explicit = []
    def walk_explicit(obj, path=''):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f'{path}.{k}' if path else str(k)
                nk = ''.join(ch for ch in str(k).lower() if ch.isalnum())
                if nk in wanted and isinstance(v, (int, float, str, bool)):
                    explicit.append({'path': p, 'key': k, 'value': v})
                walk_explicit(v, p)
        elif isinstance(obj, list):
            for i, v in enumerate(obj[:500]):
                walk_explicit(v, f'{path}[{i}]')
    walk_explicit(info, 'info')
    walk_explicit(equipment, 'equipment')

    return {
        'ok': True,
        'schema': 'OFFICIAL_COMBINED_V1',
        'source': ['plaync-character-info', 'plaync-character-equipment'],
        'name': nickname,
        'server': server,
        'serverId': sid,
        'characterId': cid,
        'infoStatCount': len(stat_list),
        'infoStatList': stat_list,
        'skillCount': len(normalized_skills),
        'skills': normalized_skills,
        'equipmentCount': len(normalized_equipment),
        'equipment': normalized_equipment,
        'explicitFinalCombatStatCount': len(explicit),
        'explicitFinalCombatStats': explicit,
    }
