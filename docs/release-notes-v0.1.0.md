# Video Files Renamer 0.1.0

Первый релиз локального исправлятора имён видео для Windows.

## Что умеет

- Ищет файл в StashDB по OSHASH; при отсутствии точного совпадения — по тексту.
- Собирает имя в формате `Студия - ДД.ММ.ГГГГ - Исполнители - Название`.
- Показывает CSV на ревью и переименовывает только `approved=y`.
- Ведёт `rename_log.csv` и проверяет OSHASH/size перед переименованием.
- Работает офлайн с кэшем и эвристикой.
- Умеет собирать студии из StashDB, FansDB и ThePornDB.
- Имеет TUI на Textual: выбор папки, прогресс, таблица, фильтры, apply.

## Установка

Нужен Python 3.10+ (проверено на 3.14) и Windows 10/11.

```powershell
# TUI: батник сам создаст .venv и поставит Textual
scripts\start_tui.bat

# CLI без установки
python -m video_renamer.cli preview D:\Videos\PH --csv preview.csv
```

Для установки пакета:

```powershell
python -m pip install -e ".[tui]"
video-renamer --help
video-renamer-tui
```

Ключи StashDB/FansDB/ThePornDB кладутся в `.env` (см. `.env.example`) или в
переменные окружения.

## Известные ограничения

- OSHASH совпадает только с точной копией файла; перекодированные файлы
  требуют текстового поиска.
- ThePornDB не поддерживает `queryStudios`; его скрапер обогащает известные
  имена через `findStudio`.
- TUI пока не управляет скраперами и не выбирает альтернативных кандидатов.
