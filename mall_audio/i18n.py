"""Interface translations.

The source strings are the English ones, so an untranslated key still renders
readable text rather than a placeholder. Everything lives in this module: the
app ships as a single offline bundle, and Qt's .ts/.qm pipeline would add a
build step and data files to install alongside it for no benefit at this size.
"""

from __future__ import annotations

# Code -> the language's own name, which is how it is offered in Settings.
LANGUAGES = {
    "en": "English",
    "tk": "Türkmen",
    "ru": "Русский",
}

DEFAULT_LANGUAGE = "en"

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "tk": {
        # Navigation and chrome
        "Dashboard": "Dolandyryş",
        "Settings": "Sazlamalar",
        "Playback log": "Oýnatma žurnaly",
        "Offline • Local storage": "Awtonom • Ýerli ammar",
        # Now playing
        "NOW PLAYING": "HÄZIR ÝAŇLANÝAR",
        "VOICE AD": "SES YGLANY",
        "Nothing playing": "Hiç zat ýaňlanmaýar",
        "Previous voice ad": "Öňki ses yglany",
        "Next voice ad": "Indiki ses yglany",
        "Pause voice ad": "Ses yglanyny wagtlaýyn sakla",
        "Resume voice ad": "Ses yglanyny dowam etdir",
        "Rules apply to the whole voice-ad playlist: at each scheduled time every recording plays, one after another, in list order.":
            "Düzgünler tutuş ses yglanlary sanawyna degişli: her meýilleşdirilen wagtda ähli ýazgylar sanaw tertibinde yzly-yzyna ýaňlanýar.",
        "Next: all {count} voice ads — {when}": "Indiki: ähli {count} ses yglany — {when}",
        "No recordings yet. Add some in the Voice ads panel and the rules will play them in turn.":
            "Heniz ýazgy ýok. Ses yglanlary panelinde goşuň — düzgünler olary gezekli ýaňlandyrar.",
        "●  ON AIR": "●  EFIRDE",
        "●  OFF AIR": "●  EFIRDE DÄL",
        "●  PAUSED": "●  SAKLANDY",
        "●  AUDIO ERROR": "●  SES ÝALŇYŞLYGY",
        "Click to change the visualiser style and colours": "Wizualizatoryň stilini we reňklerini üýtgetmek üçin basyň",
        "Style": "Stil",
        "Colour": "Reňk",
        # Transport
        "Previous track": "Öňki aýdym",
        "Next track": "Indiki aýdym",
        "Play music": "Sazy oýnat",
        "Pause music": "Sazy sakla",
        "Music volume": "Saz sesi",
        "Mix: on": "Garyşdyr: açyk",
        "Mix: off": "Garyşdyr: ýapyk",
        "Repeat all: on": "Ählisini gaýtala: açyk",
        "Repeat all: off": "Ählisini gaýtala: ýapyk",
        "No scheduled voice ad": "Meýilleşdirilen ses yglany ýok",
        "Next: {name} — {when}": "Indiki: {name} — {when}",
        # Playlist panels
        "Music playlist": "Saz sanawy",
        "Voice ads": "Ses yglanlary",
        "Track": "Aýdym",
        "Length": "Dowamlylygy",
        "At": "Wagty",
        "No music": "Saz ýok",
        "No voice ads": "Ses yglany ýok",
        "Drop audio files here": "Ses faýllaryny şu ýere taşlaň",
        "Drag a row to change the running order.": "Tertibi üýtgetmek üçin setiri süýşüriň.",
        "Search": "Gözle",
        "Nothing matches that search": "Gözlege laýyk zat tapylmady",
        "Remove selected music": "Saýlanan sazy aýyr",
        "Play selected voice recording": "Saýlanan ses ýazgysyny oýnat",
        "Remove selected voice ad": "Saýlanan ses yglanyny aýyr",
        "Voice ad automation": "Ses yglanlaryny awtomatlaşdyrmak",
        "Refresh playlist": "Sanawy täzele",
        "Select folder": "Bukja saýla",
        "Select music": "Saz saýla",
        "Select recordings": "Ýazgylary saýla",
        "Choose a music folder": "Saz bukjasyny saýlaň",
        "Choose a voice-ad folder": "Ses yglanlary bukjasyny saýlaň",
        "Select a folder for this list first; sync re-scans it.": "Ilki bu sanaw üçin bukja saýlaň; sinhronlama ony täzeden barlaýar.",
        "Show in Finder": "Finder-de görkez",
        "Show in Explorer": "Explorer-de görkez",
        "Stop after": "Soň sakla",
        "Until": "Çenli",
        "Midnight": "Ýarygije",
        "until {time}": "{time} çenli",
        "until midnight": "ýarygijä çenli",
        ", then {interval} {end}": ", soňra {end} {interval}",
        "The stop time must be later than the first play.": "Saklanma wagty ilkinji oýnatmadan giç bolmaly.",
        "Sync playlist with its folder": "Sanawy bukjasy bilen sinhronla",
        "Sync voice ads with their folder": "Ses yglanlaryny bukjasy bilen sinhronla",
        "Added {count} new file(s)": "{count} täze faýl goşuldy",
        # Scheduling popup
        "New playback rule": "Täze oýnatma düzgüni",
        "Save rule": "Düzgüni ýatda sakla",
        "Save automated playback": "Awtomatik oýnatmany ýatda sakla",
        "FIRST PLAY": "ILKINJI OÝNATMA",
        "REPEAT INTERVAL": "GAÝTALANMA ARALYGY",
        "ACTIVE DAYS": "IŞJEŇ GÜNLER",
        "Scheduled playbacks": "Meýilleşdirilen oýnatmalar",
        "Remove selected automation": "Saýlanan awtomatizasiýany aýyr",
        "First play": "Ilkinji oýnatma",
        "Days": "Günler",
        "Repeat": "Gaýtalama",
        "Play once a day": "Günde bir gezek",
        " min": " min",
        "Uses this computer's local clock.": "Bu kompýuteriň ýerli sagadyny ulanýar.",
        "No playback rules yet": "Heniz oýnatma düzgüni ýok",
        "Saved rules appear here and run while the app stays open.":
            "Ýatda saklanan düzgünler şu ýerde görünýär we programma açykka işleýär.",
        "Pick at least one day for this rule to run.":
            "Bu düzgüniň işlemegi üçin iň azyndan bir gün saýlaň.",
        "Select a voice recording in the Voice ads panel first.":
            "Ilki Ses yglanlary panelinde ses ýazgysyny saýlaň.",
        # Days
        "Mon": "Duş", "Tue": "Siş", "Wed": "Çar", "Thu": "Pen",
        "Fri": "Ann", "Sat": "Şen", "Sun": "Ýek",
        "All": "Ählisi", "Weekdays": "Iş günleri", "Weekend": "Dynç günleri",
        "Every day": "Her gün",
        "Mon–Fri": "Duş–Ann",
        "Sat & Sun": "Şen we Ýek",
        "Once a day": "Günde bir gezek",
        # Rule summary
        "Voice ads play {days} at {time}{cadence}, in playlist order.": "Ses yglanlary {days} sagat {time}-da ýaňlanýar{cadence}, sanaw tertibinde.",
        "every hour": "her sagat",
        "every {count} hours": "her {count} sagatdan",
        "every {count} minutes": "her {count} minutdan",
        "every day": "her gün",
        "on weekdays": "iş günleri",
        "on weekends": "dynç günleri",
        "on {days}": "{days} günleri",
        "{first} and {last}": "{first} we {last}",
        # Settings
        "Saved locally and applied immediately.": "Ýerli ýatda saklanýar we derrew ulanylýar.",
        "Audio playback": "Ses oýnatmasy",
        "Output device": "Çykyş enjamy",
        "Automatic (system default)": "Awtomatik (ulgamyň kadasy)",
        "Voice ad volume": "Ses yglanynyň sesi",
        "Fade duration": "Sönme dowamlylygy",
        " ms": " ms",
        "Language": "Dil",
        "In a mall installation choose the dedicated USB audio interface here, not the computer’s built-in speakers.":
            "Söwda merkezinde kompýuteriň içki gürleýjileri däl-de, ýörite USB ses enjamyny saýlaň.",
        "Behaviour": "Işleýşi",
        "Fade the music back in after a voice ad": "Ses yglanyndan soň sazy kem-kemden gaýtar",
        "Start automatically when this computer logs in": "Bu kompýutere girilende awtomatik başla",
        "Start playing music when the app opens": "Programma açylanda sazy awtomatik başlat",
        "Save settings": "Sazlamalary ýatda sakla",
        # Playback log
        "Local audit trail for scheduled and manual playback.":
            "Meýilleşdirilen we el bilen oýnatmalaryň ýerli ýazgysy.",
        "Recent activity": "Soňky hereketler",
        "Time": "Wagt",
        "Type": "Görnüşi",
        "Message": "Habar",
        "Refresh": "Täzele",
        "Export CSV": "CSV eksport et",
        # Log event types
        "Scheduled": "Meýilleşdirilen",
        "Manual": "El bilen",
        "Skipped": "Geçirilen",
        "Failed": "Şowsuz",
        "Music": "Saz",
        "Error": "Ýalňyşlyk",
        "Announcement": "Yglan",
        # File dialogs
        "Choose audio files": "Ses faýllaryny saýla",
        "Audio files (*.mp3 *.wav *.aac *.m4a *.ogg *.flac)": "Ses faýllary (*.mp3 *.wav *.aac *.m4a *.ogg *.flac)",
        "Export playback log": "Oýnatma žurnalyny eksport et",
        "CSV files (*.csv)": "CSV faýllary (*.csv)",
    },
    "ru": {
        # Navigation and chrome
        "Dashboard": "Панель",
        "Settings": "Настройки",
        "Playback log": "Журнал воспроизведения",
        "Offline • Local storage": "Автономно • Локальное хранилище",
        # Now playing
        "NOW PLAYING": "СЕЙЧАС ИГРАЕТ",
        "VOICE AD": "ГОЛОСОВОЕ ОБЪЯВЛЕНИЕ",
        "Nothing playing": "Ничего не играет",
        "Previous voice ad": "Предыдущее объявление",
        "Next voice ad": "Следующее объявление",
        "Pause voice ad": "Приостановить объявление",
        "Resume voice ad": "Продолжить объявление",
        "Rules apply to the whole voice-ad playlist: at each scheduled time every recording plays, one after another, in list order.":
            "Правила действуют на весь список объявлений: в каждое запланированное время звучат все записи подряд, по порядку списка.",
        "Next: all {count} voice ads — {when}": "Далее: все объявления ({count}) — {when}",
        "No recordings yet. Add some in the Voice ads panel and the rules will play them in turn.":
            "Записей пока нет. Добавьте их в панели «Голосовые объявления» — правила будут воспроизводить их по очереди.",
        "●  ON AIR": "●  В ЭФИРЕ",
        "●  OFF AIR": "●  НЕ В ЭФИРЕ",
        "●  PAUSED": "●  ПАУЗА",
        "●  AUDIO ERROR": "●  ОШИБКА ЗВУКА",
        "Click to change the visualiser style and colours": "Нажмите, чтобы изменить стиль и цвета визуализатора",
        "Style": "Стиль",
        "Colour": "Цвет",
        # Transport
        "Previous track": "Предыдущий трек",
        "Next track": "Следующий трек",
        "Play music": "Воспроизвести музыку",
        "Pause music": "Приостановить музыку",
        "Music volume": "Громкость музыки",
        "Mix: on": "Вперемешку: вкл.",
        "Mix: off": "Вперемешку: выкл.",
        "Repeat all: on": "Повтор всех: вкл.",
        "Repeat all: off": "Повтор всех: выкл.",
        "No scheduled voice ad": "Нет запланированных объявлений",
        "Next: {name} — {when}": "Далее: {name} — {when}",
        # Playlist panels
        "Music playlist": "Плейлист",
        "Voice ads": "Голосовые объявления",
        "Track": "Трек",
        "Length": "Длительность",
        "At": "Время",
        "No music": "Нет музыки",
        "No voice ads": "Нет объявлений",
        "Drop audio files here": "Перетащите аудиофайлы сюда",
        "Drag a row to change the running order.": "Перетащите строку, чтобы изменить порядок.",
        "Search": "Поиск",
        "Nothing matches that search": "Ничего не найдено",
        "Remove selected music": "Удалить выбранную музыку",
        "Play selected voice recording": "Воспроизвести выбранную запись",
        "Remove selected voice ad": "Удалить выбранное объявление",
        "Voice ad automation": "Автоматизация объявлений",
        "Refresh playlist": "Обновить плейлист",
        "Select folder": "Выбрать папку",
        "Select music": "Выбрать музыку",
        "Select recordings": "Выбрать записи",
        "Choose a music folder": "Выберите папку с музыкой",
        "Choose a voice-ad folder": "Выберите папку с объявлениями",
        "Select a folder for this list first; sync re-scans it.": "Сначала выберите папку для этого списка; синхронизация сканирует её заново.",
        "Show in Finder": "Показать в Finder",
        "Show in Explorer": "Показать в проводнике",
        "Stop after": "Остановить после",
        "Until": "До",
        "Midnight": "Полночь",
        "until {time}": "до {time}",
        "until midnight": "до полуночи",
        ", then {interval} {end}": ", затем {interval} {end}",
        "The stop time must be later than the first play.": "Время остановки должно быть позже первого воспроизведения.",
        "Sync playlist with its folder": "Синхронизировать плейлист с папкой",
        "Sync voice ads with their folder": "Синхронизировать объявления с папкой",
        "Added {count} new file(s)": "Добавлено новых файлов: {count}",
        # Scheduling popup
        "New playback rule": "Новое правило воспроизведения",
        "Save rule": "Сохранить правило",
        "Save automated playback": "Сохранить автоматическое воспроизведение",
        "FIRST PLAY": "ПЕРВОЕ ВОСПРОИЗВЕДЕНИЕ",
        "REPEAT INTERVAL": "ИНТЕРВАЛ ПОВТОРА",
        "ACTIVE DAYS": "АКТИВНЫЕ ДНИ",
        "Scheduled playbacks": "Запланированные воспроизведения",
        "Remove selected automation": "Удалить выбранное правило",
        "First play": "Первое воспроизведение",
        "Days": "Дни",
        "Repeat": "Повтор",
        "Play once a day": "Раз в день",
        " min": " мин",
        "Uses this computer's local clock.": "Используются локальные часы этого компьютера.",
        "No playback rules yet": "Правил воспроизведения пока нет",
        "Saved rules appear here and run while the app stays open.":
            "Сохранённые правила появляются здесь и работают, пока приложение открыто.",
        "Pick at least one day for this rule to run.":
            "Выберите хотя бы один день для работы этого правила.",
        "Select a voice recording in the Voice ads panel first.":
            "Сначала выберите голосовую запись в панели «Голосовые объявления».",
        # Days
        "Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт",
        "Fri": "Пт", "Sat": "Сб", "Sun": "Вс",
        "All": "Все", "Weekdays": "Будни", "Weekend": "Выходные",
        "Every day": "Каждый день",
        "Mon–Fri": "Пн–Пт",
        "Sat & Sun": "Сб и Вс",
        "Once a day": "Раз в день",
        # Rule summary
        "Voice ads play {days} at {time}{cadence}, in playlist order.": "Объявления звучат {days} в {time}{cadence}, по порядку списка.",
        "every hour": "каждый час",
        "every {count} hours": "каждые {count} ч",
        "every {count} minutes": "каждые {count} мин",
        "every day": "каждый день",
        "on weekdays": "по будням",
        "on weekends": "по выходным",
        "on {days}": "по {days}",
        "{first} and {last}": "{first} и {last}",
        # Settings
        "Saved locally and applied immediately.": "Сохраняется локально и применяется сразу.",
        "Audio playback": "Воспроизведение звука",
        "Output device": "Устройство вывода",
        "Automatic (system default)": "Автоматически (по умолчанию)",
        "Voice ad volume": "Громкость объявлений",
        "Fade duration": "Длительность затухания",
        " ms": " мс",
        "Language": "Язык",
        "In a mall installation choose the dedicated USB audio interface here, not the computer’s built-in speakers.":
            "В торговом центре выбирайте здесь отдельный USB-аудиоинтерфейс, а не встроенные динамики компьютера.",
        "Behaviour": "Поведение",
        "Fade the music back in after a voice ad": "Плавно возвращать музыку после объявления",
        "Start automatically when this computer logs in": "Запускать автоматически при входе в систему",
        "Start playing music when the app opens": "Начинать музыку при запуске приложения",
        "Save settings": "Сохранить настройки",
        # Playback log
        "Local audit trail for scheduled and manual playback.":
            "Локальный журнал запланированных и ручных воспроизведений.",
        "Recent activity": "Последние события",
        "Time": "Время",
        "Type": "Тип",
        "Message": "Сообщение",
        "Refresh": "Обновить",
        "Export CSV": "Экспорт CSV",
        # Log event types
        "Scheduled": "По расписанию",
        "Manual": "Вручную",
        "Skipped": "Пропущено",
        "Failed": "Ошибка",
        "Music": "Музыка",
        "Error": "Ошибка",
        "Announcement": "Объявление",
        # File dialogs
        "Choose audio files": "Выберите аудиофайлы",
        "Audio files (*.mp3 *.wav *.aac *.m4a *.ogg *.flac)": "Аудиофайлы (*.mp3 *.wav *.aac *.m4a *.ogg *.flac)",
        "Export playback log": "Экспорт журнала воспроизведения",
        "CSV files (*.csv)": "Файлы CSV (*.csv)",
    },
}

_current = DEFAULT_LANGUAGE


def set_language(code: str) -> None:
    global _current
    _current = code if code in LANGUAGES else DEFAULT_LANGUAGE


def current_language() -> str:
    return _current


def tr(text: str, **fields: object) -> str:
    """Translate one source string, filling any {named} placeholders.

    An unknown key falls through to the English text it was written as, so a
    missing translation degrades to readable rather than blank.
    """
    translated = _TRANSLATIONS.get(_current, {}).get(text, text)
    return translated.format(**fields) if fields else translated
