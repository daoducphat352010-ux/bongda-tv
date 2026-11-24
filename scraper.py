import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Bạn có thể thêm các tên miền dự phòng khác của trang bóng đá vào danh sách này
SOURCE_URLS = [
    "https://gavang33.gg/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}


def fetch_html(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def extract_matches():
    matches = []
    seen = set()
    errors = []

    for base_url in SOURCE_URLS:
        try:
            html = fetch_html(base_url)
        except urllib.error.HTTPError as e:
            errors.append(f"{base_url} bị chặn (HTTP {e.code})")
            continue
        except Exception as e:
            errors.append(f"{base_url} lỗi kết nối ({type(e).__name__})")
            continue

        # Quét rộng tất cả các đường dẫn trận đấu trên trang
        raw_links = re.findall(r'(?:href|data-href)=["\']([^"\']+)["\']', html, re.I)
        for href in raw_links:
            href = href.strip()
            if href.startswith("/"):
                href = base_url.rstrip("/") + href
            if not href.startswith("http") or href in seen:
                continue

            lower = href.lower()
            if not any(k in lower for k in ("truc-tiep", "xem-bong-da", "xem-truc-tiep", "/live")):
                continue

            seen.add(href)

            # Tách tên 2 đội từ đường link
            slug = href.rstrip("/").split("/")[-1]
            clean_name = re.sub(r"-[a-zA-Z0-9]{4,}$", "", slug).replace("-", " ").title()
            parts = re.split(r"\s+(?:vs|v)\s+", clean_name, maxsplit=1, flags=re.I)
            home = parts[0].strip() if parts and parts[0].strip() else clean_name
            away = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Trực Tiếp"

            streams = []
            try:
                match_html = fetch_html(href)
                m3u8_list = list(
                    dict.fromkeys(
                        re.findall(
                            r'https?://[^\s"\'<>\\)]+\.(?:m3u8|mpd)(?:\?[^\s"\'<>\\)]*)?',
                            match_html,
                            re.I,
                        )
                    )
                )
                for idx, m_url in enumerate(m3u8_list[:3], 1):
                    streams.append(
                        {
                            "label": f"Server {idx} (HD Tự động)",
                            "url": m_url.replace("\\/", "/"),
                            "referer": base_url,
                        }
                    )
            except Exception:
                pass

            if not streams:
                streams.append(
                    {
                        "label": "Mở trực tiếp trên Web Player",
                        "url": href,
                        "referer": base_url,
                    }
                )

            matches.append(
                {
                    "id": f"auto_{len(matches) + 1}",
                    "league": "Bóng Đá Trực Tiếp",
                    "status": "LIVE",
                    "kickoff": "LIVE",
                    "home_name": home,
                    "home_logo": "",
                    "away_name": away,
                    "away_logo": "",
                    "score": "vs",
                    "streams": streams,
                }
            )

            if len(matches) >= 24:
                break

    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M %d/%m/%Y")

    # Nếu trang nguồn chặn server GitHub hoặc chưa có trận, tạo thẻ thông báo kèm kênh test để app không bị trống
    if not matches:
        reason = "; ".join(errors) if errors else "Trang web không trả về danh sách trận tĩnh"
        matches.append(
            {
                "id": "status_fallback",
                "league": "Kiểm tra kết nối",
                "status": "LIVE",
                "kickoff": vn_time,
                "home_name": "Link đã hoạt động",
                "home_logo": "",
                "away_name": reason,
                "away_logo": "",
                "score": "OK",
                "streams": [
                    {
                        "label": "Kênh Video Thử Nghiệm (Bấm để test TV)",
                        "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
                        "referer": "",
                    },
                    {
                        "label": "Mở trực tiếp trang GaVang33 trên TV",
                        "url": SOURCE_URLS[0],
                        "referer": SOURCE_URLS[0],
                    },
                ],
            }
        )

    feed = {
        "app_title": f"Cập nhật lúc {vn_time}",
        "updated_at": vn_time,
        "matches": matches,
    }

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    extract_matches()
