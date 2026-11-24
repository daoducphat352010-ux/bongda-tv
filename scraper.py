import json
import re
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

SOURCE_URLS = [
    "https://gavang33.gg/",
]


def parse_match_info(raw_text: str, base_href: str):
    slug = base_href.rstrip("/").split("/")[-1]

    # 1. Tách đúng giờ đá từ đường dẫn (vd: ...-luc-1915-ngay-24-11-2026-...)
    kickoff = "LIVE"
    m_slug_time = re.search(r"-luc-(\d{2})(\d{2})-ngay-(\d{2}-\d{2}-\d{4})", slug, re.I)
    if m_slug_time:
        hh, mm, _ = m_slug_time.groups()
        kickoff = f"{hh}:{mm}"
    else:
        m_text_time = re.search(r"\b(\d{1,2}:\d{2})\b", raw_text)
        if m_text_time:
            kickoff = m_text_time.group(1)

    # 2. Cắt bỏ phần "-luc-1915-ngay-..." để tên 2 đội sạch sẽ
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

    # 3. Phân loại LIVE hay SẮP ĐÁ theo giờ Việt Nam
    now_vn = datetime.now(timezone(timedelta(hours=7)))
    status = "LIVE"
    if re.match(r"^\d{2}:\d{2}$", kickoff):
        kh, km = map(int, kickoff.split(":"))
        kickoff_dt = now_vn.replace(hour=kh, minute=km, second=0, microsecond=0)
        diff_minutes = (now_vn - kickoff_dt).total_seconds() / 60.0
        status = "LIVE" if -15 <= diff_minutes <= 130 else "SẮP ĐÁ"

    return home, away, kickoff, status


def clean_commentator_label(raw_label: str, fallback_idx: int) -> str:
    txt = re.sub(r"\s+", " ", (raw_label or "")).strip()
    # Loại bỏ các chữ rác nếu dính tên đội hoặc giờ
    if not txt or len(txt) > 36 or " vs " in txt.lower() or re.match(r"^\d{1,2}:\d{2}$", txt):
        return f"Phòng BLV {fallback_idx}"
    if not txt.lower().startswith("blv"):
        return f"BLV {txt}"
    return txt


def extract_details_from_match_page(page):
    # Lấy 2 logo đội bóng + tên các BLV của các phòng bên trong trang trận đấu
    return page.evaluate(
        """() => {
            const logos = [];
            const seenLogo = new Set();
            const imgs = Array.from(document.querySelectorAll('img'));
            for (const img of imgs) {
                const src = (img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '').trim();
                if (!src.startsWith('http') || seenLogo.has(src)) continue;
                const lower = src.toLowerCase();
                if (/banner|ads|logo-web|favicon|\\.gif|\\.svg|button|loading|avatar/i.test(lower)) continue;

                const rect = img.getBoundingClientRect();
                if (rect.top >= 0 && rect.top <= 750 && rect.width >= 28 && rect.width <= 150 && rect.height >= 28 && rect.height <= 150) {
                    const ratio = rect.width / rect.height;
                    if (ratio >= 0.65 && ratio <= 1.55) {
                        seenLogo.add(src);
                        logos.push(src);
                    }
                }
                if (logos.length >= 2) break;
            }

            // Quét danh sách nút/link chọn phòng BLV (?anchorId=...) trong trang trận đấu
            const rooms = [];
            const seenRoom = new Set();
            const roomAnchors = Array.from(document.querySelectorAll('a[href*="anchorId="], [data-anchor-id]'));
            for (const el of roomAnchors) {
                const href = (el.href || '').trim();
                const name = (el.innerText || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
                if (href && !seenRoom.has(href)) {
                    seenRoom.add(href);
                    rooms.push({ url: href, name: name });
                }
            }

            return { logos, rooms };
        }"""
    )


