import os
import re
import json
import time
from io import BytesIO
from urllib.parse import urljoin

import requests
import pandas as pd
from bs4 import BeautifulSoup


# -------------------- CONFIG --------------------
PAGE_URL = "https://www.arnold-premium.ru/raspisanie"
LINK_TEXT_PREFIX = "Расписание работы бассейна"

# Telegram token must be in env:
#   BOT_TOKEN=123456:ABCDEF...
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Env BOT_TOKEN is required. Example: export BOT_TOKEN='123:ABC'")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Buttons
BTN_GET = "get_free_swim"
BTN_EVENING = "get_free_swim_evening"

# Days order in the table (usually Mon..Sun)
DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

# Evening start hour
EVENING_FROM_HOUR = 18
# ------------------------------------------------


def _norm(s: str) -> str:
    s = (s or "").replace("\u00a0", " ").strip()
    s = re.sub(r"(\d{1,2})\.(\d{2})", r"\1:\2", s)  # 08.00 -> 08:00
    s = re.sub(r"\s+", " ", s)
    return s


def find_xls_link() -> tuple[str, str]:
    """
    Finds the XLS link where anchor text starts with LINK_TEXT_PREFIX.
    Returns (title_text, absolute_url).
    """
    r = requests.get(PAGE_URL, timeout=30, headers={"User-Agent": "pool-bot/1.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Strict: text + endswith .xls
    for a in soup.find_all("a"):
        text = (a.get_text() or "").strip()
        href = a.get("href") or ""
        if text.startswith(LINK_TEXT_PREFIX) and href.lower().endswith(".xls"):
            return text, urljoin(PAGE_URL, href)

    # Soft: text + contains .xls (query params possible)
    for a in soup.find_all("a"):
        text = (a.get_text() or "").strip()
        href = a.get("href") or ""
        if text.startswith(LINK_TEXT_PREFIX) and ".xls" in href.lower():
            return text, urljoin(PAGE_URL, href)

    raise RuntimeError(f"XLS link not found. Expected link text starting with: {LINK_TEXT_PREFIX}")


def download_xls(url: str) -> bytes:
    r = requests.get(url, timeout=60, headers={"User-Agent": "pool-bot/1.0"})
    r.raise_for_status()
    return r.content


def parse_free_swim_from_xls(xls_bytes: bytes) -> dict:
    """
    Returns dict:
      {
        "Понедельник – 16 февраля": {"free": [...], "sanitary_time": [...], "sanitary_day": [...]},
        ...
      }

    Strategy:
    - read all sheets
    - find row with dates like "16 февраля", "17 февраля"...
    - for each day column:
        - collect free swim times
        - collect sanitary time (санитарное время)
        - collect sanitary day (санитарный день)
    - ignore 'семейное'
    """
    sheets = pd.read_excel(BytesIO(xls_bytes), sheet_name=None, engine="xlrd")

    date_pat = re.compile(r"^\s*(\d{1,2})\s+([А-Яа-я]+)\s*$")
    time_pat = re.compile(r"\bс\s*\d{1,2}[:.]\d{2}", re.IGNORECASE)

    best = {}
    best_score = -1

    for _, df in sheets.items():
        df = df.fillna("").astype(str)

        # Find the header row with dates
        date_row_idx = None
        for i in range(min(len(df), 60)):
            row = [_norm(x) for x in df.iloc[i].tolist()]
            hits = sum(1 for x in row if date_pat.match(x))
            if hits >= 3:
                date_row_idx = i
                break

        if date_row_idx is None:
            continue

        header_dates = [_norm(x) for x in df.iloc[date_row_idx].tolist()]
        day_cols = [idx for idx, cell in enumerate(header_dates) if date_pat.match(cell)]
        if not day_cols:
            continue

        local = {}
        for j, col_idx in enumerate(day_cols[:7]):
            day_name = DAYS[j]
            date_txt = header_dates[col_idx]  # e.g. "16 февраля"
            key = f"{day_name} – {date_txt}"

            local[key] = {"free": [], "sanitary_time": [], "sanitary_day": []}

            col_cells = [_norm(x) for x in df.iloc[date_row_idx + 1 :, col_idx].tolist()]

            mode = None  # None / "free" / "sanitary_time" / "sanitary_day"

            for c in col_cells:
                if not c:
                    continue

                low = c.lower()

                # skip family visits
                if "семейное" in low:
                    continue

                # Switch modes by markers
                if "санитарный день" in low:
                    mode = "sanitary_day"
                    # may contain time ranges in the same cell
                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, flags=re.IGNORECASE)
                    if m:
                        local[key]["sanitary_day"].append(_norm(m.group(1)))
                    continue

                # "санитарное время" marker (covers also "санитарное")
                if "санитар" in low:
                    mode = "sanitary_time"
                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, flags=re.IGNORECASE)
                    if m:
                        local[key]["sanitary_time"].append(_norm(m.group(1)))
                    continue

                if "свободное" in low:
                    mode = "free"
                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, flags=re.IGNORECASE)
                    if m:
                        local[key]["free"].append(_norm(m.group(1)))
                    continue

                # Time lines: attach to the current mode
                if time_pat.search(low) and mode in ("free", "sanitary_time", "sanitary_day"):
                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, flags=re.IGNORECASE)
                    if m:
                        t = _norm(m.group(1))
                        t = re.sub(r"\bс\s*(\d)\:", r"с 0\1:", t)  # 8:00 -> 08:00
                        local[key][mode].append(t)

            # Deduplicate preserving order
            for k in ("free", "sanitary_time", "sanitary_day"):
                seen = set()
                cleaned = []
                for t in local[key][k]:
                    if t not in seen:
                        seen.add(t)
                        cleaned.append(t)
                local[key][k] = cleaned

        score = sum(1 for v in local.values() if v["free"] or v["sanitary_time"] or v["sanitary_day"])
        if score > best_score:
            best = local
            best_score = score

    if not best:
        raise RuntimeError("Failed to parse XLS (no recognizable date/time layout).")

    return best


