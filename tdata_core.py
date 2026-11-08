#!/usr/bin/env python3
"""
tdata_core — общая логика tdata-switcher (без GUI и без ввода/вывода в консоль).

Здесь: поиск аккаунтов, запуск Telegram Portable, ярлыки на рабочий стол,
импорт tdata из архивов zip / rar / 7z.

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

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Callable

__version__ = "1.1.0"

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ============== БАЗОВАЯ ПАПКА ==============
# При сборке в .exe (PyInstaller --onefile) __file__ указывает во временную
# папку _MEI..., поэтому берём папку, где лежит сам exe.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# ============== НАСТРОЙКИ ==============
TDATA_ROOT = BASE_DIR / "tdata"
TELEGRAM_EXE = BASE_DIR / "Telegram" / "Telegram.exe"
SETTINGS_FILE = BASE_DIR / "last_account.json"
# =======================================

class SwitcherError(Exception):
    """Ошибка с понятным пользователю текстом."""


class TelegramNotFound(SwitcherError):
    """Не найден Telegram.exe."""


class PasswordRequired(SwitcherError):
    """Архив защищён паролем (или пароль неверный)."""


# --------------------------------------------------------------------------
# Настройки (last_account.json: last, telegram, close_after_launch, ...)
# --------------------------------------------------------------------------

def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(**changes) -> None:
    data = load_settings()
    data.update(changes)
    try:
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # папка может быть только для чтения — настройки не критичны


def load_last() -> str | None:
    last = load_settings().get("last")
    return last if isinstance(last, str) else None


def save_last(name: str) -> None:
    save_settings(last=name)


# --------------------------------------------------------------------------
# Поиск аккаунтов
# --------------------------------------------------------------------------

def is_tdata_content(path: Path) -> bool:
    """Проверяет, что папка содержит файлы tdata (key_datas / hex)."""
    if not path.is_dir():
        return False
    try:
        names = {p.name for p in path.iterdir()}
    except OSError:
        return False
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
        if item.name.startswith("."):  # служебные папки (.import_xxxx)
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


def resolve_account(accounts: list[tuple[str, Path]], query: str) -> tuple[str, Path]:
    """Находит аккаунт по имени: точно, без регистра, по началу/подстроке или по номеру."""
    q = query.strip()
    for acc in accounts:
        if acc[0] == q:
            return acc
    low = q.lower()
    exact = [a for a in accounts if a[0].lower() == low]
    if len(exact) == 1:
        return exact[0]
    if q.isdigit() and 1 <= int(q) <= len(accounts):
        return accounts[int(q) - 1]
    part = [a for a in accounts if a[0].lower().startswith(low)]
    if not part:
        part = [a for a in accounts if low in a[0].lower()]
    if len(part) == 1:
        return part[0]
    if not part:
        raise SwitcherError(f"Аккаунт «{query}» не найден.")
    names = ", ".join(a[0] for a in part[:8])
    raise SwitcherError(f"Название «{query}» подходит сразу нескольким аккаунтам: {names}")


# --------------------------------------------------------------------------
# Запуск Telegram
# --------------------------------------------------------------------------

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
        work_base = BASE_DIR / "_workdirs"
        work_base.mkdir(exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in account_path.name)[:40]
        wd = work_base / safe
        tdata_link = wd / "tdata"

        if tdata_link.exists():
            try:
                if tdata_link.resolve() == account_path.resolve():
                    return wd
            except Exception:
                pass
            if tdata_link.is_symlink() or tdata_link.is_dir():
                try:
                    tdata_link.unlink()
                except Exception:
                    shutil.rmtree(tdata_link, ignore_errors=True)

        wd.mkdir(parents=True, exist_ok=True)

        if IS_WINDOWS:
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(tdata_link), str(account_path)],
                capture_output=True, text=True, creationflags=NO_WINDOW,
            )
            if r.returncode != 0:
                raise SwitcherError(f"Не удалось создать junction: {r.stderr.strip() or r.stdout.strip()}")
        else:
            try:
                tdata_link.symlink_to(account_path, target_is_directory=True)
            except OSError as e:
                raise SwitcherError(f"Не удалось создать symlink: {e}") from e

        return wd

    return account_path


def find_telegram() -> Path | None:
    configured = load_settings().get("telegram")
    candidates = [Path(configured)] if isinstance(configured, str) and configured else []
    candidates += [
        TELEGRAM_EXE,
        BASE_DIR / "Telegram.exe",
        Path(os.environ.get("APPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    for p in BASE_DIR.rglob("Telegram.exe"):
        return p
    return None


def launch(name: str, account_path: Path, telegram: Path | None = None) -> subprocess.Popen:
    """Запускает Telegram Portable с выбранным аккаунтом и запоминает его как последний."""
    tg = telegram or find_telegram()
    if tg is None:
        raise TelegramNotFound(
            "Telegram.exe не найден.\n"
            f"Положи Portable в папку {TELEGRAM_EXE.parent} (должен быть Telegram/Telegram.exe) "
            "или укажи путь к Telegram.exe вручную."
        )
    workdir = ensure_workdir_has_tdata(account_path)
    save_last(name)
    flags = subprocess.CREATE_NEW_CONSOLE if IS_WINDOWS else 0
    try:
        return subprocess.Popen(
            [str(tg), "-workdir", str(workdir.resolve())],
            cwd=str(tg.parent),
            creationflags=flags,
        )
    except OSError as e:
        raise SwitcherError(f"Не удалось запустить {tg}: {e}") from e


# --------------------------------------------------------------------------
# Ярлыки на рабочий стол
# --------------------------------------------------------------------------

_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_PS_SHORTCUT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TDS_LNK);"
    "$s.TargetPath=$env:TDS_TARGET;"
    "$s.Arguments=$env:TDS_ARGS;"
    "$s.WorkingDirectory=$env:TDS_WD;"
    "$s.Description=$env:TDS_DESC;"
    "if($env:TDS_ICON){$s.IconLocation=$env:TDS_ICON};"
    "$s.Save()"
)


def desktop_dir() -> Path:
    if IS_WINDOWS:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            # 0x10 = CSIDL_DESKTOPDIRECTORY (учитывает перенос рабочего стола в OneDrive)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Desktop"


def _safe_name(name: str, limit: int = 60) -> str:
    safe = _BAD_FILENAME.sub("_", name).strip(" .")
    return safe[:limit] or "account"


def _self_command() -> tuple[str, list[str]]:
    """Чем запускать сам switcher из ярлыка: (программа, начальные аргументы)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        if exe.stem.lower().endswith("_cli"):
            gui = exe.with_name("tdata_switcher" + exe.suffix)
            if gui.is_file():  # ярлык без мигающей консоли
                return str(gui), []
        return str(exe), []
    exe = Path(sys.executable)
    if IS_WINDOWS:
        pyw = exe.with_name("pythonw.exe")
        if pyw.is_file():
            exe = pyw
    return str(exe), [str(Path(__file__).resolve().parent / "tdata_switcher.py")]


