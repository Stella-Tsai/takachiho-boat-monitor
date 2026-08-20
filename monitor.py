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
    r = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False},
        timeout=20,
    )
    r.raise_for_status()


def target_parts():
    return map(int, TARGET_DATE.split("-"))


def find_calendar_frame(page):
    # EIPRO currently renders the calendar in the main document, but keep this
    # iframe-safe in case that changes again.
    for frame in page.frames:
        try:
            if frame.locator(".fc-view, .fc-calendar, [class*=calendar], [class*=Calendar]").count():
                return frame
        except Exception:
            pass
    return page.main_frame


def click_week_view(frame):
    for selector in (
        "button:has-text('週')",
        "input[value='週']",
        "a:has-text('週')",
        "text=週",
    ):
        try:
            loc = frame.locator(selector).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                frame.wait_for_timeout(1200)
                return True
        except Exception:
            pass
    return False


def target_visible(frame):
    year, month, day = map(int, TARGET_DATE.split("-"))
    selectors = [
        f"[data-date='{TARGET_DATE}']",
        f"[data-date='{year}-{month:02d}-{day:02d}']",
        f"[data-date='{year}-{month}-{day}']",
    ]
    for selector in selectors:
        try:
            if frame.locator(selector).count():
                return True
        except Exception:
            pass

    # EIPRO/FullCalendar may render only the day number in week headers.
    for selector in (".fc-day", ".fc-daygrid-day", ".fc-col-header-cell", "[class*=day]"):
        try:
            for node in frame.locator(selector).all():
                text = node.inner_text().strip()
                if re.search(rf"(^|\D){day}(?:日|\s|$)", text):
                    return True
        except Exception:
            pass
    return False


def click_next_week(frame):
    # The site says the calendar's top '< >' controls move to the next week.
    # Depending on the EIPRO build those controls can be button, input, link,
    # or a FullCalendar element, so inspect all likely attributes rather than
    # relying on one CSS class.
    candidates = [
        "button[aria-label*='next' i]",
        "button[title*='next' i]",
        "button[aria-label*='次' ]",
        "button[title*='次' ]",
        ".fc-next-button",
        ".fc-button-next",
        "input[value='>']",
        "input[value='＞']",
        "input[value='次']",
        "a[title*='next' i]",
        "a[aria-label*='next' i]",
    ]
    for selector in candidates:
        try:
            loc = frame.locator(selector).last
            if loc.count() and loc.is_visible():
                loc.click(timeout=2500)
                frame.wait_for_timeout(900)
                return True
        except Exception:
            pass

    # Last resort: find ANY visible element whose text/value/aria/title is a
    # navigation chevron. This is intentionally DOM-wide because the EIPRO
    # arrows are not consistently exposed as buttons.
    try:
        nodes = frame.locator("button, input, a, [role='button'], span, i").all()
        for node in nodes:
            try:
                if not node.is_visible():
                    continue
                attrs = " ".join(
                    str(node.get_attribute(name) or "")
                    for name in ("aria-label", "title", "value", "class")
                )
                text = node.inner_text().strip()
                combined = f"{attrs} {text}".lower()
                if any(token in combined for token in (">", "＞", "next", "次へ", "次の週")):
                    node.click(timeout=2000)
                    frame.wait_for_timeout(900)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def go_to_target_week(frame):
    if target_visible(frame):
        return True

    # At most 12 week advances are needed for the configured target date.
    for _ in range(12):
        if not click_next_week(frame):
            break
        if target_visible(frame):
            return True
    return target_visible(frame)


def find_target_container(frame):
    year, month, day = map(int, TARGET_DATE.split("-"))

    for selector in (
        f"[data-date='{TARGET_DATE}']",
        f"[data-date='{year}-{month:02d}-{day:02d}']",
        f"[data-date='{year}-{month}-{day}']",
    ):
        try:
            loc = frame.locator(selector).first
            if loc.count():
                return loc
        except Exception:
            pass

    # Walk upward from the visible day number to the nearest calendar day cell.
    patterns = [
        rf"^0?{day}(?:日)?$",
        rf"^{month}\s*[/月]\s*0?{day}(?:日)?$",
        rf"^{month}月0?{day}日$",
    ]
    for pattern in patterns:
        try:
            loc = frame.get_by_text(re.compile(pattern)).first
            if loc.count() and loc.is_visible():
                parent = loc.locator(
                    "xpath=ancestor::*[contains(@class,'fc-day') or contains(@class,'day')][1]"
                )
                if parent.count():
                    return parent
                return loc.locator("xpath=..").first
        except Exception:
            pass

    for selector in (".fc-day", ".fc-daygrid-day", ".fc-col-header-cell", "[class*=day]"):
        try:
            for node in frame.locator(selector).all():
                text = node.inner_text().strip()
                if re.search(rf"(^|\D)0?{day}(?:日|\s|$)", text):
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
        # Print DOM diagnostics instead of the old generic error. This makes
        # the next failure actionable if EIPRO changes its markup again.
        print("=== CALENDAR NAVIGATION DIAGNOSTIC ===")
        print("URL:", page.url)
        print("Title:", page.title())
        try:
            print("Buttons/inputs/links:")
            for node in frame.locator("button, input, a").all():
                try:
                    if node.is_visible():
                        print(
                            "TAG=",
                            node.evaluate("el => el.tagName"),
                            "TEXT=",
                            repr(node.inner_text().strip()),
                            "VALUE=",
                            repr(node.get_attribute("value")),
                            "TITLE=",
                            repr(node.get_attribute("title")),
                            "ARIA=",
                            repr(node.get_attribute("aria-label")),
                        )
                except Exception:
                    pass
            print("BODY:")
            print(frame.locator("body").inner_text()[:12000])
        except Exception as exc:
            print("Diagnostic collection failed:", repr(exc))
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

    clickable = cell.locator(
        "a, button, [role='button'], .fc-event, .event, input[type='button'], "
        "[data-event-id], [class*=event]"
    )
    if clickable.count() > 0:
        return True, text, html

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
