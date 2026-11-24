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
            if not away
