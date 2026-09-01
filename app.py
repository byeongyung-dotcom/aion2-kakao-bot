
import asyncio
import re
import time
from urllib.parse import urljoin

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

app = FastAPI(title="AION2 Kakao Skill Server")

AION2_HOME = "https://aion2.plaync.com/ko-kr"
NOTICE_URL = "https://aion2.plaync.com/ko-kr/board/notice/list"
UPDATE_URL = "https://aion2.plaync.com/ko-kr/board/update/list"
NOTMETER_URL = "https://notmeter.com/"
NOTMETER_FIELD_BOSS = "https://notmeter.com/?view=field-boss"
FIXED_SERVER = "지켈"
KAKAO_CHANNEL_URL = "http://pf.kakao.com/_xorUxaX"

_pw = None
_browser = None
_lock = asyncio.Lock()

CLASSES = [
    "검성", "수호성", "살성", "궁성", "마도성",
    "정령성", "치유성", "호법성", "집행자", "권성", "원소술사"
]

STONE_STATS = [
    "무기 피해 증폭", "치명타 피해 증폭", "후방 피해 증폭", "전방 피해 증폭",
    "공격력", "추가 공격력", "치명타", "치명타 저항",
    "추가 명중", "명중", "막기", "방어력", "생명력",
    "추가 회피", "회피", "정신력", "완벽", "강타"
]

def compact(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def kakao_text(text: str):
    # 카카오 오픈빌더 Skill Response v2
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text[:1000]}}
            ]
        }
    }

def kakao_card(title: str, description: str, url: str):
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "basicCard": {
                        "title": title[:50],
                        "description": description[:230],
                        "buttons": [
                            {
                                "action": "webLink",
                                "label": "원문 보기",
                                "webLinkUrl": url
                            }
                        ]
                    }
                }
            ]
        }
    }

@app.on_event("startup")
async def startup():
    global _pw, _browser
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )

@app.on_event("shutdown")
async def shutdown():
    global _pw, _browser
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()

async def new_page():
    return await _browser.new_page(
        viewport={"width": 1440, "height": 1200},
        locale="ko-KR"
    )

