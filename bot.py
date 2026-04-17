import os
import re
import json
import time
from io import BytesIO
from urllib.parse import urljoin
from datetime import datetime, date

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
BTN_SUBSCRIBE = "subscribe_auto"
BTN_DAILY_TEST = "subscribe_daily_test"

DAYS = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье"
]

MAX_XLS_FILES = 10
MAX_MESSAGE_LEN = 3900

WEEKLY_SUBSCRIBERS_FILE = "weekly_subscribers.json"
DAILY_TEST_SUBSCRIBERS_FILE = "daily_test_subscribers.json"

AUTO_SEND_HOUR = 10
AUTO_SEND_MINUTE = 0

TEST_SEND_HOUR = 17
TEST_SEND_MINUTE = 0
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


def _escape_html(text: str) -> str:
    text = str(text or "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _extract_date_range_from_title(title: str) -> str:
    title = _norm(title)

    patterns = [
        r"(\d{1,2}\.\d{1,2}\.\d{2,4})\s*[-–]\s*(\d{1,2}\.\d{1,2}\.\d{2,4})",
        r"(\d{1,2}\.\d{1,2})\s*[-–]\s*(\d{1,2}\.\d{1,2})",
        r"(\d{1,2}\s+[А-Яа-яA-Za-z]+)\s*[-–]\s*(\d{1,2}\s+[А-Яа-яA-Za-z]+)",
    ]

    for pat in patterns:
        m = re.search(pat, title)
        if m:
            return f"{m.group(1)} – {m.group(2)}"

    cleaned = title.replace(LINK_TEXT_PREFIX, "").strip(" -–—")
    return cleaned or title


def load_ids(path):
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return []

        result = []
        for x in data:
            try:
                result.append(int(x))
            except Exception:
                pass

        return sorted(list(set(result)))
    except Exception:
        return []


def save_ids(path, ids_list):
    unique_ids = sorted(list(set(int(x) for x in ids_list)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(unique_ids, f, ensure_ascii=False, indent=2)


def add_id(path, chat_id):
    ids_list = load_ids(path)
    if int(chat_id) not in ids_list:
        ids_list.append(int(chat_id))
        save_ids(path, ids_list)
        return True
    return False


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


def _parse_day_month_year(text):
    text = _norm(text).lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)

    month_map = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }

    m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = m.group(3)

        if year:
            year = int(year)
            if year < 100:
                year += 2000
        else:
            year = datetime.now().year

        try:
            return date(year, month, day)
        except Exception:
            return None

    m = re.search(r"(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?", text)
    if m:
        day = int(m.group(1))
        month_name = m.group(2)
        month = month_map.get(month_name)
        if not month:
            return None

        year = m.group(3)
        year = int(year) if year else datetime.now().year

        try:
            return date(year, month, day)
        except Exception:
            return None

    return None


def _extract_date_from_day_key(day_key):
    parts = day_key.split("–", 1)
    if len(parts) == 2:
        return _parse_day_month_year(parts[1].strip())
    return _parse_day_month_year(day_key)


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


def _filter_past_days(parsed):
    today = date.today()
    filtered = {}

    for day_key, payload in parsed.items():
        d = _extract_date_from_day_key(day_key)
        if d is None or d >= today:
            filtered[day_key] = payload

    return filtered


def _short_file_title(title: str) -> str:
    return _extract_date_range_from_title(title)


def build_message_html_all(files_payload):
    parts = []

    parts.append("🏊 <b>Расписание свободного плавания</b>")
    parts.append("")

    shown_any_file = False

    for title, parsed in files_payload:
        filtered_parsed = _filter_past_days(parsed) if parsed else {}

        if not filtered_parsed:
            continue

        shown_any_file = True
        short_title = _short_file_title(title)

        parts.append("━━━━━━━━━━━━━━")
        parts.append(f"🗓 <b>{_escape_html(short_title)}</b>")
        parts.append("━━━━━━━━━━━━━━")
        parts.append("")

        for day_key, payload in filtered_parsed.items():
            free_times = payload.get("free", [])
            sanitary_time = payload.get("sanitary_time", [])
            sanitary_day = payload.get("sanitary_day", [])

            parts.append(f"📅 <b>{_escape_html(day_key)}</b>")

            if not free_times and not sanitary_time and not sanitary_day:
                parts.append("🧼 <b>Санитарный день</b>")
                parts.append("└ весь день")
                parts.append("")
                continue

            parts.append("🏊 <b>Свободное плавание</b>")
            if free_times:
                for t in free_times:
                    parts.append(f"└ {_escape_html(t)}")
            else:
                parts.append("└ нет")

            if sanitary_time:
                parts.append("🧽 <b>Санитарное время</b>")
                for t in sanitary_time:
                    parts.append(f"└ {_escape_html(t)}")

            if sanitary_day:
                parts.append("🚫 <b>Санитарный день</b>")
                for t in sanitary_day:
                    parts.append(f"└ {_escape_html(t)}")

            parts.append("")

        parts.append("")

    if not shown_any_file:
        return (
            "🏊 <b>Расписание свободного плавания</b>\n\n"
            "⚠️ Нет актуальных дней в найденных файлах."
        )

    text = "\n".join(parts).strip()

    if len(text) > MAX_MESSAGE_LEN:
        text = text[:MAX_MESSAGE_LEN - 120] + "\n\n⚠️ Часть данных была обрезана, потому что сообщение слишком длинное."

    return text


def keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🏊 Получить расписание", "callback_data": BTN_GET}],
            [{"text": "🔔 Получать автоматически в пн", "callback_data": BTN_SUBSCRIBE}],
            [{"text": "🕔 Получать ежедневно в 17:00", "callback_data": BTN_DAILY_TEST}]
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


def fetch_schedule_message():
    links = find_xls_links()

    if not links:
        return "⚠️ Не удалось найти файлы с расписанием."

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

    return build_message_html_all(files_payload)


def handle_start(chat_id):
    tg_send_message(
        chat_id,
        "Привет 🌿\n\nВыбери действие:",
        reply_markup=keyboard()
    )


def handle_get_schedule(chat_id, callback_id=None):
    if callback_id:
        tg_answer_callback(callback_id, "⏳ Загружаю расписание...")

    msg = fetch_schedule_message()

    tg_send_message(
        chat_id,
        msg,
        reply_markup=keyboard(),
        parse_mode="HTML"
    )


def handle_subscribe_weekly(chat_id, callback_id):
    added = add_id(WEEKLY_SUBSCRIBERS_FILE, chat_id)

    if added:
        tg_answer_callback(callback_id, "✅ Автоотправка включена")
        text = (
            "🔔 <b>Автоотправка включена</b>\n\n"
            "Теперь я буду присылать новое расписание "
            "каждый <b>понедельник в 10:00</b>."
        )
    else:
        tg_answer_callback(callback_id, "ℹ️ Уже включено")
        text = (
            "ℹ️ <b>Автоотправка уже включена</b>\n\n"
            "Ты уже подписана на получение расписания "
            "каждый <b>понедельник в 10:00</b>."
        )

    tg_send_message(
        chat_id,
        text,
        reply_markup=keyboard(),
        parse_mode="HTML"
    )


def handle_subscribe_daily_test(chat_id, callback_id):
    added = add_id(DAILY_TEST_SUBSCRIBERS_FILE, chat_id)

    if added:
        tg_answer_callback(callback_id, "✅ Тест включен")
        text = (
            "🕔 <b>Тестовая ежедневная отправка включена</b>\n\n"
            "Теперь я буду присылать расписание каждый день "
            f"в <b>{TEST_SEND_HOUR:02d}:{TEST_SEND_MINUTE:02d}</b>."
        )
    else:
        tg_answer_callback(callback_id, "ℹ️ Уже включено")
        text = (
            "ℹ️ <b>Тестовая ежедневная отправка уже включена</b>\n\n"
            "Ты уже подписана на ежедневную отправку "
            f"в <b>{TEST_SEND_HOUR:02d}:{TEST_SEND_MINUTE:02d}</b>."
        )

    tg_send_message(
        chat_id,
        text,
        reply_markup=keyboard(),
        parse_mode="HTML"
    )


def is_weekly_time(now=None):
    now = now or datetime.now()
    return now.weekday() == 0 and now.hour == AUTO_SEND_HOUR and now.minute == AUTO_SEND_MINUTE


def is_daily_test_time(now=None):
    now = now or datetime.now()
    return now.hour == TEST_SEND_HOUR and now.minute == TEST_SEND_MINUTE


def auto_send_weekly_if_needed(last_weekly_send_key):
    now = datetime.now()
    current_key = now.strftime("%Y-%m-%d %H:%M")

    if not is_weekly_time(now):
        return last_weekly_send_key

    if current_key == last_weekly_send_key:
        return last_weekly_send_key

    subscribers = load_ids(WEEKLY_SUBSCRIBERS_FILE)
    if not subscribers:
        return current_key

    try:
        msg = fetch_schedule_message()
    except Exception as e:
        msg = f"⚠️ Не удалось автоматически получить расписание.\n\nОшибка: {e}"

    for chat_id in subscribers:
        try:
            tg_send_message(
                chat_id,
                msg,
                reply_markup=keyboard(),
                parse_mode="HTML"
            )
            time.sleep(0.4)
        except Exception as e:
            print(f"Weekly auto send error for {chat_id}: {e}")

    return current_key


def auto_send_daily_test_if_needed(last_daily_send_key):
    now = datetime.now()
    current_key = now.strftime("%Y-%m-%d %H:%M")

    if not is_daily_test_time(now):
        return last_daily_send_key

    if current_key == last_daily_send_key:
        return last_daily_send_key

    subscribers = load_ids(DAILY_TEST_SUBSCRIBERS_FILE)
    if not subscribers:
        return current_key

    try:
        msg = fetch_schedule_message()
    except Exception as e:
        msg = f"⚠️ Не удалось автоматически получить расписание.\n\nОшибка: {e}"

    for chat_id in subscribers:
        try:
            tg_send_message(
                chat_id,
                msg,
                reply_markup=keyboard(),
                parse_mode="HTML"
            )
            time.sleep(0.4)
        except Exception as e:
            print(f"Daily auto send error for {chat_id}: {e}")

    return current_key


def run_bot():
    offset = 0
    last_weekly_send_key = ""
    last_daily_send_key = ""

    print("Bot running")

    while True:
        try:
            last_weekly_send_key = auto_send_weekly_if_needed(last_weekly_send_key)
            last_daily_send_key = auto_send_daily_test_if_needed(last_daily_send_key)

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
                        handle_get_schedule(chat_id, cq_id)
                    elif data_btn == BTN_SUBSCRIBE:
                        handle_subscribe_weekly(chat_id, cq_id)
                    elif data_btn == BTN_DAILY_TEST:
                        handle_subscribe_daily_test(chat_id, cq_id)
                    else:
                        tg_answer_callback(cq_id)

        except Exception as e:
            print("Error:", e)
            time.sleep(3)


if __name__ == "__main__":
    run_bot()
