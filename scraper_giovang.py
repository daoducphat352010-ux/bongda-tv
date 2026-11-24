import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

ROOT_DOMAIN = "https://giovang.guru"
SOURCE_URLS = [
    f"{ROOT_DOMAIN}/",
]
OUTPUT_FILE = "giovang_feed.json"

KNOWN_LEAGUES = [
    "AFC Champions League Elite",
    "AFC Champions League",
    "UEFA Champions League",
    "Champions League",
    "World Cup U17",
    "AFC Cup",
    "Europa League",
    "Premier League",
    "Ngoại hạng Anh",
    "La Liga",
    "Serie A",
    "Bundesliga",
    "Ligue 1",
    "V-League",
    "MLS",
    "TRITON",
    "PGL Major Singapore 2026 Stage 1",
    "Cúp C1",
    "Cúp C2",
    "Cúp C3",
]


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def clean_team_name(name: str) -> str:
    t = clean_text(name)
    t = re.sub(r"^(?:xem\s+)?tr[uự]c\s*ti[eế]p\s*[:\-]?\s*", "", t, flags=re.I)
    t = re.sub(r"\s+\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?$", "", t)
    t = re.sub(r"\s+\d{1,2}\s+\d{1,2}(?:\s+\d{2,4})?$", "", t)
    t = re.sub(r"\b[a-f0-9]{8,}\b$", "", t, flags=re.I)
    t = re.sub(r"\s+(?:full\s*hd|hd|live|hom\s*nay|hôm\s*nay)$", "", t, flags=re.I)
    return t.strip(" -|:")[:38]


def is_banned_url(href: str, allowed_host: str) -> bool:
    low = href.lower()
    parsed = urlparse(low)
    if parsed.netloc and allowed_host and parsed.netloc != allowed_host:
        return True
    banned_paths = (
        "/livescore", "/ket-qua", "/bang-xep-hang", "/lich-thi-dau",
        "/tin-tuc", "/soi-keo", "/nhan-dinh", "/highlight", "/video",
        "/khuyen-mai", "/nha-cai", "/lien-he", "/gioi-thieu",
        "/tran-dau/", "/tracking-link", "/tag/", "/category/"
    )
    if any(p in low for p in banned_paths):
        return True
    return "/truc-tiep-" not in low and "-vs-" not in low


def parse_teams_from_slug(base_href: str) -> tuple[str, str]:
    slug = base_href.rstrip("/").split("/")[-1]
    clean_slug = re.sub(r"^(?:xem-)?truc-tiep-", "", slug, flags=re.I)
    clean_slug = re.sub(r"-luc-\d{4}.*$", "", clean_slug, flags=re.I)
    clean_slug = re.sub(r"-(?:[a-f0-9]{10,}|[a-z0-9]{8,14})$", "", clean_slug, flags=re.I)
    clean_slug = re.sub(r"-\d{1,2}-\d{1,2}(?:-\d{2,4})?$", "", clean_slug)
    clean_slug = re.sub(r"-\d{1,2}-\d{1,2}(?:-\d{2,4})?$", "", clean_slug)

    parts = re.split(r"-vs-", clean_slug, maxsplit=1, flags=re.I)
    if len(parts) == 2 and parts[0] and parts[1]:
        home_raw = re.sub(r"-\d{1,2}-\d{1,2}(?:-\d{2,4})?$", "", parts[0]).replace("-", " ").strip()
        away_raw = re.sub(r"-\d{1,2}-\d{1,2}(?:-\d{2,4})?$", "", parts[1]).replace("-", " ").strip()
        if home_raw and away_raw:
            return clean_team_name(home_raw.title()), clean_team_name(away_raw.title())
    return "", ""


