import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://giovang.guru"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": BASE_URL + "/",
}

VN_TZ = timezone(timedelta(hours=7))


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_blv_label(raw: str) -> str:
    txt = clean_text(raw)
    if not txt:
        return ""
    if not txt.lower().startswith("blv"):
        return f"BLV {txt}"
    return txt


def extract_img_url(img_tag) -> str:
    if not img_tag:
        return ""
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        val = (img_tag.get(attr) or "").strip()
        if val and not val.startswith("data:image") and "placeholder" not in val.lower():
            return urljoin(BASE_URL, val)
    return ""


def parse_teams_from_slug(url: str):
    m = re.search(r"/truc-tiep-([a-z0-9-]+?)-vs-([a-z0-9-]+?)(?:-\d{2}-\d{2}|-[a-f0-9]{10,}|$)", url)
    if not m:
        return "", ""
    home = m.group(1).replace("-", " ").title()
    away = m.group(2).replace("-", " ").title()
    return home, away


def sanitize_league(raw_league: str, home: str, away: str, blvs: list[str]) -> str:
    league = clean_text(raw_league)
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


def extract_card_blvs(card, home_name: str, away_name: str) -> list[str]:
    """Chỉ lấy tên BLV nằm bên trong thẻ trận hiện tại."""
    blvs = []

    # 1. Tìm trong các thẻ con chuyên chứa tên BLV / commentator
    for el in card.select("[class*='blv'], [class*='commentator'], [class*='caster'], [class*='author']"):
        txt = clean_text(el.get_text())
        if txt and len(txt) <= 25 and txt.lower() not in (home_name.lower(), away_name.lower()):
            label = normalize_blv_label(txt)
            if label not in blvs:
                blvs.append(label)

    # 2. Nếu không có class riêng, bắt mẫu "BLV Tên" trong text của thẻ trận
    if not blvs:
        card_text = clean_text(card.get_text(" "))
        for m in re.findall(r"BLV\s+[A-Za-zÀ-ỹ0-9_]+", card_text):
            label = normalize_blv_label(m)
            if label not in blvs:
                blvs.append(label)

    return blvs


def fetch_stream_details(session: requests.Session, match_url: str, card_blvs: list[str]):
    m3u8_urls = []
    player_blvs = []

    try:
        resp = session.get(match_url, timeout=12)
        if resp.status_code == 200:
            html = resp.text

            # Lấy link .m3u8 trực tiếp của trận này
            for u in re.findall(r"https?://[^\s\"'<>\\]+\.m3u8[^\s\"'<>\\]*", html):
                u = u.replace("\\/", "/")
                if u not in m3u8_urls:
                    m3u8_urls.append(u)

            # Chỉ quét nút chọn server/BLV bên trong vùng player (bỏ qua sidebar trận khác)
            soup = BeautifulSoup(html, "html.parser")
            player_zone = soup.select_one(
                "#player, .player-container, .player-wrap, .server-list, .list-server, .box-player"
            )
            if player_zone:
                for btn in player_zone.select("button, a, [data-link], [data-url], [data-src]"):
                    txt = clean_text(btn.get_text())
                    if txt.lower().startswith("blv") and len(txt) <= 25:
                        label = normalize_blv_label(txt)
                        if label not in player_blvs:
                            player_blvs.append(label)
    except Exception:
        pass

    # Ưu tiên danh sách BLV lấy trực tiếp từ player_zone, nếu không có thì dùng BLV trên thẻ trận
    final_blvs = player_blvs if player_blvs else card_blvs
    if not final_blvs:
        final_blvs = ["BLV Trực Tiếp"]

    streams = []
    if m3u8_urls:
        # Không bao giờ nhân bản 1 link m3u8 thành 3 phòng trùng nhau
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
        # Chưa có link m3u8 trực tiếp thì chỉ giữ đúng số phòng BLV thật của trận
        for label in final_blvs:
            streams.append({
                "label": label,
                "url": match_url,
                "referer": match_url,
            })

    return streams


def scrape_giovang():
    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(BASE_URL, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    seen_urls = set()
    matches = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        full_url = urljoin(BASE_URL, href)

        if "/truc-tiep-" not in full_url:
            continue
        if "keobongvip" in full_url or "soi-keo" in full_url:
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        card = a_tag
        if len(clean_text(a_tag.get_text())) < 10 and a_tag.parent:
            card = a_tag.parent

        team_els = card.select("[class*='team-name'], [class*='name'], .home, .away")
        unique_teams = []
        for el in team_els:
            txt = clean_text(el.get_text())
            if (
                2 <= len(txt) <= 32
                and not re.match(r"^\d{1,2}:\d{2}$", txt)
                and not txt.lower().startswith("blv")
                and txt not in unique_teams
            ):
                unique_teams.append(txt)

        slug_home, slug_away = parse_teams_from_slug(full_url)
        home_name = unique_teams[0] if len(unique_teams) >= 2 else slug_home
        away_name = unique_teams[1] if len(unique_teams) >= 2 else slug_away
        if not home_name or not away_name:
            continue

        imgs = [extract_img_url(img) for img in card.find_all("img") if extract_img_url(img)]
        team_logos = [u for u in imgs if "/icon" not in u.lower() and "/logo-web" not in u.lower()]
        home_logo = team_logos[0] if len(team_logos) >= 1 else ""
        away_logo = team_logos[1] if len(team_logos) >= 2 else ""

        card_blvs = extract_card_blvs(card, home_name, away_name)

        league_el = card.select_one(
            "[class*='league'], [class*='tournament'], [class*='tour'], [class*='champ'], .title-league"
        )
        raw_league = league_el.get_text() if league_el else ""
        league = sanitize_league(raw_league, home_name, away_name, card_blvs)

        card_text = clean_text(card.get_text(" "))
        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", card_text)
        kickoff = time_match.group(1) if time_match else "20:00"

        is_live = bool(re.search(r"\b(LIVE|Đang đá|Hiệp 1|Hiệp 2|HT)\b", card_text, re.IGNORECASE))
        status = "LIVE" if is_live else "SẮP ĐÁ"

        streams = fetch_stream_details(session, full_url, card_blvs)

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

    matches.sort(key=lambda m: 0 if m["status"] == "LIVE" else 1)

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
