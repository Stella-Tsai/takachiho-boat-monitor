import os
import re
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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


def target_parts():
    year, month, day = map(int, TARGET_DATE.split("-"))
    return year, month, day


def find_calendar_frame(page):
    """Return the frame that contains the EIPRO calendar."""
    for frame in page.frames:
        try:
            if frame.locator("button").filter(has_text=re.compile(r"週|日|月")).count():
                return frame
            if frame.locator(".fc-view, .fc-calendar, [class*=calendar]").count():
                return frame
        except Exception:
            continue
    return page.main_frame


def click_week_view(frame):
    """EIPRO recommends week/day view because month-view symbols can be wrong."""
    candidates = [
        frame.get_by_role("button", name=re.compile(r"^週$")),
        frame.locator("button").filter(has_text=re.compile(r"^週$")),
        frame.locator("text=週").first,
    ]
    for locator in candidates:
        try:
            if locator.count():
                locator.click(timeout=3_000)
                frame.wait_for_timeout(1_500)
                return True
        except Exception:
            pass
    return False


def go_to_target_week(frame):
    """Move the calendar week-by-week until TARGET_DATE is visible."""
    year, month, day = target_parts()

    # First try a native FullCalendar date cell. Different EIPRO deployments
    # use slightly different markup, so don't rely on one selector only.
    date_selectors = [
        f"[data-date='{TARGET_DATE}']",
        f"[data-date='{year}-{month:02d}-{day:02d}']",
        f"[data-date='{year}-{month}-{day}']",
    ]

    def has_target():
        for selector in date_selectors:
            if frame.locator(selector).count():
                return True
        # Week/day headers may be rendered as Japanese text rather than data-date.
        patterns = [
            rf"{month}\s*[/月]\s*{day}\s*(?:日)?",
            rf"{month}月{day}日",
            rf"{day}\s*(?:日|\([月火水木金土日]\))",
        ]
        for pattern in patterns:
            try:
                if frame.get_by_text(re.compile(pattern)).count():
                    return True
            except Exception:
                pass
        return False

    if has_target():
        return True

    # EIPRO documents that < and > move to the previous/next week.
    next_selectors = [
        "button[aria-label*='next' i]",
        "button[title*='next' i]",
        ".fc-next-button",
        "button:has-text('>')",
        "button:has-text('＞')",
    ]

    for _ in range(10):
        clicked = False
        for selector in next_selectors:
            try:
                button = frame.locator(selector).last
                if button.count() and button.is_visible():
                    button.click(timeout=2_000)
                    frame.wait_for_timeout(1_000)
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            # Last-resort fallback: inspect buttons containing a right-chevron glyph.
            try:
                for button in frame.locator("button").all():
                    label = (button.get_attribute("aria-label") or "") + " " + (button.inner_text() or "")
                    if any(x in label for x in (">", "＞", "next", "次")):
                        button.click(timeout=2_000)
                        frame.wait_for_timeout(1_000)
                        clicked = True
                        break
            except Exception:
                pass
        if not clicked or has_target():
            break

    return has_target()


def find_target_container(frame):
    year, month, day = target_parts()

    # Prefer an exact date cell if the calendar exposes one.
    for selector in (
        f"[data-date='{TARGET_DATE}']",
        f"[data-date='{year}-{month:02d}-{day:02d}']",
        f"[data-date='{year}-{month}-{day}']",
    ):
        locator = frame.locator(selector).first
        if locator.count():
            return locator

    # Otherwise locate the visible date heading and walk up to a calendar day cell.
    patterns = [
        rf"^{month}\s*[/月]\s*{day}\s*(?:日)?$",
        rf"^{month}月{day}日$",
        rf"^{day}\s*(?:日|\([月火水木金土日]\))$",
    ]
    for pattern in patterns:
        try:
            loc = frame.get_by_text(re.compile(pattern)).first
            if loc.count() and loc.is_visible():
                parent = loc.locator("xpath=ancestor::*[contains(@class,'fc-day') or contains(@class,'day')][1]")
                if parent.count():
                    return parent
                return loc.locator("xpath=..").first
        except Exception:
            pass

    # FullCalendar week view often has a day column with a data-date attribute
    # on a child element. Search all calendar day-like nodes and inspect text.
    for selector in (".fc-day", ".fc-daygrid-day", ".fc-col-header-cell", "[class*=day]"):
        try:
            for node in frame.locator(selector).all():
                text = node.inner_text().strip()
                if re.search(rf"(^|\D){day}(?:日|\D|$)", text):
                    return node
        except Exception:
            continue

    return None


def inspect_availability(page):
    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(8_000)

    frame = find_calendar_frame(page)
    click_week_view(frame)

    if not go_to_target_week(frame):
        raise RuntimeError(
            f"Could not navigate calendar to {TARGET_DATE}. "
            "The EIPRO calendar markup may have changed."
        )

    cell = find_target_container(frame)
    if cell is None:
        # Save enough diagnostics in the Actions log to make future selector
        # changes straightforward instead of failing silently.
        visible_text = frame.locator("body").inner_text()[:8_000]
        print("Calendar diagnostic text:")
        print(visible_text)
        raise RuntimeError(f"Could not find calendar cell for {TARGET_DATE}")

    text = cell.inner_text().strip()
    html = cell.inner_html()
    print(f"Target calendar text: {text!r}")

    # EIPRO says × means closed/sold out and the hourglass means reservations
    # have not opened yet. These are definitive unavailable signals.
    unavailable_markers = ["×", "砂時計", "⌛", "締め切り", "完売", "受付終了"]
    if any(marker in text for marker in unavailable_markers):
        return False, text, html

    # In week/day view, available reservations are rendered as clickable events.
    clickable = cell.locator(
        "a, button, [role='button'], .fc-event, .event, "
        "input[type='button'], [data-event-id], [class*=event]"
    )
    if clickable.count() > 0:
        return True, text, html

    # Japanese reservation labels are also a useful fallback signal.
    if re.search(r"予約|空席|受付|乗船|\b\d{1,2}:\d{2}\b", html, re.I):
        return True, text, html

    return False, text, html


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1440, "height": 1000},
        )
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
        "⚠️ 空位會即時變動，通知代表監控頁偵測到可預約訊號，請以官方頁面當下狀態為準。"
    )
    telegram(message)
    STATE_FILE.write_text(state_key, encoding="utf-8")
    print("Telegram alert sent.")


if __name__ == "__main__":
    main()
