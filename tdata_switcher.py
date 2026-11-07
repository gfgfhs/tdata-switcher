#!/usr/bin/env python3
"""
tdata-switcher — быстрый переключатель tdata + запуск Telegram Portable

Структура (любая из):
  tdata/
    account1/tdata/          ← обычный вид
    account2/tdata/
  или
  tdata/
    account1/                ← сама папка = содержимое tdata (key_datas ...)
    account2/
"""

from __future__ import annotations

import os
import sys
import subprocess
import json
import time
from pathlib import Path

# ============== НАСТРОЙКИ ==============
TDATA_ROOT = Path(__file__).parent / "tdata"
TELEGRAM_EXE = Path(__file__).parent / "Telegram" / "Telegram.exe"
# =======================================


def is_tdata_content(path: Path) -> bool:
    """Проверяет, что папка содержит файлы tdata (key_datas / hex)."""
    if not path.is_dir():
        return False
    names = {p.name for p in path.iterdir()}
    if "key_datas" in names or "key_data" in names:
        return True
    hex_count = sum(
        1 for n in names
        if len(n) >= 16 and all(c in "0123456789ABCDEFabcdef" for c in n)
    )
    return hex_count >= 1


def find_accounts(root: Path) -> list[tuple[str, Path]]:
    """
    Возвращает список (красивое_имя, путь_к_папке_которая_содержит_tdata_или_является_tdata).
    workdir будет = этот путь (если внутри есть tdata) или parent (если путь сам tdata-content).
    """
    results: list[tuple[str, Path]] = []
    seen = set()

    if not root.exists():
        return results

    def add(name: str, workdir: Path):
        workdir = workdir.resolve()
        if workdir in seen:
            return
        seen.add(workdir)
        results.append((name, workdir))

    # 1. Сама root — tdata content?
    if is_tdata_content(root):
        add(root.name, root)
        return results

    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue

        # item/tdata  (классика)
        nested = item / "tdata"
        if nested.is_dir() and is_tdata_content(nested):
            add(item.name, item)
            continue

        # item сам = tdata content (без вложенной tdata)
        if is_tdata_content(item):
            add(item.name, item)
            continue

        # глубже: item/*/tdata
        for child in item.iterdir():
            if child.is_dir() and child.name.lower() == "tdata" and is_tdata_content(child):
                add(f"{item.name}/{child.parent.name}", child.parent)
            elif child.is_dir() and is_tdata_content(child):
                add(f"{item.name}/{child.name}", child)

    return results


def ensure_workdir_has_tdata(account_path: Path) -> Path:
    """
    Telegram требует, чтобы в -workdir была подпапка tdata.
    Если account_path уже содержит tdata/ — возвращаем его.
    Если account_path сам является содержимым tdata — создаём рядом временную
    структуру через junction (Windows) / symlink.
    """
    if (account_path / "tdata").is_dir() and is_tdata_content(account_path / "tdata"):
        return account_path

    if is_tdata_content(account_path):
        # Нужно, чтобы workdir/tdata указывал на account_path
        # Создаём папку workdirs/<name> с junction
        work_base = Path(__file__).parent / "_workdirs"
        work_base.mkdir(exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in account_path.name)[:40]
        wd = work_base / safe
        tdata_link = wd / "tdata"

        if tdata_link.exists():
            # уже есть — проверяем, куда указывает
            try:
                if tdata_link.resolve() == account_path.resolve():
                    return wd
            except Exception:
                pass
            # пересоздаём
            if tdata_link.is_symlink() or tdata_link.is_dir():
                try:
                    tdata_link.unlink()
                except Exception:
                    import shutil
                    shutil.rmtree(tdata_link, ignore_errors=True)

        wd.mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32":
            # junction (не требует админа)
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(tdata_link), str(account_path)],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                # fallback — копировать нельзя (медленно), просто вернём parent
                print(f"[!] Не удалось создать junction: {r.stderr}")
                return account_path.parent
        else:
            tdata_link.symlink_to(account_path, target_is_directory=True)

        return wd

    # fallback
    return account_path


def find_telegram() -> Path | None:
    candidates = [
        TELEGRAM_EXE,
        Path(__file__).parent / "Telegram.exe",
        Path(os.environ.get("APPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    for p in Path(__file__).parent.rglob("Telegram.exe"):
        return p
    return None


def load_last() -> str | None:
    cfg = Path(__file__).parent / "last_account.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text(encoding="utf-8")).get("last")
        except Exception:
            pass
    return None


def save_last(name: str):
    cfg = Path(__file__).parent / "last_account.json"
    cfg.write_text(json.dumps({"last": name}, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    print("=" * 52)
    print("  tdata-switcher  ·  быстрый запуск Portable")
    print("=" * 52)

    root = TDATA_ROOT
    if len(sys.argv) > 1:
        root = Path(sys.argv[1]).expanduser().resolve()

    print(f"\nПапка с аккаунтами: {root}")

    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        print(f"[!] Папка создана. Положи туда tdata-папки и запусти снова.")
        input("\nEnter...")
        return

    accounts = find_accounts(root)
    if not accounts:
        print("[!] tdata не найдены.")
        print("    Ожидаемая структура:")
        print("      tdata/account1/tdata/   или   tdata/account1/  (с key_datas)")
        input("\nEnter...")
        return

    last = load_last()

    print(f"\nНайдено аккаунтов: {len(accounts)}\n")
    for i, (name, path) in enumerate(accounts, 1):
        mark = "  ← last" if last and name == last else ""
        print(f"  {i:3}. {name}{mark}")

    print()
    choice = input("Номер (Enter = последний): ").strip()

    selected_name = None
    selected_path = None

    if not choice and last:
        for name, path in accounts:
            if name == last:
                selected_name, selected_path = name, path
                break
    elif choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(accounts):
            selected_name, selected_path = accounts[idx - 1]

    if selected_path is None:
        print("[!] Неверный выбор")
        input("\nEnter...")
        return

    save_last(selected_name)
    workdir = ensure_workdir_has_tdata(selected_path)

    tg = find_telegram()
    if not tg:
        print(f"\n[!] Telegram.exe не найден.")
        print(f"    Положи Portable в папку:  {Path(__file__).parent / 'Telegram'}")
        print(f"    (должен быть Telegram/Telegram.exe)")
        print(f"\nworkdir: {workdir}")
        input("\nEnter...")
        return

    print(f"\n[+] Запускаю «{selected_name}»")
    print(f"    {tg.name}  -workdir  {workdir}")

    try:
        flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
        subprocess.Popen(
            [str(tg), "-workdir", str(workdir.resolve())],
            cwd=str(tg.parent),
            creationflags=flags,
        )
        print("[+] Готово")
    except Exception as e:
        print(f"[!] Ошибка: {e}")

    if getattr(sys, "frozen", False):
        time.sleep(1.2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОтмена")
    except Exception as e:
        print(f"\n[!] {type(e).__name__}: {e}")
        input("\nEnter...")
