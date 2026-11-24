import json
import re
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

# Danh sách trang nguồn (nếu sau này web đổi tên miền, bạn chỉ cần sửa link ở đây)
SOURCE_URLS = [
    "https://gavang33.gg/",
]


def clean_team_names(raw_text: str, href: str):
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    # Lọc bỏ các chữ phụ như LIVE, HD, giờ đá, tên giải ngắn
    filtered = [
        ln for ln in lines
        if not re.match(r"^\d{1,2}:\d{2}$", ln)
        and ln.upper() not in ("LIVE", "HD", "FULL HD", "XEM NGAY", "TRỰC TIẾP", "VS")
        and len(ln) <= 40
    ]

    kickoff = "LIVE"
    for ln in lines:
        m_time = re.search(r"\b(\d{1,2}:\d{2})\b", ln)
        if m_time:
            kickoff = m_time.group(1)
            break

    is_live = any("LIVE" in ln.upper() or "ĐANG ĐÁ" in ln.upper() or "HIỆP" in ln.upper() for ln in lines)
    status = "LIVE" if is_live or kickoff == "LIVE" else "UPCOMING"

    # Thử tách tên 2 đội từ text trên thẻ
    joined = " ".join(filtered)
    vs_split = re.split(r"\s+(?:vs|v|-|–)\s+", joined, maxsplit=1, flags=re.I)
    if len(vs_split) == 2 and len(vs_split[0]) >= 2 and len(vs_split[1]) >= 2:
        return vs_split[0][:28], vs_split[1][:28], kickoff, status

    if len(filtered) >= 2:
        return filtered[0][:28], filtered[1][:28], kickoff, status

    # Dự phòng: lấy tên từ đường dẫn URL
    slug = href.rstrip("/").split("/")[-1]
    clean_slug = re.sub(r"-[a-zA-Z0-9]{4,}$", "", slug).replace("-", " ").title()
    parts = re.split(r"\s+(?:vs|v)\s+", clean_slug, maxsplit=1, flags=re.I)
    home = parts[0].strip() if parts and parts[0].strip() else clean_slug
    away = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Trực Tiếp"
    return home[:28], away[:28], kickoff, status


def scrape_all():
    matches = []
    seen_urls = set()
    page_diagnostics = []

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
                page_diagnostics.append(f"{page.title()} ({page.url})")
            except Exception as e:
                page_diagnostics.append(f"Lỗi mở {base_url}: {type(e).__name__}")
                continue

            # Trích xuất toàn bộ thẻ trận đấu sau khi JavaScript đã chạy xong
            candidates = page.evaluate(
                """() => {
                    const out = [];
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = a.href || '';
                        if (!href.startsWith('http')) continue;
                        const lower = href.toLowerCase();
                        const card = a.closest('article, li, [class*="match"], [class*="item"], [class*="card"], [class*="game"], tr') || a;
                        const text = (card.innerText || a.innerText || '').trim();
                        const imgs = Array.from(card.querySelectorAll('img'))
                            .map(img => img.src || img.getAttribute('data-src') || '')
                            .filter(src => src.startsWith('http') && !src.includes('logo-web') && !src.includes('banner'));

                        const isMatchUrl = /truc-tiep|xem-|live|match|tran-dau/i.test(lower);
                        const hasTwoLogos = imgs.length >= 2 && text.length >= 6;
                        if (isMatchUrl || hasTwoLogos) {
                            out.push({
                                href: href,
                                text: text,
                                logos: imgs.slice(0, 2)
                            });
                        }
                    }
                    return out;
                }"""
            )

            for item in candidates:
                href = item.get("href", "").strip()
                if not href or href in seen_urls or href.rstrip("/") == base_url.rstrip("/"):
                    continue
                seen_urls.add(href)

                home_name, away_name, kickoff, status = clean_team_names(item.get("text", ""), href)
                logos = item.get("logos") or []
                home_logo = logos[0] if len(logos) >= 1 else ""
                away_logo = logos[1] if len(logos) >= 2 else ""

                matches.append(
                    {
                        "id": f"m_{len(matches) + 1}",
                        "league": "Trực Tiếp Bóng Đá",
                        "status": status,
                        "kickoff": kickoff,
                        "home_name": home_name,
                        "home_logo": home_logo,
                        "away_name": away_name,
                        "away_logo": away_logo,
                        "score": "vs",
                        "page_url": href,
                        "streams": [],
                    }
                )

                if len(matches) >= 18:
                    break

        # Vào thử 10 trận đầu tiên để bắt link luồng phát (.m3u8 / .mpd) trực tiếp cho ExoPlayer
        for m in matches[:10]:
            captured_m3u8 = []

            def on_request(req):
                u = req.url
                if (".m3u8" in u.lower() or ".mpd" in u.lower()) and u not in captured_m3u8:
                    captured_m3u8.append(u)

            page.on("request", on_request)
            try:
                page.goto(m["page_url"], wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(3500)
                html_dump = page.content()
                extra = re.findall(r'https?://[^\s"\'<>\\)]+\.(?:m3u8|mpd)(?:\?[^\s"\'<>\\)]*)?', html_dump, re.I)
                for u in extra:
                    clean_u = u.replace("\\/", "/")
                    if clean_u not in captured_m3u8:
                        captured_m3u8.append(clean_u)
            except Exception:
                pass
            finally:
                page.remove_listener("request", on_request)

            streams = []
            for idx, s_url in enumerate(captured_m3u8[:3], 1):
                streams.append(
                    {
                        "label": f"Server {idx} - Phát trực tiếp (HD)",
                        "url": s_url,
                        "referer": m["page_url"],
                    }
                )

            streams.append(
                {
                    "label": "Server Dự Phòng (Mở Web Player)",
                    "url": m["page_url"],
                    "referer": SOURCE_URLS[0],
                }
            )
            m["streams"] = streams
            del m["page_url"]

        # Các trận còn lại dùng Web Player làm nguồn phát
        for m in matches[10:]:
            m["streams"] = [
                {
                    "label": "Mở trực tiếp trận đấu",
                    "url": m["page_url"],
                    "referer": SOURCE_URLS[0],
                }
            ]
            del m["page_url"]

        browser.close()

    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M %d/%m/%Y")

    if not matches:
        diag = " | ".join(page_diagnostics) if page_diagnostics else "Không có dữ liệu"
        matches.append(
            {
                "id": "diag_1",
                "league": "Chưa có trận hiển thị",
                "status": "LIVE",
                "kickoff": vn_time,
                "home_name": "Nguồn đã phản hồi",
                "home_logo": "",
                "away_name": diag[:60],
                "away_logo": "",
                "score": "INFO",
                "streams": [
                    {
                        "label": "Mở trực tiếp trang GaVang33 trên TV",
                        "url": SOURCE_URLS[0],
                        "referer": SOURCE_URLS[0],
                    },
                    {
                        "label": "Kênh Video Thử Nghiệm (Full HD)",
                        "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
                        "referer": "",
                    },
                ],
            }
        )

    feed = {
        "app_title": f"Cập nhật lúc {vn_time}",
        "updated_at": vn_time,
        "matches": matches,
    }

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    scrape_all()
