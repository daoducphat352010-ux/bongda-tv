import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

ROOT_DOMAIN = "https://giovang.co"
SOURCE_URLS = [
    f"{ROOT_DOMAIN}/",
    f"{ROOT_DOMAIN}/truc-tiep-bong-da",
]
OUTPUT_FILE = "giovang_feed.json"

BANNED_BLV_KEYWORDS = (
    "soi kèo", "soi keo", "cược", "cuoc", "nhà cái", "nha cai",
    "đăng ký", "dang ky", "đăng nhập", "dang nhap", "khuyến mãi",
    "tỷ lệ", "ty le", "kèo nhà cái", "telegram", "zalo", "facebook",
    "tải app", "tai app", "trang chủ", "lịch thi đấu", "kết quả",
    "bảng xếp hạng", "livescore", "tin tức", "highlight"
)

NON_FOOTBALL_KEYWORDS = (
    "sultanlar ligi", "pgl major", "triton", "dota", "cs2", "csgo",
    "valorant", "lol", "nba", "tennis", "atp", "wta", "bóng rổ",
    "bóng chuyền", "cầu lông", "esports"
)


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def clean_team_name(name: str) -> str:
    t = clean_text(name)
    t = re.sub(r"^(?:xem\s+)?tr[uự]c\s*ti[eế]p\s*[:\-]?\s*", "", t, flags=re.I)
    t = re.sub(r"\s+\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?$", "", t)
    t = re.sub(r"\s+\d{1,2}\s+\d{1,2}(?:\s+\d{2,4})?$", "", t)
    t = re.sub(r"\s+(?:full\s*hd|hd|live|hom\s*nay|hôm\s*nay)$", "", t, flags=re.I)
    return t.strip(" -|:")[:34]


def is_banned_url(href: str) -> bool:
    low = href.lower()
    banned_paths = (
        "/livescore", "/ket-qua", "/bang-xep-hang", "/lich-thi-dau",
        "/tin-tuc", "/soi-keo", "/nhan-dinh", "/highlight", "/video",
        "/khuyen-mai", "/nha-cai", "/lien-he", "/gioi-thieu"
    )
    if any(p in low for p in banned_paths):
        return True
    return "-vs-" not in low


def is_valid_league_label(ln: str) -> bool:
    low = ln.lower()
    if len(ln) < 3 or len(ln) > 48:
        return False
    if low in ("ngày", "ngay", "giờ", "gio", "live", "hd", "full hd", "vs", "sắp đá", "đang đá", "xem ngay", "trực tiếp", "kết thúc", "ht", "ft"):
        return False
    if low.startswith(("ngày ", "ngay ", "blv ", "phòng ")):
        return False
    if " vs " in low or re.match(r"^\d{1,2}[:/]\d{2}", ln):
        return False
    if re.search(r"tr[uự]c\s*ti[eế]p", low):
        return False
    return True


def parse_match_info(raw_text: str, base_href: str):
    slug = base_href.rstrip("/").split("/")[-1]

    kickoff = "LIVE"
    m_slug_time = re.search(r"-luc-(\d{2})(\d{2})", slug, re.I)
    if m_slug_time:
        kickoff = f"{m_slug_time.group(1)}:{m_slug_time.group(2)}"
    else:
        m_text_time = re.search(r"\b(\d{1,2}:\d{2})\b", raw_text)
        if m_text_time:
            kickoff = m_text_time.group(1)

    lines = [clean_text(ln) for ln in raw_text.splitlines() if clean_text(ln)]
    noise_exact = {
        "LIVE", "HD", "FULL HD", "VS", "SẮP ĐÁ", "ĐANG ĐÁ",
        "XEM NGAY", "TRỰC TIẾP", "KẾT THÚC", "HT", "FT", "NGÀY"
    }

    league_name = "GioVang TV"
    for ln in lines[:3]:
        if is_valid_league_label(ln):
            league_name = ln
            break

    home = ""
    away = ""
    vs_idx = -1
    for i, ln in enumerate(lines):
        if ln.upper() == "VS" or re.match(r"^\d+\s*-\s*\d+$", ln):
            vs_idx = i
            break

    if vs_idx > 0:
        for back in range(vs_idx - 1, -1, -1):
            cand = lines[back]
            if (
                cand.upper() not in noise_exact
                and cand != league_name
                and not re.match(r"^\d{1,2}[:/]\d{2}", cand)
                and not re.search(r"tr[uự]c\s*ti[eế]p", cand, re.I)
            ):
                home = clean_team_name(cand)
                break
        for fwd in range(vs_idx + 1, len(lines)):
            cand = lines[fwd]
            if (
                cand.upper() not in noise_exact
                and not re.match(r"^\d{1,2}[:/]\d{2}", cand)
                and not re.search(r"tr[uự]c\s*ti[eế]p", cand, re.I)
                and not cand.lower().startswith("blv")
            ):
                away = clean_team_name(cand)
                break

    if not home or not away:
        clean_slug = re.sub(r"^(?:xem-)?truc-tiep-", "", slug, flags=re.I)
        clean_slug = re.sub(r"-luc-\d{4}.*$", "", clean_slug, flags=re.I)
        clean_slug = re.sub(r"-\d{1,2}-\d{1,2}(?:-\d{2,4})?$", "", clean_slug)
        clean_slug = re.sub(r"-[a-zA-Z0-9]{6,}$", "", clean_slug)
        parts = re.split(r"-vs-", clean_slug, maxsplit=1, flags=re.I)
        if len(parts) == 2 and parts[0] and parts[1]:
            if not home:
                home = clean_team_name(parts[0].replace("-", " ").title())
            if not away:
                away = clean_team_name(parts[1].replace("-", " ").title())

    home = home or "Đội Nhà"
    away = away or "Đội Khách"

    now_vn = datetime.now(timezone(timedelta(hours=7)))
    status = "LIVE"
    if re.match(r"^\d{2}:\d{2}$", kickoff):
        kh, km = map(int, kickoff.split(":"))
        kickoff_dt = now_vn.replace(hour=kh, minute=km, second=0, microsecond=0)
        diff_minutes = (now_vn - kickoff_dt).total_seconds() / 60.0
        status = "LIVE" if -15 <= diff_minutes <= 130 else "SẮP ĐÁ"

    return league_name, home, away, kickoff, status


