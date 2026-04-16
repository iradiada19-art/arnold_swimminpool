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
    raise RuntimeError("Env BOT_TOKEN is required")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

BTN_GET = "get_free_swim"
BTN_EVENING = "get_free_swim_evening"

DAYS = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье"
]

EVENING_FROM_HOUR = 18
MAX_XLS_FILES = 10
MAX_MESSAGE_LEN = 3900
# ------------------------------------------------


def _norm(s: str) -> str:
    s = (s or "").replace("\u00a0", " ").strip()
    s = re.sub(r"(\d{1,2})\.(\d{2})", r"\1:\2", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _http_get(url: str, timeout: int = 30):
    last_error = None
    for _ in range(3):
        try:
            r = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "pool-bot"}
            )
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            time.sleep(2)
    raise RuntimeError(f"Ошибка запроса: {last_error}")


def find_xls_links():
    r = _http_get(PAGE_URL)
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

    uniq = []
    seen = set()

    for t, u in found:
        key = (t, u)
        if key not in seen:
            seen.add(key)
            uniq.append((t, u))

    return uniq[:MAX_XLS_FILES]


def download_xls(url: str):
    r = _http_get(url, timeout=60)
    return r.content


def parse_free_swim_from_xls(xls_bytes: bytes):
    sheets = pd.read_excel(BytesIO(xls_bytes), sheet_name=None, engine="xlrd")

    date_pat = re.compile(r"^\s*(\d{1,2})\s+([А-Яа-я]+)")
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
        day_cols = [
            idx for idx, cell in enumerate(header_dates)
            if date_pat.match(cell)
        ]

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

            col_cells = [
                _norm(x)
                for x in df.iloc[date_row_idx + 1:, col_idx].tolist()
            ]

            mode = None

            for c in col_cells:
                if not c:
                    continue

                low = c.lower()

                if "семейн" in low:
                    mode = "family_skip"
                    continue

                if mode == "family_skip":
                    if ("свободное" in low) or ("санитар" in low):
                        mode = None
                    else:
                        continue

                if "санитарный день" in low:
                    mode = "sanitary_day"

                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, re.IGNORECASE)
                    if m:
                        local[key]["sanitary_day"].append(_norm(m.group(1)))
                    else:
                        local[key]["sanitary_day"].append("весь день")
                    continue

                if "санитар" in low:
                    mode = "sanitary_time"

                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, re.IGNORECASE)
                    if m:
                        local[key]["sanitary_time"].append(_norm(m.group(1)))
                    continue

                if "свободное" in low:
                    mode = "free"

                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, re.IGNORECASE)
                    if m:
                        local[key]["free"].append(_norm(m.group(1)))
                    continue

                if time_pat.search(low) and mode in ("free", "sanitary_time", "sanitary_day"):
                    m = re.search(r"(с\s*\d{1,2}[:.]\d{2}.*)$", c, re.IGNORECASE)
                    if m:
                        local[key][mode].append(_norm(m.group(1)))

            score = sum(
                len(v["free"]) + len(v["sanitary_time"]) + len(v["sanitary_day"])
                for v in local.values()
            )

            if score > best_score:
                best = local
                best_score = score

    return best


def _start_hour_from_time_line(line):
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

    mode_title = (
        "🌆 <b>Вечернее расписание</b>"
        if evening_only
        else "🏊 <b>Расписание свободного плавания</b>"
    )
    parts.append(mode_title)
    parts.append("")

    for title, parsed in files_payload:
        parts.append("━━━━━━━━━━━━━━")
        parts.append(f"📄 <b>{_norm(title)}</b>")
        parts.append("━━━━━━━━━━━━━━")
        parts.append("")

        if not parsed:
            parts.append("⚠️ <i>Не удалось получить данные из файла</i>")
            parts.append("")
            continue

        for day_key, payload in parsed.items():
            free_times = payload.get("free", [])
            sanitary_time = payload.get("sanitary_time", [])
            sanitary_day = payload.get("sanitary_day", [])

            if evening_only:
                free_times = _filter_evening(free_times)
                sanitary_time = _filter_evening(sanitary_time)
                sanitary_day = _filter_evening(sanitary_day)

            parts.append(f"📅 <b>{day_key}</b>")

            if not free_times and not sanitary_time and not sanitary_day:
                parts.append("🧼 <b>Санитарный день</b>")
                parts.append("└ весь день")
                parts.append("")
                continue

            parts.append("🏊 <b>Свободное плавание</b>")
            if free_times:
                for t in free_times:
                    parts.append(f"└ {t}")
            else:
                parts.append("└ нет")

            if sanitary_time:
                parts.append("🧽 <b>Санитарное время</b>")
                for t in sanitary_time:
                    parts.append(f"└ {t}")

            if sanitary_day:
                parts.append("🚫 <b>Санитарный день</b>")
                for t in sanitary_day:
                    parts.append(f"└ {t}")

            parts.append("")

        parts.append("")

    text = "\n".join(parts).strip()

    if len(text) > MAX_MESSAGE_LEN:
        text = text[:MAX_MESSAGE_LEN - 120] + "\n\n⚠️ Часть данных была обрезана, потому что сообщение слишком длинное."

    return text


def keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🏊 Получить расписание", "callback_data": BTN_GET}],
            [{"text": "🌆 Только вечер", "callback_data": BTN_EVENING}]
        ]
    }


def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

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
    tg_send_message(
        chat_id,
        "Привет 🌿\n\nВыбери, какое расписание показать:",
        reply_markup=keyboard()
    )


def handle_button(chat_id, callback_id, evening_only):
    tg_answer_callback(callback_id, "⏳ Загружаю расписание...")

    links = find_xls_links()

    if not links:
        tg_send_message(
            chat_id,
            "⚠️ Не удалось найти файлы с расписанием.",
            reply_markup=keyboard(),
            parse_mode="HTML"
        )
        return

    files_payload = []

    for title, url in links:
        try:
            xls = download_xls(url)
            parsed = parse_free_swim_from_xls(xls)
            files_payload.append((title, parsed))
        except Exception:
            files_payload.append((
                title,
                {
                    "Ошибка": {
                        "free": [],
                        "sanitary_time": [],
                        "sanitary_day": ["Не удалось обработать файл"]
                    }
                }
            ))

    msg = build_message_html_all(files_payload, evening_only)

    tg_send_message(
        chat_id,
        msg,
        reply_markup=keyboard(),
        parse_mode="HTML"
    )


def run_bot():
    offset = 0
    print("Bot running")

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
