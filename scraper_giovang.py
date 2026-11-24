import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from playwright.sync_api import Frame, Page, Request, Response, sync_playwright

TARGET_URL = "https://giovang.co"
OUTPUT_FILE = "giovang.json"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

EXCLUDED_PATH_KEYWORDS = (
    "/tin-tuc",
    "/news",
    "/soi-keo",
    "/nhan-dinh",
    "/ty-so",
    "/livescore",
    "/lich-thi-dau",
    "/bang-xep-hang",
    "/bxh",
    "/highlight",
    "/highlights",
    "/video",
    "/tracking-link",
    "/khuyen-mai",
    "/nha-cai",
    "/dang-nhap",
    "/dang-ky",
    "/lien-he",
    "/chinh-sach",
    "/dieu-khoan",
)

BAD_LABEL_KEYWORDS = (
    "soi kèo",
    "nhận định",
    "cược ngay",
    "đặt cược",
    "nhà cái",
    "khuyến mãi",
    "tặng tiền",
    "code",
    "đăng ký",
    "đăng nhập",
    "bxh",
    "bảng xếp hạng",
    "lịch thi đấu",
    "tỷ số",
    "highlight",
    "tin tức",
    "trang chủ",
    "facebook",
    "telegram",
    "zalo",
    "youtube",
    "tiktok",
)

STREAM_EXT_RE = re.compile(r"https?://[^\s\"'<>\\]+\.(?:m3u8|mpd)(?:\?[^\s\"'<>\\]*)?", re.IGNORECASE)
STREAM_ANY_RE = re.compile(r"https?://[^\s\"'<>\\]+(?:\.m3u8|\.mpd)[^\s\"'<>\\]*", re.IGNORECASE)


def normalize_url(url: str, base_url: str = TARGET_URL) -> str:
    raw = (url or "").strip()
    if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:")):
        return ""
    full = urljoin(base_url, raw)
    parsed = urlparse(full)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    clean_path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), clean_path or "/", "", "", ""))


def is_same_or_sub_domain(url: str, root_domain: str = "giovang.co") -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    root = root_domain.lower().removeprefix("www.")
    return host == root or host.endswith("." + root)


def is_valid_match_url(url: str, home_url: str = "https://giovang.co") -> bool:
    norm = normalize_url(url, home_url)
    if not norm or norm == normalize_url(home_url):
        return False
    if not is_same_or_sub_domain(norm, "giovang.co"):
        return False
    parsed = urlparse(norm)
    path = parsed.path.lower().strip("/")
    if not path or len(path) < 3:
        return False
    if any(kw in f"/{path}" for kw in EXCLUDED_PATH_KEYWORDS):
        return False
    if re.search(r"\.(jpg|jpeg|png|webp|gif|svg|css|js|ico|woff2?|ttf|mp4|m3u8|mpd)$", path):
        return False
    # Thường link trận có slug chứa ký tự gạch ngang hoặc ID trận
    if "-" not in path and not re.search(r"\d{2,}", path):
        return False
    return True


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def is_junk_label(label: str) -> bool:
    low = clean_text(label).lower()
    if not low or len(low) > 60:
        return True
    if low.startswith(("http://", "https://")):
        return True
    return any(bad in low for bad in BAD_LABEL_KEYWORDS)


def format_room_label(raw_label: str, idx: int) -> str:
    lbl = clean_text(raw_label)
    lbl = re.sub(r"^(?:xem\s+|link\s+|kênh\s+|phòng\s+|server\s+)+", "", lbl, flags=re.IGNORECASE).strip()
    if not lbl or is_junk_label(lbl):
        return f"Link {idx}"
    if re.match(r"^blv\b", lbl, flags=re.IGNORECASE):
        rest = re.sub(r"^blv\s*[:\-–—]?\s*", "", lbl, flags=re.IGNORECASE).strip()
        return f"BLV {rest}" if rest else f"Link {idx}"
    # Nếu trông giống tên người / biệt danh BLV (1-3 từ chữ cái, không chứa số/ký hiệu kỹ thuật)
    if re.fullmatch(r"[A-Za-zÀ-ỹ\s._-]{2,25}", lbl) and not re.search(r"\d|hd|fhd|sd|cam|vip|link|server|live", lbl, re.IGNORECASE):
        words = lbl.split()
        if 1 <= len(words) <= 3:
            return f"BLV {' '.join(w.capitalize() if w.islower() else w for w in words)}"
    return lbl


