import gzip
import html as html_lib
import json
import re
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from urllib.request import Request, urlopen

BASE_URL = "https://giovang.guru"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": BASE_URL + "/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
}
VN_TZ = timezone(timedelta(hours=7))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def fetch_html(url: str, timeout: int = 15) -> str:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="ignore")


def strip_tags(raw_html: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", raw_html or "")
    txt = html_lib.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def normalize_blv_label(raw: str) -> str:
    txt = strip_tags(raw)
    if not txt:
        return ""
    if not txt.lower().startswith("blv"):
        return f"BLV {txt}"
    return txt


def parse_teams_from_slug(url: str):
    m = re.search(
        r"/truc-tiep-([a-z0-9-]+?)-vs-([a-z0-9-]+?)(?:-\d{2}-\d{2}|-[a-f0-9]{10,}|$)",
        url,
    )
    if not m:
        return "", ""
    home = m.group(1).replace("-", " ").title()
    away = m.group(2).replace("-", " ").title()
    return home, away


def sanitize_league(raw_league: str, home: str, away: str, blvs: list) -> str:
    league = strip_tags(raw_league)
    stop_tokens = [home, away, "BLV", "Trực tiếp", "LIVE", "SẮP ĐÁ"] + [
        b.replace("BLV ", "") for b in blvs
    ]
    for token in stop_tokens:
        if token and len(token) >= 2 and token.lower() in league.lower():
            idx = league.lower().find(token.lower())
            league = league[:idx].strip(" -|•:")
    if len(league) > 40 or not league:
        return "Bóng Đá Trực Tiếp"
    return league


def extract_logos_from_block(block_html: str) -> tuple[str, str]:
    logos = []
    for img_tag in re.findall(r"<img\b[^>]*>", block_html, flags=re.IGNORECASE):
        src = ""
        for attr in ("data-src", "data-lazy-src", "data-original", "src"):
            m = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, flags=re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if val and not val.startswith("data:image") and "placeholder" not in val.lower():
                    src = urljoin(BASE_URL, val)
                    break
        if src and "/icon" not in src.lower() and "/logo-web" not in src.lower() and src not in logos:
            logos.append(src)
    return (logos[0] if len(logos) >= 1 else ""), (logos[1] if len(logos) >= 2 else "")


def fetch_stream_details(match_url: str, card_blvs: list) -> list:
    m3u8_urls = []
    try:
        detail_html = fetch_html(match_url, timeout=12)
        for u in re.findall(r"https?://[^\s\"'<>\\]+\.m3u8[^\s\"'<>\\]*", detail_html):
            u = u.replace("\\/", "/")
            if u not in m3u8_urls:
                m3u8_urls.append(u)
    except Exception:
        pass

    final_blvs = card_blvs if card_blvs else ["BLV Trực Tiếp"]
    streams = []

    if m3u8_urls:
        if len(m3u8_urls) == 1:
            streams.append({
                "label": final_blvs[0],
                "url": m3u8_urls[0],
                "referer": match_url,
            })
        else:
            for idx, stream_url in enumerate(m3u8_urls):
                label = final_blvs[idx] if idx < len(final_blvs) else f"Server {idx + 1}"
                streams.append({
                    "label": label,
                    "url": stream_url,
                    "referer": match_url,
                })
    else:
        for label in final_blvs:
            streams.append({
                "label": label,
                "url": match_url,
                "referer": match_url,
            })

    return streams


def scrape_giovang():
    page_html = fetch_html(BASE_URL, timeout=15)
    print(f"Đã tải HTML trang chủ: {len(page_html)} bytes")

    if "Just a moment..." in page_html or "cf-browser-verification" in page_html:
        print("CẢNH BÁO: Trang đang bật Cloudflare chống bot trên IP GitHub Actions.")

    seen_urls = set()
    matches = []

    # Tìm tất cả vị trí chứa link /truc-tiep-... (bỏ qua keobongvip / soi-keo)
    for m in re.finditer(r'href\s*=\s*["\']([^"\']*?/truc-tiep-[^"\']+)["\']', page_html, flags=re.IGNORECASE):
        raw_href = m.group(1).strip()
        full_url = urljoin(BASE_URL, raw_href)

        if "keobongvip" in full_url or "soi-keo" in full_url:
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # Lấy vùng HTML bao quanh thẻ trận đấu (trước 600 ký tự, sau 1800 ký tự)
        start_pos = max(0, m.start() - 600)
        end_pos = min(len(page_html), m.end() + 1800)
        chunk = page_html[start_pos:end_pos]

        # Cắt gọn chunk trong phạm vi 1 thẻ trận (không tràn sang link trận kế tiếp)
        next_match = re.search(r'href\s*=\s*["\'][^"\']*?/truc-tiep-', chunk[m.end() - start_pos:], flags=re.IGNORECASE)
        if next_match:
            chunk = chunk[: (m.end() - start_pos) + next_match.start()]

        slug_home, slug_away = parse_teams_from_slug(full_url)
        if not slug_home or not slug_away:
            continue

        # Lấy tên 2 đội từ thẻ HTML nếu có, fallback về tên chuẩn từ slug URL
        team_candidates = []
        for t_html in re.findall(
            r'<(?:span|div|p|h\d)[^>]*class=["\'][^"\']*(?:team|name|home|away)[^"\']*["\'][^>]*>(.*?)</(?:span|div|p|h\d)>',
            chunk,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            t_clean = strip_tags(t_html)
            if (
                2 <= len(t_clean) <= 32
                and not re.match(r"^\d{1,2}:\d{2}$", t_clean)
                and not t_clean.lower().startswith("blv")
                and t_clean not in team_candidates
            ):
                team_candidates.append(t_clean)

        home_name = team_candidates[0] if len(team_candidates) >= 2 else slug_home
        away_name = team_candidates[1] if len(team_candidates) >= 2 else slug_away

        home_logo, away_logo = extract_logos_from_block(chunk)

        chunk_text = strip_tags(chunk)

        # Lấy đúng BLV của riêng thẻ trận này
        card_blvs = []
        for b_match in re.findall(r"BLV\s+[A-Za-zÀ-ỹ0-9_]+", chunk_text):
            lbl = normalize_blv_label(b_match)
            if lbl and lbl not in card_blvs:
                card_blvs.append(lbl)

        # Lấy tên giải đấu cục bộ
        league_m = re.search(
            r'<(?:span|div|p)[^>]*class=["\'][^"\']*(?:league|tournament|tour|champ)[^"\']*["\'][^>]*>(.*?)</(?:span|div|p)>',
            chunk,
            flags=re.IGNORECASE | re.DOTALL,
        )
        raw_league = league_m.group(1) if league_m else ""
        league = sanitize_league(raw_league, home_name, away_name, card_blvs)

        # Giờ thi đấu & trạng thái LIVE / SẮP ĐÁ
        time_m = re.search(r"\b(\d{1,2}:\d{2})\b", chunk_text)
        kickoff = time_m.group(1) if time_m else "20:00"
        is_live = bool(re.search(r"\b(LIVE|Đang đá|Hiệp 1|Hiệp 2|HT)\b", chunk_text, flags=re.IGNORECASE))
        status = "LIVE" if is_live else "SẮP ĐÁ"

        streams = fetch_stream_details(full_url, card_blvs)

        matches.append({
            "id": f"gv_{len(matches) + 1}",
            "league": league,
            "status": status,
            "kickoff": kickoff,
            "home_name": home_name,
            "home_logo": home_logo,
            "away_name": away_name,
            "away_logo": away_logo,
            "score": "vs",
            "streams": streams,
        })

    matches.sort(key=lambda x: 0 if x["status"] == "LIVE" else 1)

    now_str = datetime.now(VN_TZ).strftime("%H:%M %d/%m/%Y")
    output = {
        "app_title": f"GioVang • {now_str}",
        "updated_at": now_str,
        "matches": matches,
    }

    with open("giovang.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Đã cập nhật {len(matches)} trận đấu sạch từ GioVang.")


if __name__ == "__main__":
    scrape_giovang()
