# tdata-switcher

Быстрый переключатель папок **tdata** и запуск **Telegram Portable**.

## Как работает

1. Кладёшь все tdata-папки в `tdata/`
2. Запускаешь `tdata_switcher.exe` (или `.py`)
3. Выбираешь номер → открывается Portable с этим аккаунтом

Никакого копирования файлов — используется официальный флаг `-workdir`.

## Структура

```
tdata-switcher/
├── tdata_switcher.exe      ← скомпилированный
├── tdata_switcher.py
├── Telegram/
│   └── Telegram.exe        ← Portable с desktop.telegram.org
└── tdata/
    ├── account1/
    │   └── tdata/          ← классика
    ├── account2/
    │   └── tdata/
    └── account3/           ← или сразу содержимое tdata (key_datas ...)
```

## Сборка .exe

```bash
pip install pyinstaller
pyinstaller --onefile --console --name tdata_switcher tdata_switcher.py
```

Готовый файл будет в `dist/tdata_switcher.exe`.

## Запуск из Python

```bash
python tdata_switcher.py
# или с другой папкой:
python tdata_switcher.py "D:\my_tdatas"
```

## Примечания

- Telegram Portable нужно скачать с https://desktop.telegram.org/ (Portable version)
- Один и тот же auth-key нельзя держать открытым одновременно в нескольких клиентах
- Скрипт запоминает последний выбранный аккаунт

## Лицензия

MIT