def extract_stream_urls_from_text(content: str, base_url: str) -> list[str]:
    if not content:
        return []
    unescaped = content.replace("\\/", "/").replace("\\u002F", "/")
    found: list[str] = []
    seen: set[str] = set()
    for regex in (STREAM_EXT_RE, STREAM_ANY_RE):
        for m in regex.findall(unescaped):
            candidate = m.rstrip("\"',);]} ")
            full = urljoin(base_url, candidate)
            parsed = urlparse(full)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                if full not in seen:
                    seen.add(full)
                    found.append(full)
    return found


def auto_expand_and_scroll(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=25000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass

    # Bấm các nút tab / xem thêm / tất cả giải nếu có
    click_selectors = [
        "button:has-text('Tất cả')",
        "a:has-text('Tất cả')",
        "button:has-text('Xem thêm')",
        "a:has-text('Xem thêm')",
        "button:has-text('Tải thêm')",
        "[role='tab']",
        ".tab-item",
        ".filter-tab",
    ]
    for sel in click_selectors:
        try:
            locs = page.locator(sel)
            count = min(locs.count(), 12)
            for i in range(count):
                el = locs.nth(i)
                if el.is_visible():
                    txt = clean_text(el.inner_text(timeout=800)).lower()
                    if any(skip in txt for skip in ("đăng nhập", "đăng ký", "nhà cái", "khuyến mãi", "soi kèo")):
                        continue
                    try:
                        el.click(timeout=1000)
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
        except Exception:
            pass

    # Cuộn từng đoạn dài để kích hoạt lazy-load
    prev_height = 0
    for _ in range(25):
        try:
            scroll_info = page.evaluate(
                """() => {
                    const h = Math.max(
                        document.body.scrollHeight || 0,
                        document.documentElement.scrollHeight || 0
                    );
                    window.scrollBy(0, Math.max(600, Math.floor(window.innerHeight * 0.85)));
                    return { y: window.scrollY, innerH: window.innerHeight, h };
                }"""
            )
            page.wait_for_timeout(450)
            if scroll_info["y"] + scroll_info["innerH"] >= scroll_info["h"] - 50:
                page.wait_for_timeout(700)
                new_h = page.evaluate(
                    "() => Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0)"
                )
                if new_h <= prev_height and new_h == scroll_info["h"]:
                    break
            prev_height = scroll_info["h"]
        except Exception:
            break

    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except Exception:
        pass


def collect_homepage_matches(page: Page) -> list[dict[str, Any]]:
    raw_items: list[dict[str, Any]] = page.evaluate(
        """() => {
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            const results = [];
            const badWrapTags = new Set(['BODY', 'HTML', 'MAIN', 'HEADER', 'FOOTER', 'NAV', 'ASIDE']);

            for (const a of anchors) {
                const href = a.getAttribute('href') || '';
                if (!href) continue;

                // Tìm thẻ bọc trực tiếp của 1 trận, giới hạn số lượng link/trận để không dính cả danh sách
                let card = a;
                let cur = a.parentElement;
                for (let depth = 0; depth < 6 && cur; depth++) {
                    if (badWrapTags.has(cur.tagName)) break;
                    const matchLinksInCur = Array.from(cur.querySelectorAll('a[href]')).filter(x => {
                        const h = (x.getAttribute('href') || '').toLowerCase();
                        return h && !h.startsWith('#') && !h.startsWith('javascript:');
                    });
                    const textLen = (cur.innerText || '').trim().length;
                    if (matchLinksInCur.length > 6 || textLen > 450) {
                        break;
                    }
                    card = cur;
                    cur = cur.parentElement;
                }

                const imgs = Array.from(card.querySelectorAll('img')).map(img => ({
                    src: img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '',
                    alt: (img.getAttribute('alt') || '').trim()
                })).filter(i => i.src && !i.src.startsWith('data:'));

                const textLines = (card.innerText || '')
                    .split(/\\n+/)
                    .map(s => s.trim())
                    .filter(Boolean);

                const anchorText = (a.innerText || a.getAttribute('title') || a.getAttribute('aria-label') || '').trim();

                results.push({
                    href,
                    anchorText,
                    textLines,
                    imgs
                });
            }
            return results;
        }"""
    )

    by_url: dict[str, dict[str, Any]] = {}

    for item in raw_items:
        norm_url = normalize_url(item.get("href", ""), TARGET_URL)
        if not is_valid_match_url(norm_url, TARGET_URL):
            continue
        if norm_url in by_url:
            continue

        lines = [clean_text(x) for x in (item.get("textLines") or []) if clean_text(x)]
        # Loại bỏ các dòng rác phổ biến
        filtered_lines = [ln for ln in lines if not is_junk_label(ln) and len(ln) <= 80]

        imgs = item.get("imgs") or []
        home_logo = normalize_url(imgs[0]["src"], norm_url) if len(imgs) >= 1 else ""
        away_logo = normalize_url(imgs[1]["src"], norm_url) if len(imgs) >= 2 else ""

        # Tìm thời gian / trạng thái LIVE
        full_blob = " | ".join(filtered_lines).lower()
        is_live = bool(
            re.search(r"\b(live|trực tiếp|đang đá|hiệp \d|ht|ft|\d+['’])\b", full_blob, re.IGNORECASE)
        )
        status = "LIVE" if is_live else "UPCOMING"

        kickoff_match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", " ".join(filtered_lines))
        kickoff = ""
        if kickoff_match:
            hh = int(kickoff_match.group(1))
            mm = kickoff_match.group(2)
            kickoff = f"{hh:02d}:{mm}"
        elif status == "LIVE":
            kickoff = "LIVE"

        # Tìm tỷ số nếu có (ví dụ 1 - 0 hoặc 2-1, tránh nhầm giờ HH:MM)
        score = "vs"
        for ln in filtered_lines:
            m_score = re.fullmatch(r"(\d{1,2})\s*[-:–]\s*(\d{1,2})", ln)
            if m_score:
                # Bỏ qua nếu là giờ (ví dụ 20:00)
                if ":" in ln and int(m_score.group(1)) <= 23 and int(m_score.group(2)) <= 59 and status != "LIVE":
                    continue
                score = f"{m_score.group(1)} - {m_score.group(2)}"
                break

        # Tách tên đội và tên giải
        team_candidates: list[str] = []
        league = ""
        for ln in filtered_lines:
            if re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", ln):
                continue
            if re.fullmatch(r"\d{1,2}\s*[-:–]\s*\d{1,2}", ln):
                continue
            if ln.lower() in ("vs", "live", "trực tiếp", "ht", "ft", "sắp đá"):
                continue
            if re.search(r"(league|cup|champions|premier|la liga|serie a|bundesliga|ligue 1|v-league|world cup|euro|afc|uefa|giải|cúp|hạng)", ln, re.IGNORECASE):
                if not league:
                    league = ln
                continue
            team_candidates.append(ln)

        # Thử tách từ chuỗi "Đội A vs Đội B" hoặc "Đội A - Đội B" nếu chỉ có 1 dòng dài
        home_name = ""
        away_name = ""
        if len(team_candidates) >= 2:
            home_name = team_candidates[0]
            away_name = team_candidates[1]
        elif len(team_candidates) == 1:
            parts = re.split(r"\s+(?:vs\.?|–|-|x)\s+", team_candidates[0], maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                home_name, away_name = clean_text(parts[0]), clean_text(parts[1])
            else:
                home_name = team_candidates[0]

        if not home_name and imgs and imgs[0].get("alt"):
            home_name = clean_text(imgs[0]["alt"])
        if not away_name and len(imgs) >= 2 and imgs[1].get("alt"):
            away_name = clean_text(imgs[1]["alt"])

        # Fallback từ slug URL nếu thiếu tên đội
        if not home_name or not away_name:
            slug = urlparse(norm_url).path.strip("/").split("/")[-1]
            slug_parts = [p for p in re.split(r"\s+(?:vs|-)\s+", slug.replace("-", " "), maxsplit=1) if p]
            if len(slug_parts) == 2:
                if not home_name:
                    home_name = slug_parts[0].title()
                if not away_name:
                    away_name = slug_parts[1].title()
            elif not home_name:
                home_name = slug.replace("-", " ").title() or "Đội nhà"

        by_url[norm_url] = {
            "url": norm_url,
            "league": league or "Giải đấu",
            "status": status,
            "kickoff": kickoff or "00:00",
            "home_name": home_name or "Đội nhà",
            "home_logo": home_logo,
            "away_name": away_name or "Đội khách",
            "away_logo": away_logo,
            "score": score,
        }

    return list(by_url.values())


def extract_detail_streams(context: Any, match_url: str, fallback_status: str, fallback_score: str) -> tuple[list[dict[str, str]], str, str]:
    page: Page = context.new_page()
    page.set_default_timeout(20000)

    captured_streams: list[str] = []
    seen_streams: set[str] = set()

    def record_stream_url(u: str) -> None:
        clean_u = (u or "").strip().rstrip("\"',);]} ")
        if not clean_u:
            return
        full_u = urljoin(match_url, clean_u)
        low_u = full_u.lower()
        if (".m3u8" in low_u or ".mpd" in low_u) and full_u.startswith(("http://", "https://")):
            if full_u not in seen_streams:
                seen_streams.add(full_u)
                captured_streams.append(full_u)

    def on_request(req: Request) -> None:
        record_stream_url(req.url)
        for found in extract_stream_urls_from_text(req.post_data or "", match_url):
            record_stream_url(found)

    def on_response(res: Response) -> None:
        record_stream_url(res.url)

    page.on("request", on_request)
    page.on("response", on_response)

    status = fallback_status
    score = fallback_score
    player_rooms: list[tuple[str, str]] = []  # (label, candidate_url)

    try:
        page.goto(match_url, wait_until="domcontentloaded", timeout=25000)
        try:
            page.wait_for_load_state("networkidle", timeout=9000)
        except Exception:
            pass
        page.wait_for_timeout(1200)

        # Quét nút / link / thuộc tính chỉ trong khu vực player / khung xem
        room_candidates: list[dict[str, str]] = page.evaluate(
            """() => {
                const playerContainers = Array.from(document.querySelectorAll(
                    '[class*="player"], [id*="player"], [class*="stream"], [id*="stream"], ' +
                    '[class*="blv"], [id*="blv"], [class*="server"], [id*="server"], ' +
                    '[class*="video-box"], [class*="watch"], iframe'
                ));
                const scopes = playerContainers.length > 0 ? playerContainers : [document.body];
                const out = [];
                const seenPairs = new Set();

                for (const sc of scopes) {
                    if (!sc || !sc.querySelectorAll) continue;
                    // Bỏ qua nếu nằm trong nav / header / footer / sidebar / bài viết nhận định
                    if (sc.closest('header, nav, footer, aside, .sidebar, .menu, .navbar, .post-content, .article, .related-matches')) {
                        continue;
                    }
                    const nodes = Array.from(sc.querySelectorAll('button, a, [role="button"], [data-url], [data-link], [data-stream], [data-src], iframe'));
                    for (const el of nodes) {
                        if (el.closest('header, nav, footer, aside, .sidebar, .menu, .navbar, .related-matches')) {
                            continue;
                        }
                        const label = (
                            el.getAttribute('data-blv') ||
                            el.getAttribute('data-label') ||
                            el.getAttribute('data-name') ||
                            el.getAttribute('title') ||
                            el.getAttribute('aria-label') ||
                            el.innerText ||
                            ''
                        ).trim();
                        const target = (
                            el.getAttribute('data-stream') ||
                            el.getAttribute('data-url') ||
                            el.getAttribute('data-link') ||
                            el.getAttribute('data-src') ||
                            el.getAttribute('src') ||
                            el.getAttribute('href') ||
                            ''
                        ).trim();
                        const key = label + '||' + target;
                        if (!seenPairs.has(key)) {
                            seenPairs.add(key);
                            out.push({ label, target });
                        }
                    }
                }
                return out;
            }"""
        )

        for rc in room_candidates:
            raw_lbl = clean_text(rc.get("label", ""))
            raw_tgt = (rc.get("target") or "").strip()
            if raw_tgt:
                full_tgt = urljoin(match_url, raw_tgt)
                if ".m3u8" in full_tgt.lower() or ".mpd" in full_tgt.lower():
                    record_stream_url(full_tgt)
            if not is_junk_label(raw_lbl):
                player_rooms.append((raw_lbl, raw_tgt))

        # Thử click các nút chuyển BLV / link trong khu vực player để kích hoạt request .m3u8/.mpd
        try:
            player_btns = page.locator(
                "[class*='player'] button, [class*='blv'] button, [class*='server'] button, "
                "[class*='stream'] button, [class*='blv'] a, [class*='server'] a"
            )
            btn_count = min(player_btns.count(), 8)
            for idx in range(btn_count):
                b = player_btns.nth(idx)
                if b.closest("header, nav, footer, aside, .menu, .navbar"):
                    continue
                if b.is_visible():
                    txt = clean_text(b.inner_text(timeout=600))
                    if not is_junk_label(txt):
                        try:
                            b.click(timeout=1000)
                            page.wait_for_timeout(600)
                        except Exception:
                            pass
        except Exception:
            pass

        # Quét page.content() và các thẻ <script>
        html_content = page.content()
        for u in extract_stream_urls_from_text(html_content, match_url):
            record_stream_url(u)

        scripts_text = page.evaluate(
            "() => Array.from(document.querySelectorAll('script')).map(s => s.textContent || s.src || '').join('\\n')"
        )
        for u in extract_stream_urls_from_text(scripts_text, match_url):
            record_stream_url(u)

        # Quét toàn bộ page.frames (bao gồm iframe player)
        for frame in page.frames:
            try:
                f_url = frame.url or ""
                record_stream_url(f_url)
                f_html = frame.content()
                for u in extract_stream_urls_from_text(f_html, f_url or match_url):
                    record_stream_url(u)
            except Exception:
                pass

        # Kiểm tra thêm trạng thái LIVE hoặc tỷ số trên trang chi tiết
        body_snip = clean_text(page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 1500) : ''"))
        if re.search(r"\b(trực tiếp|đang đá|hiệp \d|live)\b", body_snip, re.IGNORECASE):
            status = "LIVE"
        if score == "vs":
            m_sc = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b", body_snip)
            if m_sc:
                score = f"{m_sc.group(1)} - {m_sc.group(2)}"

    except Exception:
        pass
    finally:
        try:
            page.close()
        except Exception:
            pass

    # Lọc các nhãn phòng / BLV sạch, tránh trùng nhãn và tránh nhân bản 1 .m3u8 thành nhiều phòng ảo
    clean_labels: list[str] = []
    seen_labels: set[str] = set()
    for raw_lbl, _ in player_rooms:
        lbl = format_room_label(raw_lbl, len(clean_labels) + 1)
        if lbl and lbl.lower() not in seen_labels and not is_junk_label(lbl):
            seen_labels.add(lbl.lower())
            clean_labels.append(lbl)

    streams: list[dict[str, str]] = []
    if captured_streams:
        # Gán mỗi link .m3u8/.mpd thực tế với 1 nhãn sạch; không nhân bản 1 link thành nhiều phòng giống nhau
        for idx, stream_url in enumerate(captured_streams):
            label = clean_labels[idx] if idx < len(clean_labels) else f"BLV {idx + 1}" if idx == 0 else f"Luồng {idx + 1}"
            streams.append({
                "label": label,
                "url": stream_url,
                "referer": match_url,
            })
    else:
        if clean_labels:
            streams.append({
                "label": clean_labels[0],
                "url": match_url,
                "referer": match_url,
            })
        else:
            streams.append({
                "label": "Trực tiếp",
                "url": match_url,
                "referer": match_url,
            })

    return streams, status, score


def run_scraper() -> dict[str, Any]:
    now_vn = datetime.now(TZ)
    updated_at = now_vn.strftime("%H:%M %d/%m/%Y")
    app_title = f"Giờ Vàng TV • {updated_at}"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        page = context.new_page()
        page.set_default_timeout(25000)

        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            auto_expand_and_scroll(page)
            raw_matches = collect_homepage_matches(page)
        finally:
            try:
                page.close()
            except Exception:
                pass

        enriched_matches: list[dict[str, Any]] = []
        for m in raw_matches:
            streams, updated_status, updated_score = extract_detail_streams(
                context=context,
                match_url=m["url"],
                fallback_status=m["status"],
                fallback_score=m["score"],
            )
            enriched_matches.append({
                "league": m["league"],
                "status": updated_status,
                "kickoff": m["kickoff"],
                "home_name": m["home_name"],
                "home_logo": m["home_logo"],
                "away_name": m["away_name"],
                "away_logo": m["away_logo"],
                "score": updated_score,
                "streams": streams,
            })

        context.close()
        browser.close()

    # Sắp xếp đẩy toàn bộ trận LIVE lên đầu
    enriched_matches.sort(
        key=lambda item: (
            0 if item.get("status") == "LIVE" else 1,
            item.get("kickoff") or "99:99",
            item.get("league") or "",
        )
    )

    final_matches: list[dict[str, Any]] = []
    for idx, item in enumerate(enriched_matches, start=1):
        digest = hashlib.md5(f"{idx}:{item['home_name']}:{item['away_name']}".encode("utf-8")).hexdigest()[:6]
        final_matches.append({
            "id": f"match_{idx}_{digest}" if False else f"match_{idx}",
            "league": item["league"],
            "status": item["status"],
            "kickoff": item["kickoff"],
            "home_name": item["home_name"],
            "home_logo": item["home_logo"],
            "away_name": item["away_name"],
            "away_logo": item["away_logo"],
            "score": item["score"],
            "streams": item["streams"],
        })

    payload = {
        "app_title": app_title,
        "updated_at": updated_at,
        "matches": final_matches,
    }

    out_dir = os.path.dirname(os.path.abspath(OUTPUT_FILE))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return payload


if __name__ == "__main__":
    run_scraper()
