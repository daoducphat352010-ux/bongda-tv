import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright

SOURCE_URLS = [
    "https://gavang33.gg/",
]


def clean_league_text(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"\s+", " ", raw).strip()
    txt = re.sub(r"^[^\w\u00C0-\u1EF9]+|[^\w\u00C0-\u1EF9)]+$", "", txt).strip()
    return txt


def is_valid_league(cand: str, home: str, away: str, blv_names=None) -> bool:
    c = clean_league_text(cand)
    if len(c) < 3 or len(c) > 60:
        return False

    cl = c.lower()
    hl = (home or "").lower().strip()
    al = (away or "").lower().strip()

    if hl and len(hl) >= 3 and (hl in cl or cl in hl):
        return False
    if al and len(al) >= 3 and (al in cl or cl in al):
        return False

    if blv_names:
        for b in blv_names:
            bl = clean_league_text(b).lower()
            if bl and len(bl) >= 3 and (bl in cl or cl in bl):
                return False

    if re.search(r"\bvs\b", cl) or re.match(r"^\d{1,2}:\d{2}", c):
        return False
    if re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", cl):
        return False

    banned_phrases = (
        "miễn phí", "chất lượng", "fullhd", "full hd", "tốc độ", "mượt mà",
        "không giật", "không quảng cáo", "gavang", "gà vàng", "trực tiếp",
        "xem bóng", "lịch thi đấu", "kết quả", "bảng xếp hạng", "nguồn phát",
        "trang chủ", "sắp đá", "xem ngay", "đang diễn ra", "kết thúc"
    )
    if any(p in cl for p in banned_phrases):
        return False

    if cl in ("home", "live", "hd", "vs", "ft", "ht"):
        return False
    if cl.startswith("blv ") or cl.startswith("phòng "):
        return False

    return True


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


def clean_commentator_label(raw_label: str, fallback_idx: int) -> str:
    if not raw_label:
        return f"Phòng BLV {fallback_idx}"

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


def extract_details_from_match_page(page, target_path: str, home_name: str, away_name: str):
    return page.evaluate(
        r"""({ targetPath, homeName, awayName }) => {
            const normPath = (u) => {
                try {
                    return new URL(u, window.location.origin).pathname.replace(/\/+$/, '');
                } catch (e) {
                    return '';
                }
            };

            const logos = [];
            const seenLogo = new Set();
            const imgs = Array.from(document.querySelectorAll('img'));
            for (const img of imgs) {
                if (img.closest('a[href*="anchorId="]')) continue;
                const src = (img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '').trim();
                if (!src.startsWith('http') || seenLogo.has(src)) continue;
                const lower = src.toLowerCase();
                if (/banner|ads|logo-web|favicon|\.gif|\.svg|button|loading|avatar/i.test(lower)) continue;

                const rect = img.getBoundingClientRect();
                const w = rect.width || img.naturalWidth || 0;
                const h = rect.height || img.naturalHeight || 0;
                if (w >= 20 && w <= 220 && h >= 20 && h <= 220) {
                    seenLogo.add(src);
                    logos.push({ src, left: rect.left, top: rect.top });
                }
                if (logos.length >= 4) break;
            }

            // Sắp xếp theo vị trí trên khung tỷ số (trên xuống, trái sang phải)
            logos.sort((a, b) => (Math.abs(a.top - b.top) > 40 ? a.top - b.top : a.left - b.left));
            const sortedLogos = logos.map(x => x.src);

            const leagueCandidates = [];
            const pushCand = (t) => {
                const s = (t || '').replace(/\s+/g, ' ').trim();
                if (s && s.length >= 3 && s.length <= 65 && !leagueCandidates.includes(s)) {
                    leagueCandidates.push(s);
                }
            };

            const crumbNodes = Array.from(document.querySelectorAll('nav a, nav span, [class*="breadcrumb"] a, [class*="breadcrumb"] span'));
            for (const el of crumbNodes) {
                if (!el.closest('header')) pushCand(el.innerText);
            }

            const hLow = (homeName || '').toLowerCase();
            const aLow = (awayName || '').toLowerCase();
            if (hLow && aLow) {
                const blocks = Array.from(document.querySelectorAll('div, section, header'));
                let bestBlock = null;
                let bestLen = 999999;
                for (const b of blocks) {
                    if (b.closest('[class*="sidebar"], aside, footer')) continue;
                    const txt = (b.innerText || '').trim();
                    const low = txt.toLowerCase();
                    if (txt.length >= 15 && txt.length < bestLen && low.includes(hLow) && low.includes(aLow)) {
                        bestBlock = b;
                        bestLen = txt.length;
                    }
                }
                if (bestBlock) {
                    const lines = (bestBlock.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                    for (const ln of lines.slice(0, 6)) {
                        pushCand(ln);
                    }
                    if (bestBlock.previousElementSibling) {
                        const prevLines = (bestBlock.previousElementSibling.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                        for (const ln of prevLines.slice(0, 3)) {
                            pushCand(ln);
                        }
                    }
                }
            }

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

            let activeHostName = '';
            const hostNodes = Array.from(document.querySelectorAll('[class*="anchor"], [class*="blv"], [class*="commentator"], [class*="streamer"], [class*="host"]'));
            for (const node of hostNodes) {
                const parentLink = node.closest('a[href]');
                if (parentLink && normPath(parentLink.href) !== targetPath) continue;
                if (node.querySelector('a[href]')) continue;
                const txt = (node.innerText || '').trim();
                if (txt && txt.length >= 2 && txt.length <= 40 && !/\bvs\b/i.test(txt)) {
                    activeHostName = txt;
                    break;
                }
            }

            if (rooms.length === 0 && activeHostName) {
                rooms.push({ url: window.location.href, name: activeHostName });
            }

            return { logos: sortedLogos, leagueCandidates, rooms, activeHostName };
        }""",
        {"targetPath": target_path, "homeName": home_name, "awayName": away_name},
    )


