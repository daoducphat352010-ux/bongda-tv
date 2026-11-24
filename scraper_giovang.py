import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

SOURCE_URL = "https://giovang.co"
OUTPUT_FILE = "feed_giovang.json"
VN_TZ = timezone(timedelta(hours=7))

GARBAGE_LABELS = {
    "",
    "xem ngay",
    "xem ngay >",
    "soi kèo",
    "soi keo",
    "nhận định",
    "nhan dinh",
    "trực tiếp",
    "truc tiep",
    "live",
    "sắp đá",
    "sap da",
    "vs",
    "ht",
    "ft",
    "chi tiết",
    "chi tiet",
}


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def is_garbage(s: str) -> bool:
    low = clean(s).lower()
    if low in GARBAGE_LABELS:
        return True
    if len(low) < 2 or len(low) > 70:
        return True
    if re.fullmatch(r"[\d\s:./+-]+", low):
        return True
    return False


def normalize_logo_url(src: str) -> str:
    s = clean(src)
    if not s or s.startswith("data:"):
        return ""
    return urljoin(SOURCE_URL + "/", s)


def scrape_giovang() -> dict:
    raw_items = []

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
            page.goto(SOURCE_URL, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)

            # Cuộn từng bước để kích hoạt lazy-load cho cả danh sách trận và logo ảnh
            for _ in range(30):
                before_y = page.evaluate("window.scrollY")
                page.evaluate("window.scrollBy(0, 550)")
                page.wait_for_timeout(450)
                after_y = page.evaluate("window.scrollY")
                if after_y == before_y and after_y > 0:
                    page.wait_for_timeout(800)
                    after_y2 = page.evaluate("window.scrollY")
                    if after_y2 == after_y:
                        break

            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            raw_items = page.evaluate(
                """() => {
                    const out = [];
                    const seenLinks = new Set();
                    const anchors = Array.from(document.querySelectorAll('a[href]'));

                    for (const a of anchors) {
                        const href = (a.getAttribute('href') || '').trim();
                        if (!href || href.startsWith('#') || href.startsWith('javascript:') || href.startsWith('mailto:')) continue;

                        let fullUrl = '';
                        try { fullUrl = new URL(href, location.origin).href; } catch (e) { continue; }
                        if (!fullUrl.includes('giovang.co')) continue;
                        if (/\\/(login|register|dang-nhap|dang-ky|tin-tuc|news|soi-keo|nhan-dinh|dmca|privacy|terms)/i.test(fullUrl)) continue;
                        if (seenLinks.has(fullUrl)) continue;

                        let card = a;
                        let cur = a.parentElement;
                        for (let i = 0; i < 6 && cur && cur.parentElement; i++) {
                            const r = cur.getBoundingClientRect();
                            const aCnt = cur.querySelectorAll('a[href]').length;
                            if (r.height >= 36 && r.height <= 420 && aCnt <= 10) {
                                card = cur;
                                break;
                            }
                            cur = cur.parentElement;
                        }

                        const rect = card.getBoundingClientRect();
                        if (rect.width < 60 || rect.height < 24) continue;

                        const cardText = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (cardText.length < 4) continue;

                        // Tìm tên BLV nếu có
                        let blvName = '';
                        const leafEls = Array.from(card.querySelectorAll('*')).filter(el => el.children.length === 0);
                        const leaves = leafEls
                            .map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim())
                            .filter(Boolean);

                        for (const t of leaves) {
                            if (/^blv\\b/i.test(t) && t.length <= 40) {
                                blvName = t;
                                break;
                            }
                        }
                        if (!blvName) {
                            const aText = (a.innerText || '').replace(/\\s+/g, ' ').trim();
                            if (aText && aText.length <= 32 && !/^(xem ngay|soi kèo|soi keo|live|trực tiếp|chi tiết)$/i.test(aText)) {
                                blvName = aText;
                            }
                        }

                        // Lấy ảnh từ src, data-src, data-original, data-lazy-src
                        const imgs = [];
                        for (const img of card.querySelectorAll('img')) {
                            const cand =
                                img.getAttribute('data-src') ||
                                img.getAttribute('data-original') ||
                                img.getAttribute('data-lazy-src') ||
                                img.getAttribute('src') ||
                                '';
                            if (cand && !cand.startsWith('data:image/svg')) {
                                imgs.push(cand);
                            }
                        }

                        seenLinks.add(fullUrl);
                        out.push({
                            url: fullUrl,
                            cardText,
                            blvName,
                            leaves,
                            imgs
                        });
                    }
                    return out;
                }"""
            )

            browser.close()
    except Exception as exc:
        print(f"[ERROR] {exc}")

    matches = []
    seen_match_keys = set()

    for item in raw_items:
        full_url = item.get("url", "")
        card_text = clean(item.get("cardText", ""))
        leaves = [clean(x) for x in (item.get("leaves") or []) if not is_garbage(x)]

        imgs = []
        for raw_img in (item.get("imgs") or []):
            u = normalize_logo_url(raw_img)
            if u and u not in imgs:
                imgs.append(u)

        time_m = re.search(r"\b(\d{1,2}:\d{2}(?:\s*[-–/]\s*\d{1,2}/\d{1,2})?)\b", card_text)
        kickoff = clean(time_m.group(1)) if time_m else ""
        is_live = bool(re.search(r"\b(live|đang đá|trực tiếp)\b", card_text, re.I))
        status = "LIVE" if is_live else "UPCOMING"

        league = ""
        for lv in leaves:
            if re.search(
                r"(cup|league|champions|europa|conference|premier|la liga|serie a|bundesliga|ligue 1|v-league|world cup|euro|aff|sea games|giải)",
                lv,
                re.I,
            ) and len(lv) <= 45:
                league = lv
                break

        blv_raw = clean(item.get("blvName", ""))
        team_pool = []
        for lv in leaves:
            if lv == league or (blv_raw and lv.lower() == blv_raw.lower()):
                continue
            if re.fullmatch(r"\d{1,2}:\d{2}", lv):
                continue
            if lv not in team_pool:
                team_pool.append(lv)

        home_name = ""
        away_name = ""
        if len(team_pool) >= 2:
            home_name, away_name = team_pool[0], team_pool[1]
        else:
            cleaned_blob = re.sub(r"\b\d{1,2}:\d{2}\b", " ", card_text)
            for sep in [" vs ", " VS ", " - ", " – ", " v "]:
                if sep in cleaned_blob:
                    p0, p1 = cleaned_blob.split(sep, 1)
                    h = clean(p0.split("|")[-1])
                    a = clean(p1.split("|")[0])
                    if not is_garbage(h) and not is_garbage(a):
                        home_name, away_name = h, a
                        break

        if not home_name and not away_name:
            continue

        # Khóa chống trùng bám theo URL + tên đội để không làm mất các trận khác
        dedup_key = f"{full_url}|{home_name.lower()}|{away_name.lower()}"
        if dedup_key in seen_match_keys:
            continue
        seen_match_keys.add(dedup_key)

        blv_label = blv_raw if (blv_raw and not is_garbage(blv_raw)) else "BLV 1"
        home_logo = imgs[0] if len(imgs) >= 1 else ""
        away_logo = imgs[1] if len(imgs) >= 2 else ""

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
            "streams": [
                {
                    "label": blv_label,
                    "url": full_url,
                    "referer": SOURCE_URL + "/"
                }
            ]
        })

    return {
        "app_title": "GioVang TV",
        "source_name": "giovang.co",
        "updated_at": datetime.now(VN_TZ).strftime("%d/%m/%Y %H:%M"),
        "matches": matches
    }


def main() -> None:
    payload = scrape_giovang()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(payload['matches'])} matches to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
