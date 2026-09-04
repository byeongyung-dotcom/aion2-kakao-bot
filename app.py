
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

app = FastAPI(title="AION2 Server v41 CompareAttackOnly")

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

    # Existing DB hit should never become unavailable just because NotMeter dies.
    timeout_seconds = 2.8 if db_infos else 6.0

    try:
        fresh = await asyncio.wait_for(
            resolve_character(
                nickname,
                server_name,
            ),
            timeout=timeout_seconds,
        )

        if fresh and fresh.get("type") != "none":
            await _save_notmeter_resolved(fresh)
            return fresh

    except Exception:
        pass

    return db_resolved


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
    "bossDamageIncrease", "pveDamageIncrease",
    "attackSpeed", "castSpeed", "moveSpeed",
)

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

def extract_profile_stats(profile):
    """
    Offensive stats only.
    Defense / resistance / HP / block / evasion / healing stats are removed.
    """
    stats = {}

    offensive_keywords = (
        "공격", "명중", "치명", "치피", "치명타 피해",
        "강타", "후방", "후피", "무기 피해", "무피",
        "보스 피해", "보스피해", "pve", "PVE",
        "추가 피해", "추가피해",
        "관통", "방어구 관통",
        "공격 속도", "공속",
        "시전 속도", "시속",
        "피해 증가", "피해증가",
        "스킬 피해", "스킬피해",
        "일반 공격", "평타",
    )

    defensive_keywords = (
        "방어", "마법 방어", "물리 방어",
        "체력", "HP", "생명력",
        "회피", "막기", "저항",
        "피해 감소", "피해감소",
        "치유", "회복",
        "보호막", "내성",
    )

    preferred_containers = (
        "stats", "stat", "statList", "characterStats",
        "combatStats", "battleStats", "additionalStats",
    )

    def keep_stat(name):
        name = str(name or "").strip()
        if not name:
            return False

        lowered = name.lower()

        for kw in defensive_keywords:
            if kw.lower() in lowered:
                return False

        return any(
            kw.lower() in lowered
            for kw in offensive_keywords
        )

    for key in preferred_containers:
        node = profile.get(key)
        if node is None:
            continue

        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (str, int, float)):
                    name = str(k)
                    if not keep_stat(name):
                        continue
                    n = _safe_num(v)
                    if n is not None:
                        stats[name] = {
                            "name": name,
                            "value": n,
                            "raw": v,
                        }

                elif isinstance(v, dict):
                    name = str(v.get("name") or v.get("statName") or k)
                    if not keep_stat(name):
                        continue

                    raw = v.get("value")
                    n = _safe_num(raw)
                    if n is not None:
                        stats[name] = {
                            "name": name,
                            "value": n,
                            "raw": raw,
                        }

        elif isinstance(node, list):
            for item in node:
                if not isinstance(item, dict):
                    continue

                name = str(
                    item.get("name")
                    or item.get("statName")
                    or item.get("key")
                    or item.get("id")
                    or ""
                ).strip()

                if not keep_stat(name):
                    continue

                raw = item.get("value")
                if name and raw is not None:
                    n = _safe_num(raw)
                    if n is not None:
                        stats[name] = {
                            "name": name,
                            "value": n,
                            "raw": raw,
                        }

    if not stats:
        for d in _walk_dicts(profile):
            if "magicStoneStat" in d:
                continue

            name = str(
                d.get("statName")
                or d.get("name")
                or ""
            ).strip()

            if not keep_stat(name):
                continue

            if "value" not in d:
                continue

            n = _safe_num(d.get("value"))

            if n is not None and len(name) <= 40:
                stats.setdefault(
                    name,
                    {
                        "name": name,
                        "value": n,
                        "raw": d.get("value"),
                    },
                )

    # Preferred order for readability.
    order = [
        "공격력",
        "마법 공격력",
        "물리 공격력",
        "명중",
        "치명타",
        "치명타 피해",
        "공격 속도",
        "시전 속도",
        "무기 피해 증가",
        "후방 피해 증가",
        "보스 피해 증가",
        "PVE 피해 증가",
        "강타",
        "관통",
    ]

    def sort_key(row):
        name = str(row.get("name") or "")
        for i, key in enumerate(order):
            if key in name:
                return (i, name)
        return (999, name)

    return sorted(stats.values(), key=sort_key)


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
    item_details = profile.get("itemDetails")
    rows = []

    if isinstance(item_details, dict):
        iterator = list(item_details.items())
    elif isinstance(item_details, list):
        iterator = [(str(i), v) for i, v in enumerate(item_details)]
    else:
        iterator = []

    for slot_key, item in iterator:
        if not isinstance(item, dict):
            continue

        name = str(
            item.get("name")
            or item.get("itemName")
            or item.get("equipmentName")
            or item.get("title")
            or ""
        ).strip()

        slot = str(
            item.get("slotName")
            or item.get("slot")
            or item.get("equipSlot")
            or item.get("equipmentSlot")
            or slot_key
        ).strip()

        grade = str(
            item.get("gradeName")
            or item.get("grade")
            or item.get("rarity")
            or ""
        ).strip()

        enhance = (
            item.get("enhanceLevel")
            if item.get("enhanceLevel") is not None
            else item.get("enchantLevel")
        )
        if enhance is None:
            enhance = item.get("reinforceLevel")

        level = (
            item.get("itemLevel")
            if item.get("itemLevel") is not None
            else item.get("level")
        )

        icon = str(
            item.get("icon")
            or item.get("iconUrl")
            or item.get("image")
            or ""
        )

        stones = []
        stone_list = item.get("magicStoneStat")
        if isinstance(stone_list, list):
            for stone in stone_list:
                s = _normalize_stone(stone)
                if s:
                    stones.append(s)

        options = []
        for option_key in ("options", "optionStats", "additionalStats", "stats"):
            option_node = item.get(option_key)
            if isinstance(option_node, list):
                for op in option_node:
                    if not isinstance(op, dict):
                        continue
                    oname = str(op.get("name") or op.get("statName") or "").strip()
                    oval = op.get("value")
                    if oname and oval is not None:
                        options.append({
                            "name": oname,
                            "value": oval,
                            "numeric": _safe_num(oval),
                        })

        rows.append({
            "slot": slot,
            "name": name or "장비",
            "grade": grade,
            "enhance": enhance,
            "itemLevel": level,
            "icon": icon,
            "magicStones": stones,
            "options": options,
        })

    return rows

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