def _desktop_entry_quote(arg: str) -> str:
    escaped = (arg.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("$", "\\$").replace("`", "\\`"))
    return f'"{escaped}"'


def create_shortcut(name: str, root: Path, directory: Path | None = None) -> Path:
    """
    Создаёт ярлык «Telegram - <name>», который сразу запускает этот аккаунт.
    Windows → .lnk (иконка Telegram), Linux → .desktop. Возвращает путь к ярлыку.
    """
    target_dir = Path(directory) if directory else desktop_dir()
    if not target_dir.is_dir():
        raise SwitcherError(f"Папка для ярлыков не найдена: {target_dir}")

    program, base_args = _self_command()
    args = list(base_args)
    if Path(root).resolve() != TDATA_ROOT.resolve():
        args += ["--root", str(Path(root).resolve())]
    args += ["--run", name]
    title = f"Telegram - {_safe_name(name)}"

    if IS_WINDOWS:
        lnk = target_dir / f"{title}.lnk"
        tg = find_telegram()
        env = dict(os.environ)
        env.update({
            "TDS_LNK": str(lnk),
            "TDS_TARGET": program,
            "TDS_ARGS": subprocess.list2cmdline(args),
            "TDS_WD": str(BASE_DIR),
            "TDS_DESC": f"Telegram — {name}",
            "TDS_ICON": f"{tg},0" if tg else "",
        })
        encoded = base64.b64encode(_PS_SHORTCUT.encode("utf-16-le")).decode("ascii")
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                env=env, capture_output=True, timeout=30, creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SwitcherError(f"Не удалось создать ярлык: {e}") from e
        if r.returncode != 0 or not lnk.is_file():
            detail = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
            raise SwitcherError(f"Не удалось создать ярлык: {detail or 'PowerShell завершился с ошибкой'}")
        return lnk

    entry = target_dir / f"{title}.desktop"
    exec_line = " ".join(_desktop_entry_quote(a) for a in [program, *args])
    try:
        entry.write_text(
            "[Desktop Entry]\nType=Application\n"
            f"Name=Telegram — {name}\nExec={exec_line}\nTerminal=false\n",
            encoding="utf-8",
        )
        entry.chmod(0o755)
    except OSError as e:
        raise SwitcherError(f"Не удалось создать ярлык: {e}") from e
    return entry