def is_valid_blv_text(raw: str) -> bool:
    low = clean_text(raw).lower()
    if not low or len(low) < 2 or len(low) > 36:
        return False
    if any(k in low for k in BANNED_BLV_KEYWORDS):
        return False
    return True


def clean_blv_label(raw: str, idx: int) -> str:
    txt = clean_text(raw)
    for ln in [clean_text(x) for x in raw.splitlines() if clean_text(x)]:
        if re.match(r"^\d+([.,]\d+)?[kKmM]?$", ln):
            continue
        if ln.upper() in ("LIVE", "HD", "FULL HD", "SD", "VIP", "XEM NGAY", "TRỰC TIẾP"):
            continue
        if is_valid_blv_text(ln):
            txt = ln
            break

    if not is_valid_blv_text(txt):
        return ""
    if txt.lower().startswith(("blv", "server", "link", "kênh", "phòng")):
        return txt
    return f"BLV {txt}"


def extract_rooms_and_streams(page, match_url: str):
    captured_m3u8 = []

    def on_request(req):
        u = req.url
        if (".m3u8" in u.lower() or ".mpd" in u.lower()) and u not in captured_m3u8:
            captured_m3u8.append(u)

    page.on("request", on_request)
    room_entries = []
    try:
        page.goto(match_url, wait_until="domcontentloaded", timeout=18000)
        page.wait_for_timeout(2800)

        raw_rooms = page.evaluate(
            r"""() => {
                const items = [];
                const seen = new Set();
                const selectors = [
                    '.list-blv a', '.list-blv button', '.blv-item', '.server-item',
                    '.room-item', '[class*="blv"] a', '[class*="blv"] button',
                    '[class*="server"] a', '[class*="server"] button',
                    '[data-url]', '[data-link]', '[data-stream]'
                ];
                const nodes = document.querySelectorAll(selectors.join(','));
                for (const el of nodes) {
                    const label = (el.innerText || el.getAttribute('title') || '').trim();
                    const href = el.getAttribute('href') || '';
                    const dataUrl = el.getAttribute('data-url') || el.getAttribute('data-link') || el.getAttribute('data-stream') || '';
                    const target = dataUrl || href;
                    if (!label && !target) continue;
                    const key = label + '|' + target;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    items.push({ label, href, dataUrl });
                }
                return items;
            }"""
        )

        for idx, r in enumerate(raw_rooms):
            lbl = clean_blv_label(r.get("label", ""), idx + 1)
            if not lbl:
                continue
            d_url = (r.get("dataUrl") or "").strip()
            h_url = (r.get("href") or "").strip()
            resolved = ""
            if d_url.startswith("http"):
                resolved = d_url
            elif h_url and not h_url.startswith(("javascript:", "#")):
                resolved = urljoin(match_url, h_url)
            room_entries.append({"label": lbl, "url": resolved})

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
    used_urls = set()

    if room_entries:
        for idx, r in enumerate(room_entries[:4]):
            stream_url = r["url"]
            if not stream_url:
                stream_url = captured_m3u8[idx] if idx < len(captured_m3u8) else (captured_m3u8[0] if captured_m3u8 else match_url)
            elif idx == 0 and captured_m3u8 and ".m3u8" not in stream_url.lower():
                stream_url = captured_m3u8[0]

            if stream_url in used_urls:
                continue
            used_urls.add(stream_url)
            streams.append({
                "label": r["label"],
                "url": stream_url,
                "referer": match_url,
            })

    if not streams and captured_m3u8:
        for idx, m_url in enumerate(captured_m3u8[:3]):
            streams.append({
                "label": f"Phòng BLV {idx + 1}",
                "url": m_url,
                "referer": match_url,
            })

    if not streams:
        streams.append({
            "label": "Phòng Trực Tiếp",
            "url": match_url,
            "referer": match_url,
        })

    return streams


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
                page.goto(base_url, wait_until="domcontentloaded", timeout=28000)
                page.wait_for_timeout(3500)

                # Bấm tab Bóng đá hoặc nút Xem thêm nếu có
                page.evaluate(
                    r"""() => {
                        const btns = Array.from(document.querySelectorAll('button, a, li, span'));
                        for (const el of btns) {
                            const t = (el.innerText || '').trim().toLowerCase();
                            if (t === 'bóng đá' || t === 'tất cả' || t === 'xem thêm') {
                                try { el.click(); } catch (e) {}
                            }
                        }
                    }"""
                )
                page.wait_for_timeout(1500)

                for _ in range(25):
                    page.evaluate("window.scrollBy(0, 850)")
                    page.wait_for_timeout(350)
            except Exception:
                continue

            candidates = page.evaluate(
                r"""() => {
                    const out = [];
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = (a.href || '').trim();
                        if (!href.startsWith('http') || !href.toLowerCase().includes('-vs-')) continue;

                        let card = a;
                        for (let i = 0; i < 4; i++) {
                            if (!card.parentElement) break;
                            const pText = (card.parentElement.innerText || '').trim();
                            if (pText.length > 0 && pText.length <= 260) {
                                card = card.parentElement;
                            } else {
                                break;
                            }
                        }

                        const text = (card.innerText || a.innerText || '').trim();
                        const imgs = Array.from(card.querySelectorAll('img'))
                            .map(img => ({
                                src: img.getAttribute('data-src') || img.currentSrc || img.src || '',
                                alt: (img.getAttribute('alt') || '').toLowerCase(),
                                cls: (img.className || '').toLowerCase()
                            }))
                            .filter(x =>
                                x.src.startsWith('http') &&
                                !/banner|ads|logo-web|favicon|\.gif|\.svg|avatar|league|giai-dau|trophy|cup|afc|uefa|champions|europa/i.test(x.src + ' ' + x.alt + ' ' + x.cls)
                            )
                            .map(x => x.src);

                        const teamLogos = imgs.length >= 3 ? imgs.slice(-2) : imgs;
                        out.push({
                            href: href,
                            text: text,
                            homeLogo: teamLogos[0] || '',
                            awayLogo: teamLogos[1] || ''
                        });
                    }
                    return out;
                }"""
            )

            for item in candidates:
                raw_href = item.get("href", "").strip()
                if not raw_href or is_banned_url(raw_href):
                    continue

                raw_txt = item.get("text", "")
                if any(k in raw_txt.lower() for k in NON_FOOTBALL_KEYWORDS):
                    continue

                base_href = raw_href.split("?")[0].rstrip("/")
                league_name, home_name, away_name, kickoff, status = parse_match_info(raw_txt, base_href)

                # Chống trùng lặp tuyệt đối theo cặp tên đội + giờ đá
                dedup_key = f"{home_name.lower()}|{away_name.lower()}|{kickoff}"
                if dedup_key not in grouped_matches:
                    grouped_matches[dedup_key] = {
                        "id": f"gv_{len(grouped_matches) + 1}",
                        "league": league_name,
                        "status": status,
                        "kickoff": kickoff,
                        "home_name": home_name,
                        "home_logo": item.get("homeLogo") or "",
                        "away_name": away_name,
                        "away_logo": item.get("awayLogo") or "",
                        "score": "vs",
                        "page_url": base_href,
                        "streams": [],
                    }

                if len(grouped_matches) >= 80:
                    break

        matches = list(grouped_matches.values())
        for m in matches[:25]:
            m["streams"] = extract_rooms_and_streams(page, m["page_url"])
            del m["page_url"]

        for m in matches[25:]:
            m["streams"] = [
                {
                    "label": "Phòng Trực Tiếp",
                    "url": m["page_url"],
                    "referer": m["page_url"],
                }
            ]
            del m["page_url"]

        browser.close()

    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M %d/%m/%Y")
    feed = {
        "app_title": f"GioVang • {vn_time}",
        "updated_at": vn_time,
        "matches": matches,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    scrape_all()
