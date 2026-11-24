import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright

SOURCE_URLS = [
    "https://gavang33.gg/",
]


def parse_match_info(raw_text: str, base_href: str):
    slug = base_href.rstrip("/").split("/")[-1]

    # 1. Tách đúng giờ đá từ đường dẫn (vd: ...-luc-1445-ngay-24-11-2026-...)
    kickoff = "LIVE"
    m_slug_time = re.search(r"-luc-(\d{2})(\d{2})-ngay-(\d{2}-\d{2}-\d{4})", slug, re.I)
    if m_slug_time:
        hh, mm, _ = m_slug_time.groups()
        kickoff = f"{hh}:{mm}"
    else:
        m_text_time = re.search(r"\b(\d{1,2}:\d{2})\b", raw_text)
        if m_text_time:
            kickoff = m_text_time.group(1)

    # 2. Cắt bỏ phần "-luc-1445-ngay-..." để tên 2 đội sạch sẽ
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
    if not raw_label:
        return f"Phòng BLV {fallback_idx}"

    # Tách từng dòng và loại bỏ số người xem, chữ LIVE, HD, giờ đá...
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw_label.splitlines() if ln.strip()]
    picked = ""
    for ln in lines:
        if re.match(r"^\d+([.,]\d+)?[kKmM]?$", ln):
            continue
        if re.match(r"^\d{1,2}:\d{2}$", ln):
            continue
        if ln.upper() in ("LIVE", "HD", "FULL HD", "XEM NGAY", "TRỰC TIẾP", "VS", "SẮP ĐÁ"):
            continue
        if " vs " in ln.lower():
            continue
        if 2 <= len(ln) <= 36:
            picked = ln
            break

    if not picked:
        return f"Phòng BLV {fallback_idx}"
    if not picked.lower().startswith("blv"):
        return f"BLV {picked}"
    return picked


