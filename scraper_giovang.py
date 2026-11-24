import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

ROOT_DOMAIN = "https://giovang.co"
SOURCE_URLS = [
    f"{ROOT_DOMAIN}/",
]
OUTPUT_FILE = "giovang_feed.json"


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


def is_banned_url(href: str, allowed_host: str) -> bool:
    low = href.lower()
    parsed = urlparse(low)
    # Chặn toàn bộ link trỏ ra ngoài domain chính (như keobongvip.live / keobongvip.ch)
    if parsed.netloc and allowed_host and parsed.netloc != allowed_host:
        return True
    banned_paths = (
        "/livescore", "/ket-qua", "/bang-xep-hang", "/lich-thi-dau",
        "/tin-tuc", "/soi-keo", "/nhan-dinh", "/highlight", "/video",
        "/khuyen-mai", "/nha-cai", "/lien-he", "/gioi-thieu", "/tran-dau/"
    )
    if any(p in low for p in banned_paths):
        return True
    return "-vs-" not in low


def sanitize_league_name(raw_league: str, home: str, away: str) -> str:
    lg = clean_text(raw_league)
    if not lg:
        return "Bóng Đá Trực Tiếp"
    # Cắt bỏ phần tên đội hoặc tên BLV nếu bị dính vào cuối chuỗi giải đấu
    for token in (home, away, "BLV", "Trực tiếp", "LIVE", "SẮP ĐÁ", "Xem ngay"):
        if token and len(token) >= 3:
            idx = lg.lower().find(token.lower())
            if idx != -1:
                lg = lg[:idx].strip(" -|•:·/")
    lg = re.sub(r"\b\d{1,2}:\d{2}\b", "", lg).strip(" -|•:·/")
    if len(lg) < 2 or len(lg) > 42:
        return "Bóng Đá Trực Tiếp"
    return lg


def parse_match_info(item: dict, base_href: str):
    slug = base_href.rstrip("/").split("/")[-1]
    raw_text = item.get("text", "")
    lines = [clean_text(ln) for ln in raw_text.splitlines() if clean_text(ln)]

    # 1. Tách tên 2 đội từ slug trước làm chuẩn đối chiếu, sau đó ưu tiên tên hiển thị trên thẻ
    clean_slug = re.sub(r"^(?:xem-)?truc-tiep-", "", slug, flags=re.I)
    clean_slug = re.sub(r"-luc-\d{4}.*$", "", clean_slug, flags=re.I)
    clean_slug = re.sub(r"-\d{1,2}-\d{1,2}(?:-\d{2,4})?(?:-[a-f0-9]{8,})?$", "", clean_slug, flags=re.I)
    clean_slug = re.sub(r"-[a-f0-9]{10,}$", "", clean_slug, flags=re.I)

    slug_home, slug_away = "", ""
    parts = re.split(r"-vs-", clean_slug, maxsplit=1, flags=re.I)
    if len(parts) == 2 and parts[0] and parts[1]:
        slug_home = clean_team_name(parts[0].replace("-", " ").title())
        slug_away = clean_team_name(parts[1].replace("-", " ").title())

    noise_exact = {
        "LIVE", "HD", "FULL HD", "VS", "SẮP ĐÁ", "ĐANG ĐÁ",
        "XEM NGAY", "TRỰC TIẾP", "KẾT THÚC", "HT", "FT"
    }

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
                and not re.match(r"^\d{1,2}[:/]\d{2}", cand)
                and not re.search(r"tr[uự]c\s*ti[eế]p", cand, re.I)
                and not cand.lower().startswith("blv")
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

    home = home or slug_home or "Đội Nhà"
    away = away or slug_away or "Đội Khách"

    # 2. Lấy tên giải đấu từ thuộc tính riêng của thẻ hoặc tiêu đề giải gần nhất phía trên
    raw_league = item.get("league") or ""
    if not raw_league:
        for ln in lines[:2]:
            if (
                3 <= len(ln) <= 44
                and ln.upper() not in noise_exact
                and ln != home
                and ln != away
                and " vs " not in ln.lower()
                and not re.match(r"^\d{1,2}[:/]\d{2}", ln)
                and not ln.lower().startswith("blv")
            ):
                raw_league = ln
                break
    league_name = sanitize_league_name(raw_league, home, away)

    # 3. Tách chuẩn giờ đá (HH:mm) và trạng thái LIVE / SẮP ĐÁ (không bao giờ gán kickoff = "LIVE")
    kickoff = ""
    m_slug_time = re.search(r"-luc-(\d{2})(\d{2})", slug, re.I)
    if m_slug_time:
        kickoff = f"{m_slug_time.group(1)}:{m_slug_time.group(2)}"
    else:
        m_text_time = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", raw_text)
        if m_text_time:
            kickoff = m_text_time.group(0)

    has_live_badge = bool(re.search(r"\b(LIVE|ĐANG ĐÁ|HIỆP 1|HIỆP 2|HT)\b", raw_text, re.I))
    now_vn = datetime.now(timezone(timedelta(hours=7)))

    if kickoff and re.match(r"^\d{1,2}:\d{2}$", kickoff):
        kh, km = map(int, kickoff.split(":"))
        kickoff = f"{kh:02d}:{km:02d}"
        kickoff_dt = now_vn.replace(hour=kh, minute=km, second=0, microsecond=0)
        diff_minutes = (now_vn - kickoff_dt).total_seconds() / 60.0
        # Nếu giờ trận đã qua hơn 12 tiếng (ví dụ trận 01:15 rạng sáng mai), cộng thêm 1 ngày
        if diff_minutes > 720:
            diff_minutes -= 1440
        status = "LIVE" if (has_live_badge or -10 <= diff_minutes <= 125) else "SẮP ĐÁ"
    else:
        kickoff = now_vn.strftime("%H:%M")
        status = "LIVE" if has_live_badge else "SẮP ĐÁ"

    # 4. Lấy tên BLV hiển thị sẵn trên thẻ trận (nếu có)
    card_blv = clean_text(item.get("cardBlv") or "")
    if not card_blv:
        for ln in lines:
            if ln.lower().startswith("blv ") and len(ln) <= 24:
                card_blv = ln
                break

    return league_name, home, away, kickoff, status, card_blv


