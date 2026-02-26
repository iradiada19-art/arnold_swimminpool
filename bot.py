import os
import re
import json
import time
from io import BytesIO
from urllib.parse import urljoin
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from bs4 import BeautifulSoup


# -------------------- CONFIG --------------------
PAGE_URL = "https://www.arnold-premium.ru/raspisanie"
LINK_TEXT_PREFIX = "Расписание работы бассейна"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Env BOT_TOKEN is required. Example: export BOT_TOKEN='123:ABC'")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

BTN_GET = "get_free_swim"
BTN_EVENING = "get_free_swim_evening"

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

EVENING_FROM_HOUR = 18
# ------------------------------------------------


def _norm(s: str) -> str:
    s = (s or "").replace("\u00a0", " ").strip()
    s = re.sub(r"(\d{1,2})\.(\d{2})", r"\1:\2", s)
    s = re.sub(r"\s+", " ", s)
    return s


def find_xls_link() -> tuple[str, str]:
    r = requests.get(PAGE_URL, timeout=30, headers={"User-Agent": "pool-bot/1.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    for a in soup.find_all("a"):
        text = (a.get_text() or "").strip()
        href = a.get("href") or ""
        if text.startswith(LINK_TEXT_PREFIX) and ".xls" in href.lower():
            return text, urljoin(PAGE_URL, href)

    raise RuntimeError("XLS link not found.")


def download_xls(url: str) -> bytes:
    r = requests.get(url, timeout=60, headers={"User-Agent": "pool-bot/1.0"})
    r.raise_for_status()
    return r.content


def parse_free_swim_from_xls(xls_bytes: bytes) -> dict:
    sheets = pd.read_excel(BytesIO(xls_bytes), sheet_name=None, engine="xlrd")

    date_pat = re.compile(r"^\s*(\d{1,2})\s+([А-Яа-яёЁ]+)\s*$")
    time_pat = re.compile(r"\bс\s*\d{1,2}[:.]\d{2}", re.IGNORECASE)

    best = {}
    best_score = -1

    for _, df in sheets.items():
        df = df.fillna("").astype(str)

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
            date_txt = header_dates[col_idx]
            key = f"{day_name} – {date_txt}"
            local[key] = []

            col_cells = [_norm(x) for x in df.iloc[date_row_idx + 1 :, col_idx].tolist()]

            for c in col_cells:
                if not c:
                    continue

                low = c.lower()

                # ❌ Полностью игнорируем семейное посещение
                if "семейное посещение" in low or "семейное" in low:
                    continue

                # если просто текст без времени — не показываем
                if not time_pat.search(low):
                    continue

                m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, flags=re.IGNORECASE)
                if m:
                    t = _norm(m.group(1))
                    t = re.sub(r"\bс\s*(\d)\:", r"с 0\1:", t)
                    local[key].append(t)

            # удаляем дубли
            seen = set()
            cleaned = []
            for t in local[key]:
                if t not in seen:
                    seen.add(t)
                    cleaned.append(t)
            local[key] = cleaned

        score = sum(1 for v in local.values() if v)
        if score > best_score:
            best = local
            best_score = score

    if not best:
        raise RuntimeError("Failed to parse XLS.")

    return best


# -------------------- FILTER PAST DATES --------------------

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _today_warsaw() -> date:
    return datetime.now(ZoneInfo("Europe/Warsaw")).date()


def _parse_ru_day_month(s: str):
    s = _norm(s).lower()
    m = re.match(r"^\s*(\d{1,2})\s+([а-яё]+)\s*$", s)
    if not m:
        return None
    return int(m.group(1)), RU_MONTHS.get(m.group(2))


def _date_from_day_key(day_key: str, today: date):
    if "–" in day_key:
        date_part = day_key.split("–", 1)[1].strip()
    else:
        return None

    dm = _parse_ru_day_month(date_part)
    if not dm:
        return None

    d, mth = dm
    if not mth:
        return None

    candidates = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(y, mth, d))
        except ValueError:
            continue

    if not candidates:
        return None

    return min(candidates, key=lambda dt: abs((dt - today).days))


def filter_out_past_days(free_swim: dict) -> dict:
    today = _today_warsaw()
    out = {}
    for day_key, times in free_swim.items():
        dt = _date_from_day_key(day_key, today)
        if dt is None or dt >= today:
            out[day_key] = times
    return out


# ----------------------------------------------------------


def _start_hour_from_time_line(line: str):
    m = re.search(r"\bс\s*(\d{1,2})\s*[:.]\s*(\d{2})", line, flags=re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def build_message_html(free_swim: dict, evening_only=False):
    free_swim = filter_out_past_days(free_swim)

    if not free_swim:
        return "Нет актуальных дат в расписании."

    parts = []
    for day_key, times in free_swim.items():
        parts.append(f"<b>{day_key}</b>")
        parts.append("свободное плавание")

        filtered = times
        if evening_only:
            filtered = []
            for t in times:
                h = _start_hour_from_time_line(t)
                if h is not None and h >= EVENING_FROM_HOUR:
                    filtered.append(t)

        if filtered:
            parts.extend(filtered)
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


def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    if parse_mode:
        payload["parse_mode"] = parse_mode

    r = requests.post(f"{TG_API}/sendMessage", data=payload, timeout=30)
    r.raise_for_status()


def tg_answer_callback(callback_query_id, text=""):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(f"{TG_API}/answerCallbackQuery", data=payload, timeout=30)


def get_updates(offset):
    r = requests.get(
        f"{TG_API}/getUpdates",
        params={"timeout": 30, "offset": offset},
        timeout=35
    )
    r.raise_for_status()
    return r.json()


def handle_start(chat_id):
    tg_send_message(chat_id, "Нажми кнопку:", reply_markup=keyboard())


def handle_button(chat_id, callback_id, evening_only):
    tg_answer_callback(callback_id, "Скачиваю расписание...")
    _, xls_url = find_xls_link()
    xls_bytes = download_xls(xls_url)
    free_swim = parse_free_swim_from_xls(xls_bytes)
    msg = build_message_html(free_swim, evening_only)
    tg_send_message(chat_id, msg, reply_markup=keyboard(), parse_mode="HTML")


def run_bot():
    offset = 0
    print("Bot is running.")

    while True:
        try:
            data = get_updates(offset)

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                if "message" in upd and "text" in upd["message"]:
                    if upd["message"]["text"].strip() == "/start":
                        handle_start(upd["message"]["chat"]["id"])

                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    chat_id = cq["message"]["chat"]["id"]
                    data_btn = cq.get("data")

                    if data_btn == BTN_GET:
                        handle_button(chat_id, cq["id"], False)
                    elif data_btn == BTN_EVENING:
                        handle_button(chat_id, cq["id"], True)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Loop error:", e)
            time.sleep(3)


if __name__ == "__main__":
    run_bot()