def get_room_key(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    anchor_id = (qs.get("anchorId") or [""])[0].strip()
    if anchor_id:
        return f"{parsed.path.rstrip('/')}?anchorId={anchor_id}"
    return parsed.path.rstrip("/")


def extract_details_from_match_page(page, target_path: str):
    return page.evaluate(
        r"""(targetPath) => {
            const normPath = (u) => {
                try {
                    return new URL(u, window.location.origin).pathname.replace(/\/+$/, '');
                } catch (e) {
                    return '';
                }
            };

            // 1. Lấy 2 logo đội bóng ở phần đầu trang trận đấu
            const logos = [];
            const seenLogo = new Set();
            const imgs = Array.from(document.querySelectorAll('img'));
            for (const img of imgs) {
                const src = (img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '').trim();
                if (!src.startsWith('http') || seenLogo.has(src)) continue;
                const lower = src.toLowerCase();
                if (/banner|ads|logo-web|favicon|\.gif|\.svg|button|loading|avatar/i.test(lower)) continue;

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

            // 2. Lấy tên giải đấu từ breadcrumb hoặc tiêu đề giải phía trên khung phát
            let league = '';
            const crumbs = Array.from(document.querySelectorAll('[class*="breadcrumb"] a, nav a, [class*="league"], [class*="tournament"], [class*="competition"]'));
            for (const el of crumbs) {
                if (el.closest('header, footer, [class*="sidebar"], [class*="menu"]')) continue;
                const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
                if (!txt || txt.length < 3 || txt.length > 55) continue;
                if (/^(trang chủ|home|trực tiếp|bóng đá|lịch thi đấu|xem bóng đá)$/i.test(txt)) continue;
                if (/\bvs\b/i.test(txt)) continue;
                league = txt;
                break;
            }

            // 3. CHỈ lấy các phòng BLV có đường dẫn trùng khớp với trận đấu hiện tại (bỏ qua sidebar trận khác)
            const rooms = [];
            const seenHref = new Set();
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            for (const a of anchors) {
                const href = (a.href || '').trim();
                if (!href.startsWith('http')) continue;
                if (normPath(href) !== targetPath) continue;
                if (!href.includes('anchorId=')) continue;
                if (seenHref.has(href)) continue;
                seenHref.add(href);

                const imgAlt = a.querySelector('img')?.getAttribute('alt') || '';
                const name = (a.innerText || a.getAttribute('title') || imgAlt || '').trim();
                rooms.push({ url: href, name: name });
            }

            // Nếu trận chỉ có 1 BLV (không hiện nút chuyển phòng <a>), đọc tên BLV đang phát ở khung thông tin trận
            if (rooms.length === 0) {
                const hostNodes = Array.from(document.querySelectorAll('[class*="anchor"], [class*="blv"], [class*="commentator"], [class*="streamer"], [class*="host"]'));
                for (const node of hostNodes) {
                    const parentLink = node.closest('a[href]');
                    if (parentLink && normPath(parentLink.href) !== targetPath) continue;
                    if (node.querySelector('a[href]')) continue;
                    const txt = (node.innerText || '').trim();
                    if (txt && txt.length >= 2 && txt.length <= 40 && !/\bvs\b/i.test(txt)) {
                        rooms.push({ url: window.location.href, name: txt });
                        break;
                    }
                }
            }

            return { logos, league, rooms };
        }""",
        target_path,
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

                # Bấm nút "Xem thêm" nếu web thu gọn danh sách trận
                for _ in range(3):
                    clicked = page.evaluate(
                        r"""() => {
                            const btns = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                            for (const b of btns) {
                                const t = (b.innerText || '').trim();
                                if (/^(xem thêm|tải thêm|load more|xem tất cả)/i.test(t) && b.offsetParent !== null) {
                                    b.click();
                                    return true;
                                }
                            }
                            return false;
                        }"""
                    )
                    if not clicked:
                        break
                    page.wait_for_timeout(1200)

                # Cuộn trang 15 nhịp để bung hết toàn bộ ~68 trận sắp tới
                for _ in range(15):
                    page.evaluate("window.scrollBy(0, 1200)")
                    page.wait_for_timeout(500)
            except Exception:
                continue

            candidates = page.evaluate(
                r"""() => {
                    const out = [];
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = (a.href || '').trim();
                        if (!href.startsWith('http')) continue;
                        if (!/truc-tiep|xem-|live|match|tran-dau/i.test(href.toLowerCase())) continue;

                        const card = a.closest('article, li, [class*="match"], [class*="item"], [class*="card"], [class*="game"], tr') || a;
                        const text = (card.innerText || a.innerText || '').trim();

                        // Tìm tên giải đấu ở tiêu đề nhóm phía trên thẻ trận đấu
                        let league = '';
                        const insideLeague = card.querySelector('[class*="league"], [class*="tournament"], [class*="comp"], [class*="stage"]');
                        if (insideLeague && insideLeague.innerText) {
                            league = insideLeague.innerText.trim().split('\n')[0].trim();
                        }
                        if (!league) {
                            let cur = card;
                            for (let step = 0; step < 4 && cur; step++) {
                                let prev = cur.previousElementSibling;
                                while (prev) {
                                    if (!prev.querySelector('a[href*="/truc-tiep/"]')) {
                                        const cand = (prev.innerText || '').trim().split('\n')[0].trim();
                                        if (cand.length >= 3 && cand.length <= 55 && !/\bvs\b/i.test(cand) && !/^\d{1,2}:\d{2}$/.test(cand)) {
                                            league = cand;
                                            break;
                                        }
                                    }
                                    prev = prev.previousElementSibling;
                                }
                                if (league) break;
                                cur = cur.parentElement;
                            }
                        }

                        // Lấy tên BLV ngay trên thẻ trang chủ nếu link gắn với phòng BLV (?anchorId=...)
                        let roomName = '';
                        if (href.includes('anchorId=')) {
                            const imgAlt = a.querySelector('img')?.getAttribute('alt') || '';
                            roomName = (a.innerText || a.getAttribute('title') || imgAlt || '').trim();
                        }

                        // Lấy sẵn logo đội bóng trên thẻ trang chủ
                        const imgs = Array.from(card.querySelectorAll('img'))
                            .map(img => (img.currentSrc || img.src || img.getAttribute('data-src') || '').trim())
                            .filter(src => src.startsWith('http') && !/banner|ads|logo-web|favicon|\.gif|\.svg|avatar/i.test(src.toLowerCase()));
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
                        "rooms": {},
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

                # Chỉ lưu link có anchorId làm phòng BLV riêng
                if "anchorId=" in raw_href:
                    r_key = get_room_key(raw_href)
                    r_name = item.get("roomName", "").strip()
                    if r_key not in grouped_matches[base_href]["rooms"]:
                        grouped_matches[base_href]["rooms"][r_key] = {"url": raw_href, "name": r_name}
                    elif not grouped_matches[base_href]["rooms"][r_key]["name"] and r_name:
                        grouped_matches[base_href]["rooms"][r_key]["name"] = r_name

                if len(grouped_matches) >= 100:
                    break

        matches = list(grouped_matches.values())

        # Vào chi tiết 25 trận đầu tiên (đang đá & sắp đá gần nhất) để lấy logo, tên giải, BLV thật và link .m3u8
        for m in matches[:25]:
            captured_streams = []
            target_path = urlparse(m["page_url"]).path.rstrip("/")

            def on_request(req):
                u = req.url
                if (".m3u8" in u.lower() or ".mpd" in u.lower()) and u not in captured_streams:
                    captured_streams.append(u)

            page.on("request", on_request)
            try:
                # Nếu trận đã có sẵn link phòng BLV cụ thể từ trang chủ, vào thẳng phòng đầu tiên đó
                entry_url = next(iter(m["rooms"].values()))["url"] if m["rooms"] else m["page_url"]
                page.goto(entry_url, wait_until="domcontentloaded", timeout=14000)
                page.wait_for_timeout(2600)

                details = extract_details_from_match_page(page, target_path)
                page_logos = details.get("logos") or []
                if len(page_logos) >= 1 and not m["home_logo"]:
                    m["home_logo"] = page_logos[0]
                if len(page_logos) >= 2 and not m["away_logo"]:
                    m["away_logo"] = page_logos[1]

                page_league = re.sub(r"\s+", " ", details.get("league", "")).strip()
                if page_league and m["league"] == "Bóng Đá Trực Tiếp":
                    m["league"] = page_league[:42]

                # Cập nhật danh sách phòng BLV CHỈ thuộc về trận đấu này
                page_rooms = details.get("rooms") or []
                if page_rooms:
                    for pr in page_rooms:
                        pr_url = pr.get("url", "").strip()
                        pr_name = pr.get("name", "").strip()
                        if not pr_url:
                            continue
                        r_key = get_room_key(pr_url)
                        if r_key not in m["rooms"]:
                            m["rooms"][r_key] = {"url": pr_url, "name": pr_name}
                        elif pr_name:
                            m["rooms"][r_key]["name"] = pr_name

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

            room_items = list(m["rooms"].values())
            for r_idx, room in enumerate(room_items[:4], 1):
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

        # Toàn bộ các trận còn lại (từ trận 26 đến ~68+) vẫn hiển thị đầy đủ trên lưới
        for m in matches[25:]:
            streams = []
            room_items = list(m["rooms"].values())
            for r_idx, room in enumerate(room_items[:4], 1):
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
