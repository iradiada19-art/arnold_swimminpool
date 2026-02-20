def build_message_html(free_swim: dict, evening_only: bool = False) -> str:
    """
    Output format:
    <b>Понедельник – 16 февраля</b>
    свободное плавание
    ...
    санитарное время (только если есть)
    ...
    санитарный день (только если есть)
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

        # свободное плавание — показываем всегда
        parts.append("свободное плавание")
        if free_times:
            parts.extend(free_times)
        else:
            parts.append("нет данных")

        # санитарное время — только если есть
        if sanitary_time:
            parts.append("санитарное время")
            parts.extend(sanitary_time)

        # санитарный день — только если есть
        if sanitary_day:
            parts.append("санитарный день")
            parts.extend(sanitary_day)

        parts.append("")

    return "\n".join(parts).strip()