def is_league_label(text: str) -> bool:
    t = clean_text(text)
    if not t or len(t) > 50 or len(t) < 2:
        return False
    low = t.lower()
    if low in (
        "hôm nay,", "hôm nay", "ngày mai,", "ngày mai", "live", "sắp đá",
        "vs", "xem ngay", "trực tiếp", "bóng đá trực tiếp", "đội nhà", "đội khách",
        "tất cả", "bóng đá", "bóng chuyền", "bóng rổ", "esports"
    ):
        return False
    if any(lg.lower() in low for lg in KNOWN_LEAGUES):
        return True
    return bool(
        re.search(
            r"\b(cup|league|championship|major|series|u17|u19|u21|u23|la liga|serie a|bundesliga|ligue 1|premier|afc|uefa|fifa|nba|cba|tbl)\b",
            low,
        )
    )


def extract_league_name(item: dict, home: str, away: str) -> str:
    candidates = []
    for key in ("league", "sectionLeague", "topLeague"):
        val = clean_text(item.get(key) or "")
        if val:
            candidates.append(val)

    lines = [clean_text(ln) for ln in (item.get("text") or "").splitlines() if clean_text(ln)]
    for ln in lines:
        if ln.lower() != home.lower() and ln.lower() != away.lower():
            candidates.append(ln)

    for cand in candidates:
        for sub in [clean_text(x) for x in cand.splitlines() if clean_text(x)]:
            lg = sub
            for token in (home, away, "BLV", "Trực tiếp", "LIVE", "SẮP ĐÁ", "Xem ngay"):
                if token and len(token) >= 3:
                    idx = lg.lower().find(token.lower())
                    if idx != -1:
                        lg = lg[:idx].strip(" -|•:·/,")
            lg = re.sub(r"\b\d{1,2}:\d{2}\b", "", lg).strip(" -|•:·/,")
            if is_league_label(lg):
                return lg

    return "Bóng Đá Trực Tiếp"


def clean_blv_name(raw: str, home: str = "", away: str = "") -> str:
    if not raw:
        return ""
    lines = [clean_text(x) for x in raw.splitlines() if clean_text(x)]
    picked = ""
    for ln in lines:
        if re.search(r"\bblv\b", ln, flags=re.I):
            picked = ln
            break
    if not picked and lines:
        picked = lines[0]

    txt = clean_text(picked)
    txt = re.split(r"[|•·/(\[\n]", txt)[0].strip()
    txt = re.sub(r"^(?:xem\s+)?tr[uự]c\s*ti[eế]p\s*(?:cùng\s*)?", "", txt, flags=re.I).strip()

    low = txt.lower()
    if not txt or len(txt) > 22:
        return ""

    junk_exact = {
        "blv", "live", "hd", "full hd", "sd", "vip", "vs", "xem ngay",
        "trực tiếp", "sắp đá", "đang đá", "kết thúc", "ht", "ft",
        "soi kèo", "cược ngay", "đặt cược", "nhà cái", "khuyến mãi",
        "trang chủ", "lịch thi đấu", "kết quả", "bảng xếp hạng",
        "phòng trực tiếp", "blv trực tiếp", "server", "link", "kênh", "phòng"
    }
    if low in junk_exact:
        return ""

    junk_substrings = (
        "soi kèo", "cược ngay", "đặt cược", "nhà cái", "khuyến mãi",
        "bình luận viên", "bình luận trực tiếp", "nhận định", "tỷ số",
        "trang chủ", "lịch thi đấu", "kết quả", "bảng xếp hạng",
        "http", "www.", ".com", ".net", ".gg", ".guru", ".live"
    )
    if any(s in low for s in junk_substrings):
        return ""

    for t in (home, away):
        if t and len(t) >= 3 and (low == t.lower() or t.lower() in low):
            return ""
    if is_league_label(txt):
        return ""

    m_blv = re.match(r"^blv[\s:.-]*(.+)$", txt, flags=re.I)
    if m_blv:
        name_part = clean_text(m_blv.group(1))
        words = name_part.split()
        if not words or len(words) > 3:
            return ""
        clean_name = " ".join(words)
        if clean_name.lower() in junk_exact:
            return ""
        return f"BLV {clean_name}"

    if re.match(r"^(?:server|link|kênh|phòng)\s*\d*$", low):
        return txt.title()

    words = txt.split()
    if 1 <= len(words) <= 2 and re.match(r"^[A-Za-zÀ-ỹ]+(?:\s+[A-Za-zÀ-ỹ]+)?$", txt):
        return f"BLV {txt}"

    return ""


