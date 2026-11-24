import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

SOURCE_URL = "https://giovang.co"
OUTPUT_FILE = "feed_giovang.json"
VN_TZ = timezone(timedelta(hours=7))

NON_STREAM_LABELS = {
    "soi kèo",
    "soi keo",
    "nhận định",
    "nhan dinh",
    "xem ngay",
    "xem ngay >",
    "chi tiết",
    "chi tiet",
}

JUNK_TEAM_TEXTS = NON_STREAM_LABELS | {
    "vs",
    "live",
    "trực tiếp",
    "truc tiep",
    "sắp đá",
    "sap da",
    "ht",
    "ft",
}


def clean_text(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()


def is_junk_team(text: str) -> bool:
    t = clean_text(text).lower()
    if not t or len(t) < 2 or len(t) > 60:
        return True
    if t in JUNK_TEAM_TEXTS:
        return True
    if re.fullmatch(r"[\d\s:./+-]+", t):
        return True
    return False


def extract_league_name(card, card_text: str) -> str:
    # Ưu tiên lấy từ các thẻ có class/text liên quan giải đấu bên trong thẻ
    for sel in ["[class*='league']", "[class*='tour']", "[class*='cup']", "[class*='cate']"]:
        try:
            for el in card.locator(sel).all():
                txt = clean_text(el.inner_text())
                if 2 <= len(txt) <= 45 and not re.search(r"\b\d{1,2}:\d{2}\b", txt):
                    return txt
        except Exception:
            continue

    patterns = [
        r"(AFC Champions League[^\n|]*|Cúp C1 Châu Á)",
        r"(UEFA Champions League|Champions League)",
        r"(UEFA Europa League|Europa League)",
        r"(Conference League)",
        r"(Premier League|Ngoại Hạng Anh|Ngoai Hang Anh)",
        r"(La Liga)",
        r"(Serie A)",
        r"(Bundesliga)",
        r"(Ligue 1)",
        r"(V-League[^\n|]*|V\.League[^\n|]*)",
        r"(World Cup[^\n|]*|Euro[^\n|]*|AFF Cup[^\n|]*|SEA Games[^\n|]*)",
    ]
    for pat in patterns:
        m = re.search(pat, card_text, re.I)
        if m:
            return clean_text(m.group(1))
    return ""


def extract_kickoff_and_status(card_text: str) -> tuple[str, str]:
    text = clean_text(card_text)
    is_live = bool(re.search(r"\b(live|đang đá|trực tiếp)\b", text, re.I))
    status = "LIVE" if is_live else "UPCOMING"
    time_match = re.search(r"\b(\d{1,2}:\d{2}(?:\s*[-–/]\s*\d{1,2}/\d{1,2})?)\b", text)
    kickoff = clean_text(time_match.group(1)) if time_match else ""
    return kickoff, status


def extract_teams(card, card_text: str) -> tuple[str, str]:
    candidates = []
    for sel in ["[class*='team']", "[class*='club']", "[class*='name']", "h2", "h3", "h4", "strong", "span", "p", "div"]:
        try:
            for el in card.locator(sel).all():
                # Chỉ lấy text trực tiếp hoặc của node lá ngắn để tránh gom cả khối dài
                txt = clean_text(el.inner_text())
                if not is_junk_team(txt) and txt not in candidates:
                    candidates.append(txt)
        except Exception:
            continue

    # Ưu tiên các ứng viên nằm ở đầu/ngắn gọn (tên 2 đội thường xuất hiện đầu tiên)
    short_teams = [c for c in candidates if len(c) <= 36]
    if len(short_teams) >= 2:
        return short_teams[0], short_teams[1]

    blob = re.sub(r"\b\d{1,2}:\d{2}\b", " ", card_text)
    for sep in [" vs ", " VS ", " - ", " – ", " v "]:
        if sep in blob:
            left, right = blob.split(sep, 1)
            h = clean_text(left.split("|")[-1])
            a = clean_text(right.split("|")[0])
            if not is_junk_team(h) and not is_junk_team(a) and h.lower() != a.lower():
                return h, a

    if len(short_teams) == 1:
        return short_teams[0], ""
    return "", ""


def scroll_entire_page(page) -> None:
    prev_height = 0
    for _ in range(15):
        curr_height = page.evaluate("document.body.scrollHeight")
        page.evaluate(f"window.scrollTo(0, {curr_height})")
        page.wait_for_timeout(900)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == curr_height and curr_height == prev_height:
            break
        prev_height = new_height
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


def collect_match_containers(page):
    selectors = [
        "[class*='match']",
        "[class*='fixture']",
        "[class*='live-item']",
        "[class*='stream-item']",
        "[class*='event']",
        "[data-match]",
        "article",
        "li",
    ]
    containers = []
    seen_rects = set()

    for sel in selectors:
        try:
            for loc in page.locator(sel).all():
                try:
                    box = loc.bounding_box()
                    if not box or box["width"] < 80 or box["height"] < 32:
                        continue
                    # Bỏ qua container bao quá lớn (cả trang/danh sách)
                    if box["height"] > 450 and loc.locator("a[href]").count() > 8:
                        continue
                    if loc.locator("a[href]").count() == 0:
                        continue
                    rect_key = f"{round(box['x'])}_{round(box['y'])}_{round(box['width'])}_{round(box['height'])}"
                    if rect_key in seen_rects:
                        continue
                    seen_rects.add(rect_key)
                    containers.append(loc)
                except Exception:
                    continue
        except Exception:
            continue

    return containers


def scrape_giovang() -> dict:
    matches = []
    seen_matches = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 960},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="vi-VN",
            )
            page = context.new_page()
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)

            scroll_entire_page(page)
            containers = collect_match_containers(page)

            for card in containers:
                try:
                    card_text = clean_text(card.inner_text())
                    if len(card_text) < 5:
                        continue

                    streams = []
                    seen_stream_urls = set()
                    blv_counter = 1

                    for a_el in card.locator("a[href]").all():
                        href = (a_el.get_attribute("href") or "").strip()
                        if not href or href.startswith("#") or href.startswith("javascript:"):
                            continue
                        full_url = urljoin(SOURCE_URL, href)
                        if full_url in seen_stream_urls:
                            continue

                        a_txt = clean_text(a_el.inner_text())
                        a_low = a_txt.lower()
                        if a_low in NON_STREAM_LABELS or "soi-keo" in full_url.lower() or "soi_keo" in full_url.lower():
                            continue

                        seen_stream_urls.add(full_url)
                        if a_txt and len(a_txt) <= 24 and a_low not in JUNK_TEAM_TEXTS:
                            label = a_txt
                        else:
                            label = f"BLV {blv_counter}"
                            blv_counter += 1

                        streams.append({
                            "label": label,
                            "url": full_url,
                            "referer": SOURCE_URL + "/"
                        })

                    if not streams:
                        continue

                    home_name, away_name = extract_teams(card, card_text)
                    if not home_name and not away_name:
                        continue

                    league = extract_league_name(card, card_text)
                    kickoff, status = extract_kickoff_and_status(card_text)

                    logos = []
                    for img in card.locator("img[src]").all():
                        src = (img.get_attribute("src") or "").strip()
                        if not src or src.startswith("data:image/svg"):
                            continue
                        full_img = urljoin(SOURCE_URL, src)
                        if full_img not in logos:
                            logos.append(full_img)

                    home_logo = logos[0] if len(logos) >= 1 else ""
                    away_logo = logos[1] if len(logos) >= 2 else ""

                    match_key = f"{home_name.lower()}|{away_name.lower()}|{kickoff}"
                    if match_key in seen_matches:
                        continue
                    seen_matches.add(match_key)

                    matches.append({
                        "id": f"gv_{len(matches) + 1}",
                        "league": league,
                        "status": status,
                        "kickoff": kickoff,
                        "home_name": home_name,
                        "home_logo": home_logo,
                        "away_name": away_name,
                        "away_logo": away_logo,
                        "score": "vs",
                        "streams": streams[:8]
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
        "matches": matches[:250]
    }


def main() -> None:
    payload = scrape_giovang()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(payload['matches'])} matches to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
