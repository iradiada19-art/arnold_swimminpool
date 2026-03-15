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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Env BOT_TOKEN is required.")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

BTN_GET = "get_free_swim"
BTN_EVENING = "get_free_swim_evening"

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

EVENING_FROM_HOUR = 18
MAX_XLS_FILES = 10
# ------------------------------------------------


def _norm(s: str) -> str:
    s = (s or "").replace("\u00a0", " ").strip()
    s = re.sub(r"(\d{1,2})\.(\d{2})", r"\1:\2", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _http_get(url: str, timeout: int = 30) -> requests.Response:
    return requests.get(url, timeout=timeout, headers={"User-Agent": "pool-bot/1.1"})


def find_xls_links() -> list[tuple[str, str]]:
    r = _http_get(PAGE_URL)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    found = []

    for a in soup.find_all("a"):
        text = (a.get_text() or "").strip()
        href = (a.get("href") or "").strip()

        if not text.startswith(LINK_TEXT_PREFIX):
            continue

        if ".xls" not in href.lower():
            continue

        abs_url = urljoin(PAGE_URL, href)
        found.append((text, abs_url))

    seen = set()
    uniq = []

    for t, u in found:
        if (t, u) not in seen:
            seen.add((t, u))
            uniq.append((t, u))

    return uniq[:MAX_XLS_FILES]


def download_xls(url: str) -> bytes:
    r = _http_get(url, timeout=60)
    r.raise_for_status()
    return r.content


def parse_free_swim_from_xls(xls_bytes: bytes) -> dict:

    sheets = pd.read_excel(BytesIO(xls_bytes), sheet_name=None, engine="xlrd")

    date_pat = re.compile(r"^\s*(\d{1,2})\s+([А-Яа-я]+)\s*$")
    time_pat = re.compile(r"\bс\s*\d{1,2}[:.]\d{2}", re.IGNORECASE)

    best = {}
    best_score = -1

    for _, df in sheets.items():

        df = df.fillna("").astype(str)

        date_row_idx = None

        for i in range(min(len(df), 80)):
            row = [_norm(x) for x in df.iloc[i].tolist()]
            hits = sum(1 for x in row if date_pat.match(x))

            if hits >= 3:
                date_row_idx = i
                break

        if date_row_idx is None:
            continue

        header_dates = [_norm(x) for x in df.iloc[date_row_idx].tolist()]
        day_cols = [idx for idx, cell in enumerate(header_dates) if date_pat.match(cell)]

        local = {}

        for j, col_idx in enumerate(day_cols[:7]):

            day_name = DAYS[j]
            date_txt = header_dates[col_idx]

            key = f"{day_name} – {date_txt}"

            local[key] = {
                "free": [],
                "sanitary_time": [],
                "sanitary_day": []
            }

            col_cells = [_norm(x) for x in df.iloc[date_row_idx + 1:, col_idx].tolist()]

            mode = None

            for c in col_cells:

                if not c:
                    continue

                low = c.lower()

                # --- семейное плавание ---
                if "семейн" in low:
                    mode = "family_skip"
                    continue

                if mode == "family_skip":

                    if ("свободное" in low) or ("санитар" in low):
                        mode = None
                    else:
                        continue

                # --- санитарный день ---
                if "санитарный день" in low:

                    mode = "sanitary_day"

                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, re.IGNORECASE)

                    if m:
                        local[key]["sanitary_day"].append(_norm(m.group(1)))
                    else:
                        local[key]["sanitary_day"].append("весь день")

                    continue

                # --- санитарное время ---
                if "санитар" in low:

                    mode = "sanitary_time"

                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, re.IGNORECASE)

                    if m:
                        local[key]["sanitary_time"].append(_norm(m.group(1)))

                    continue

                # --- свободное ---
                if "свободное" in low:

                    mode = "free"

                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, re.IGNORECASE)

                    if m:
                        local[key]["free"].append(_norm(m.group(1)))

                    continue

                # --- строки времени ---
                if time_pat.search(low) and mode in ("free", "sanitary_time", "sanitary_day"):

                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, re.IGNORECASE)

                    if m:
                        t = _norm(m.group(1))
                        t = re.sub(r"\bс\s*(\d)\:", r"с 0\1:", t)

                        local[key][mode].append(t)

            # удаляем дубликаты

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

    return best