# --------------------------------------------------------------------------
# Импорт из архивов (zip / rar / 7z)
# --------------------------------------------------------------------------

MAX_ARCHIVE_FILES = 500_000
MAX_ARCHIVE_BYTES = 16 * 1024 ** 3  # защита от «бомб»: 16 ГБ распакованного
ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z")
_PASSWORD_HINTS = ("password", "encrypted", "wrong password")


def detect_archive_format(path: Path) -> str:
    """'zip' | 'rar' | '7z' — по сигнатуре файла, затем по расширению."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        raise SwitcherError(f"Не удалось открыть {path.name}: {e}") from e
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    suffix = path.suffix.lower()
    if suffix in ARCHIVE_SUFFIXES:
        return suffix[1:]
    raise SwitcherError(f"Неизвестный формат архива: {path.name} (поддерживаются zip, rar, 7z).")


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _extract_zip(archive: Path, dest: Path, password: str | None) -> None:
    pwd = password.encode("utf-8") if password else None
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise SwitcherError("В архиве слишком много файлов.")
            if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                raise SwitcherError("Архив слишком большой после распаковки.")
            for info in infos:
                if _is_symlink_entry(info):
                    continue  # ссылки из чужих архивов не распаковываем
                # ZipFile.extract сам отрезает «..», абсолютные пути и недопустимые символы
                try:
                    zf.extract(info, dest, pwd=pwd)
                except RuntimeError as e:
                    if any(h in str(e).lower() for h in _PASSWORD_HINTS):
                        raise PasswordRequired("Архив защищён паролем.") from e
                    raise SwitcherError(f"Ошибка распаковки: {e}") from e
    except zipfile.BadZipFile as e:
        raise SwitcherError(f"Повреждённый zip-архив: {e}") from e


def _find_exe(names: list[str], rel_paths: list[str]) -> str | None:
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    bases = [os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    for base in filter(None, bases):
        for rel in rel_paths:
            c = Path(base) / rel
            if c.is_file():
                return str(c)
    return None


def _find_bsdtar() -> str | None:
    candidates = [shutil.which("bsdtar"), shutil.which("tar")]
    sysroot = os.environ.get("SystemRoot")
    if sysroot:
        candidates.append(str(Path(sysroot) / "System32" / "tar.exe"))
    for c in dict.fromkeys(filter(None, candidates)):
        try:
            r = subprocess.run([c, "--version"], capture_output=True, timeout=10,
                               creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            continue
        out = (r.stdout + r.stderr).decode("utf-8", "replace").lower()
        if "bsdtar" in out or "libarchive" in out:  # GNU tar rar не читает
            return c
    return None


def _find_tools(fmt: str) -> list[tuple[str, str]]:
    """Внешние распаковщики по убыванию предпочтения: (вид, путь)."""
    tools: list[tuple[str, str]] = []
    p7 = _find_exe(["7z", "7zz", "7za"], ["7-Zip/7z.exe"])
    if p7:
        tools.append(("7z", p7))
    if fmt == "rar":
        unrar = _find_exe(["unrar", "UnRAR.exe"], ["WinRAR/UnRAR.exe"])
        if unrar:
            tools.append(("unrar", unrar))
        winrar = _find_exe([], ["WinRAR/WinRAR.exe"])
        if winrar:
            tools.append(("winrar", winrar))
    tar = _find_bsdtar()
    if tar:
        tools.append(("tar", tar))
    return tools


def _extract_cmd(kind: str, exe: str, archive: Path, dest: Path, password: str | None) -> list[str]:
    if kind == "7z":
        cmd = [exe, "x", "-y", f"-o{dest}"]
        if password:
            cmd.append(f"-p{password}")
        return cmd + ["--", str(archive)]
    if kind in ("unrar", "winrar"):
        cmd = [exe, "x", "-y"]
        if kind == "winrar":
            cmd.append("-ibck")
        cmd.append(f"-p{password}" if password else "-p-")
        return cmd + [str(archive), str(dest) + os.sep]
    cmd = [exe]
    if password:
        cmd += ["--passphrase", password]
    return cmd + ["-xf", str(archive), "-C", str(dest)]


def _extract_external(archive: Path, dest: Path, fmt: str, password: str | None) -> None:
    tools = _find_tools(fmt)
    if not tools:
        raise SwitcherError(
            f"Для .{fmt} нужен внешний распаковщик: установи 7-Zip (7-zip.org) или WinRAR. "
            "Архивы .zip распаковываются без дополнительных программ."
        )
    last_error = ""
    for kind, exe in tools:
        cmd = _extract_cmd(kind, exe, archive, dest, password)
        try:
            r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                               timeout=1800, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            last_error = str(e)
            continue
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        ok_codes = (0, 1) if kind != "tar" else (0,)  # 1 = «предупреждение» у 7z/unrar
        if r.returncode in ok_codes:
            return
        if any(h in out.lower() for h in _PASSWORD_HINTS):
            raise PasswordRequired("Архив защищён паролем (или пароль неверный).")
        last_error = out.strip()[-300:] or f"код {r.returncode}"
    raise SwitcherError(f"Не удалось распаковать {archive.name}: {last_error}")


def _strip_links(top: Path) -> None:
    """Удаляет symlink/junction из распакованного дерева (внешние тулзы могут их создать)."""
    is_junction = getattr(os.path, "isjunction", lambda _p: False)
    for cur, dirs, files in os.walk(top, followlinks=False):
        for entry in dirs + files:
            p = os.path.join(cur, entry)
            if os.path.islink(p) or is_junction(p):
                try:
                    os.unlink(p)
                except OSError:
                    try:
                        os.rmdir(p)
                    except OSError:
                        pass
        dirs[:] = [d for d in dirs if not (os.path.islink(os.path.join(cur, d))
                                           or is_junction(os.path.join(cur, d)))]


def find_tdata_dirs(top: Path, max_depth: int = 6) -> list[Path]:
    """Ищет внутри распакованного архива папки с содержимым tdata."""
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if is_tdata_content(d):
            found.append(d)
            return
        if depth >= max_depth:
            return
        try:
            children = sorted(c for c in d.iterdir() if c.is_dir() and not c.is_symlink())
        except OSError:
            return
        for c in children:
            walk(c, depth + 1)

    walk(top, 0)
    return found


def _unique_dir(root: Path, name: str) -> Path:
    cand, i = root / name, 2
    while cand.exists():
        cand = root / f"{name}_{i}"
        i += 1
    return cand


def import_archive(
    archive: Path,
    root: Path,
    name: str | None = None,
    password: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[tuple[str, Path]]:
    """
    Распаковывает архив (zip/rar/7z), находит в нём tdata и кладёт в root
    в виде <имя>/tdata/. Если аккаунт один — имя берётся из имени архива
    (или параметра name), если несколько — из имён папок.
    Возвращает список импортированных аккаунтов (имя, путь).
    """
    archive, root = Path(archive), Path(root)
    say = progress or (lambda _msg: None)
    if not archive.is_file():
        raise SwitcherError(f"Файл не найден: {archive}")
    fmt = detect_archive_format(archive)

    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".import_{uuid.uuid4().hex[:8]}"  # на том же диске → перенос мгновенный
    extract_root = tmp / "x"
    extract_root.mkdir(parents=True)
    try:
        say(f"Распаковка {archive.name}…")
        if fmt == "zip":
            _extract_zip(archive, extract_root, password)
        else:
            _extract_external(archive, extract_root, fmt, password)
        _strip_links(extract_root)

        say(f"Поиск tdata в {archive.name}…")
        found = find_tdata_dirs(extract_root)
        if not found:
            raise SwitcherError(
                f"В архиве {archive.name} не найдено tdata (ожидаются key_datas или hex-папки)."
            )

        results: list[tuple[str, Path]] = []
        for content in found:
            if len(found) == 1:
                base = name or archive.stem
            elif content.name.lower() == "tdata":
                base = content.parent.name
            else:
                base = content.name
            dest = _unique_dir(root, _safe_name(base))
            dest.mkdir()
            shutil.move(str(content), str(dest / "tdata"))
            results.append((dest.name, dest))
            say(f"Импортирован: {dest.name}")
        return results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Метки: цветные, назначаются любым аккаунтам. Хранятся в <root>/labels.json
#   {"labels":   {"Работа": "#539bf5", ...},      ← порядок = порядок показа
#    "accounts": {"Ivan": ["Работа", ...], ...}}
# --------------------------------------------------------------------------

LABELS_FILENAME = "labels.json"
LABEL_MAX_LEN = 30
LABEL_PALETTE = [
    "#e5534b", "#f0883e", "#e3b341", "#57ab5a", "#39c5cf",
    "#539bf5", "#986ee2", "#e275ad", "#8b949e", "#6cb6a0",
]
_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


def normalize_color(color: str) -> str:
    """'#ABC' / 'abc' / '#AABBCC' → '#aabbcc'."""
    m = _HEX_COLOR.fullmatch(str(color).strip())
    if not m:
        raise SwitcherError(f"Некорректный цвет: {color!r} (нужен вид #RRGGBB).")
    h = m.group(1).lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def text_color_for(bg: str) -> str:
    """Чёрный или белый текст — что читаемее на фоне bg (#rrggbb)."""
    r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


def labels_path(root: Path) -> Path:
    return Path(root) / LABELS_FILENAME


def load_labels(root: Path) -> dict:
    """Читает метки; повреждённое/лишнее молча отбрасывает, чтобы файл никогда не ломал программу."""
    data: dict = {"labels": {}, "accounts": {}}
    try:
        raw = json.loads(labels_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    if not isinstance(raw, dict):
        return data
    labels = raw.get("labels")
    if isinstance(labels, dict):
        for name, color in labels.items():
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                data["labels"][name] = normalize_color(color)
            except SwitcherError:
                data["labels"][name] = LABEL_PALETTE[len(data["labels"]) % len(LABEL_PALETTE)]
    accounts = raw.get("accounts")
    if isinstance(accounts, dict):
        for acc, names in accounts.items():
            if not isinstance(acc, str) or not isinstance(names, list):
                continue
            seen: list[str] = []
            for n in names:
                if isinstance(n, str) and n in data["labels"] and n not in seen:
                    seen.append(n)
            if seen:
                data["accounts"][acc] = seen
    return data


def _save_labels(root: Path, data: dict) -> None:
    path = labels_path(root)
    tmp = path.with_name(path.name + ".tmp")
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # атомарно: файл не останется наполовину записанным
    except OSError as e:
        raise SwitcherError(f"Не удалось сохранить метки: {e}") from e


def check_label_name(data: dict, name: str, ignore: str | None = None) -> str:
    """Возвращает очищенное имя метки или бросает SwitcherError (пусто, длинно, дубликат)."""
    clean = " ".join(str(name).split())
    if not clean:
        raise SwitcherError("Название метки не может быть пустым.")
    if len(clean) > LABEL_MAX_LEN:
        raise SwitcherError(f"Название метки длиннее {LABEL_MAX_LEN} символов.")
    for existing in data["labels"]:
        if existing.lower() == clean.lower() and existing != ignore:
            raise SwitcherError(f"Метка «{existing}» уже существует.")
    return clean


def next_label_color(data: dict) -> str:
    """Первый ещё не занятый цвет палитры (когда все заняты — по кругу)."""
    used = set(data["labels"].values())
    for c in LABEL_PALETTE:
        if c not in used:
            return c
    return LABEL_PALETTE[len(data["labels"]) % len(LABEL_PALETTE)]


def add_label(root: Path, name: str, color: str | None = None) -> dict:
    data = load_labels(root)
    clean = check_label_name(data, name)
    data["labels"][clean] = normalize_color(color) if color else next_label_color(data)
    _save_labels(root, data)
    return data


def remove_label(root: Path, name: str) -> dict:
    data = load_labels(root)
    if name not in data["labels"]:
        raise SwitcherError(f"Метки «{name}» нет.")
    del data["labels"][name]
    for acc in list(data["accounts"]):
        data["accounts"][acc] = [n for n in data["accounts"][acc] if n != name]
        if not data["accounts"][acc]:
            del data["accounts"][acc]
    _save_labels(root, data)
    return data


def recolor_label(root: Path, name: str, color: str) -> dict:
    data = load_labels(root)
    if name not in data["labels"]:
        raise SwitcherError(f"Метки «{name}» нет.")
    data["labels"][name] = normalize_color(color)
    _save_labels(root, data)
    return data


def rename_label(root: Path, old: str, new: str) -> dict:
    data = load_labels(root)
    if old not in data["labels"]:
        raise SwitcherError(f"Метки «{old}» нет.")
    clean = check_label_name(data, new, ignore=old)
    data["labels"] = {(clean if n == old else n): c for n, c in data["labels"].items()}
    for acc, names in data["accounts"].items():
        data["accounts"][acc] = [clean if n == old else n for n in names]
    _save_labels(root, data)
    return data


def set_account_label(root: Path, accounts: list[str], label: str, present: bool) -> dict:
    """Ставит (present=True) или снимает метку сразу у нескольких аккаунтов."""
    data = load_labels(root)
    if label not in data["labels"]:
        raise SwitcherError(f"Метки «{label}» нет.")
    for acc in accounts:
        names = [n for n in data["accounts"].get(acc, []) if n != label]
        if present:
            names.append(label)
        if names:
            data["accounts"][acc] = names
        else:
            data["accounts"].pop(acc, None)
    _save_labels(root, data)
    return data


def account_labels(data: dict, account: str) -> list[str]:
    """Метки аккаунта в порядке, заданном списком меток."""
    mine = set(data["accounts"].get(account, []))
    return [n for n in data["labels"] if n in mine]


def label_counts(data: dict) -> dict[str, int]:
    counts = {n: 0 for n in data["labels"]}
    for names in data["accounts"].values():
        for n in names:
            if n in counts:
                counts[n] += 1
    return counts
