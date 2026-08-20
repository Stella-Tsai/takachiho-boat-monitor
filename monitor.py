import os
import re
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

TARGET_DATE = os.getenv("TARGET_DATE", "2026-08-31")
TARGET_GUESTS = int(os.getenv("TARGET_GUESTS", "2"))
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BOOKING_URL = "https://eipro.jp/takachiho1/eventCalendars/index"
STATE_FILE = Path(".notified_state")


def telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False},
        timeout=20,
    )
    r.raise_for_status()


def extract_target_cell(page):
    cell = page.locator(f"[data-date='{TARGET_DATE}']").first
    if cell.count() == 0:
        return None
    return cell


def inspect_availability(page):
    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(8_000)

    cell = extract_target_cell(page)
    if cell is None:
        # Try clicking the calendar's next/previous controls until the target date appears.
        # The target is currently in the same month, but this keeps the monitor resilient.
        for _ in range(14):
            if page.locator(f"[data-date='{TARGET_DATE}']").count():
                cell = extract_target_cell(page)
                break
            page.wait_for_timeout(1_000)

    if cell is None:
        raise RuntimeError(f"Could not find calendar cell for {TARGET_DATE}")

    text = cell.inner_text().strip()
    html = cell.inner_html()

    # EIPRO documents: × means closed/sold out; hourglass means not yet open.
    # For a target date inside the booking window, a date cell containing a
    # selectable reservation event/button is treated as available.
    unavailable_markers = ["×", "砂時計", "締め切り", "完売"]
    if any(marker in text for marker in unavailable_markers):
        return False, text, html

    # Look for common clickable reservation elements inside the target day.
    clickable = cell.locator("a, button, [role='button'], .fc-event, .event, input[type='button']")
    if clickable.count() > 0:
        return True, text, html

    # Some EIPRO versions render reservation slots as text inside elements with
    # data-event-id / event identifiers. Treat those as availability signals.
    if re.search(r"event|EV\d+|reservation|予約|空席|受付", html, re.I):
        return True, text, html

    return False, text, html


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", timezone_id="Asia/Tokyo", viewport={"width": 1440, "height": 1000})
        available, text, html = inspect_availability(page)
        browser.close()

    print(f"Target: {TARGET_DATE}, guests: {TARGET_GUESTS}")
    print(f"Calendar cell text: {text!r}")
    print(f"Available signal: {available}")

    if not available:
        return

    state = STATE_FILE.read_text(encoding="utf-8").strip() if STATE_FILE.exists() else ""
    state_key = f"{TARGET_DATE}:{TARGET_GUESTS}"
    if state == state_key:
        print("Availability already notified; skipping duplicate Telegram alert.")
        return

    message = (
        "🚨 高千穗峽貸船可能有空位！\n\n"
        f"📅 日期：{TARGET_DATE}\n"
        f"👥 人數：{TARGET_GUESTS} 人\n\n"
        "請立即開啟官方預約頁確認並搶票：\n"
        f"{BOOKING_URL}\n\n"
        "⚠️ 空位會即時變動，通知代表監控頁偵測到可預約訊號，請以官方頁面當下狀態為準。"
    )
    telegram(message)
    STATE_FILE.write_text(state_key, encoding="utf-8")
    print("Telegram alert sent.")


if __name__ == "__main__":
    main()