def parse_match_info(item: dict, base_href: str):
    raw_text = item.get("text", "")
    lines = [clean_text(ln) for ln in raw_text.splitlines() if clean_text(ln)]
    slug_home, slug_away = parse_teams_from_slug(base_href)

    noise_exact = {
        "LIVE", "HD", "FULL HD", "VS", "SẮP ĐÁ", "ĐANG ĐÁ",
        "XEM NGAY", "TRỰC TIẾP", "KẾT THÚC", "HT", "FT", "SOI KÈO", "CƯỢC NGAY"
    }

    home = clean_team_name(item.get("homeText") or "")
    away = clean_team_name(item.get("awayText") or "")

    if is_league_label(home):
        home = ""
    if is_league_label(away):
        away = ""

    if not home or not away:
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
                    and not is_league_label(cand)
                    and not re.match(r"^\d{1,2}[:/]\d{2}", cand)
                    and not re.search(r"tr[uự]c\s*ti[eế]p", cand, re.I)
                    and not cand.lower().startswith("blv")
                ):
                    home = home or clean_team_name(cand)
                    break
            for fwd in range(vs_idx + 1, len(lines)):
                cand = lines[fwd]
                if (
                    cand.upper() not in noise_exact
                    and not is_league_label(cand)
                    and not re.match(r"^\d{1,2}[:/]\d{2}", cand)
                    and not re.search(r"tr[uự]c\s*ti[eế]p", cand, re.I)
                    and not cand.lower().startswith("blv")
                ):
                    away = away or clean_team_name(cand)
                    break

    home = home or slug_home or "Đội Nhà"
    away = away or slug_away or "Đội Khách"
    league_name = extract_league_name(item, home, away)

    kickoff = ""
    m_text_time = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", raw_text)
    if m_text_time:
        kickoff = m_text_time.group(0)
    else:
        m_slug_time = re.search(r"-luc-(\d{2})(\d{2})", base_href, re.I)
        if m_slug_time:
            kickoff = f"{m_slug_time.group(1)}:{m_slug_time.group(2)}"

    has_live_badge = bool(re.search(r"\b(LIVE|ĐANG ĐÁ|HIỆP 1|HIỆP 2|HT)\b", raw_text, re.I))
    now_vn = datetime.now(timezone(timedelta(hours=7)))

    if kickoff and re.match(r"^\d{1,2}:\d{2}$", kickoff):
        kh, km = map(int, kickoff.split(":"))
        kickoff = f"{kh:02d}:{km:02d}"
        kickoff_dt = now_vn.replace(hour=kh, minute=km, second=0, microsecond=0)
        diff_minutes = (now_vn - kickoff_dt).total_seconds() / 60.0
        if diff_minutes > 720:
            diff_minutes -= 1440
        status = "LIVE" if (has_live_badge or -10 <= diff_minutes <= 125) else "SẮP ĐÁ"
    else:
        kickoff = now_vn.strftime("%H:%M")
        status = "LIVE" if has_live_badge else "SẮP ĐÁ"

    card_blv = clean_blv_name(item.get("cardBlv") or "", home, away)
    if not card_blv:
        for ln in lines:
            cand_blv = clean_blv_name(ln, home, away)
            if cand_blv:
                card_blv = cand_blv
                break

    return league_name, home, away, kickoff, status, card_blv


