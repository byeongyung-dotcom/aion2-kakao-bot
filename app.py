
import asyncio
import re
from collections import defaultdict
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="AION2 Kakao Skill Server v2")

# -----------------------------
# Fixed settings
# -----------------------------
NOTMETER_API = "https://notmeter59-27-108-81.sslip.io"
SERVER_ID = 2002
SERVER_NAME = "지켈"

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

# Kakao skill hard timeout is short, so keep upstream timeouts tight.
HTTP_TIMEOUT = httpx.Timeout(connect=1.5, read=2.8, write=1.0, pool=1.0)

# Short cache: repeat lookups become almost immediate.
CACHE_TTL = 60
_cache = {}

# NotMeter magicStoneStat values:
# +100 for damage amp stones is shown by NotMeter as +1%.
PERCENT_STONE_IDS = {
    "AmplifyWeaponDamage",     # 무기 피해 증폭
    "AmplifyCriticalDamage",   # 치명타 피해 증폭
    "AmplifyBackAttack",       # 후방 피해 증폭
    "AmplifyFrontAttack",      # 전방 피해 증폭
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

def number_from_value(value):
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else 0.0

def pretty_number(v):
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.1f}".rstrip("0").rstrip(".")

def cache_get(key):
    import time
    item = _cache.get(key)
    if not item:
        return None
    saved, value = item
    if time.time() - saved > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return value

def cache_set(key, value):
    import time
    _cache[key] = (time.time(), value)

async def api_get(path: str, params: dict):
    async with httpx.AsyncClient(
        headers=HEADERS,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        http2=True,
    ) as client:
        response = await client.get(NOTMETER_API + path, params=params)
        response.raise_for_status()
        return response.json()

async def find_zikel_character(nickname: str):
    data = await api_get(
        "/character/v1/search",
        {
            "name": nickname,
            "region": "kr",
            "lang": "ko",
            "fast": "1",
        },
    )

    results = data.get("results") or []

    # Exact nickname + Zikel first.
    for row in results:
        if (
            str(row.get("name", "")).strip() == nickname
            and int(row.get("serverId") or 0) == SERVER_ID
        ):
            return row

    # Fallback: Zikel result whose nickname differs only by case/spacing.
    target = nickname.casefold()
    for row in results:
        if int(row.get("serverId") or 0) != SERVER_ID:
            continue
        if str(row.get("name", "")).strip().casefold() == target:
            return row

    return None

async def get_profile(character_id: str):
    return await api_get(
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

    for _, item in item_details.items():
        if not isinstance(item, dict):
            continue

        stones = item.get("magicStoneStat") or []
        for stone in stones:
            if not isinstance(stone, dict):
                continue

            name = str(stone.get("name") or "").strip()
            stat_id = str(stone.get("id") or "").strip()
            if not name:
                continue

            val = number_from_value(stone.get("value"))
            totals[name] += val
            ids_by_name[name] = stat_id

    formatted = []

    # known stats first
    already = set()
    for name in STONE_ORDER:
        if name not in totals:
            continue

        value = totals[name]
        stat_id = ids_by_name.get(name, "")

        if stat_id in PERCENT_STONE_IDS:
            # NotMeter stone convention: 100 == 1%
            shown = value / 100.0
            formatted.append((name, f"+{pretty_number(shown)}%"))
        else:
            formatted.append((name, f"+{pretty_number(value)}"))

        already.add(name)

    # Any future/unknown stone stat also gets shown.
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
    profile = (
        profile_json.get("info", {})
        .get("profile", {})
    )

    name = profile.get("characterName") or requested_name
    job = profile.get("className") or "확인 실패"
    combat_power = int(profile.get("combatPower") or 0)
    server = profile.get("serverName") or SERVER_NAME

    # User wants 922412 -> 922
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

    # Ensure the detail API is complete and still points to Zikel.
    actual_profile = profile_json.get("info", {}).get("profile", {})
    if int(actual_profile.get("serverId") or 0) != SERVER_ID:
        return None

    result = format_character(profile_json, nickname)
    cache_set(cache_key, result)
    return result

# -----------------------------
# Routes
# -----------------------------

@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "AION2 Kakao Skill Server v2 API",
        "server": SERVER_NAME,
        "characterMode": "NotMeter direct API",
    }

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/debug/character/{nickname}")
async def debug_character(nickname: str):
    """
    Browser test endpoint.
    Example:
    /debug/character/윤이
    """
    try:
        result = await asyncio.wait_for(character_lookup(nickname), timeout=4.4)
        if not result:
            return {"ok": False, "message": "character not found"}
        return {"ok": True, "result": result}
    except Exception as e:
        return {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e)[:300],
        }

@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    try:
        payload = await request.json()
        utterance = (
            payload.get("userRequest", {}).get("utterance")
            or ""
        )

        command = clean_command(utterance)

        if not command.startswith("!"):
            return JSONResponse(
                kakao_text(
                    "명령어 앞에 !를 붙여주세요.\n"
                    "예: !윤이"
                )
            )

        body = command[1:].strip()

        if not body:
            return JSONResponse(
                kakao_text(
                    "사용법\n"
                    "!윤이 → 지켈 캐릭터 검색"
                )
            )

        if body in ("도움", "도움말") or body.lower() == "help":
            return JSONResponse(
                kakao_text(
                    "🤖 AION2 BOT\n"
                    "!닉네임 → 지켈 캐릭터 직업 / 전투력 / 마석작\n"
                    "예: !윤이"
                )
            )

        # v2 first priority: fast character API.
        # !공지 / !CM / !필보 will be added back after their direct APIs are identified.
        if body in ("공지", "필보", "업데이트") or body.lower() == "cm":
            return JSONResponse(
                kakao_text(
                    f"⚙️ !{body} 기능은 API 직결 방식으로 교체 중입니다.\n"
                    "현재는 !닉네임 캐릭터 검색부터 테스트해 주세요."
                )
            )

        try:
            result = await asyncio.wait_for(character_lookup(body), timeout=4.35)
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
        # Always return valid Kakao JSON quickly instead of hanging.
        return JSONResponse(
            kakao_text("⚠️ 캐릭터 조회 중 오류가 발생했습니다.")
        )