def _start_hour_from_time_line(line: str):

    m = re.search(r"\bс\s*(\d{1,2})[:.](\d{2})", line)

    if not m:
        return None

    return int(m.group(1))


def _filter_evening(times):

    out = []

    for t in times:

        h = _start_hour_from_time_line(t)

        if h is not None and h >= EVENING_FROM_HOUR:
            out.append(t)

    return out


def build_message_html_all(files_payload, evening_only=False):

    parts = []

    for title, parsed in files_payload:

        parts.append(f"<b>{_norm(title)}</b>")
        parts.append("")

        for day_key, payload in parsed.items():

            parts.append(f"<b>{day_key}</b>")

            free_times = payload.get("free", [])
            sanitary_time = payload.get("sanitary_time", [])
            sanitary_day = payload.get("sanitary_day", [])

            if evening_only:

                free_times = _filter_evening(free_times)
                sanitary_time = _filter_evening(sanitary_time)
                sanitary_day = _filter_evening(sanitary_day)

            parts.append("свободное плавание")

            if free_times:
                parts.extend(free_times)
            else:
                parts.append("нет данных")

            if sanitary_time:

                parts.append("санитарное время")
                parts.extend(sanitary_time)

            if sanitary_day:

                parts.append("санитарный день")
                parts.extend(sanitary_day)

            parts.append("")

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

    return r.json()


def tg_answer_callback(callback_query_id, text=""):

    payload = {"callback_query_id": callback_query_id}

    if text:
        payload["text"] = text

    r = requests.post(f"{TG_API}/answerCallbackQuery", data=payload, timeout=30)

    r.raise_for_status()

    return r.json()


def get_updates(offset):

    r = requests.get(
        f"{TG_API}/getUpdates",
        params={"timeout": 30, "offset": offset},
        timeout=35
    )

    r.raise_for_status()

    return r.json()


def handle_start(chat_id):

    tg_send_message(chat_id, "Нажми кнопку, чтобы получить расписание:", reply_markup=keyboard())


def handle_button(chat_id, callback_id, evening_only):

    tg_answer_callback(callback_id, "Скачиваю расписание...")

    links = find_xls_links()

    files_payload = []

    for title, xls_url in links:

        try:

            xls_bytes = download_xls(xls_url)

            parsed = parse_free_swim_from_xls(xls_bytes)

            files_payload.append((title, parsed))

        except Exception as e:

            files_payload.append((title, {"": {"free": [f"Ошибка: {e}"], "sanitary_time": [], "sanitary_day": []}}))

    msg = build_message_html_all(files_payload, evening_only)

    tg_send_message(chat_id, msg, reply_markup=keyboard(), parse_mode="HTML")


def run_bot():

    offset = 0

    print("Bot started")

    while True:

        try:

            data = get_updates(offset)

            for upd in data.get("result", []):

                offset = upd["update_id"] + 1

                if "message" in upd and "text" in upd["message"]:

                    chat_id = upd["message"]["chat"]["id"]

                    text = upd["message"]["text"]

                    if text == "/start":
                        handle_start(chat_id)

                if "callback_query" in upd:

                    cq = upd["callback_query"]

                    cq_id = cq["id"]

                    chat_id = cq["message"]["chat"]["id"]

                    data_btn = cq.get("data")

                    if data_btn == BTN_GET:
                        handle_button(chat_id, cq_id, False)

                    elif data_btn == BTN_EVENING:
                        handle_button(chat_id, cq_id, True)

                    else:
                        tg_answer_callback(cq_id)

        except Exception as e:

            print("Error:", e)

            time.sleep(3)


if __name__ == "__main__":
    run_bot()