def extract_rooms_and_streams(
    page, match_url: str, card_blv: str, cur_home_logo: str, cur_away_logo: str, home_name: str, away_name: str
):
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

        detail_data = page.evaluate(
            r"""() => {
                const items = [];
                const seen = new Set();
                const root = document.querySelector('#player, .player-wrapper, .player-container, .box-player, .watch-box') || document.body;
                const selectors = [
                    'a[href*="anchor"]', 'a[href*="blv"]', 'a[href*="server"]',
                    '[data-url]', '[data-link]', '[data-stream]',
                    '.list-blv a', '.list-blv button', '.blv-item', '.server-item',
                    '.room-item', '[class*="blv"] a', '[class*="blv"] button',
                    '[class*="server"] a', '[class*="server"] button'
                ];
                const nodes = root.querySelectorAll(selectors.join(','));
                for (const el of nodes) {
                    if (el.closest('[class*="related"], [class*="other-match"], [class*="sidebar"], [class*="list-match"], [class*="odds"], [class*="keo"], article, .entry-content, .post-content')) continue;
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

                const logos = Array.from(document.querySelectorAll('img'))
                    .map(img => img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.currentSrc || img.src || '')
                    .filter(src =>
                        src.startsWith('http') &&
                        /\/images\/teams\//i.test(src) &&
                        !/\/kbv\.svg|logo-web|banner|league/i.test(src)
                    );

                return { rooms: items, logos: [...new Set(logos)] };
            }"""
        )

        detail_logos = detail_data.get("logos", [])
        if len(detail_logos) >= 1 and (not home_logo or "/kbv.svg" in home_logo or "/league/" in home_logo):
            home_logo = detail_logos[0]
        if len(detail_logos) >= 2 and (not away_logo or "/kbv.svg" in away_logo or "/league/" in away_logo):
            away_logo = detail_logos[1]

        for r in detail_data.get("rooms", []):
            lbl = clean_blv_name(r.get("label", ""), home_name, away_name)
            if not lbl:
                continue
            d_url = (r.get("dataUrl") or "").strip()
            h_url = (r.get("href") or "").strip()
            if "/tracking-link" in (d_url + h_url).lower():
                continue
            resolved = ""
            if d_url.startswith("http"):
                resolved = d_url
            elif h_url and not h_url.startswith(("javascript:", "#")):
                resolved = urljoin(match_url, h_url)
            if not any(x["label"].lower() == lbl.lower() for x in room_entries):
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

    default_blv = card_blv or "BLV Trực Tiếp"
    streams = []

    if captured_m3u8:
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
                    default_blv if idx == 0 else f"Server {idx + 1}"
                )
                streams.append({
                    "label": lbl,
                    "url": m_url,
                    "referer": match_url,
                })
    elif room_entries:
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


