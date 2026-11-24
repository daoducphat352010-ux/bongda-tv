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
]


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def clean_team_name(name: str) -> str:
    t = clean_text(name)
    t = re.sub(r"^(?:xem\s+)?tr[uự]c\s*ti[eế]p\s*[:\-]?\s*", "", t, flags=re.I)
    # Cắt bỏ mã hash cuối slug hoặc ngày tháng dư (như "24 11 Tou7N5Ebpqv2")
    t = re.sub(r"\b\d{1,2}\s+\d{1,2}(?:\s+\d{2,4})?\b.*$", "", t)
    t = re.sub(r"\b[a-z0-9]{8,}\b$", "", t, flags=re.I)
    t = re.sub(r"\s+\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?$", "", t)
    t = re.sub(r"\s+(?:full\s*hd|hd|live|hom\s*nay|hôm\s*nay)$", "", t, flags=re.I)
    return t.strip(" -|:")[:36]


def is_banned_url(href: str, allowed_host: str) -> bool:
    low = href.lower()
    parsed = urlparse(low)
    if parsed.netloc and allowed_host and parsed.netloc != allowed_host:
        return True
    banned_paths = (
        "/livescore", "/ket-qua", "/bang-xep-hang", "/lich-thi-dau",
        "/tin-tuc", "/soi-keo", "/nhan-dinh", "/highlight", "/video",
        "/khuyen-mai", "/nha-cai", "/lien-he", "/gioi-thieu",
        "/tran-dau/", "/tracking-link"
    )
    if any(p in low for p in banned_paths):
        return True
    return "/truc-tiep-" not in low and "-vs-" not in low


def parse_teams_from_slug(base_href: str) -> tuple[str, str]:
    slug = base_href.rstrip("/").split("/")[-1]
    clean_slug = re.sub(r"^(?:xem-)?truc-tiep-", "", slug, flags=re.I)
    clean_slug = re.sub(r"-luc-\d{4}.*$", "", clean_slug, flags=re.I)
    # Cắt đoạn ngày dd-mm(-yyyy) và mã hash ở cuối slug
    clean_slug = re.sub(r"-\d{1,2}-\d{1,2}(?:-\d{2,4})?(?:-[a-z0-9]{6,})?$", "", clean_slug, flags=re.I)
    clean_slug = re.sub(r"-[a-z0-9]{8,}$", "", clean_slug, flags=re.I)
    parts = re.split(r"-vs-", clean_slug, maxsplit=1, flags=re.I)
    if len(parts) == 2 and parts[0] and parts[1]:
        return (
            clean_team_name(parts[0].replace("-", " ").title()),
            clean_team_name(parts[1].replace("-", " ").title()),
        )
    return "", ""


def is_league_label(text: str) -> bool:
    t = clean_text(text)
    if not t or len(t) > 45:
        return False
    low = t.lower()
    if low in ("hôm nay,", "hôm nay", "ngày mai,", "ngày mai", "live", "sắp đá", "vs"):
        return False
    if any(lg.lower() in low for lg in KNOWN_LEAGUES):
        return True
    return bool(re.search(r"\b(cup|league|championship|major|series|u17|u19|u21|u23|la liga|serie a|bundesliga)\b", low))


def extract_league_name(item: dict, home: str, away: str) -> str:
    raw_league = clean_text(item.get("league") or "")
    lines = [clean_text(ln) for ln in (item.get("text") or "").splitlines() if clean_text(ln)]

    candidates = []
    if raw_league:
        candidates.append(raw_league)
    for ln in lines:
        if is_league_label(ln) and ln != home and ln != away:
            candidates.append(ln)

    for cand in candidates:
        lg = cand
        for token in (home, away, "BLV", "Trực tiếp", "LIVE", "SẮP ĐÁ", "Xem ngay"):
            if token and len(token) >= 3:
                idx = lg.lower().find(token.lower())
                if idx != -1:
                    lg = lg[:idx].strip(" -|•:·/,")
        lg = re.sub(r"\b\d{1,2}:\d{2}\b", "", lg).strip(" -|•:·/,")
        if lg and len(lg) <= 45 and not lg.lower().startswith(("hôm nay", "ngày mai")):
            return lg

    return "Bóng Đá Trực Tiếp"


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

    # Nếu home/away lấy nhầm tên giải thì bỏ qua để dùng tên đội chuẩn
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

    card_blv = clean_text(item.get("cardBlv") or "")
    if re.search(r"soi\s*kèo|cược\s*ngay", card_blv, re.I):
        card_blv = ""

    return league_name, home, away, kickoff, status, card_blv


