import os
import sys
import httpx

BASE_URL = os.getenv(
    "AION2_BOT_BASE_URL",
    "https://aion2-kakao-bot.onrender.com",
).rstrip("/")
SECRET = os.getenv("ALERT_CRON_SECRET", "").strip()

url = f"{BASE_URL}/alerts/check"
params = {"secret": SECRET} if SECRET else {}

try:
    response = httpx.get(url, params=params, timeout=30.0)
    print(response.status_code)
    print(response.text)
    response.raise_for_status()
except Exception as e:
    print(f"cron failed: {type(e).__name__}: {e}")
    sys.exit(1)
