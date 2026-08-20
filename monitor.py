import os
import re
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
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=20)
    r.raise_for_status()


def find_calendar_frame(page):
    return page.main_frame


def click_week_view(frame):
    for selector in ("button:has-text('週')", "input[value='週']", "a:has-text('週')"):
        try:
            loc = frame.locator(selector).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                frame.wait_for_timeout(1000)
                return True
        except Exception:
            pass
    return False


def target_week_label():
    from datetime import date, timedelta
    target = date.fromisoformat(TARGET_DATE)
    start = target - timedelta(days=target.weekday() - 3)  # EIPRO week starts Thu
    end = start + timedelta(days=6)
    return f"{start:%Y/%m/%d}～{end:%Y/%m/%d}"


def go_to_target_week(frame):
    """EIPRO exposes direct week-range links above the calendar.

    The diagnostic showed the exact links, e.g. 2026/08/27～2026/09/02,
    so clicking that range is much more reliable than guessing arrow selectors.
    """
    label = target_week_label()
    print("Target week link:", label)

    try:
        exact = frame.get_by_text(label, exact=True)
        if exact.count():
            exact.first.click(timeout=5000)
            frame.wait_for_timeout(1500)
            return True
    except Exception as exc:
        print("Exact week-link click failed:", repr(exc))

    # Fallback: locate an anchor whose text contains the exact range.
    try:
        for a in frame.locator("a").all():
            if not a.is_visible():
                continue
            text = a.inner_text().strip()
            if label in text:
                a.click(timeout=5000)
                frame.wait_for_timeout(1500)
                return True
    except Exception:
        pass

    # Final fallback: click the next week arrow by geometry/order. In the
    # current page the direct range links above make this normally unnecessary.
    return False


def find_target_container(frame):
    day = int(TARGET_DATE[-2:])
    for selector in (
        f"[data-date='{TARGET_DATE}']",
        f"[data-date='{TARGET_DATE.replace('-','/')}']",
    ):
        try:
            loc = frame.locator(selector).first
            if loc.count():
                return loc
        except Exception:
            pass

    # In the observed EIPRO week view, each column header is rendered as
    # '8/31 月曜日'. Find that header and use its nearest calendar column.
    patterns = [
        rf"^{int(TARGET_DATE[5:7])}/0?{day}\s*月曜日$",
        rf"^{int(TARGET_DATE[5:7])}/0?{day}\s*.*曜日$",
        rf"^0?{day}$",
    ]
    for pattern in patterns:
        try:
            loc = frame.get_by_text(re.compile(pattern)).first
            if loc.count() and loc.is_visible():
                parent = loc.locator("xpath=ancestor::*[contains(@class,'fc-col') or contains(@class,'day')][1]")
                if parent.count():
                    return parent
                return loc.locator("xpath=..").first
        except Exception:
            pass

    # Generic day-column fallback.
    for selector in (".fc-col-header-cell", ".fc-day", ".fc-daygrid-day", "[class*=day]"):
        try:
            for node in frame.locator(selector).all():
                text = node.inner_text().strip()
                if re.search(rf"(^|\D)0?{day}(?:\s|日|$)", text):
                    return node
        except Exception:
            continue
    return None


def inspect_availability(page):
    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(7000)
    frame = find_calendar_frame(page)
    click_week_view(frame)

    if not go_to_target_week(frame):
        print("=== CALENDAR NAVIGATION DIAGNOSTIC ===")
        print("Target week:", target_week_label())
        print(frame.locator("body").inner_text()[:12000])
        raise RuntimeError(f"Could not navigate calendar to {TARGET_DATE}")

    cell = find_target_container(frame)
    if cell is None:
        print("Calendar diagnostic text:")
        print(frame.locator("body").inner_text()[:12000])
        raise RuntimeError(f"Could not find calendar cell for {TARGET_DATE}")

    text = cell.inner_text().strip()
    html = cell.inner_html()
    print(f"Target calendar text: {text!r}")

    unavailable_markers = ["×", "砂時計", "⌛", "締め切り", "完売", "受付終了"]
    if any(marker in text for marker in unavailable_markers):
        return False, text, html

    clickable = cell.locator("a, button, [role='button'], .fc-event, .event, input[type='button'], [class*=event]")
    if clickable.count() > 0:
        return True, text, html

    if re.search(r"予約|空席|受付|乗船|\b\d{1,2}:\d{2}\b", html, re.I):
        return True, text, html
    return False, text, html


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", timezone_id="Asia/Tokyo", viewport={"width": 1440, "height": 1000})
        try:
            available, text, html = inspect_availability(page)
        finally:
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
        "⚠️ 空位會即時變動，請以官方頁面當下狀態為準。"
    )
    telegram(message)
    STATE_FILE.write_text(state_key, encoding="utf-8")
    print("Telegram alert sent.")


if __name__ == "__main__":
    main()
