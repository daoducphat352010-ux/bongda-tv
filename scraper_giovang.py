import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

SOURCE_URL = "https://giovang.co"
OUTPUT_FILE = "feed_giovang.json"
VN_TZ = timezone(timedelta(hours=7))

JUNK_LABELS = {
    "xem ngay",
    "soi kèo",
    "soi keo",
    "trực tiếp",
    "truc tiep",
    "live",
    "xem thêm",
    "chi tiết",
    "chi tiet",
}


def clean_text(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()


def is_junk_team_text(text: str) -> bool:
    t = clean_text(text).lower()
    if not t or len(t) < 2 or len(t) > 55:
        return True
    if t in JUNK_LABELS:
        return True
    if re.fullmatch(r"[\d\s:/.-]+", t):
        return True
    return False


def extract_league_and_time(block_text: str) -> tuple[str, str, str]:
    text = clean_text(block_text)
    status = "LIVE" if re.search(r"\b(live|đang đá|trực tiếp|ht|ft)\b", text, re.I) else "UPCOMING"

    time_match = re.search(r"\b(\d{1,2}:\d{2}(?:\s*[-–]\s*\d{1,2}/\d{1,2})?)\b", text)
    kickoff = clean_text(time_match.group(1)) if time_match else ""

    league = "GioVang.co"
    league_patterns = [
        r"(UEFA Champions League|Champions League|C1)",
        r"(UEFA Europa League|Europa League|C2)",
        r"(Conference League|C3)",
        r"(Premier League|Ngoại Hạng Anh|Ngoai Hang Anh)",
        r"(La Liga)",
        r"(Serie A)",
        r"(Bundesliga)",
        r"(Ligue 1)",
        r"(V-League|V\.League|VLeague)",
        r"(AFC Champions League|Cúp C1 Châu Á)",
        r"(World Cup|Euro|Copa America|AFF Cup|SEA Games)",
    ]
    for pat in league_patterns:
        m = re.search(pat, text, re.I)
        if m:
            league = clean_text(m.group(1))
            break

    return league, kickoff, status


def split_home_away(candidates: list[str], raw_blob: str) -> tuple[str, str]:
    filtered = []
    for c in candidates:
        ct = clean_text(c)
        if not is_junk_team_text(ct) and ct not in filtered:
            filtered.append(ct)

    if len(filtered) >= 2:
        return filtered[0], filtered[1]

    # Thử tách từ chuỗi gộp nếu có dấu phân cách giữa 2 đội
    blob_clean = re.sub(r"\b\d{1,2}:\d{2}\b", " ", raw_blob)
    for sep in [" vs ", " VS ", " - ", " – ", " v "]:
        if sep in blob_clean:
            parts = blob_clean.split(sep, 1)
            h = clean_text(parts[0].split("|")[-1].split("\n")[-1])
            a = clean_text(parts[1].split("|")[0].split("\n")[0])
            if not is_junk_team_text(h) and not is_junk_team_text(a) and h.lower() != a.lower():
                return h, a

    if len(filtered) == 1:
        return filtered[0], ""
    return "Trận đấu", ""


def collect_card_locators(page):
    selectors = [
        "[class*='match']",
        "[class*='fixture']",
        "[class*='live-item']",
        "[class*='stream-item']",
        "[data-match]",
        "article",
        ".card",
        ".item",
    ]
    found = []
    seen_handles = set()
    for sel in selectors:
        try:
            locs = page.locator(sel).all()
            for loc in locs:
                try:
                    box = loc.bounding_box()
                    if not box or box["width"] < 140 or box["height"] < 55:
                        continue
                    txt = clean_text(loc.inner_text())
                    if len(txt) < 8 or len(txt) > 600:
                        continue
                    a_count = loc.locator("a[href]").count()
                    if a_count == 0:
                        continue
                    key = f"{round(box['x'])}_{round(box['y'])}_{round(box['width'])}_{round(box['height'])}"
                    if key in seen_handles:
                        continue
                    seen_handles.add(key)
                    found.append(loc)
                except Exception:
                    continue
        except Exception:
            continue
    return found


def scrape_giovang() -> dict:
    matches = []
    seen_keys = set()

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

            for _ in range(5):
                page.evaluate("window.scrollBy(0, 800)")
                page.wait_for_timeout(700)

            cards = collect_card_locators(page)

            for card in cards:
                try:
                    card_text = clean_text(card.inner_text())
                    links = card.locator("a[href]").all()
                    streams = []
                    seen_stream_urls = set()

                    for idx, a_el in enumerate(links):
                        href = (a_el.get_attribute("href") or "").strip()
                        if not href or href.startswith("#") or href.startswith("javascript:"):
                            continue
                        full_url = urljoin(SOURCE_URL, href)
                        if full_url in seen_stream_urls:
                            continue
                        seen_stream_urls.add(full_url)

                        a_txt = clean_text(a_el.inner_text())
                        blv_label = a_txt if (a_txt and len(a_txt) <= 28 and a_txt.lower() not in {"xem ngay", "xem ngay >"}) else f"BLV {idx + 1}"
                        streams.append({
                            "label": blv_label,
                            "url": full_url,
                            "referer": SOURCE_URL + "/"
                        })

                    if not streams:
                        continue

                    # Thu thập ứng viên tên đội từ các phần tử con trong thẻ
                    team_candidates = []
                    for sub_sel in ["[class*='team']", "[class*='club']", "[class*='name']", "h2", "h3", "h4", "strong", "span", "p"]:
                        try:
                            for sub in card.locator(sub_sel).all():
                                sub_t = clean_text(sub.inner_text())
                                if not is_junk_team_text(sub_t):
                                    team_candidates.append(sub_t)
                        except Exception:
                            continue

                    home_name, away_name = split_home_away(team_candidates, card_text)
                    if home_name == "Trận đấu" and not away_name:
                        continue

                    league, kickoff, status = extract_league_and_time(card_text)

                    # Thu thập logo 2 đội
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

                    unique_match_key = f"{home_name.lower()}|{away_name.lower()}|{kickoff}|{streams[0]['url']}"
                    if unique_match_key in seen_keys:
                        continue
                    seen_keys.add(unique_match_key)

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
        "matches": matches[:100]
    }


def main() -> None:
    payload = scrape_giovang()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(payload['matches'])} matches to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