async def _full_profile_for_exact_character(nickname: str, server_name: str):
    """
    Find the exact server character once, then fetch FULL profile with retry.
    This avoids one side getting only basic info when two detailed requests hit
    NotMeter concurrently/rate-limit each other.
    """
    candidates = await search_characters_all_servers(nickname)

    target_sid = SERVER_ID_MAP.get(server_name)
    if not target_sid:
        return None, None

    # Prefer serverId, because search rows may omit serverName.
    exact_rows = [
        row for row in candidates
        if row_server_id(row) == int(target_sid)
    ]

    # Fallback to serverName if needed.
    if not exact_rows:
        target = server_name.casefold()
        exact_rows = [
            row for row in candidates
            if row_server_name(row)
            and row_server_name(row).casefold() == target
        ]

    if not exact_rows:
        return None, None

    row = exact_rows[0]
    sid = row_server_id(row)
    cid = row_character_id(row)

    if not sid or not cid:
        return row, None

    cache_key = f"compare-full-profile:{sid}:{cid}"
    cached = cache_get(cache_key, 120)
    if cached is not None:
        return row, cached

    # Full profile retry. Small backoff prevents paired compare requests from
    # stepping on each other.
    last_error = None
    for attempt in range(3):
        try:
            profile = await get_profile(sid, cid, fast=False)

            # A valid full profile should at least contain info/profile or itemDetails.
            has_profile = bool(
                ((profile.get("info") or {}).get("profile"))
                or profile.get("itemDetails")
            )

            if has_profile:
                cache_set(cache_key, profile)
                return row, profile

        except Exception as e:
            last_error = e

        await asyncio.sleep(0.45 * (attempt + 1))

    # Fast profile is only a last fallback for basic character fields.
    try:
        profile = await get_profile(sid, cid, fast=True)
        return row, profile
    except Exception:
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
                full_profile.get("itemDetails")
                or ((full_profile.get("info") or {}).get("profile")
                    and any(
                        key in full_profile
                        for key in ("itemDetails", "stats", "statList", "characterStats")
                    ))
            )
        elif not full_info:
            full_info = profile_info(
                {},
                nickname,
                server_name,
                row,
            )

    equipment = (
        _equipment_rows(full_profile)
        if full_profile and full_profile.get("itemDetails")
        else []
    )

    stones = _stone_totals_from_equipment(equipment)
    stats = extract_profile_stats(full_profile) if full_profile else []

    return {
        "ok": bool(full_info),
        "profileAvailable": profile_available,
        "info": full_info,
        "equipment": equipment,
        "magicStoneTotals": stones,
        "stats": stats,
    }