async def goto(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(800)

async def latest_board(label: str, url: str, href_fragment: str, limit: int = 3):
    page = await new_page()
    try:
        await goto(page, url)
        anchors = page.locator("a")
        n = await anchors.count()
        out = []
        seen = set()

        for i in range(n):
            a = anchors.nth(i)
            try:
                href = await a.get_attribute("href") or ""
                if href_fragment not in href:
                    continue
                title = compact(await a.inner_text(timeout=500))
                if len(title) < 3:
                    continue
                full = urljoin(url, href)
                if full in seen:
                    continue
                seen.add(full)
                out.append((title, full))
                if len(out) >= limit:
                    break
            except Exception:
                continue
        return out
    finally:
        await page.close()

async def latest_notice():
    items = await latest_board("공지", NOTICE_URL, "/board/notice/view", 3)
    if not items:
        return kakao_text("📢 최신 공지를 읽지 못했습니다.")
    lines = ["📢 AION2 최신 공지"]
    for idx, (title, url) in enumerate(items, 1):
        lines.append(f"{idx}. {title}\n{url}")
    return kakao_text("\n\n".join(lines))

async def latest_update():
    items = await latest_board("업데이트", UPDATE_URL, "/board/update/view", 3)
    if not items:
        return kakao_text("🛠️ 최신 업데이트를 읽지 못했습니다.")
    lines = ["🛠️ AION2 최신 업데이트"]
    for idx, (title, url) in enumerate(items, 1):
        lines.append(f"{idx}. {title}\n{url}")
    return kakao_text("\n\n".join(lines))

async def find_cm_page(page):
    await goto(page, AION2_HOME)
    anchors = page.locator("a")
    n = await anchors.count()
    for i in range(n):
        a = anchors.nth(i)
        try:
            txt = compact(await a.inner_text(timeout=300))
            href = await a.get_attribute("href") or ""
            if "CM 아지트" in txt and href:
                return urljoin(AION2_HOME, href)
        except Exception:
            continue
    return "https://lounge.plaync.com/"

async def latest_cm():
    page = await new_page()
    try:
        cm_url = await find_cm_page(page)
        await goto(page, cm_url)
        anchors = page.locator("a")
        n = await anchors.count()
        out, seen = [], set()

        for i in range(n):
            a = anchors.nth(i)
            try:
                href = await a.get_attribute("href") or ""
                if "/feed/" not in href:
                    continue
                title = compact(await a.inner_text(timeout=400))
                if len(title) < 4:
                    continue
                full = urljoin(cm_url, href)
                if full in seen:
                    continue
                seen.add(full)
                out.append((title, full))
                if len(out) >= 3:
                    break
            except Exception:
                continue

        if not out:
            return kakao_text("💬 최신 CM 글을 읽지 못했습니다.")

        lines = ["💬 AION2 최신 CM"]
        for idx, (title, url) in enumerate(out, 1):
            lines.append(f"{idx}. {title}\n{url}")
        return kakao_text("\n\n".join(lines))
    finally:
        await page.close()

async def select_zikel(page):
    await goto(page, NOTMETER_FIELD_BOSS)
    inputs = page.locator('input[placeholder*="서버"]')
    if await inputs.count():
        inp = inputs.first
        await inp.fill(FIXED_SERVER)
        await page.wait_for_timeout(700)
        try:
            await page.get_by_text(FIXED_SERVER, exact=True).last.click(timeout=2000)
        except Exception:
            try:
                await inp.press("Enter")
            except Exception:
                pass
        await page.wait_for_timeout(1800)

async def get_boss_lines(page):
    await select_zikel(page)
    text = await page.locator("body").inner_text()
    start = text.find("필드보스 현황")
    section = text[start:start + 7000] if start >= 0 else text[:7000]

    for marker in ["CP 800K+", "CP 보정 직업 DPS", "클래스별 종합 TOP 10"]:
        p = section.find(marker)
        if p > 0:
            section = section[:p]

    ignore = {
        "필드보스 현황", "← 랭킹으로 돌아가기", "서버", "↻ 새로고침",
        "필드보스 공유 캐시를 불러오는 중입니다",
        "필드보스 캐시를 불러오지 못했습니다",
        "선택한 서버에서 수집된 필드보스 시간이 아직 없습니다",
        "다시 시도", "—", "— —", "!"
    }

    return [compact(x) for x in section.splitlines()
            if compact(x) and compact(x) not in ignore]

def looks_time(s):
    return bool(
        re.search(r"\b\d{1,2}:\d{2}\b", s)
        or re.search(r"\d+\s*(?:시간|분|초)", s)
        or "남음" in s or "출현" in s or "젠" in s
    )

async def boss_search(name=None):
    page = await new_page()
    try:
        lines = await get_boss_lines(page)
        if not name:
            if not lines:
                return None
            return "👹 지켈 필드보스\n" + "\n".join(lines[:30])

        for i, line in enumerate(lines):
            if name.lower() in line.lower():
                picked = [line]
                if not looks_time(line):
                    for j in range(i + 1, min(i + 4, len(lines))):
                        picked.append(lines[j])
                        if looks_time(lines[j]):
                            break
                return f"👹 지켈 | {name}\n" + "\n".join(picked)
        return None
    finally:
        await page.close()

def parse_character(text: str, nickname: str):
    job = None
    for cls in CLASSES:
        if cls in text:
            job = cls
            break

    cp = None
    for pat in [
        r"(?:전투력|CP)\s*[:：]?\s*([0-9][0-9,.]*)\s*K?",
        r"\b([0-9]{3,4})\s*K\b"
    ]:
        m = re.search(pat, text, re.I)
        if m:
            cp = m.group(1).replace(",", "")
            break

    idx = text.find("장착 마석 총합")
    section = text[idx:idx + 6000] if idx >= 0 else text

    stones = []
    used = set()
    for stat in STONE_STATS:
        m = re.search(re.escape(stat) + r"\s*([+-]\s*[0-9,.]+%?)", section)
        if m and stat not in used:
            stones.append((stat, m.group(1).replace(" ", "")))
            used.add(stat)

    if not job and not cp and not stones:
        return None

    lines = [f"🔎 {nickname}"]
    lines.append(f"직업 : {job or '확인 실패'}")
    lines.append(f"전투력 : {cp or '확인 실패'}")
    lines.append("")
    lines.append("💎 장착 마석 총합")
    if stones:
        for stat, val in stones:
            lines.append(f"{stat} {val}")
    else:
        lines.append("마석 정보를 읽지 못했습니다.")
    return "\n".join(lines)

async def character_search(nickname):
    page = await new_page()
    try:
        await goto(page, NOTMETER_URL)

        inp = page.locator('input[placeholder*="캐릭터 이름"]').first
        if not await inp.count():
            return None

        before = await page.locator("body").inner_text()
        await inp.fill(nickname)

        btn = page.get_by_role("button", name=re.compile("^검색$"))
        if await btn.count():
            await btn.first.click()
        else:
            await inp.press("Enter")

        # 검색 결과가 서버 후보 목록을 띄우면 지켈을 우선 선택
        await page.wait_for_timeout(1200)
        try:
            exact = page.get_by_text(FIXED_SERVER, exact=True)
            if await exact.count():
                await exact.last.click(timeout=1500)
        except Exception:
            pass

        for _ in range(24):
            text = await page.locator("body").inner_text()
            if text != before and (
                "장착 마석 총합" in text
                or "캐릭터 정보" in text
                or nickname in text
            ):
                parsed = parse_character(text, nickname)
                if parsed:
                    return parsed
            await page.wait_for_timeout(500)

        text = await page.locator("body").inner_text()
        return parse_character(text, nickname)
    finally:
        await page.close()

async def route_command(utterance: str):
    cmd = compact(utterance)
    if not cmd.startswith("!"):
        return kakao_text("명령어는 !를 붙여서 입력해 주세요.\n예: !윤이 / !아그로 / !공지")

    body = cmd[1:].strip()
    if not body:
        return kakao_text("사용법\n!윤이\n!아그로\n!필보\n!공지\n!CM\n!업데이트")

    low = body.lower()
    if low in ("도움", "도움말", "help"):
        return kakao_text(
            "🤖 AION2 봇\n"
            "!닉네임 → 직업/전투력/마석작\n"
            "!보스명 → 지켈 필드보스 시간\n"
            "!필보 → 지켈 필드보스 전체\n"
            "!공지 → 최신 공지\n"
            "!CM → 최신 CM\n"
            "!업데이트 → 최신 업데이트"
        )

    if body == "공지":
        return await latest_notice()
    if low == "cm":
        return await latest_cm()
    if body == "업데이트":
        return await latest_update()
    if body == "필보":
        result = await boss_search(None)
        return kakao_text(result or "👹 지켈 필드보스 정보를 읽지 못했습니다.")

    # 통합검색: 보스 우선 -> 캐릭터
    boss = await boss_search(body)
    if boss:
        return kakao_text(boss)

    char = await character_search(body)
    if char:
        return kakao_text(char)

    return kakao_text(f"🔎 {body}\n지켈 필드보스 또는 캐릭터 정보를 찾지 못했습니다.")

@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "AION2 Kakao Skill Server",
        "server": FIXED_SERVER,
        "channel": KAKAO_CHANNEL_URL
    }

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    try:
        payload = await request.json()
        utterance = (
            payload.get("userRequest", {}).get("utterance")
            or payload.get("action", {}).get("params", {}).get("utterance")
            or ""
        )

        async with _lock:
            result = await asyncio.wait_for(route_command(utterance), timeout=28)
        return JSONResponse(result)
    except asyncio.TimeoutError:
        return JSONResponse(kakao_text("⚠️ 조회 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."))
    except Exception as e:
        return JSONResponse(kakao_text("⚠️ 조회 중 오류가 발생했습니다."))
