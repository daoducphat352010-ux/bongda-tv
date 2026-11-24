import gzip
import html as html_lib
import json
import re
import ssl
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from urllib.request import Request, urlopen

BASE_URL = "https://giovang.guru"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
VN_TZ = timezone(timedelta(hours=7))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

SKIP_PATH_KEYWORDS = (
    "keobongvip", "soi-keo", "nhan-dinh", "tin-tuc", "bang-xep-hang",
    "ket-qua", "lich-thi-dau", "livescore", "highlight", "khuyen-mai",
    "dang-nhap", "dang-ky", "lien-he", "gioi-thieu"
)


def fetch_page(url: str, referer: str = BASE_URL + "/", timeout: int = 18) -> tuple[str, str]:
    headers = dict(HEADERS)
    headers["Referer"] = referer
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        final_url = resp.geturl()
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw.decode("utf-8", errors="ignore"), final_url


def strip_tags(raw_html: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", raw_html or "")
    txt = html_lib.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def normalize_blv(raw: str) -> str:
    txt = strip_tags(raw)
    if not txt:
        return ""
    txt = re.sub(r"^blv[\s:.-]*", "", txt, flags=re.IGNORECASE).strip()
    if not txt or len(txt) > 22:
        return ""
    return f"BLV {txt}"


def parse_teams_from_slug_or_text(url: str, chunk_text: str) -> tuple[str, str]:
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0].strip("/")
    slug = path.split("/")[-1] if "/" in path else path
    slug_clean = re.sub(r"^(?:truc-tiep-|xem-|tran-dau-|match-)", "", slug, flags=re.IGNORECASE)
    slug_clean = re.sub(r"-\d{1,2}-\d{1,2}(?:-\d{2,4})?(?:-[a-f0-9]{6,})?$", "", slug_clean, flags=re.IGNORECASE)
    slug_clean = re.sub(r"-[a-f0-9]{8,}$", "", slug_clean, flags=re.IGNORECASE)

    for sep in ("-vs-", "-voi-", "-"):
        if sep in slug_clean.lower():
            parts = re.split(re.escape(sep), slug_clean, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2 and len(parts[0]) >= 2 and len(parts[1]) >= 2:
                return parts[0].replace("-", " ").title(), parts[1].replace("-", " ").title()

    m_txt = re.search(r"([A-Za-zÀ-ỹ0-9\s.-]{2,28}?)\s+(?:vs|v|-)\s+([A-Za-zÀ-ỹ0-9\s.-]{2,28})", chunk_text, flags=re.IGNORECASE)
    if m_txt:
        return strip_tags(m_txt.group(1)), strip_tags(m_txt.group(2))

    return "", ""


def clean_league_name(raw_league: str, home: str, away: str, blvs: list[str]) -> str:
    league = strip_tags(raw_league)
    stop_words = [home, away, "BLV", "Trực tiếp", "LIVE", "SẮP ĐÁ", "Hôm nay", "Ngày mai"] + [
        b.replace("BLV ", "") for b in blvs
    ]
    for token in stop_words:
        if token and len(token) >= 2:
            idx = league.lower().find(token.lower())
            if idx != -1:
                league = league[:idx].strip(" -|•:·/")
    league = re.sub(r"\b\d{1,2}:\d{2}\b", "", league).strip(" -|•:·/")
    if not league or len(league) > 42:
        return "Bóng Đá Trực Tiếp"
    return league


def extract_team_logos(html_snippet: str, base_url: str) -> tuple[str, str]:
    logos = []
    for u in re.findall(
        r"https?://[^\s\"'<>\\]+/images/teams/[^\s\"'<>\\]+\.(?:png|jpg|jpeg|webp|svg)",
        html_snippet,
        flags=re.IGNORECASE,
    ):
        u = u.replace("\\/", "/")
        if u not in logos:
            logos.append(u)

    if len(logos) < 2:
        for img_tag in re.findall(r"<img\b[^>]*>", html_snippet, flags=re.IGNORECASE):
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                m = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, flags=re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    if (
                        val
                        and not val.startswith("data:image")
                        and "placeholder" not in val.lower()
                        and "/icon" not in val.lower()
                        and "/logo-web" not in val.lower()
                        and "banner" not in val.lower()
                    ):
                        full_img = urljoin(base_url, val)
                        if full_img not in logos:
                            logos.append(full_img)
                        break

    return (logos[0] if len(logos) >= 1 else ""), (logos[1] if len(logos) >= 2 else "")


def inspect_match_page(
    match_url: str,
    home_url: str,
    card_blvs: list[str],
    cur_home_logo: str,
    cur_away_logo: str,
    cur_kickoff: str,
) -> tuple[list[dict], str, str, str]:
    m3u8_urls = []
    page_blvs = []
    home_logo = cur_home_logo
    away_logo = cur_away_logo
    kickoff = cur_kickoff

    try:
        detail_html, _ = fetch_page(match_url, referer=home_url, timeout=12)
        normalized_detail = detail_html.replace("\\/", "/")

        for u in re.findall(r"https?://[^\s\"'<>\\]+\.m3u8[^\s\"'<>\\]*", normalized_detail):
            if u not in m3u8_urls:
                m3u8_urls.append(u)

        if not home_logo or not away_logo:
            d_home, d_away = extract_team_logos(normalized_detail, match_url)
            if not home_logo:
                home_logo = d_home
            if not away_logo:
                away_logo = d_away

        if not kickoff:
            tm = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", strip_tags(detail_html[:15000]))
            if tm:
                kickoff = tm.group(0)

        if not card_blvs:
            for b_txt in re.findall(
                r">(?:\s*BLV\s+([A-Za-zÀ-ỹ0-9_]{1,18})\s*)<",
                detail_html,
                flags=re.IGNORECASE,
            ):
                lbl = normalize_blv(b_txt)
                if lbl and lbl not in page_blvs:
                    page_blvs.append(lbl)
    except Exception:
        pass

    final_blvs = card_blvs if card_blvs else (page_blvs[:1] if page_blvs else ["BLV Trực Tiếp"])
    streams = []

    if m3u8_urls:
        for idx, stream_url in enumerate(m3u8_urls):
            label = final_blvs[idx] if idx < len(final_blvs) else (
                final_blvs[0] if len(m3u8_urls) == 1 else f"Server {idx + 1}"
            )
            streams.append({
                "label": label,
                "url": stream_url,
                "referer": match_url,
            })
    else:
        streams.append({
            "label": final_blvs[0],
            "url": match_url,
            "referer": match_url,
        })

    return streams, home_logo, away_logo, (kickoff or "20:00")


def scrape_giovang():
    raw_html, active_base = fetch_page(BASE_URL, timeout=18)
    print(f"URL thực tế: {active_base} | Dung lượng HTML: {len(raw_html)} bytes")

    searchable_html = raw_html.replace("\\/", "/")

    league_headers = []
    for hm in re.finditer(
        r"<(?:h[1-4]|div|span|p)[^>]*class=[\"'][^\"']*(?:league|tournament|tour-name|title-league|champ)[^\"']*[\"'][^>]*>(.*?)</(?:h[1-4]|div|span|p)>",
        searchable_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        txt = strip_tags(hm.group(1))
        if 2 <= len(txt) <= 45 and "trực tiếp" not in txt.lower():
            league_headers.append((hm.start(), txt))

    seen_urls = set()
    matches = []

    # Quét mọi href nội bộ /truc-tiep-... hoặc đường dẫn trận có chứa -vs-
    url_pattern = re.compile(
        r'href\s*=\s*["\']((?:https?://[^"\']*?)?/(?:truc-tiep-[^"\']+|[^"\']*?-vs-[^"\']+))["\']',
        flags=re.IGNORECASE,
    )

    sample_links = []
    for m in url_pattern.finditer(searchable_html):
        rel_or_abs = m.group(1).strip()
        full_url = urljoin(active_base, rel_or_abs)
        low_url = full_url.lower()

        if any(k in low_url for k in SKIP_PATH_KEYWORDS):
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        if len(sample_links) < 10:
            sample_links.append(full_url)

        start_pos = max(0, m.start() - 500)
        end_pos = min(len(searchable_html), m.end() + 1600)
        chunk = searchable_html[start_pos:end_pos]

        next_link = url_pattern.search(chunk, pos=(m.end() - start_pos))
        if next_link:
            chunk = chunk[: next_link.start()]

        chunk_text = strip_tags(chunk)

        team_candidates = []
        for t_raw in re.findall(
            r"<(?:span|div|p|h\d)[^>]*class=[\"'][^\"']*(?:team|home|away|name)[^\"']*[\"'][^>]*>(.*?)</(?:span|div|p|h\d)>",
            chunk,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            t_clean = strip_tags(t_raw)
            if (
                2 <= len(t_clean) <= 32
                and not re.match(r"^\d{1,2}:\d{2}$", t_clean)
                and not t_clean.lower().startswith("blv")
                and t_clean not in team_candidates
            ):
                team_candidates.append(t_clean)

        slug_home, slug_away = parse_teams_from_slug_or_text(full_url, chunk_text)
        home_name = team_candidates[0] if len(team_candidates) >= 2 else slug_home
        away_name = team_candidates[1] if len(team_candidates) >= 2 else slug_away
        if not home_name or not away_name:
            continue

        home_logo, away_logo = extract_team_logos(chunk, active_base)

        card_blvs = []
        for b_raw in re.findall(
            r"<(?:span|div|p|button)[^>]*class=[\"'][^\"']*(?:blv|commentator|caster)[^\"']*[\"'][^>]*>(.*?)</(?:span|div|p|button)>",
            chunk,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            lbl = normalize_blv(b_raw)
            if lbl and lbl not in card_blvs:
                card_blvs.append(lbl)

        if not card_blvs:
            for b_m in re.findall(r"\bBLV\s+([A-Za-zÀ-ỹ0-9_]{1,18})\b", chunk_text, flags=re.IGNORECASE):
                lbl = normalize_blv(b_m)
                if lbl and lbl not in card_blvs:
                    card_blvs.append(lbl)

        raw_league = ""
        in_card_league = re.search(
            r"<(?:span|div|p)[^>]*class=[\"'][^\"']*(?:league|tournament|tour|champ)[^\"']*[\"'][^>]*>(.*?)</(?:span|div|p)>",
            chunk,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if in_card_league:
            raw_league = in_card_league.group(1)
        else:
            for h_pos, h_txt in reversed(league_headers):
                if h_pos < m.start():
                    raw_league = h_txt
                    break

        league = clean_league_name(raw_league, home_name, away_name, card_blvs)

        time_m = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", chunk_text)
        kickoff = time_m.group(0) if time_m else ""
        is_live = bool(re.search(r"\b(LIVE|Đang đá|Hiệp 1|Hiệp 2|HT)\b", chunk_text, flags=re.IGNORECASE))
        status = "LIVE" if is_live else "SẮP ĐÁ"

        streams, home_logo, away_logo, kickoff = inspect_match_page(
            full_url, active_base, card_blvs, home_logo, away_logo, kickoff
        )

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

    matches.sort(key=lambda item: 0 if item["status"] == "LIVE" else 1)

    now_str = datetime.now(VN_TZ).strftime("%H:%M %d/%m/%Y")
    output = {
        "app_title": f"GioVang • {now_str}",
        "updated_at": now_str,
        "matches": matches,
    }

    for fname in ("giovang_feed.json", "giovang.json"):
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Đã cập nhật {len(matches)} trận đấu sạch từ GioVang.")
    if not matches and sample_links:
        print("Link mẫu bắt được:", sample_links)


if __name__ == "__main__":
    scrape_giovang()
