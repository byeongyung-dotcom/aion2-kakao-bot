
import asyncio
import os
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="AION2 Kakao Skill Server v5")

# =========================================================
# Common
# =========================================================

SERVER_ID = 2002
SERVER_NAME = "지켈"
KST = ZoneInfo("Asia/Seoul")

# Character API - current NotMeter endpoint
NOTMETER_API = "https://notmeter.59-27-108-81.sslip.io"

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

HTTP_TIMEOUT = httpx.Timeout(connect=1.5, read=2.8, write=1.0, pool=1.0)

CACHE_TTL = 60
FIELD_BOSS_CACHE_TTL = 120
_cache = {}

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

async def http_json(url: str, params=None):
    async with httpx.AsyncClient(
        headers=HEADERS,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        http2=True,
    ) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()

# =========================================================
# Character search
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

async def character_api_get(path, params):
    return await http_json(NOTMETER_API + path, params=params)

async def find_zikel_character(nickname: str):
    data = await character_api_get(
        "/character/v1/search",
        {
            "name": nickname,
            "region": "kr",
            "lang": "ko",
            "fast": "1",
        },
    )

    results = data.get("results") or []

    for row in results:
        if (
            str(row.get("name", "")).strip() == nickname
            and int(row.get("serverId") or 0) == SERVER_ID
        ):
            return row

    target = nickname.casefold()
    for row in results:
        if int(row.get("serverId") or 0) != SERVER_ID:
            continue
        if str(row.get("name", "")).strip().casefold() == target:
            return row

    return None

async def get_profile(character_id: str):
    return await character_api_get(
        "/character/v1/profile",
        {
            "serverId": SERVER_ID,
            "characterId": character_id,
            "region": "kr",
            "lang": "ko",
            "fast": "1",
        },
    )

def aggregate_magic_stones(profile_json):
    totals = defaultdict(float)
    ids_by_name = {}

    item_details = profile_json.get("itemDetails") or {}

    for item in item_details.values():
        if not isinstance(item, dict):
            continue

        for stone in item.get("magicStoneStat") or []:
            if not isinstance(stone, dict):
                continue

            name = str(stone.get("name") or "").strip()
            stat_id = str(stone.get("id") or "").strip()
            if not name:
                continue

            totals[name] += number_from_value(stone.get("value"))
            ids_by_name[name] = stat_id

    formatted = []
    already = set()

    for name in STONE_ORDER:
        if name not in totals:
            continue
        value = totals[name]
        stat_id = ids_by_name.get(name, "")

        if stat_id in PERCENT_STONE_IDS:
            formatted.append((name, f"+{pretty_number(value / 100.0)}%"))
        else:
            formatted.append((name, f"+{pretty_number(value)}"))
        already.add(name)

    for name, value in totals.items():
        if name in already:
            continue
        stat_id = ids_by_name.get(name, "")
        if stat_id in PERCENT_STONE_IDS:
            formatted.append((name, f"+{pretty_number(value / 100.0)}%"))
        else:
            formatted.append((name, f"+{pretty_number(value)}"))

    return formatted

def format_character(profile_json, requested_name):
    profile = profile_json.get("info", {}).get("profile", {})

    name = profile.get("characterName") or requested_name
    job = profile.get("className") or "확인 실패"
    combat_power = int(profile.get("combatPower") or 0)
    server = profile.get("serverName") or SERVER_NAME

    # 922412 -> 922
    cp_short = round(combat_power / 1000) if combat_power else 0

    stones = aggregate_magic_stones(profile_json)

    lines = [
        f"🔎 {name}",
        f"서버 : {server}",
        f"직업 : {job}",
        f"전투력 : {cp_short}",
        "",
        "💎 장착 마석 총합",
    ]

    if stones:
        for stat_name, value in stones:
            lines.append(f"{stat_name} {value}")
    else:
        lines.append("마석 정보를 찾지 못했습니다.")

    return "\n".join(lines)

async def character_lookup(nickname: str):
    cache_key = f"char:{nickname}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    candidate = await find_zikel_character(nickname)
    if not candidate:
        return None

    character_id = candidate.get("characterId")
    if not character_id:
        return None

    profile_json = await get_profile(character_id)
    actual_profile = profile_json.get("info", {}).get("profile", {})

    if int(actual_profile.get("serverId") or 0) != SERVER_ID:
        return None

    result = format_character(profile_json, nickname)
    cache_set(cache_key, result)
    return result

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