def _start_hour_from_time_line(line: str) -> int | None:
    """
    Extract start hour from string like 'с 20:15 до 22:45' -> 20
    """
    m = re.search(r"\bс\s*(\d{1,2})\s*[:.]\s*(\d{2})", line, flags=re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def _filter_evening(times: list[str]) -> list[str]:
    out = []
    for t in times:
        h = _start_hour_from_time_line(t)
        if h is not None and h >= EVENING_FROM_HOUR:
            out.append(t)
    return out


def build_message_html(free_swim: dict, evening_only: bool = False) -> str:
    """
    Output format:
    <b>Понедельник – 16 февраля</b>
    свободное плавание
    ...
    санитарное время
    ...
    санитарный день
    ...
    """
    parts = []
    for day_key, payload in free_swim.items():
        parts.append(f"<b>{day_key}</b>")

        free_times = payload.get("free", [])
        sanitary_time = payload.get("sanitary_time", [])
        sanitary_day = payload.get("sanitary_day", [])

        if evening_only:
            free_times = _filter_evening(free_times)
            sanitary_time = _filter_evening(sanitary_time)
            sanitary_day = _filter_evening(sanitary_day)

        # Free swim
        parts.append("свободное плавание")
        if free_times:
            parts.extend(free_times)
        else:
            parts.append("нет данных")

        # Sanitary time
        parts.append("санитарное время")
        if sanitary_time:
            parts.extend(sanitary_time)
        else:
            parts.append("нет данных")

        # Sanitary day
        parts.append("санитарный день")
        if sanitary_day:
            parts.extend(sanitary_day)
        else:
            parts.append("нет данных")

        parts.append("")

    return "\n".join(parts).strip()


def keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Получить расписание (свободное плавание)", "callback_data": BTN_GET}],
            [{"text": "Только вечер", "callback_data": BTN_EVENING}],
        ]
    }


def tg_send_message(chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    if parse_mode:
        payload["parse_mode"] = parse_mode

    r = requests.post(f"{TG_API}/sendMessage", data=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def tg_answer_callback(callback_query_id: str, text: str = ""):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text

    r = requests.post(f"{TG_API}/answerCallbackQuery", data=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def get_updates(offset: int) -> dict:
    r = requests.get(
        f"{TG_API}/getUpdates",
        params={"timeout": 30, "offset": offset},
        timeout=35
    )
    r.raise_for_status()
    return r.json()


def handle_start(chat_id: int):
    tg_send_message(chat_id, "Нажми кнопку, чтобы получить расписание:", reply_markup=keyboard())


def handle_button(chat_id: int, callback_id: str, evening_only: bool):
    tg_answer_callback(callback_id, "Скачиваю расписание...")

    title, xls_url = find_xls_link()
    xls_bytes = download_xls(xls_url)
    parsed = parse_free_swim_from_xls(xls_bytes)

    msg = build_message_html(parsed, evening_only=evening_only)

    tg_send_message(chat_id, msg, reply_markup=keyboard(), parse_mode="HTML")


def run_bot():
    offset = 0
    print("Bot is running. Press Ctrl+C to stop.")

    while True:
        try:
            data = get_updates(offset)

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                # messages
                if "message" in upd and "text" in upd["message"]:
                    chat_id = upd["message"]["chat"]["id"]
                    text = upd["message"]["text"].strip()

                    if text == "/start":
                        handle_start(chat_id)

                # callback buttons
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    cq_id = cq["id"]
                    chat_id = cq["message"]["chat"]["id"]
                    data_btn = cq.get("data")

                    try:
                        if data_btn == BTN_GET:
                            handle_button(chat_id, cq_id, evening_only=False)
                        elif data_btn == BTN_EVENING:
                            handle_button(chat_id, cq_id, evening_only=True)
                        else:
                            tg_answer_callback(cq_id)
                    except Exception as e:
                        tg_send_message(chat_id, f"Ошибка: {e}", reply_markup=keyboard())

        except KeyboardInterrupt:
            print("Stopping...")
            break
        except Exception as e:
            # keep alive on temporary network issues
            print("Loop error:", e)
            time.sleep(3)


if __name__ == "__main__":
    run_bot()