COMPARE_SITE_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AION2 공격 세팅 비교</title>
<style>
:root{
  --bg:#07101c;
  --panel:#101a2a;
  --panel2:#0b1422;
  --line:#26364e;
  --txt:#f4f7fb;
  --muted:#9cafc8;
  --accent:#77a8ff;
  --good:#6ed69b;
  --bad:#ff7f8b;
  --gold:#f7cf68;
}
*{box-sizing:border-box}
body{
  margin:0;
  background:linear-gradient(180deg,#07101c,#0b1320 45%,#101728);
  color:var(--txt);
  font-family:Arial,"Noto Sans KR",sans-serif;
}
.wrap{max-width:1320px;margin:auto;padding:28px 16px 60px}
.hero,.box{
  background:rgba(16,26,42,.97);
  border:1px solid var(--line);
  border-radius:18px;
  padding:18px;
}
h1{margin:0;font-size:30px}
.subtitle{color:var(--muted);margin-top:6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.search{margin-top:18px}
.row{display:grid;grid-template-columns:1fr 150px;gap:8px;margin-top:8px}
input,button{
  padding:11px 12px;
  border-radius:10px;
  border:1px solid var(--line);
  background:#091322;
  color:var(--txt);
  font-size:14px;
}
button{
  background:var(--accent);
  color:#07101c;
  font-weight:800;
  cursor:pointer;
  border:none;
}
.actions{margin-top:12px}
.status{font-size:12px;color:var(--muted);margin-top:8px;min-height:18px}
.section{margin-top:16px}
.hidden{display:none}
.profile{
  display:grid;
  grid-template-columns:88px 1fr;
  gap:14px;
  align-items:center;
}
.avatar{
  width:88px;height:88px;border-radius:16px;
  object-fit:cover;
  background:#0a1321;
  border:1px solid var(--line);
}
.avatar.fallback{
  display:flex;align-items:center;justify-content:center;
  font-size:34px;font-weight:900;color:var(--accent);
}
.char-name{font-size:24px;font-weight:900}
.meta{color:var(--muted);margin-top:4px}
.cp{font-size:38px;font-weight:900;margin-top:8px;color:var(--gold)}
.headline{font-size:20px;font-weight:900;margin-bottom:12px}
table{
  width:100%;
  border-collapse:collapse;
  background:var(--panel2);
  border-radius:12px;
  overflow:hidden;
}
th,td{padding:11px 10px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);text-align:left}
td:nth-child(n+2),th:nth-child(n+2){text-align:right}
tr:last-child td{border-bottom:none}
.plus{color:var(--good);font-weight:900}
.minus{color:var(--bad);font-weight:900}
.equipment-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.equip{
  display:grid;
  grid-template-columns:54px 1fr;
  gap:10px;
  background:var(--panel2);
  border:1px solid var(--line);
  border-radius:13px;
  padding:12px;
  margin-bottom:9px;
}
.equip-icon{
  width:54px;height:54px;border-radius:10px;object-fit:cover;
  background:#0a1321;border:1px solid var(--line)
}
.slot{font-size:11px;color:var(--muted)}
.ename{font-weight:900;margin-top:2px}
.eqmeta{font-size:12px;color:var(--muted);margin-top:3px}
.stone{
  display:inline-block;
  padding:4px 7px;
  border-radius:8px;
  background:#1a2a44;
  margin:5px 4px 0 0;
  font-size:12px;
}
.stone strong{color:#fff}
.notice{
  padding:12px 14px;
  border:1px dashed var(--line);
  border-radius:12px;
  background:#0a1321;
  color:var(--muted)
}
.recs{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.rec{
  background:var(--panel2);
  border:1px solid var(--line);
  border-radius:12px;
  padding:14px;
}
.rec b{display:block;margin-bottom:6px}
.kicker{font-size:12px;color:var(--accent);font-weight:800;margin-bottom:4px}
@media(max-width:860px){
  .grid2,.equipment-grid,.recs,.row{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="kicker">AION2 OFFENSE COMPARE</div>
    <h1>공격 세팅 상세 비교</h1>
    <div class="subtitle">방어/체력 계열은 제외하고 공격력 · 명중 · 치명타 · 피해증가 · 마석 · 장비만 비교합니다.</div>

    <div class="grid2 search">
      <div class="box">
        <b>내 캐릭터</b>
        <div class="row">
          <input id="na" value="윤이" placeholder="캐릭터명">
          <input id="sa" value="지켈" placeholder="서버">
        </div>
        <div class="status" id="sta"></div>
      </div>

      <div class="box">
        <b>비교 캐릭터</b>
        <div class="row">
          <input id="nb" placeholder="캐릭터명">
          <input id="sb" value="지켈" placeholder="서버">
        </div>
        <div class="status" id="stb"></div>
      </div>
    </div>

    <div class="actions">
      <button id="go">상세 비교하기</button>
    </div>
  </div>

  <div id="result" class="hidden">
    <div class="grid2 section">
      <div class="box" id="ca"></div>
      <div class="box" id="cb"></div>
    </div>

    <div class="box section">
      <div class="headline">공격 관련 스탯 비교</div>
      <table>
        <thead>
          <tr><th>항목</th><th id="ha">A</th><th id="hb">B</th><th>차이</th></tr>
        </thead>
        <tbody id="stats"></tbody>
      </table>
    </div>

    <div class="box section">
      <div class="headline">마석 정밀 비교</div>
      <table>
        <thead>
          <tr><th>마석</th><th>A 개수 / 총합 / 평균</th><th>B 개수 / 총합 / 평균</th><th>총합 차이</th></tr>
        </thead>
        <tbody id="stones"></tbody>
      </table>
    </div>

    <div class="box section">
      <div class="headline">전체 장비 + 장착 마석</div>
      <div class="equipment-grid">
        <div>
          <b id="eqha">A</b>
          <div id="eqa" style="margin-top:8px"></div>
        </div>
        <div>
          <b id="eqhb">B</b>
          <div id="eqb" style="margin-top:8px"></div>
        </div>
      </div>
    </div>

    <div class="box section">
      <div class="headline">공격 세팅 분석</div>
      <div class="recs" id="recs"></div>
    </div>
  </div>
</div>

<script>
const API="";
const E=s=>String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));
const F=n=>(Number(n||0)).toLocaleString("ko-KR");
const D=(a,b)=>{const d=Number(a||0)-Number(b||0);return `<span class="${d>0?'plus':d<0?'minus':''}">${d>0?'+':''}${F(d)}</span>`};

function avatar(info){
  const src=info.profileImage||"";
  if(src){
    return `<img class="avatar" src="${E(src)}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="avatar fallback" style="display:none">${E((info.name||'?').slice(0,1))}</div>`;
  }
  return `<div class="avatar fallback">${E((info.name||'?').slice(0,1))}</div>`;
}

function card(x){
  const i=x.info||{};
  return `
    <div class="profile">
      <div>${avatar(i)}</div>
      <div>
        <div class="char-name">${E(i.name||"-")}</div>
        <div class="meta">${E(i.server||"-")} · ${E(i.job||"-")} · Lv.${F(i.level)}</div>
        <div class="cp">${Math.round(Number(i.combatPower||0)/1000)}</div>
        <div class="meta">전투력</div>
      </div>
    </div>
    <div class="status" style="margin-top:12px">
      ${x.profileAvailable ? "상세 장비/마석 프로필 연결됨" : "기본정보만 표시 중"}
    </div>
  `;
}

function mapStats(x){const m={};(x.stats||[]).forEach(s=>m[s.name]=s);return m}
function mapStones(x){const m={};(x.magicStoneTotals||[]).forEach(s=>m[s.name]=s);return m}

function equipment(rows){
  if(!rows.length) return `<div class="notice">장비 상세 데이터 없음</div>`;

  return rows.map(x=>`
    <div class="equip">
      <div>
        ${x.icon
          ? `<img class="equip-icon" src="${E(x.icon)}" onerror="this.style.display='none'">`
          : `<div class="equip-icon"></div>`}
      </div>
      <div>
        <div class="slot">${E(x.slot)}</div>
        <div class="ename">${x.enhance!=null?`+${E(x.enhance)} `:''}${E(x.name)}</div>
        <div class="eqmeta">${E(x.grade||'')} ${x.itemLevel?`· Lv.${E(x.itemLevel)}`:''}</div>
        <div>
          ${(x.magicStones||[]).map(s=>`<span class="stone"><strong>${E(s.name)}</strong> ${E(s.value)}</span>`).join("")}
        </div>
      </div>
    </div>
  `).join("");
}

function recommendations(a,b,sa,sb){
  const arr=[];
  const cpGap=Number(b.info?.combatPower||0)-Number(a.info?.combatPower||0);

  arr.push({
    t:"전투력 차이",
    b:cpGap>0 ? `상대보다 ${F(cpGap)} 낮음` : `내 캐릭터가 ${F(Math.abs(cpGap))} 높거나 같음`
  });

  const diffs=[...new Set([...Object.keys(sa),...Object.keys(sb)])]
    .map(n=>({n,d:(sb[n]?.total||0)-(sa[n]?.total||0)}))
    .sort((x,y)=>y.d-x.d);

  if(diffs.length && diffs[0].d>0){
    arr.push({t:"가장 부족한 마석",b:`${diffs[0].n} 총합 ${F(diffs[0].d)} 부족`});
  }else{
    arr.push({t:"마석",b:"상대보다 크게 부족한 마석 총합 없음"});
  }

  arr.push({
    t:"분석 기준",
    b:"방어력/체력 계열은 제외하고 공격 관련 수치만 비교합니다."
  });

  recs.innerHTML=arr.map(x=>`
    <div class="rec">
      <b>${E(x.t)}</b>
      <div class="muted">${E(x.b)}</div>
    </div>
  `).join("");
}

function render(a,b){
  ca.innerHTML=card(a);
  cb.innerHTML=card(b);

  ha.textContent=eqha.textContent=a.info?.name||"A";
  hb.textContent=eqhb.textContent=b.info?.name||"B";

  const ma=mapStats(a);
  const mb=mapStats(b);
  const names=[...new Set([...Object.keys(ma),...Object.keys(mb)])];

  const basics=[
    ["전투력",{value:a.info?.combatPower||0},{value:b.info?.combatPower||0}]
  ];

  const detailRows=names.map(n=>[n,ma[n]||{value:0},mb[n]||{value:0}]);

  stats.innerHTML=basics.concat(detailRows).map(r=>`
    <tr>
      <td>${E(r[0])}</td>
      <td>${F(r[1].value)}</td>
      <td>${F(r[2].value)}</td>
      <td>${D(r[1].value,r[2].value)}</td>
    </tr>
  `).join("");

  const sa1=mapStones(a), sb1=mapStones(b);
  const sn=[...new Set([...Object.keys(sa1),...Object.keys(sb1)])];

  stones.innerHTML=sn.length
    ? sn.map(n=>{
        const x=sa1[n]||{count:0,total:0,average:0};
        const y=sb1[n]||{count:0,total:0,average:0};
        return `
          <tr>
            <td>${E(n)}</td>
            <td>${x.count} / ${F(x.total)} / ${Number(x.average).toFixed(2)}</td>
            <td>${y.count} / ${F(y.total)} / ${Number(y.average).toFixed(2)}</td>
            <td>${D(x.total,y.total)}</td>
          </tr>
        `;
      }).join("")
    : `<tr><td colspan="4">마석 상세 데이터 없음</td></tr>`;

  eqa.innerHTML=equipment(a.equipment||[]);
  eqb.innerHTML=equipment(b.equipment||[]);

  recommendations(a,b,sa1,sb1);

  result.classList.remove("hidden");
}

async function compare(){
  const nameA=na.value.trim(), serverA=sa.value.trim();
  const nameB=nb.value.trim(), serverB=sb.value.trim();

  if(!nameA||!serverA||!nameB||!serverB){
    sta.textContent=stb.textContent="캐릭터명과 서버를 모두 입력하세요.";
    return;
  }

  sta.textContent=stb.textContent="상세 정보 불러오는 중...";

  const p=new URLSearchParams({
    name_a:nameA,server_a:serverA,name_b:nameB,server_b:serverB
  });

  try{
    const r=await fetch(`/api/compare?${p}`);
    const d=await r.json();

    if(!d.ok){
      sta.textContent=d.a?.error||"내 캐릭터 조회 실패";
      stb.textContent=d.b?.error||"비교 캐릭터 조회 실패";
      return;
    }

    sta.textContent=d.a.profileAvailable?"상세 프로필 연결됨":"기본정보만";
    stb.textContent=d.b.profileAvailable?"상세 프로필 연결됨":"기본정보만";

    render(d.a,d.b);
  }catch(e){
    sta.textContent=stb.textContent="비교 서버 연결 실패";
  }
}

go.addEventListener("click",compare);
</script>
</body>
</html>"""


@app.get("/compare", response_class=HTMLResponse)
async def compare_site():
    return HTMLResponse(
        COMPARE_SITE_HTML,
        media_type="text/html; charset=utf-8"
    )


@app.get("/api/compare-health")
async def api_compare_health():
    return {
        "ok": True,
        "version": "v41-compare-attack-only",
        "cors": True,
    }


@app.get("/api/compare-character")
async def api_compare_character(nickname: str, server: str):
    try:
        data = await detailed_character_data(nickname, server)
        return {
            "version": "v41-compare-attack-only",
            **data,
        }
    except Exception as e:
        return {
            "ok": False,
            "version": "v41-compare-attack-only",
            "error": f"{type(e).__name__}: {str(e)[:400]}",
        }


@app.get("/api/compare")
async def api_compare(
    name_a: str,
    server_a: str,
    name_b: str,
    server_b: str,
):
    # Load sequentially on purpose. Full-profile endpoint can throttle paired
    # simultaneous requests, which caused one side to show details and the other
    # side to fall back to basic info.
    a = await detailed_character_data(name_a, server_a)

    # Tiny spacing before the second full-profile request.
    await asyncio.sleep(0.35)

    b = await detailed_character_data(name_b, server_b)

    return {
        "ok": bool(a.get("ok") and b.get("ok")),
        "version": "v41-compare-attack-only",
        "a": a,
        "b": b,
    }


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

DEFAULT_BOSS_RULES = {
    "kairaHours": [1, 5, 9, 13, 17, 21],
    "nahmaWeekdays": [4, 6],     # Fri / Sun, Monday=0
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

def _parse_maintenance_end_from_notice(row):
    title = str((row or {}).get("title") or "")
    raw = str((row or {}).get("rawText") or "")
    source = title + "\n" + raw
    posted = str((row or {}).get("date") or "")

    m = re.search(
        r"(?P<month>\d{1,2})\s*/\s*(?P<day>\d{1,2})"
        r".{0,80}?"
        r"(?P<sh>\d{1,2})\s*:\s*(?P<sm>\d{2})"
        r"\s*(?:~|∼|～|-)\s*"
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
        start_hour = int(m.group("sh"))
        start_minute = int(m.group("sm"))
        end_hour = int(m.group("eh"))
        end_minute = int(m.group("em"))

        start = datetime(year, month, day, start_hour, start_minute, tzinfo=KST)
        end = datetime(year, month, day, end_hour, end_minute, tzinfo=KST)

        if end <= start:
            end += timedelta(days=1)

        return end
    except Exception:
        return None

async def latest_maintenance_anchor():
    now_ts = time.time()
    cached = _maintenance_anchor_cache.get("value")
    cached_ts = float(_maintenance_anchor_cache.get("ts") or 0)

    if cached is not None and now_ts - cached_ts < 300:
        return cached

    anchor = None
    source_title = None

    try:
        rows = await fetch_board_latest("공지", limit=18)
        for row in rows:
            if "점검" not in str(row.get("title") or ""):
                continue
            parsed = _parse_maintenance_end_from_notice(row)
            if parsed is not None:
                anchor = parsed
                source_title = row.get("title")
                break
    except Exception:
        anchor = None

    if anchor is None:
        anchor = AGRO_FALLBACK_ANCHOR

    _maintenance_anchor_cache["value"] = anchor
    _maintenance_anchor_cache["ts"] = now_ts

    if source_title:
        BOSS_RULES_META["sources"]["maintenance"] = str(source_title)

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
                        "url": post["link"],
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
        "version": "v41-compare-attack-only",
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
        "version": "v41-compare-attack-only",
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
        "version": "v41-compare-attack-only",
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


@app.get("/debug/official-raw")
async def debug_official_raw(nickname: str, server: str):
    server_id = SERVER_ID_MAP.get(server)
    if not server_id:
        return {"ok": False, "error": "unknown server"}

    race = _official_server_race(server_id)
    client = await get_http_client()
    result = {
        "version": "v41-compare-attack-only",
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
            "version": "v41-compare-attack-only",
            "count": len(rows),
            "results": details,
        }

    except Exception as e:
        return {
            "ok": False,
            "version": "v41-compare-attack-only",
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
                "version": "v41-compare-attack-only",
                "type": "detail",
                "info": info,
            }

        if resolved.get("type") == "multiple":
            return {
                "ok": True,
                "version": "v41-compare-attack-only",
                "type": "multiple",
                "items": [
                    item.get("info") or {}
                    for item in resolved.get("items") or []
                ],
            }

        return {
            "ok": True,
            "version": "v41-compare-attack-only",
            "type": "none",
        }
    except Exception as e:
        return {
            "ok": False,
            "version": "v41-compare-attack-only",
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
        "version": "v41-compare-attack-only",
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
        text = "✅ AION2 v41 서버 정상" if body == "테스트" else "📱 알림 설정은 휴대폰 봇에서 처리됩니다."
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
