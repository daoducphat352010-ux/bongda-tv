import json
import re
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

SOURCE_URLS = [
    "https://gavang33.gg/",
]


def parse_match_info(raw_text: str, base_href: str):
    slug = base_href.rstrip("/").split("/")[-1]

    kickoff = "LIVE"
    m_slug_time = re.search(r"-luc-(\d{2})(\d{2})-ngay-(\d{2}-\d{2}-\d{4})", slug, re.I)
    if m_slug_time:
        hh, mm, _ = m_slug_time.groups()
        kickoff = f"{hh}:{mm}"
    else:
        m_text_time = re.search(r"\b(\d{1,2}:\d{2})\b", raw_text)
        if m_text_time:
            kickoff = m_text_time.group(1)

    clean_slug = re.sub(r"-luc-\d{4}-ngay-.*$", "", slug, flags=re.I)
    clean_slug = re.sub(r"-[a-zA-Z0-9]{6,}$", "", clean_slug)
    teams_from_slug = re.split(r"-vs-", clean_slug, maxsplit=1, flags=re.I)

    if len(teams_from_slug) == 2 and teams_from_slug[0] and teams_from_slug[1]:
        home = teams_from_slug[0].replace("-", " ").title().strip()
        away = teams_from_slug[1].replace("-", " ").title().strip()
    else:
        lines = [
            ln.strip()
            for ln in raw_text.splitlines()
            if ln.strip()
            and not re.match(r"^\d{1,2}:\d{2}$", ln.strip())
            and ln.strip().upper() not in ("LIVE", "HD", "FULL HD", "VS", "XEM NGAY", "TRỰC TIẾP")
        ]
        home = lines[0][:28] if len(lines) >= 1 else clean_slug.replace("-", " ").title()
        away = lines[1][:28] if len(lines) >= 2 else "Trực Tiếp"

    now_vn = datetime.now(timezone(timedelta(hours=7)))
    status = "LIVE"
    if re.match(r"^\d{2}:\d{2}$", kickoff):
        kh, km = map(int, kickoff.split(":"))
        kickoff_dt = now_vn.replace(hour=kh, minute=km, second=0, microsecond=0)
        diff_minutes = (now_vn - kickoff_dt).total_seconds() / 60.0
        status = "LIVE" if -15 <= diff_minutes <= 130 else "SẮP ĐÁ"

    return home, away, kickoff, status


def extract_team_logos_from_page(page):
    # Tìm 2 ảnh logo vuông (28px - 150px) ở phần đầu trang trận đấu, loại bỏ banner quảng cáo & icon nhỏ
    logos = page.evaluate(
        """() => {
            const found = [];
            const seen = new Set();
            const imgs = Array.from(document.querySelectorAll('img'));
            for (const img of imgs) {
                const src = (img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '').trim();
                if (!src.startsWith('http') || seen.has(src)) continue;
                const lower = src.toLowerCase();
                if (/banner|ads|logo-web|favicon|\\.gif|\\.svg|button|loading|avatar/i.test(lower)) continue;

                const rect = img.getBoundingClientRect();
                if (rect.top >= 0 && rect.top <= 750 && rect.width >= 28 && rect.width <= 150 && rect.height >= 28 && rect.height <= 150) {
                    const ratio = rect.width / rect.height;
                    if (ratio >= 0.65 && ratio <= 1.55) {
                        seen.add(src);
                        found.push(src);
                    }
                }
                if (found.length >= 2) break;
            }
            return found;
        }"""
    )
    h_logo = logos[0] if len(logos) >= 1 else ""
    a_logo = logos[1] if len(logos) >= 2 else ""
    return h_logo, a_logo


def scrape_all():
    grouped_matches = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()

        for base_url in SOURCE_URLS:
            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(4500)
            except Exception:
                continue

            candidates = page.evaluate(
                """() => {
                    const out = [];
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = a.href || '';
                        if (!href.startsWith('http')) continue;
                        if (/truc-tiep|xem-|live|match|tran-dau/i.test(href.toLowerCase())) {
                            const card = a.closest('article, li, [class*="match"], [class*="item"], [class*="card"], [class*="game"], tr') || a;
                            out.push({
                                href: href,
                                text: (card.innerText || a.innerText || '').trim()
                            });
                        }
                    }
                    return out;
                }"""
            )

            for item in candidates:
                raw_href = item.get("href", "").strip()
                if not raw_href or raw_href.rstrip("/") == base_url.rstrip("/"):
                    continue

                base_href = raw_href.split("?")[0].rstrip("/")
                if base_href not in grouped_matches:
                    home_name, away_name, kickoff, status = parse_match_info(item.get("text", ""), base_href)
                    grouped_matches[base_href] = {
                        "id": f"m_{len(grouped_matches) + 1}",
                        "league": "Trực Tiếp Bóng Đá",
                        "status": status,
                        "kickoff": kickoff,
                        "home_name": home_name,
                        "home_logo": "",
                        "away_name": away_name,
                        "away_logo": "",
                        "score": "vs",
                        "page_url": base_href,
                        "room_urls": [],
                        "streams": [],
                    }

                if raw_href not in grouped_matches[base_href]["room_urls"]:
                    grouped_matches[base_href]["room_urls"].append(raw_href)

                if len(grouped_matches) >= 16:
                    break

        matches = list(grouped_matches.values())

        # Vào từng trang trận đấu để lấy đủ 2 logo đội bóng + link phát trực tiếp (.m3u8 / .mpd)
        for m in matches:
            captured_streams = []

            def on_request(req):
                u = req.url
                if (".m3u8" in u.lower() or ".mpd" in u.lower()) and u not in captured_streams:
                    captured_streams.append(u)

            page.on("request", on_request)
            try:
                page.goto(m["page_url"], wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(3200)

                h_logo, a_logo = extract_team_logos_from_page(page)
                if h_logo:
                    m["home_logo"] = h_logo
                if a_logo:
                    m["away_logo"] = a_logo

                html_dump = page.content()
                extra = re.findall(r'https?://[^\s"\'<>\\)]+\.(?:m3u8|mpd)(?:\?[^\s"\'<>\\)]*)?', html_dump, re.I)
                for u in extra:
                    clean_u = u.replace("\\/", "/")
                    if clean_u not in captured_streams:
                        captured_streams.append(clean_u)
            except Exception:
                pass
            finally:
                page.remove_listener("request", on_request)

            streams = []
            for idx, s_url in enumerate(captured_streams[:3], 1):
                streams.append(
                    {
                        "label": f"Server {idx} - Phát trực tiếp (HD)",
                        "url": s_url,
                        "referer": m["page_url"],
                    }
                )

            for r_idx, r_url in enumerate(m["room_urls"][:3], 1):
                streams.append(
                    {
                        "label": f"Phòng BLV {r_idx} (Mở Web Player)",
                        "url": r_url,
                        "referer": SOURCE_URLS[0],
                    }
                )

            m["streams"] = streams
            del m["page_url"]
            del m["room_urls"]

        browser.close()

    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M %d/%m/%Y")
    feed = {
        "app_title": f"Cập nhật lúc {vn_time}",
        "updated_at": vn_time,
        "matches": matches,
    }

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    scrape_all()
