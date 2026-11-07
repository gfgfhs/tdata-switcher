#!/usr/bin/env python3
"""
tdata-switcher — быстрый переключатель tdata + запуск Telegram Portable

Без аргументов открывается окно (GUI). Режимы командной строки:

  tdata_switcher.py                       окно с аккаунтами
  tdata_switcher.py --cli                 текстовое меню
  tdata_switcher.py --run NAME            сразу запустить аккаунт (так работают ярлыки)
  tdata_switcher.py --import a.zip        импорт tdata из zip / rar / 7z (флаг можно повторять)
  tdata_switcher.py --shortcut NAME       ярлык на рабочий стол (--shortcut-all — для всех)
  tdata_switcher.py --list                список аккаунтов
  tdata_switcher.py [--root] "D:\\my_tdatas"   другая папка с аккаунтами

Структура папки с аккаунтами (любая из):
  tdata/
    account1/tdata/          ← обычный вид
    account2/tdata/
  или
  tdata/
    account1/                ← сама папка = содержимое tdata (key_datas ...)
    account2/
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import tdata_core as core
from tdata_core import (
    PasswordRequired,
    SwitcherError,
    create_shortcut,
    find_accounts,
    import_archive,
    launch,
    load_last,
    resolve_account,
)


# ----------------------------------------------------------------- вывод ошибок

def report_error(message: str) -> None:
    """В консоль, если она есть; иначе (оконный exe, ярлык) — диалогом."""
    stderr = sys.stderr
    if stderr is not None and stderr.isatty():
        print(f"[!] {message}", file=stderr)
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        win = tk.Tk()
        win.withdraw()
        messagebox.showerror("tdata-switcher", message, parent=win)
        win.destroy()
    except Exception:
        if stderr is not None:
            print(f"[!] {message}", file=stderr)


def _say(text: str) -> None:
    if sys.stdout is not None:
        print(text)


# ------------------------------------------------------------ действия без окна

def do_import(archives: list[str], root: Path, password: str | None) -> int:
    code = 0
    for raw in archives:
        archive = Path(raw.strip().strip('"')).expanduser()
        pw = password
        for _attempt in range(3):
            try:
                res = import_archive(archive, root, password=pw, progress=_say)
                _say(f"[+] {archive.name}: " + ", ".join(n for n, _ in res))
                break
            except PasswordRequired:
                if sys.stdin is None or not sys.stdin.isatty():
                    report_error(f"{archive.name}: архив защищён паролем (используйте --password).")
                    code = 1
                    break
                import getpass
                pw = getpass.getpass(f"Пароль для {archive.name} (пусто = пропустить): ") or None
                if pw is None:
                    code = 1
                    break
            except SwitcherError as e:
                report_error(f"{archive.name}: {e}")
                code = 1
                break
        else:
            report_error(f"{archive.name}: неверный пароль")
            code = 1
    return code


def do_shortcuts(names: list[str], root: Path) -> int:
    accounts = find_accounts(root)
    code = 0
    for query in names:
        try:
            name, _ = resolve_account(accounts, query)
            _say(f"[+] Ярлык: {create_shortcut(name, root)}")
        except SwitcherError as e:
            report_error(str(e))
            code = 1
    return code


def do_run(query: str, root: Path) -> int:
    try:
        name, path = resolve_account(find_accounts(root), query)
        launch(name, path)
        return 0
    except SwitcherError as e:
        report_error(str(e))
        return 1


# ------------------------------------------------------------------ CLI-меню

def run_cli(root: Path) -> None:
    print("=" * 52)
    print("  tdata-switcher  ·  быстрый запуск Portable")
    print("=" * 52)
    print(f"\nПапка с аккаунтами: {root}")

    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        print("[!] Папка создана. Положи туда tdata-папки (или импортируй архив) и запусти снова.")

    while True:
        accounts = find_accounts(root)
        last = load_last()

        if accounts:
            print(f"\nНайдено аккаунтов: {len(accounts)}\n")
            for i, (name, _path) in enumerate(accounts, 1):
                mark = "  ← last" if last and name == last else ""
                print(f"  {i:3}. {name}{mark}")
        else:
            print("\n[!] tdata не найдены.")
            print("    Ожидаемая структура:")
            print("      tdata/account1/tdata/   или   tdata/account1/  (с key_datas)")

        print("\n  i — импорт архива (zip/rar/7z)   s N — ярлык на рабочий стол   q — выход")
        choice = input("Номер (Enter = последний): ").strip()
        low = choice.lower()

        if low in ("q", "quit", "exit"):
            return

        if low == "i" or low.startswith("i "):
            path = choice[1:].strip() or input("Путь к архиву: ").strip()
            if path:
                do_import([path], root, None)
            continue

        m = re.fullmatch(r"s\s*(\d+)", low)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= len(accounts):
                try:
                    print(f"[+] Ярлык: {create_shortcut(accounts[idx - 1][0], root)}")
                except SwitcherError as e:
                    print(f"[!] {e}")
            else:
                print("[!] Неверный номер")
            continue

        selected = None
        if not choice and last:
            selected = next((a for a in accounts if a[0] == last), None)
        elif choice.isdigit() and 1 <= int(choice) <= len(accounts):
            selected = accounts[int(choice) - 1]

        if selected is None:
            print("[!] Неверный выбор")
            continue

        name, path = selected
        try:
            tg = core.find_telegram()
            print(f"\n[+] Запускаю «{name}»")
            if tg:
                print(f"    {tg.name}  -workdir  {core.ensure_workdir_has_tdata(path)}")
            launch(name, path, telegram=tg)
            print("[+] Готово")
        except SwitcherError as e:
            print(f"\n[!] {e}")
            input("\nEnter...")
            return
        if getattr(sys, "frozen", False):
            time.sleep(1.2)
        return


# ------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tdata_switcher", description="Быстрый переключатель tdata + запуск Telegram Portable")
    p.add_argument("root_pos", nargs="?", metavar="ROOT",
                   help="папка с аккаунтами (по умолчанию tdata/ рядом с программой)")
    p.add_argument("--root", help="папка с аккаунтами")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--gui", action="store_true", help="открыть окно")
    mode.add_argument("--cli", action="store_true", help="текстовое меню")
    p.add_argument("--run", metavar="NAME", help="запустить аккаунт и выйти")
    p.add_argument("--import", dest="imports", action="append", metavar="ARCHIVE",
                   help="импортировать tdata из архива zip/rar/7z (можно несколько раз)")
    p.add_argument("--password", help="пароль архива для --import")
    p.add_argument("--shortcut", action="append", metavar="NAME",
                   help="создать ярлык аккаунта на рабочем столе (можно несколько раз)")
    p.add_argument("--shortcut-all", action="store_true", help="ярлыки для всех аккаунтов")
    p.add_argument("--list", action="store_true", help="показать список аккаунтов")
    p.add_argument("--version", action="version", version=f"tdata-switcher {core.__version__}")
    return p


def main(argv: list[str] | None = None, default_mode: str = "gui") -> int:
    args = build_parser().parse_args(argv)
    root_arg = args.root or args.root_pos
    root = Path(root_arg).expanduser().resolve() if root_arg else core.TDATA_ROOT

    headless = bool(args.run or args.imports or args.shortcut or args.shortcut_all or args.list)
    if headless:
        code = 0
        if args.imports:
            code |= do_import(args.imports, root, args.password)
        if args.list:
            for i, (name, _p) in enumerate(find_accounts(root), 1):
                _say(f"{i:3}. {name}")
        if args.shortcut_all:
            code |= do_shortcuts([n for n, _ in find_accounts(root)], root)
        if args.shortcut:
            code |= do_shortcuts(args.shortcut, root)
        if args.run:
            code |= do_run(args.run, root)
        return code

    mode = "cli" if args.cli else "gui" if args.gui else default_mode
    if mode == "gui":
        try:
            from tdata_gui import run_gui
        except ImportError:
            print("[!] tkinter недоступен — открываю текстовое меню.")
            mode = "cli"
        else:
            run_gui(root)
            return 0

    run_cli(root)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОтмена")
    except Exception as e:
        report_error(f"{type(e).__name__}: {e}")
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nEnter...")