def boss_time_text(target_at):
    target = datetime.fromtimestamp(target_at / 1000, tz=KST)
    now = datetime.now(KST)
    seconds = int((target - now).total_seconds())

    clock = target.strftime("%m/%d %H:%M")

    if seconds <= 0:
        ago = abs(seconds)
        if ago < 60:
            status = "시간 도달"
        elif ago < 3600:
            status = f"{ago // 60}분 지남"
        else:
            status = f"{ago // 3600}시간 {(ago % 3600) // 60}분 지남"
        return f"{clock} ({status})"

    if seconds < 60:
        left = f"{seconds}초 남음"
    elif seconds < 3600:
        left = f"{seconds // 60}분 {seconds % 60}초 남음"
    else:
        left = f"{seconds // 3600}시간 {(seconds % 3600) // 60}분 남음"

    return f"{clock} ({left})"

def format_all_field_bosses(cache):
    rows = zikel_boss_entries(cache)

    if not rows:
        return "👹 지켈 필드보스\n수집된 시간 정보가 없습니다."

    generated = int(cache.get("generatedAt") or 0)
    if generated:
        generated_text = datetime.fromtimestamp(
            generated / 1000, tz=KST
        ).strftime("%m/%d %H:%M")
    else:
        generated_text = "-"

    lines = [
        "👹 지켈 필드보스",
        f"갱신 : {generated_text}",
        "",
    ]

    # Kakao simpleText limit is 1000 chars.
    # Show nearest/current timers first.
    for row in rows:
        line = f"{row['name']} · {boss_time_text(row['targetAt'])}"
        if len("\n".join(lines + [line])) > 930:
            lines.append(f"... 외 {len(rows) - (len(lines) - 3)}개")
            break
        lines.append(line)

    return "\n".join(lines)

def normalize_boss_query(query):
    return re.sub(r"\s+", "", str(query or "")).casefold()

def format_one_boss(cache, query):
    rows = zikel_boss_entries(cache)
    q = normalize_boss_query(query)

    matches = [
        row for row in rows
        if q in normalize_boss_query(row["name"])
    ]

    if not matches:
        return (
            f"👹 {query}\n"
            "지켈 서버에서 수집된 출현 시간을 찾지 못했습니다."
        )

    lines = [f"👹 {query} · 지켈", ""]
    for row in matches:
        lines.append(
            f"{row['name']}\n"
            f"{row['region']} · {boss_time_text(row['targetAt'])}"
        )
    return "\n".join(lines)

async def field_boss_lookup(query=None):
    cache = await fetch_field_boss_cache()
    if not query:
        return format_all_field_bosses(cache)
    return format_one_boss(cache, query)


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

    url = (
        f"{COMMUNITY_API}/{alias}/article/search/moreArticle"
    )

    data = await http_json(
        url,
        params={
            "isVote": "true",
            "moreSize": "18",
            "moreDirection": "BEFORE",
            "previousArticleId": "0",
        },
    )

    rows = []
    for item in data.get("contentList") or []:
        if not isinstance(item, dict):
            continue

        content_id = ((item.get("snow") or {}).get("contentId"))
        title = str(item.get("title") or "").strip()
        posted = ((item.get("timestamps") or {}).get("postDateTime") or "")

        if not content_id or not title:
            continue

        date_text = str(posted)[:10] if posted else ""
        link = (
            f"https://aion2.plaync.com/ko-kr/board/"
            f"{config['view']}/view?articleId={content_id}"
        )

        rows.append({
            "id": int(content_id),
            "title": title,
            "date": date_text,
            "link": link,
        })

        if len(rows) >= limit:
            break

    return rows

def format_board_latest(command: str, rows):
    label = BOARD_CONFIGS[command]["label"]

    if not rows:
        return f"📢 AION2 {label}\n최신 글을 찾지 못했습니다."

    lines = [f"📢 AION2 {label} 최신", ""]

    for index, row in enumerate(rows, 1):
        block = (
            f"{index}. {row['title']}\n"
            f"{row['date']}\n"
            f"{row['link']}"
        )

        candidate = "\n\n".join(lines + [block])
        if len(candidate) > 950:
            break

        lines.append(block)

    return "\n\n".join(lines)

async def board_lookup(command: str):
    cache_key = f"board:{command}"
    cached = cache_get(cache_key, 60)
    if cached:
        return cached

    rows = await fetch_board_latest(command, limit=5)
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

NOTICE_ALERT_KEYWORDS = (
    "점검",
    "정기점검",
    "임시점검",
    "긴급점검",
    "서버점검",
)

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
        "공지 : 새 점검 공지만\n"
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
        "service": "AION2 Kakao Skill Server v5",
        "server": SERVER_NAME,
        "character": "NotMeter direct API",
        "fieldBoss": "NotMeter public cache",
        "officialBoards": ["공지", "CM", "업데이트"],
    }

@app.get("/health")
async def health():
    return {"ok": True}

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
                character_lookup(body),
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
                    "지켈 서버에서 캐릭터를 찾지 못했습니다."
                )
            )

        return JSONResponse(kakao_text(result))

    except Exception:
        return JSONResponse(
            kakao_text("⚠️ 조회 중 오류가 발생했습니다.")
        )