def scrape_all():
    grouped_matches = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        for base_url in SOURCE_URLS:
            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(3500)
                # Cuộn trang nhiều lần để web tải hết toàn bộ danh sách ~68 trận sắp tới
                for _ in range(7):
                    page.evaluate("window.scrollBy(0, 1400)")
                    page.wait_for_timeout(650)
            except Exception:
                continue

            candidates = page.evaluate(
                """() => {
                    const out = [];
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = (a.href || '').trim();
                        if (!href.startsWith('http')) continue;
                        if (!/truc-tiep|xem-|live|match|tran-dau/i.test(href.toLowerCase())) continue;

                        const card = a.closest('article, li, [class*="match"], [class*="item"], [class*="card"], [class*="game"], tr') || a;
                        const text = (card.innerText || a.innerText || '').trim();

                        // 1. Tìm tên giải đấu ở tiêu đề nhóm phía trên hoặc nhãn giải trong thẻ
                        let league = '';
                        const insideLeague = card.querySelector('[class*="league"], [class*="tournament"], [class*="comp"], [class*="stage"]');
                        if (insideLeague && insideLeague.innerText) {
                            league = insideLeague.innerText.trim();
                        }
                        if (!league) {
                            let groupBox = card.parentElement;
                            for (let step = 0; step < 4 && groupBox; step++) {
                                const header = groupBox.querySelector('h1, h2, h3, h4, [class*="league"], [class*="tournament"], [class*="group-title"], [class*="section-title"]');
                                if (header && header.innerText) {
                                    const cand = header.innerText.trim().split('\\n')[0].trim();
                                    if (cand.length >= 3 && cand.length <= 55 && !/\\bvs\\b/i.test(cand)) {
                                        league = cand;
                                        break;
                                    }
                                }
                                groupBox = groupBox.parentElement;
                            }
                        }

                        // 2. Lấy tên BLV nếu nút link là phòng bình luận viên (?anchorId=...)
                        let roomName = '';
                        if (href.includes('anchorId=')) {
                            const imgAlt = a.querySelector('img')?.getAttribute('alt') || '';
                            const rawBtn = (a.innerText || a.getAttribute('title') || imgAlt || '').trim();
                            roomName = rawBtn;
                        }

                        // 3. Lấy sẵn logo đội bóng nếu có trên trang chủ
                        const imgs = Array.from(card.querySelectorAll('img'))
                            .map(img => (img.currentSrc || img.src || img.getAttribute('data-src') || '').trim())
                            .filter(src => src.startsWith('http') && !/banner|ads|logo-web|favicon|\\.gif|\\.svg|avatar/i.test(src.toLowerCase()));
                        const uniqueImgs = Array.from(new Set(imgs));

                        out.push({
                            href: href,
                            text: text,
                            league: league,
                            roomName: roomName,
                            logos: uniqueImgs.slice(0, 2)
                        });
                    }
                    return out;
                }"""
            )

            for item in candidates:
                raw_href = item.get("href", "").strip()
                if not raw_href or raw_href.rstrip("/") == base_url.rstrip("/"):
                    continue

                base_href = raw_href.split("?")[0].rstrip("/")
                league_name = re.sub(r"\s+", " ", item.get("league", "")).strip()
                if not league_name:
                    league_name = "Bóng Đá Trực Tiếp"

                logos = item.get("logos") or []
                h_logo = logos[0] if len(logos) >= 1 else ""
                a_logo = logos[1] if len(logos) >= 2 else ""

                if base_href not in grouped_matches:
                    home_name, away_name, kickoff, status = parse_match_info(item.get("text", ""), base_href)
                    grouped_matches[base_href] = {
                        "id": f"m_{len(grouped_matches) + 1}",
                        "league": league_name[:42],
                        "status": status,
                        "kickoff": kickoff,
                        "home_name": home_name,
                        "home_logo": h_logo,
                        "away_name": away_name,
                        "away_logo": a_logo,
                        "score": "vs",
                        "page_url": base_href,
                        "rooms": [],
                        "streams": [],
                    }
                else:
                    existing = grouped_matches[base_href]
                    if existing["league"] == "Bóng Đá Trực Tiếp" and league_name != "Bóng Đá Trực Tiếp":
                        existing["league"] = league_name[:42]
                    if not existing["home_logo"] and h_logo:
                        existing["home_logo"] = h_logo
                    if not existing["away_logo"] and a_logo:
                        existing["away_logo"] = a_logo

                # Lưu danh sách phòng BLV kèm tên BLV
                rooms_list = grouped_matches[base_href]["rooms"]
                if not any(r["url"] == raw_href for r in rooms_list):
                    rooms_list.append({
                        "url": raw_href,
                        "name": item.get("roomName", "").strip(),
                    })

                # Cho phép lấy tối đa 80 trận (đủ toàn bộ 68 trận trên web)
                if len(grouped_matches) >= 80:
                    break

        matches = list(grouped_matches.values())

        # Vào chi tiết 25 trận đầu tiên (đang đá + sắp đá gần nhất) để lấy đủ logo, tên BLV và link .m3u8
        for m in matches[:25]:
            captured_streams = []

            def on_request(req):
                u = req.url
                if (".m3u8" in u.lower() or ".mpd" in u.lower()) and u not in captured_streams:
                    captured_streams.append(u)

            page.on("request", on_request)
            try:
                page.goto(m["page_url"], wait_until="domcontentloaded", timeout=14000)
                page.wait_for_timeout(2600)

                details = extract_details_from_match_page(page)
                page_logos = details.get("logos") or []
                if len(page_logos) >= 1 and not m["home_logo"]:
                    m["home_logo"] = page_logos[0]
                if len(page_logos) >= 2 and not m["away_logo"]:
                    m["away_logo"] = page_logos[1]

                # Cập nhật tên BLV thật từ trang chi tiết trận đấu
                page_rooms = details.get("rooms") or []
                if page_rooms:
                    merged_rooms = []
                    for pr in page_rooms:
                        pr_url = pr.get("url", "").strip()
                        pr_name = pr.get("name", "").strip()
                        if pr_url and not any(x["url"] == pr_url for x in merged_rooms):
                            merged_rooms.append({"url": pr_url, "name": pr_name})
                    if merged_rooms:
                        m["rooms"] = merged_rooms

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

            for r_idx, room in enumerate(m["rooms"][:4], 1):
                blv_label = clean_commentator_label(room.get("name", ""), r_idx)
                streams.append(
                    {
                        "label": f"{blv_label} (Web Player)",
                        "url": room["url"],
                        "referer": SOURCE_URLS[0],
                    }
                )

            if not streams:
                streams.append(
                    {
                        "label": "Mở trực tiếp trận đấu",
                        "url": m["page_url"],
                        "referer": SOURCE_URLS[0],
                    }
                )

            m["streams"] = streams
            del m["page_url"]
            del m["rooms"]

        # Các trận đá muộn hơn (từ trận 26 trở đi) vẫn hiển thị đầy đủ trên lưới
        for m in matches[25:]:
            streams = []
            for r_idx, room in enumerate(m["rooms"][:4], 1):
                blv_label = clean_commentator_label(room.get("name", ""), r_idx)
                streams.append(
                    {
                        "label": f"{blv_label} (Web Player)",
                        "url": room["url"],
                        "referer": SOURCE_URLS[0],
                    }
                )
            if not streams:
                streams.append(
                    {
                        "label": "Mở trực tiếp trận đấu",
                        "url": m["page_url"],
                        "referer": SOURCE_URLS[0],
                    }
                )
            m["streams"] = streams
            del m["page_url"]
            del m["rooms"]

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