def collect_candidates_from_page(page):
    return page.evaluate(
        r"""() => {
            // Thu thập các tiêu đề giải/mục nằm trên trang kèm tọa độ Y thực tế
            const headers = [];
            const hNodes = document.querySelectorAll('h1, h2, h3, h4, h5, [class*="league"], [class*="tournament"], [class*="tour"], [class*="champ"], [class*="title"], [class*="header"]');
            for (const hn of hNodes) {
                if (hn.querySelector('a[href*="/truc-tiep-"], a[href*="-vs-"]')) continue;
                const rect = hn.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                const txt = (hn.innerText || '').trim();
                if (!txt || txt.length < 2 || txt.length > 50 || txt.includes('\n')) continue;
                headers.push({ top: rect.top + window.scrollY, text: txt });
            }

            const out = [];
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            for (const a of anchors) {
                const href = (a.href || '').trim();
                if (!href.startsWith('http')) continue;
                const lowHref = href.toLowerCase();
                if (!lowHref.includes('/truc-tiep-') && !lowHref.includes('-vs-')) continue;

                let card = a;
                for (let i = 0; i < 5; i++) {
                    const parent = card.parentElement;
                    if (!parent) break;
                    const linksInParent = parent.querySelectorAll('a[href*="/truc-tiep-"], a[href*="-vs-"]').length;
                    const pText = (parent.innerText || '').trim();
                    if (linksInParent <= 2 && pText.length > 0 && pText.length <= 240) {
                        card = parent;
                    } else {
                        break;
                    }
                }

                const cardRect = card.getBoundingClientRect();
                const cardTop = cardRect.top + window.scrollY;

                // Lấy tiêu đề giải gần nhất nằm phía trên thẻ trận
                let topLeague = '';
                let bestTop = -1;
                for (const h of headers) {
                    if (h.top <= cardTop + 8 && h.top > bestTop) {
                        bestTop = h.top;
                        topLeague = h.text;
                    }
                }

                const homeEl = card.querySelector('[class*="home"] [class*="name"], .team-home, .home-name, [class*="home-team"]');
                const awayEl = card.querySelector('[class*="away"] [class*="name"], .team-away, .away-name, [class*="away-team"]');

                let league = '';
                const lgEl = card.querySelector('[class*="league"], [class*="tournament"], [class*="tour"], [class*="champ"], [class*="cate"]');
                if (lgEl) {
                    league = (lgEl.innerText || '').trim();
                }

                let cardBlv = '';
                const blvEl = card.querySelector('[class*="blv"], [class*="commentator"], [class*="caster"]');
                if (blvEl) {
                    cardBlv = (blvEl.innerText || '').trim();
                }

                const text = (card.innerText || a.innerText || '').trim();

                const imgs = Array.from(card.querySelectorAll('img'))
                    .map(img => ({
                        src: img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.currentSrc || img.src || '',
                        alt: (img.getAttribute('alt') || '').toLowerCase(),
                        cls: (img.className || '').toLowerCase()
                    }))
                    .filter(x =>
                        x.src.startsWith('http') &&
                        !/\/kbv\.svg|\/league\/|banner|ads|logo-web|favicon|\.gif|avatar/i.test(x.src + ' ' + x.alt + ' ' + x.cls)
                    )
                    .map(x => x.src);

                const uniqueImgs = [...new Set(imgs)];
                const teamImgs = uniqueImgs.filter(u => /\/images\/teams\//i.test(u));
                const pool = teamImgs.length >= 2 ? teamImgs : uniqueImgs;

                out.push({
                    href: href,
                    text: text,
                    homeText: homeEl ? (homeEl.innerText || '').trim() : '',
                    awayText: awayEl ? (awayEl.innerText || '').trim() : '',
                    league: league,
                    topLeague: topLeague,
                    cardBlv: cardBlv,
                    homeLogo: pool[0] || '',
                    awayLogo: pool[1] || ''
                });
            }
            return out;
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

                for _ in range(45):
                    page.evaluate(
                        r"""() => {
                            const btns = Array.from(document.querySelectorAll('button, a, [role="tab"]'));
                            for (const b of btns) {
                                const t = (b.innerText || '').trim().toLowerCase();
                                if (/xem thêm|load more|tất cả|all|mở rộng/i.test(t) && t.length < 30) {
                                    try { b.click(); } catch (e) {}
                                }
                            }
                            window.scrollBy(0, 950);
                        }"""
                    )
                    page.wait_for_timeout(250)
            except Exception:
                continue

            active_host = urlparse(page.url).netloc.lower()
            candidates = collect_candidates_from_page(page)

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

        matches = list(grouped_matches.values())
        for m in matches[:45]:
            streams, h_logo, a_logo = extract_rooms_and_streams(
                page, m["page_url"], m["card_blv"], m["home_logo"], m["away_logo"], m["home_name"], m["away_name"]
            )
            m["streams"] = streams
            m["home_logo"] = h_logo
            m["away_logo"] = a_logo
            del m["page_url"]
            del m["card_blv"]

        for m in matches[45:]:
            blv_lbl = m["card_blv"] or "BLV Trực Tiếp"
            m["streams"] = [
                {
                    "label": blv_lbl,
                    "url": m["page_url"],
                    "referer": m["page_url"],
                }
            ]
            del m["page_url"]
            del m["card_blv"]

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

    print(f"Đã cập nhật {len(matches)} trận đấu vào {OUTPUT_FILE}.")


if __name__ == "__main__":
    scrape_all()
