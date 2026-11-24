import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

SOURCE_URL = "https://giovang.co"
OUTPUT_FILE = "feed_giovang.json"
VN_TZ = timezone(timedelta(hours=7))


def clean_text(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()


def parse_match_label(label: str) -> tuple[str, str, str, str]:
    """
    Trả về (kickoff, home_name, away_name, status) từ chuỗi chữ của thẻ trận.
    """
    text = clean_text(label)
    status = "LIVE" if re.search(r"\b(live|đang đá|trực tiếp|ht|ft)\b", text, re.I) else "UPCOMING"

    kickoff = ""
    time_match = re.search(r"\b(\d{1,2}:\d{2}(?:\s*[-–]\s*\d{1,2}/\d{1,2})?)\b", text)
    if time_match:
        kickoff = clean_text(time_match.group(1))
        text_no_time = clean_text(text.replace(time_match.group(0), " "))
    else:
        text_no_time = text

    for sep in [" vs ", " VS ", " - ", " – ", " v "]:
        if sep in text_no_time:
            parts = text_no_time.split(sep, 1)
            home = clean_text(parts[0])
            away = clean_text(parts[1])
            if home and away and len(home) <= 60 and len(away) <= 60:
                return kickoff, home, away, status

    return kickoff, text_no_time or "Trận đấu", "", status


def scrape_giovang() -> dict:
    matches = []
    seen_urls = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="vi-VN",
            )
            page = context.new_page()
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(4500)

            # Cuộn nhẹ để kích hoạt các khối trận tải lười (lazy-load) nếu có
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 700)")
                page.wait_for_timeout(800)

            anchors = page.locator("a[href]").all()
            for el in anchors:
                try:
                    href = (el.get_attribute("href") or "").strip()
                    if (
                        not href
                        or href.startswith("#")
                        or href.startswith("javascript:")
                        or href.startswith("mailto:")
                    ):
                        continue

                    full_url = urljoin(SOURCE_URL, href)
                    if "giovang.co" not in full_url:
                        continue
                    if re.search(r"/(login|register|dang-nhap|dang-ky|tin-tuc|news|privacy|terms|dmca)", full_url, re.I):
                        continue

                    raw_label = clean_text(el.inner_text())
                    if not raw_label or len(raw_label) < 4 or len(raw_label) > 180:
                        continue

                    looks_like_match = bool(
                        re.search(
                            r"(live|truc-tiep|xem|match|bong-da|vs|-|\d{1,2}:\d{2})",
                            full_url + " " + raw_label,
                            re.I,
                        )
                    )
                    if not looks_like_match:
                        continue

                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    kickoff, home_name, away_name, status = parse_match_label(raw_label)

                    # Thử lấy logo ảnh bên trong thẻ liên kết (nếu có)
                    logos = []
                    imgs = el.locator("img[src]").all()
                    for img in imgs:
                        src = (img.get_attribute("src") or "").strip()
                        if src and not src.startswith("data:image/svg"):
                            logos.append(urljoin(SOURCE_URL, src))

                    home_logo = logos[0] if len(logos) >= 1 else ""
                    away_logo = logos[1] if len(logos) >= 2 else ""

                    matches.append({
                        "id": f"gv_{len(matches) + 1}",
                        "league": "GioVang.co",
                        "status": status,
                        "kickoff": kickoff,
                        "home_name": home_name,
                        "home_logo": home_logo,
                        "away_name": away_name,
                        "away_logo": away_logo,
                        "score": "vs",
                        "streams": [
                            {
                                "label": "BLV 1",
                                "url": full_url,
                                "referer": SOURCE_URL + "/"
                            }
                        ]
                    })
                except Exception:
                    continue

            browser.close()
    except Exception as exc:
        print(f"[ERROR] Failed scraping {SOURCE_URL}: {exc}")

    return {
        "app_title": "GioVang TV",
        "source_name": "giovang.co",
        "updated_at": datetime.now(VN_TZ).strftime("%d/%m/%Y %H:%M"),
        "matches": matches[:80]
    }


def main() -> None:
    payload = scrape_giovang()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(payload['matches'])} matches to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