def capture_room_streams(page, url: str):
    captured = []

    def on_request(req):
        u = req.url
        if (".m3u8" in u.lower() or ".mpd" in u.lower()) and u not in captured:
            captured.append(u)

    page.on("request", on_request)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=14000)
        page.wait_for_timeout(2400)
        html_dump = page.content()
        extra = re.findall(r'https?://[^\s"\'<>\\)]+\.(?:m3u8|mpd)(?:\?[^\s"\'<>\\)]*)?', html_dump, re.I)
        for u in extra:
            clean_u = u.replace("\\/", "/")
            if clean_u not in captured:
                captured.append(clean_u)
    except Exception:
        pass
    finally:
        page.remove_listener("request", on_request)

    return captured


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

                for _ in range(15):
                    page.evaluate("window.scrollBy(0, 1200)")
                    page.wait_for_timeout(500)
            except Exception:
                continue

            candidates = page.evaluate(
                r"""() => {
                    const out = [];
                    const normPath = (u) => {
                        try {
                            return new URL(u, window.location.origin).pathname.replace(/\/+$/, '');
                        } catch (e) {
                            return '';
                        }
                    };
                    const isMatchHref = (u) => /truc-tiep|xem-|live|match|tran-dau/i.test(u.toLowerCase());

                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = (a.href || '').trim();
                        if (!href.startsWith('http') || !isMatchHref(href)) continue;

                        const myPath = normPath(href);
                        if (!myPath) continue;

                        let card = a;
                        let cur = a.parentElement;
                        for (let step = 0; step < 7 && cur && cur !== document.body; step++) {
                            const subLinks = Array.from(cur.querySelectorAll('a[href]')).filter(x => isMatchHref(x.href || ''));
                            const distinctPaths = new Set(subLinks.map(x => normPath(x.href)).filter(Boolean));
                            if (distinctPaths.size === 1 && distinctPaths.has(myPath)) {
                                card = cur;
                                cur = cur.parentElement;
                            } else {
                                break;
                            }
                        }

                        const text = (card.innerText || a.innerText || '').trim();
                        const cardLines = text
                            .split('\n')
                            .map(s => s.replace(/\s+/g, ' ').trim())
                            .filter(Boolean)
                            .slice(0, 8);

                        let groupHeader = '';
                        let prev = card.previousElementSibling;
                        if (prev && !prev.querySelector('a[href]') && (prev.innerText || '').trim().length <= 65) {
                            groupHeader = (prev.innerText || '').split('\n')[0].trim();
                        } else if (card.parentElement && card.parentElement.previousElementSibling) {
                            const pPrev = card.parentElement.previousElementSibling;
                            if (!pPrev.querySelector('a[href]') && (pPrev.innerText || '').trim().length <= 65) {
                                groupHeader = (pPrev.innerText || '').split('\n')[0].trim();
                            }
                        }

                        let roomName = '';
                        if (href.includes('anchorId=')) {
                            const imgAlt = a.querySelector('img')?.getAttribute('alt') || '';
                            roomName = (a.innerText || a.getAttribute('title') || imgAlt || '').trim();
                        }

                        const imgs = Array.from(card.querySelectorAll('img'))
                            .filter(img => !img.closest('a[href*="anchorId="]'))
                            .map(img => ({
                                src: (img.currentSrc || img.src || img.getAttribute('data-src') || '').trim(),
                                left: img.getBoundingClientRect().left || 0,
                                top: img.getBoundingClientRect().top || 0
                            }))
                            .filter(x => x.src.startsWith('http') && !/banner|ads|logo-web|favicon|\.gif|\.svg|avatar/i.test(x.src.toLowerCase()));

                        imgs.sort((a, b) => (Math.abs(a.top - b.top) > 35 ? a.top - b.top : a.left - b.left));
                        const uniqueImgs = Array.from(new Set(imgs.map(x => x.src)));

                        out.push({
                            href: href,
                            text: text,
                            cardLines: cardLines,
                            groupHeader: groupHeader,
                            roomName: roomName,
                            logos: uniqueImgs.slice(0, 3)
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
                home_name, away_name, kickoff, status = parse_match_info(item.get("text", ""), base_href)

                picked_league = ""
                candidate_lines = list(item.get("cardLines") or [])
                if item.get("groupHeader"):
                    candidate_lines.append(item["groupHeader"])

                for ln in candidate_lines:
                    if is_valid_league(ln, home_name, away_name, [item.get("roomName", "")]):
                        picked_league = clean_league_text(ln)[:45]
                        break

                logos = item.get("logos") or []
                h_logo = logos[0] if len(logos) >= 1 else ""
                a_logo = logos[1] if len(logos) >= 2 else ""

                if base_href not in grouped_matches:
                    grouped_matches[base_href] = {
                        "id": f"m_{len(grouped_matches) + 1}",
                        "league": picked_league or "Bóng Đá Trực Tiếp",
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
                    if existing["league"] == "Bóng Đá Trực Tiếp" and picked_league:
                        existing["league"] = picked_league
                    if not existing["home_logo"] and h_logo:
                        existing["home_logo"] = h_logo
                    if not existing["away_logo"] and a_logo:
                        existing["away_logo"] = a_logo

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

        # Đếm tần suất xuất hiện ảnh để tự động bỏ qua icon quả bóng dùng chung nhiều trận
        img_freq = {}
        for m in matches:
            for key in ("home_logo", "away_logo"):
                u = m.get(key) or ""
                if u:
                    img_freq[u] = img_freq.get(u, 0) + 1

        for m in matches:
            for key in ("home_logo", "away_logo"):
                u = m.get(key) or ""
                if u and img_freq.get(u, 0) >= 5:
                    m[key] = ""

        for m in matches[:20]:
            target_path = urlparse(m["page_url"]).path.rstrip("/")
            entry_url = next(iter(m["rooms"].values()))["url"] if m["rooms"] else m["page_url"]

            first_room_m3u8 = capture_room_streams(page, entry_url)
            details = {}
            try:
                details = extract_details_from_match_page(page, target_path, m["home_name"], m["away_name"])
            except Exception:
                pass

            page_logos = [u for u in (details.get("logos") or []) if img_freq.get(u, 0) < 5]
            if len(page_logos) >= 2:
                if not m["home_logo"]:
                    m["home_logo"] = page_logos[0]
                if not m["away_logo"]:
                    m["away_logo"] = page_logos[1]
            elif len(page_logos) == 1:
                if not m["home_logo"]:
                    m["home_logo"] = page_logos[0]
                elif not m["away_logo"] and page_logos[0] != m["home_logo"]:
                    m["away_logo"] = page_logos[0]

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

            blv_list = [r.get("name", "") for r in m["rooms"].values()]
            if m["league"] == "Bóng Đá Trực Tiếp":
                for cand_lg in details.get("leagueCandidates") or []:
                    if is_valid_league(cand_lg, m["home_name"], m["away_name"], blv_list):
                        m["league"] = clean_league_text(cand_lg)[:45]
                        break

            streams = []
            room_items = list(m["rooms"].values())

            if room_items:
                for r_idx, room in enumerate(room_items[:3], 1):
                    blv_label = clean_commentator_label(room.get("name", ""), r_idx)
                    if r_idx == 1 and room["url"] == entry_url:
                        room_m3u8 = first_room_m3u8
                    else:
                        room_m3u8 = capture_room_streams(page, room["url"])

                    for s_url in room_m3u8[:1]:
                        streams.append(
                            {
                                "label": f"{blv_label} (HD Trực Tiếp)",
                                "url": s_url,
                                "referer": room["url"],
                            }
                        )

                    streams.append(
                        {
                            "label": f"{blv_label} (Web Player)",
                            "url": room["url"],
                            "referer": SOURCE_URLS[0],
                        }
                    )
            else:
                host_label = clean_commentator_label(details.get("activeHostName", ""), 1)
                for s_url in first_room_m3u8[:2]:
                    streams.append(
                        {
                            "label": f"{host_label} (HD Trực Tiếp)",
                            "url": s_url,
                            "referer": m["page_url"],
                        }
                    )
                streams.append(
                    {
                        "label": f"{host_label} (Web Player)",
                        "url": m["page_url"],
                        "referer": SOURCE_URLS[0],
                    }
                )

            m["streams"] = streams
            del m["page_url"]
            del m["rooms"]

        for m in matches[20:]:
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
