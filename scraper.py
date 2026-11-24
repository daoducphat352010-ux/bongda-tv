import json
import re
from datetime import datetime, timezone, timedelta
import urllib.request

SOURCE_URLS = [
    "https://gavang33.gg/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def extract_matches():
    matches = []
    seen = set()

    for base_url in SOURCE_URLS:
        try:
            html = fetch_html(base_url)
        except Exception:
            continue

        links = re.findall(r'href=["\']([^"\']*(?:truc-tiep|live|xem-)[^"\']*)["\']', html, re.I)
        for href in links:
            if href.startswith("/"):
                href = base_url.rstrip("/") + href
            if not href.startswith("http") or href in seen:
                continue
            seen.add(href)

            slug = href.rstrip("/").split("/")[-1]
            clean_name = re.sub(r"-[a-zA-Z0-9]{4,}$", "", slug).replace("-", " ").title()
            parts = re.split(r"\s+(?:vs|v)\s+", clean_name, maxsplit=1, flags=re.I)
            home = parts[0].strip() if parts else clean_name
            away = parts[1].strip() if len(parts) > 1 else "Trực Tiếp"

            streams = []
            try:
                match_html = fetch_html(href)
                m3u8_list = list(dict.fromkeys(re.findall(r'https?://[^\s"\'<>\\)]+\.m3u8(?:\?[^\s"\'<>\\)]*)?', match_html, re.I)))
                for idx, m_url in enumerate(m3u8_list[:3], 1):
                    streams.append({
                        "label": f"Server {idx} (Tự động)",
                        "url": m_url.replace("\\/", "/"),
                        "referer": base_url
                    })
            except Exception:
                pass

            if not streams:
                streams.append({
                    "label": "Mở trực tiếp trên Web Player",
                    "url": href,
                    "referer": base_url
                })

            matches.append({
                "id": f"auto_{len(matches) + 1}",
                "league": "Trực Tiếp Hôm Nay",
                "status": "LIVE",
                "kickoff": "LIVE",
                "home_name": home,
                "home_logo": "",
                "away_name": away,
                "away_logo": "",
                "score": "vs",
                "streams": streams
            })

            if len(matches) >= 20:
                break

    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M %d/%m/%Y")
    feed = {
        "app_title": f"Cập nhật tự động lúc {vn_time}",
        "updated_at": vn_time,
        "matches": matches
    }

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    extract_matches()