def is_valid_blv_label(lbl: str) -> bool:
    low = lbl.lower().strip()
    if not low or len(low) > 26:
        return False
    bad_words = (
        "soi kèo", "cược ngay", "đặt cược", "nhà cái", "khuyến mãi",
        "trang chủ", "lịch thi đấu", "kết quả", "bảng xếp hạng", "xem ngay"
    )
    return not any(w in low for w in bad_words)


def clean_blv_label(raw: str, idx: int) -> str:
    txt = clean_text(raw)
    if not txt:
        return f"BLV {idx}"
    for ln in [clean_text(x) for x in raw.splitlines() if clean_text(x)]:
        if re.match(r"^\d+([.,]\d+)?[kKmM]?$", ln):
            continue
        if ln.upper() in ("LIVE", "HD", "FULL HD", "SD", "VIP", "XEM NGAY", "TRỰC TIẾP"):
            continue
        if 2 <= len(ln) <= 28:
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

        detail_data = page.evaluate(
            r"""() => {
                const items = [];
                const seen = new Set();
                const root = document.querySelector('#player, .player-wrapper, .player-container, .box-player, .watch-box, main') || document.body;
                const selectors = [
                    'a[href*="anchor"]', 'a[href*="blv"]', 'a[href*="server"]',
                    '[data-url]', '[data-link]', '[data-stream]',
                    '.list-blv a', '.list-blv button', '.blv-item', '.server-item',
                    '.room-item', '[class*="blv"] a', '[class*="blv"] button',
                    '[class*="server"] a', '[class*="server"] button'
                ];
                const nodes = root.querySelectorAll(selectors.join(','));
                for (const el of nodes) {
                    if (el.closest('[class*="related"], [class*="other-match"], [class*="sidebar"], [class*="list-match"], [class*="odds"], [class*="keo"]')) continue;
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

        for idx, r in enumerate(detail_data.get("rooms", [])):
            lbl = clean_blv_label(r.get("label", ""), idx + 1)
            if not is_valid_blv_label(lbl):
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

    default_blv = card_blv if (card_blv and is_valid_blv_label(card_blv)) else "BLV Trực Tiếp"
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
                    default_blv if idx == 0 else f"BLV {idx + 1}"
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

                for _ in range(30):
                    page.evaluate(
                        r"""() => {
                            const btns = Array.from(document.querySelectorAll('button, a'));
                            for (const b of btns) {
                                const t = (b.innerText || '').trim().toLowerCase();
                                if (/xem thêm|load more|tất cả|all/i.test(t) && t.length < 25) {
                                    try { b.click(); } catch (e) {}
                                }
                            }
                            window.scrollBy(0, 900);
                        }"""
                    )
                    page.wait_for_timeout(300)
            except Exception:
                continue

            active_host = urlparse(page.url).netloc.lower()

            candidates = page.evaluate(
                r"""() => {
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
                            if (linksInParent <= 2 && pText.length > 0 && pText.length <= 220) {
                                card = parent;
                            } else {
                                break;
                            }
                        }

                        const homeEl = card.querySelector('[class*="home"] [class*="name"], .team-home, .home-name, [class*="home-team"]');
                        const awayEl = card.querySelector('[class*="away"] [class*="name"], .team-away, .away-name, [class*="away-team"]');

                        let league = '';
                        const lgEl = card.querySelector('[class*="league"], [class*="tournament"], [class*="tour"], [class*="champ"]');
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
                            cardBlv: cardBlv,
                            homeLogo: pool[0] || '',
                            awayLogo: pool[1] || ''
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

        matches = list(grouped_matches.values())
        for m in matches[:40]:
            streams, h_logo, a_logo = extract_rooms_and_streams(
                page, m["page_url"], m["card_blv"], m["home_logo"], m["away_logo"]
            )
            m["streams"] = streams
            m["home_logo"] = h_logo
            m["away_logo"] = a_logo
            del m["page_url"]
            del m["card_blv"]

        for m in matches[40:]:
            blv_lbl = m["card_blv"] if (m["card_blv"] and is_valid_blv_label(m["card_blv"])) else "BLV Trực Tiếp"
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
