import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright

ROOT_DOMAIN = "https://giovang.co"
SOURCE_URLS = [
    f"{ROOT_DOMAIN}/",
]
OUTPUT_FILE = "giovang_feed.json"


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
        "không giật", "không quảng cáo", "giovang", "giờ vàng", "gavang",
        "trực tiếp", "xem bóng", "lịch thi đấu", "kết quả", "bảng xếp hạng",
        "nguồn phát", "trang chủ", "sắp đá", "xem ngay", "đang diễn ra", "kết thúc"
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

    home = ""
    away = ""
    raw_lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw_text.splitlines() if ln.strip()]
    vs_idx = -1
    for i, ln in enumerate(raw_lines):
        if ln.upper() == "VS":
            vs_idx = i
            break

    if vs_idx > 0:
        for back in range(vs_idx - 1, -1, -1):
            cand = raw_lines[back]
            if not re.match(r"^\d{1,2}:\d{2}", cand) and cand.upper() not in ("LIVE", "HD", "SẮP ĐÁ"):
                home = cand[:32]
                break
        for fwd in range(vs_idx + 1, len(raw_lines)):
            cand = raw_lines[fwd]
            if (
                not re.match(r"^\d{1,2}:\d{2}", cand)
                and not re.match(r"^\d{1,2}/\d{1,2}", cand)
                and cand.upper() not in ("LIVE", "HD", "FULL HD", "SẮP ĐÁ", "XEM NGAY")
            ):
                away = cand[:32]
                break

    if not home or not away:
        clean_slug = re.sub(r"-luc-\d{4}-ngay-.*$", "", slug, flags=re.I)
        clean_slug = re.sub(r"-[a-zA-Z0-9]{6,}$", "", clean_slug)
        teams_from_slug = re.split(r"-vs-", clean_slug, maxsplit=1, flags=re.I)
        if len(teams_from_slug) == 2 and teams_from_slug[0] and teams_from_slug[1]:
            if not home:
                home = teams_from_slug[0].replace("-", " ").title().strip()
            if not away:
                away = teams_from_slug[1].replace("-", " ").title().strip()
        else:
            home = home or clean_slug.replace("-", " ").title()[:32]
            away = away or "Trực Tiếp"

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

                for _ in range(20):
                    page.evaluate("window.scrollBy(0, 750)")
                    page.wait_for_timeout(400)
            except Exception:
                continue

            candidates = page.evaluate(
                r"""() => {
                    const out = [];
                    const isMatchHref = (u) => /truc-tiep|xem-|live|match|tran-dau/i.test(u.toLowerCase());
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = (a.href || '').trim();
                        if (!href.startsWith('http') || !isMatchHref(href)) continue;
                        const card = a.closest('div, li, article') || a;
                        const text = (card.innerText || a.innerText || '').trim();
                        const imgs = Array.from(card.querySelectorAll('img'))
                            .map(img => img.getAttribute('data-src') || img.currentSrc || img.src || '')
                            .filter(src => src.startsWith('http') && !/banner|ads|logo-web|favicon|\.gif|\.svg|avatar/i.test(src));
                        out.push({
                            href: href,
                            text: text,
                            homeLogo: imgs[0] || '',
                            awayLogo: imgs[1] || ''
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

                if base_href not in grouped_matches:
                    grouped_matches[base_href] = {
                        "id": f"gv_{len(grouped_matches) + 1}",
                        "league": "GioVang TV",
                        "status": status,
                        "kickoff": kickoff,
                        "home_name": home_name,
                        "home_logo": item.get("homeLogo") or "",
                        "away_name": away_name,
                        "away_logo": item.get("awayLogo") or "",
                        "score": "vs",
                        "page_url": base_href,
                        "rooms": {},
                        "streams": [],
                    }

                if "anchorId=" in raw_href:
                    r_key = get_room_key(raw_href)
                    if r_key not in grouped_matches[base_href]["rooms"]:
                        grouped_matches[base_href]["rooms"][r_key] = {"url": raw_href, "name": ""}

                if len(grouped_matches) >= 80:
                    break

        matches = list(grouped_matches.values())
        for m in matches[:15]:
            entry_url = next(iter(m["rooms"].values()))["url"] if m["rooms"] else m["page_url"]
            m3u8_list = capture_room_streams(page, entry_url)
            room_items = list(m["rooms"].values())
            if room_items:
                m["streams"] = [
                    {
                        "label": clean_commentator_label(r.get("name", ""), idx + 1),
                        "url": m3u8_list[0] if (idx == 0 and m3u8_list) else r["url"],
                        "referer": r["url"],
                    }
                    for idx, r in enumerate(room_items[:4])
                ]
            else:
                m["streams"] = [
                    {
                        "label": "Phòng Trực Tiếp",
                        "url": m3u8_list[0] if m3u8_list else m["page_url"],
                        "referer": m["page_url"],
                    }
                ]
            del m["page_url"]
            del m["rooms"]

        for m in matches[15:]:
            room_items = list(m["rooms"].values())
            m["streams"] = [
                {
                    "label": clean_commentator_label(r.get("name", ""), idx + 1),
                    "url": r["url"],
                    "referer": r["url"],
                }
                for idx, r in enumerate(room_items[:4])
            ] or [{"label": "Phòng Trực Tiếp", "url": m["page_url"], "referer": m["page_url"]}]
            del m["page_url"]
            del m["rooms"]

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
