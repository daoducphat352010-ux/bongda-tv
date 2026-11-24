import gzip
import json
import re
import ssl
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

BASE_URL = "https://giovang.guru"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": BASE_URL + "/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_blv_label(raw: str) -> str:
    txt = clean_text(raw)
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


class AnchorCardParser(HTMLParser):
    """Tách toàn bộ thẻ <a href="..."> cùng các thẻ con bên trong (img, text, class)."""

    def __init__(self):
        super().__init__()
        self.anchors = []
        self._cur = None
        self._depth = 0
        self._tag_stack = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        if tag == "a" and self._cur is None:
            href = (attr_dict.get("href") or "").strip()
            self._cur = {
                "href": href,
                "texts": [],
                "imgs": [],
                "league_texts": [],
                "blv_texts": [],
                "team_texts": [],
            }
            self._depth = 1
            self._tag_stack = [(tag, attr_dict.get("class", ""))]
            return

        if self._cur is not None:
            self._depth += 1
            cls = attr_dict.get("class", "")
            self._tag_stack.append((tag, cls))
            if tag == "img":
                for k in ("data-src", "data-lazy-src", "data-original", "src"):
                    v = (attr_dict.get(k) or "").strip()
                    if v and not v.startswith("data:image") and "placeholder" not in v.lower():
                        self._cur["imgs"].append(urljoin(BASE_URL, v))
                        break

    def handle_data(self, data):
        if self._cur is None:
            return
        txt = clean_text(data)
        if not txt:
            return
        self._cur["texts"].append(txt)

        all_classes = " ".join(c for _, c in self._tag_stack).lower()
        if any(k in all_classes for k in ("league", "tournament", "tour", "champ")):
            self._cur["league_texts"].append(txt)
        if any(k in all_classes for k in ("blv", "commentator", "caster", "author")):
            self._cur["blv_texts"].append(txt)
        if any(k in all_classes for k in ("team", "home", "away", "name")):
            self._cur["team_texts"].append(txt)

    def handle_endtag(self, tag):
        if self._cur is None:
            return
        if self._tag_stack:
            self._tag_stack.pop()
        self._depth -= 1
        if self._depth <= 0:
            self.anchors.append(self._cur)
            self._cur = None
            self._depth = 0
            self._tag_stack = []


def fetch_stream_details(match_url: str, card_blvs: list) -> list:
    m3u8_urls = []
    try:
        html = fetch_html(match_url, timeout=12)
        for u in re.findall(r"https?://[^\s\"'<>\\]+\.m3u8[^\s\"'<>\\]*", html):
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
    html = fetch_html(BASE_URL, timeout=15)
    parser = AnchorCardParser()
    parser.feed(html)

    seen_urls = set()
    matches = []

    for item in parser.anchors:
        full_url = urljoin(BASE_URL, item["href"])

        if "/truc-tiep-" not in full_url:
            continue
        if "keobongvip" in full_url or "soi-keo" in full_url:
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        card_text = clean_text(" ".join(item["texts"]))

        # 1. Lấy tên 2 đội
        unique_teams = []
        for t in item["team_texts"]:
            if (
                2 <= len(t) <= 32
                and not re.match(r"^\d{1,2}:\d{2}$", t)
                and not t.lower().startswith("blv")
                and t not in unique_teams
            ):
                unique_teams.append(t)

        slug_home, slug_away = parse_teams_from_slug(full_url)
        home_name = unique_teams[0] if len(unique_teams) >= 2 else slug_home
        away_name = unique_teams[1] if len(unique_teams) >= 2 else slug_away
        if not home_name or not away_name:
            continue

        # 2. Lấy logo 2 đội
        team_logos = [
            u for u in item["imgs"]
            if "/icon" not in u.lower() and "/logo-web" not in u.lower()
        ]
        home_logo = team_logos[0] if len(team_logos) >= 1 else ""
        away_logo = team_logos[1] if len(team_logos) >= 2 else ""

        # 3. Lấy đúng BLV của riêng thẻ trận này
        card_blvs = []
        for b in item["blv_texts"]:
            if b.lower() not in (home_name.lower(), away_name.lower()) and len(b) <= 25:
                lbl = normalize_blv_label(b)
                if lbl not in card_blvs:
                    card_blvs.append(lbl)
        if not card_blvs:
            for m in re.findall(r"BLV\s+[A-Za-zÀ-ỹ0-9_]+", card_text):
                lbl = normalize_blv_label(m)
                if lbl not in card_blvs:
                    card_blvs.append(lbl)

        # 4. Lấy tên giải đấu sạch
        raw_league = " ".join(item["league_texts"])
        league = sanitize_league(raw_league, home_name, away_name, card_blvs)

        # 5. Giờ đá & trạng thái LIVE / SẮP ĐÁ
        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", card_text)
        kickoff = time_match.group(1) if time_match else "20:00"
        is_live = bool(re.search(r"\b(LIVE|Đang đá|Hiệp 1|Hiệp 2|HT)\b", card_text, re.IGNORECASE))
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