def clean_blv_label(raw: str, idx: int) -> str:
    txt = clean_text(raw)
    if not txt:
        return f"Phòng BLV {idx}"
    for ln in [clean_text(x) for x in raw.splitlines() if clean_text(x)]:
        if re.match(r"^\d+([.,]\d+)?[kKmM]?$", ln):
            continue
        if ln.upper() in ("LIVE", "HD", "FULL HD", "SD", "VIP", "XEM NGAY", "TRỰC TIẾP"):
            continue
        if 2 <= len(ln) <= 32:
            txt = ln
            break
    if txt.lower().startswith(("blv", "server", "link", "kênh", "phòng")):
        return txt
    return f"BLV {txt}"


def extract_rooms_and_streams(page, match_url: str, card_blv: str, cur_home_logo: str, cur_away_logo: str):
    captured_m3u8 = []

    def on_request(req):
        u = req.url
        if (".m3u8" in u.lower() or ".mpd" in u.lower()) and u not in captured_m3u8:
            captured_m3u8.append(u)

    page.on("request", on_request)
    room_entries = []
    home_logo = cur_home_logo
    away_logo = cur_away_logo

    try:
        page.goto(match_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)

        # Chỉ quét nút BLV bên trong vùng khung phát (loại bỏ menu/sidebar của các trận khác)
        detail_data = page.evaluate(
            r"""() => {
                const items = [];
                const seen = new Set();
                const root = document.querySelector('#player, .player-wrapper, .player-container, .box-player, .watch-box, main') || document.body;
                const selectors = [
                    'a[href*="anchor"]', 'a[href*="blv"]', 'a[href*="server"]',
                    'a[href*="link="]', '[data-url]', '[data-link]', '[data-stream]',
                    '.list-blv a', '.list-blv button', '.blv-item', '.server-item',
                    '.room-item', '[class*="blv"] a', '[class*="blv"] button',
                    '[class*="server"] a', '[class*="server"] button'
                ];
                const nodes = root.querySelectorAll(selectors.join(','));
                for (const el of nodes) {
                    // Bỏ qua nếu nút nằm trong danh sách trận liên quan
                    if (el.closest('[class*="related"], [class*="other-match"], [class*="sidebar"], [class*="list-match"]')) continue;
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

                // Bổ sung logo CLB từ trang chi tiết nếu trang chủ chưa load kịp
                const logos = Array.from(document.querySelectorAll('img'))
                    .map(img => img.getAttribute('data-src') || img.currentSrc || img.src || '')
                    .filter(src => src.startsWith('http') && /\/images\/teams\//i.test(src));

                return { rooms: items, logos: [...new Set(logos)] };
            }"""
        )

        if not home_logo and len(detail_data.get("logos", [])) >= 1:
            home_logo = detail_data["logos"][0]
        if not away_logo and len(detail_data.get("logos", [])) >= 2:
            away_logo = detail_data["logos"][1]

        for idx, r in enumerate(detail_data.get("rooms", [])):
            lbl = clean_blv_label(r.get("label", ""), idx + 1)
            if any(x in lbl.lower() for x in ("trang chủ", "lịch thi đấu", "kết quả", "bảng xếp hạng")):
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
    default_blv = card_blv if card_blv else "BLV Trực Tiếp"

    if captured_m3u8:
        # Không bao giờ nhân bản 1 link m3u8 thành 3 phòng BLV trùng nhau
        if len(captured_m3u8) == 1:
            chosen_label = room_entries[0]["label"] if room_entries else default_blv
            streams.append({
                "label": chosen_label,
                "url": captured_m3u8[0],
                "referer": match_url,
            })
        else:
            for idx, m_url in enumerate(captured_m3u8[:4]):
                lbl = room_entries[idx]["label"] if idx < len(room_entries) else (
                    default_blv if idx == 0 else f"Phòng BLV {idx + 1}"
                )
                streams.append({
                    "label": lbl,
                    "url": m_url,
                    "referer": match_url,
                })
    elif room_entries:
        # Nếu chỉ có link trang phòng riêng biệt (khác URL) thì giữ đúng các phòng đó
        seen_room_urls = set()
        for r in room_entries[:4]:
            target_u = r["url"] or match_url
            if target_u in seen_room_urls and len(room_entries) > 1:
                continue
            seen_room_urls.add(target_u)
            streams.append({
                "label": r["label"],
                "url": target_u,
                "referer": match_url,
            })
    else:
        streams.append({
            "label": default_blv,
            "url": match_url,
            "referer": match_url,
        })

    return streams, home_logo, away_logo


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
                for _ in range(18):
                    page.evaluate("window.scrollBy(0, 800)")
                    page.wait_for_timeout(350)
            except Exception:
                continue

            active_host = urlparse(page.url).netloc.lower()

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

                        // Lấy tên giải đấu từ phần tử giải bên trong thẻ hoặc tiêu đề nhóm giải phía trên
                        let league = '';
                        const lgEl = card.querySelector('[class*="league"], [class*="tournament"], [class*="tour"], [class*="champ"]');
                        if (lgEl) {
                            league = (lgEl.innerText || '').trim();
                        } else {
                            let prev = card.previousElementSibling;
                            while (prev) {
                                if (/^H[1-4]$/.test(prev.tagName) || /league|tournament|title/i.test(prev.className || '')) {
                                    league = (prev.innerText || '').trim();
                                    break;
                                }
                                prev = prev.previousElementSibling;
                            }
                        }

                        // Lấy tên BLV riêng trên thẻ trận nếu có
                        let cardBlv = '';
                        const blvEl = card.querySelector('[class*="blv"], [class*="commentator"], [class*="caster"]');
                        if (blvEl) {
                            cardBlv = (blvEl.innerText || '').trim();
                        }

                        // Giữ nguyên cấu trúc xuống dòng từng phần tử con để không bị dính chữ
                        const text = (card.innerText || a.innerText || '').trim();

                        const imgs = Array.from(card.querySelectorAll('img'))
                            .map(img => ({
                                src: img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.currentSrc || img.src || '',
                                alt: (img.getAttribute('alt') || '').toLowerCase(),
                                cls: (img.className || '').toLowerCase()
                            }))
                            .filter(x =>
                                x.src.startsWith('http') &&
                                !/banner|ads|logo-web|favicon|\.gif|avatar/i.test(x.src + ' ' + x.alt + ' ' + x.cls)
                            )
                            .map(x => x.src);

                        const teamLogos = imgs.length >= 3 ? imgs.slice(-2) : imgs;
                        out.push({
                            href: href,
                            text: text,
                            league: league,
                            cardBlv: cardBlv,
                            homeLogo: teamLogos[0] || '',
                            awayLogo: teamLogos[1] || ''
                        });
                    }
                    return out;
                }"""
            )

            for item in candidates:
                raw_href = item.get("href", "").strip()
                if not raw_href or is_banned_url(raw_href, active_host):
                    continue

                base_href = raw_href.split("?")[0].rstrip("/")
                if base_href not in grouped_matches:
                    league_name, home_name, away_name, kickoff, status, card_blv = parse_match_info(
                        item, base_href
                    )
                    grouped_matches[base_href] = {
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
                        "card_blv": card_blv,
                        "streams": [],
                    }

                if len(grouped_matches) >= 60:
                    break

        matches = list(grouped_matches.values())
        for m in matches[:25]:
            streams, h_logo, a_logo = extract_rooms_and_streams(
                page, m["page_url"], m["card_blv"], m["home_logo"], m["away_logo"]
            )
            m["streams"] = streams
            m["home_logo"] = h_logo
            m["away_logo"] = a_logo
            del m["page_url"]
            del m["card_blv"]

        for m in matches[25:]:
            m["streams"] = [
                {
                    "label": m["card_blv"] or "Phòng Trực Tiếp",
                    "url": m["page_url"],
                    "referer": m["page_url"],
                }
            ]
            del m["page_url"]
            del m["card_blv"]

        # Đẩy các trận đang LIVE lên đầu danh sách
        matches.sort(key=lambda x: 0 if x["status"] == "LIVE" else 1)

        browser.close()

    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M %d/%m/%Y")
    feed = {
        "app_title": f"GioVang • {vn_time}",
        "updated_at": vn_time,
        "matches": matches,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print(f"Đã cập nhật {len(matches)} trận đấu sạch vào {OUTPUT_FILE}.")


if __name__ == "__main__":
    scrape_all()
