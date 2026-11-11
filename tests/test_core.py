import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tdata_core as core  # noqa: E402

HEX_DIR = "D877F783D5D3EF8C"


def make_tdata(path: Path) -> None:
    """Минимальная правдоподобная папка tdata (достаточно для find_accounts, но
    не для validate_tdata — файлы там умышленно заведомо пустые/маленькие)."""
    (path / HEX_DIR).mkdir(parents=True)
    (path / HEX_DIR / "map1").write_bytes(b"x")
    (path / "key_datas").write_bytes(b"k")


def make_valid_tdata(path: Path) -> None:
    """Папка tdata, которая проходит validate_tdata без единой проблемы."""
    (path / HEX_DIR).mkdir(parents=True)
    (path / HEX_DIR / "map0").write_bytes(b"m" * 64)
    (path / HEX_DIR / "map1").write_bytes(b"m" * 64)
    (path / "key_datas").write_bytes(b"k" * 64)


def make_zip(archive: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return archive


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "tdata"
        self.root.mkdir()
        # настройки пишем во временную папку, а не рядом с кодом
        patcher = mock.patch.object(core, "SETTINGS_FILE", self.tmp / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class FindAccountsTests(Base):
    def test_classic_flat_nested_and_hidden(self):
        make_tdata(self.root / "classic" / "tdata")
        make_tdata(self.root / "flat")
        make_tdata(self.root / "group" / "inner")  # группа/папка = содержимое tdata
        make_tdata(self.root / ".import_tmp" / "tdata")  # служебная — игнорируется
        names = [n for n, _ in core.find_accounts(self.root)]
        self.assertEqual(names, ["classic", "flat", "group/inner"])

    def test_missing_root(self):
        self.assertEqual(core.find_accounts(self.tmp / "nope"), [])

    def test_resolve_account(self):
        accs = [("Work", Path("/a")), ("Personal", Path("/b")), ("Personal2", Path("/c"))]
        self.assertEqual(core.resolve_account(accs, "Work")[1], Path("/a"))
        self.assertEqual(core.resolve_account(accs, "work")[1], Path("/a"))
        self.assertEqual(core.resolve_account(accs, "Personal")[1], Path("/b"))  # точное имеет приоритет
        self.assertEqual(core.resolve_account(accs, "2")[1], Path("/b"))  # номер
        with self.assertRaises(core.SwitcherError):
            core.resolve_account(accs, "zzz")
        with self.assertRaises(core.SwitcherError):
            core.resolve_account(accs, "pers")  # неоднозначно


class ImportTests(Base):
    def test_single_account_named_after_archive(self):
        z = make_zip(self.tmp / "Ivan.zip", {
            f"tdata/{HEX_DIR}/map1": b"x", "tdata/key_datas": b"k",
        })
        res = core.import_archive(z, self.root)
        self.assertEqual([n for n, _ in res], ["Ivan"])
        self.assertTrue((self.root / "Ivan" / "tdata" / "key_datas").is_file())
        self.assertEqual([n for n, _ in core.find_accounts(self.root)], ["Ivan"])
        self.assertEqual([p.name for p in self.root.iterdir()], ["Ivan"])  # временные папки убраны

    def test_content_at_archive_top_level(self):
        z = make_zip(self.tmp / "flat.zip", {"key_datas": b"k", f"{HEX_DIR}/map1": b"x"})
        res = core.import_archive(z, self.root)
        self.assertTrue((res[0][1] / "tdata" / "key_datas").is_file())

    def test_deeply_nested_single_account(self):
        z = make_zip(self.tmp / "shop.zip", {
            "Telegram Desktop/tdata/key_datas": b"k",
            f"Telegram Desktop/tdata/{HEX_DIR}/map1": b"x",
            "Telegram Desktop/readme.txt": b"hi",
        })
        res = core.import_archive(z, self.root)
        self.assertEqual(res[0][0], "shop")
        self.assertFalse((res[0][1] / "readme.txt").exists())

    def test_multiple_accounts_use_folder_names(self):
        z = make_zip(self.tmp / "pack.zip", {
            "acc1/tdata/key_datas": b"k", "acc2/tdata/key_datas": b"k",
        })
        res = core.import_archive(z, self.root)
        self.assertEqual(sorted(n for n, _ in res), ["acc1", "acc2"])

    def test_name_collision_gets_suffix(self):
        make_tdata(self.root / "Ivan" / "tdata")
        z = make_zip(self.tmp / "Ivan.zip", {"tdata/key_datas": b"k"})
        res = core.import_archive(z, self.root)
        self.assertEqual(res[0][0], "Ivan_2")

    def test_custom_name_and_unsafe_chars(self):
        z = make_zip(self.tmp / "a.zip", {"tdata/key_datas": b"k"})
        res = core.import_archive(z, self.root, name='bad:/name?')
        self.assertEqual(res[0][0], "bad__name_")

    def test_no_tdata_raises_and_cleans_up(self):
        z = make_zip(self.tmp / "junk.zip", {"hello.txt": b"hi"})
        with self.assertRaises(core.SwitcherError):
            core.import_archive(z, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_zip_slip_is_contained(self):
        z = make_zip(self.tmp / "evil.zip", {
            "../../evil.txt": b"pwned", "/abs_evil.txt": b"pwned",
            "tdata/key_datas": b"k",
        })
        core.import_archive(z, self.root)
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertFalse((self.tmp.parent / "evil.txt").exists())
        self.assertFalse(Path("/abs_evil.txt").exists())

    @unittest.skipIf(sys.platform == "win32", "symlink в zip — только POSIX")
    def test_symlink_entries_are_skipped(self):
        z = self.tmp / "link.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("tdata/key_datas", b"k")
            info = zipfile.ZipInfo("tdata/sneaky")
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
        res = core.import_archive(z, self.root)
        self.assertFalse((res[0][1] / "tdata" / "sneaky").exists())

    def test_password_required_is_reported(self):
        z = make_zip(self.tmp / "p.zip", {"tdata/key_datas": b"k"})
        err = RuntimeError("File 'tdata/key_datas' is encrypted, password required for extraction")
        with mock.patch.object(zipfile.ZipFile, "extract", side_effect=err):
            with self.assertRaises(core.PasswordRequired):
                core.import_archive(z, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupt_zip(self):
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"PK\x03\x04 definitely not a zip")
        with self.assertRaises(core.SwitcherError):
            core.import_archive(bad, self.root)

    def test_missing_file(self):
        with self.assertRaises(core.SwitcherError):
            core.import_archive(self.tmp / "ghost.zip", self.root)

    def test_detect_format_by_signature(self):
        cases = {b"Rar!\x1a\x07\x01\x00": "rar", b"7z\xbc\xaf\x27\x1c\x00\x04": "7z",
                 b"PK\x03\x04rest": "zip"}
        for i, (head, fmt) in enumerate(cases.items()):
            f = self.tmp / f"noext{i}"
            f.write_bytes(head)
            self.assertEqual(core.detect_archive_format(f), fmt)
        weird = self.tmp / "x.bin"
        weird.write_bytes(b"hello")
        with self.assertRaises(core.SwitcherError):
            core.detect_archive_format(weird)

    def test_rar_without_any_extractor(self):
        rar = self.tmp / "a.rar"
        rar.write_bytes(b"Rar!\x1a\x07\x01\x00")
        with mock.patch.object(core, "_find_tools", return_value=[]):
            with self.assertRaises(core.SwitcherError) as ctx:
                core.import_archive(rar, self.root)
        self.assertIn("7-Zip", str(ctx.exception))

    def test_rar_pipeline_with_fake_extractor(self):
        """Проверяем цепочку rar → внешний тул → поиск tdata → перенос, подменив 7z."""
        rar = self.tmp / "Petr.rar"
        rar.write_bytes(b"Rar!\x1a\x07\x01\x00")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            out_arg = next(a for a in cmd if a.startswith("-o"))
            make_tdata(Path(out_arg[2:]) / "Telegram" / "tdata")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with mock.patch.object(core, "_find_tools", return_value=[("7z", "/fake/7z")]), \
                mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            res = core.import_archive(rar, self.root, password="secret")
        self.assertEqual(res[0][0], "Petr")
        self.assertIn("-psecret", calls[0])
        self.assertTrue((self.root / "Petr" / "tdata" / "key_datas").is_file())

    def test_external_password_error_maps_to_password_required(self):
        rar = self.tmp / "enc.rar"
        rar.write_bytes(b"Rar!\x1a\x07\x01\x00")
        fail = subprocess.CompletedProcess([], 2, b"", b"ERROR: Wrong password : x")
        with mock.patch.object(core, "_find_tools", return_value=[("7z", "/fake/7z")]), \
                mock.patch.object(core.subprocess, "run", return_value=fail):
            with self.assertRaises(core.PasswordRequired):
                core.import_archive(rar, self.root)

    def test_falls_back_to_next_tool(self):
        rar = self.tmp / "Fb.rar"
        rar.write_bytes(b"Rar!\x1a\x07\x01\x00")

        def fake_run(cmd, **kwargs):
            if cmd[0] == "/fake/7z":
                return subprocess.CompletedProcess(cmd, 2, b"", b"Unsupported archive")
            make_tdata(Path(cmd[cmd.index("-C") + 1]))
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        tools = [("7z", "/fake/7z"), ("tar", "/fake/bsdtar")]
        with mock.patch.object(core, "_find_tools", return_value=tools), \
                mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            res = core.import_archive(rar, self.root)
        self.assertEqual(res[0][0], "Fb")

    @unittest.skipIf(sys.platform == "win32", "symlink требует прав на Windows")
    def test_strip_links_removes_symlinks(self):
        d = self.tmp / "ex"
        d.mkdir()
        (d / "real.txt").write_text("x")
        os.symlink("/etc", d / "evil_dir")
        os.symlink(d / "real.txt", d / "evil_file")
        core._strip_links(d)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["real.txt"])


class ValidateTests(Base):
    def test_valid_tdata_has_no_problems(self):
        make_valid_tdata(self.root / "ok")
        self.assertEqual(core.validate_tdata(self.root / "ok"), [])
        self.assertTrue(core.is_tdata_valid(self.root / "ok"))

    def test_not_a_tdata_folder(self):
        (self.root / "junk").mkdir()
        (self.root / "junk" / "readme.txt").write_text("hi")
        problems = core.validate_tdata(self.root / "junk")
        self.assertEqual(len(problems), 1)
        self.assertIn("Не похоже на tdata", problems[0])

    def test_missing_folder(self):
        problems = core.validate_tdata(self.root / "ghost")
        self.assertIn("Папка не найдена", problems[0])

    def test_empty_key_datas(self):
        make_valid_tdata(self.root / "a")
        (self.root / "a" / "key_datas").write_bytes(b"")
        problems = core.validate_tdata(self.root / "a")
        self.assertTrue(any("пустой" in p for p in problems))
        self.assertFalse(core.is_tdata_valid(self.root / "a"))

    def test_truncated_key_datas(self):
        make_valid_tdata(self.root / "a")
        (self.root / "a" / "key_datas").write_bytes(b"k")
        problems = core.validate_tdata(self.root / "a")
        self.assertTrue(any("подозрительно маленький" in p for p in problems))

    def test_missing_key_datas(self):
        make_valid_tdata(self.root / "a")
        (self.root / "a" / "key_datas").unlink()
        problems = core.validate_tdata(self.root / "a")
        self.assertTrue(any("Нет key_datas" in p for p in problems))

    def test_missing_map_files(self):
        make_valid_tdata(self.root / "a")
        (self.root / "a" / HEX_DIR / "map0").unlink()
        (self.root / "a" / HEX_DIR / "map1").unlink()
        problems = core.validate_tdata(self.root / "a")
        self.assertTrue(any("нет map0/map1" in p for p in problems))

    def test_empty_map_file(self):
        make_valid_tdata(self.root / "a")
        (self.root / "a" / HEX_DIR / "map0").write_bytes(b"")
        problems = core.validate_tdata(self.root / "a")
        self.assertTrue(any("map0" in p and "пустой" in p for p in problems))

    def test_missing_hex_dir(self):
        self.root.joinpath("a").mkdir()
        (self.root / "a" / "key_datas").write_bytes(b"k" * 64)
        problems = core.validate_tdata(self.root / "a")
        self.assertTrue(any("Нет hex-папки" in p for p in problems))

    def test_tdata_content_dir_handles_both_layouts(self):
        make_valid_tdata(self.root / "classic" / "tdata")
        make_valid_tdata(self.root / "flat")
        self.assertEqual(core.tdata_content_dir(self.root / "classic"), self.root / "classic" / "tdata")
        self.assertEqual(core.tdata_content_dir(self.root / "flat"), self.root / "flat")

    def test_cli_validate_reports_all_and_specific(self):
        import contextlib
        import io
        import tdata_switcher as cli
        make_valid_tdata(self.root / "Ivan" / "tdata")
        make_tdata(self.root / "Petr" / "tdata")  # заведомо "битый" (пустые файлы)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--root", str(self.root), "--validate"])
        self.assertEqual(code, 1)  # Petr битый
        self.assertIn("Ivan: OK", out.getvalue())
        self.assertIn("Petr: есть проблемы", out.getvalue())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--root", str(self.root), "--validate", "Ivan"])
        self.assertEqual(code, 0)
        self.assertIn("Ivan: OK", out.getvalue())
        self.assertNotIn("Petr", out.getvalue())

    def test_cli_list_check_marks_broken(self):
        import contextlib
        import io
        import tdata_switcher as cli
        make_valid_tdata(self.root / "Ivan" / "tdata")
        make_tdata(self.root / "Petr" / "tdata")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--root", str(self.root), "--list", "--check"])
        text = out.getvalue()
        self.assertIn("Ivan   [+] OK", text)
        self.assertIn("Petr   [!] проблем", text)


class ShortcutTests(Base):
    def test_shortcut_created_and_names_sanitised(self):
        desk = self.tmp / "Desktop"
        desk.mkdir()
        p = core.create_shortcut("work/main", self.root, directory=desk)
        self.assertTrue(p.is_file())
        self.assertEqual(p.parent, desk)
        self.assertNotIn("/", p.name)
        self.assertTrue(p.name.startswith("Telegram - work_main"))

    @unittest.skipIf(sys.platform == "win32", ".desktop — только Linux")
    def test_desktop_entry_contains_run_args(self):
        desk = self.tmp / "Desktop"
        desk.mkdir()
        p = core.create_shortcut('we"ird $name', self.root, directory=desk)
        text = p.read_text(encoding="utf-8")
        self.assertIn("--run", text)
        self.assertIn('\\"', text)    # кавычка экранирована
        self.assertIn("\\$name", text)  # $ экранирован
        self.assertIn("--root", text)  # корень не по умолчанию → передаётся

    def test_missing_directory(self):
        with self.assertRaises(core.SwitcherError):
            core.create_shortcut("x", self.root, directory=self.tmp / "nope")


class LabelCliTests(Base):
    def run_cli(self, *argv):
        import contextlib
        import io
        import tdata_switcher as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(cli, "report_error") as err:
            code = cli.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), [c.args[0] for c in err.call_args_list]

    def test_label_commands(self):
        make_tdata(self.root / "Ivan" / "tdata")
        make_tdata(self.root / "Petr" / "tdata")
        code, out, errs = self.run_cli("--label-add", "Работа=#539bf5", "--tag", "ivan", "Работа")
        self.assertEqual((code, errs), (0, []))
        code, out, _ = self.run_cli("--list")
        self.assertIn("Ivan   [Работа]", out)
        self.assertNotIn("Petr   [", out)
        self.assertEqual(core.load_labels(self.root)["labels"]["Работа"], "#539bf5")
        code, out, _ = self.run_cli("--untag", "Ivan", "Работа", "--label-remove", "Работа")
        self.assertEqual(code, 0)
        self.assertEqual(core.load_labels(self.root), {"labels": {}, "accounts": {}})

    def test_errors_do_not_stop_other_commands(self):
        make_tdata(self.root / "Ivan" / "tdata")
        code, _out, errs = self.run_cli(
            "--label-add", "A", "--tag", "ghost", "A", "--tag", "Ivan", "Nope", "--tag", "Ivan", "A")
        self.assertEqual(code, 1)
        self.assertEqual(len(errs), 2)  # аккаунт не найден + метки нет
        self.assertEqual(core.load_labels(self.root)["accounts"], {"Ivan": ["A"]})


class LaunchTests(Base):
    def test_launch_without_telegram(self):
        make_tdata(self.root / "a" / "tdata")
        with mock.patch.object(core, "find_telegram", return_value=None):
            with self.assertRaises(core.TelegramNotFound):
                core.launch("a", self.root / "a")

    def test_launch_passes_workdir_and_saves_last(self):
        make_tdata(self.root / "a" / "tdata")
        tg = self.tmp / "Telegram.exe"
        tg.write_text("")
        with mock.patch.object(core.subprocess, "Popen") as popen:
            core.launch("a", self.root / "a", telegram=tg)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[1], "-workdir")
        self.assertEqual(Path(argv[2]), (self.root / "a").resolve())
        self.assertEqual(core.load_last(), "a")

    def test_settings_roundtrip_keeps_other_keys(self):
        core.save_settings(telegram="C:/x/Telegram.exe")
        core.save_last("acc")
        s = core.load_settings()
        self.assertEqual(s["telegram"], "C:/x/Telegram.exe")
        self.assertEqual(s["last"], "acc")


class LabelTests(Base):
    def test_add_assign_and_persist(self):
        core.add_label(self.root, "Работа", "#539BF5")
        core.add_label(self.root, "Личные")  # цвет подбирается из палитры
        core.set_account_label(self.root, ["Ivan", "Petr"], "Работа", True)
        data = core.set_account_label(self.root, ["Ivan"], "Личные", True)
        fresh = core.load_labels(self.root)  # читаем с диска заново
        self.assertEqual(fresh, data)
        self.assertEqual(fresh["labels"]["Работа"], "#539bf5")
        self.assertEqual(core.account_labels(fresh, "Ivan"), ["Работа", "Личные"])
        self.assertEqual(core.account_labels(fresh, "Petr"), ["Работа"])
        self.assertEqual(core.account_labels(fresh, "Nobody"), [])
        self.assertEqual(core.label_counts(fresh), {"Работа": 2, "Личные": 1})

    def test_auto_colors_are_distinct_and_cycle(self):
        for i in range(len(core.LABEL_PALETTE) + 2):
            data = core.add_label(self.root, f"L{i}")
        colors = list(data["labels"].values())
        self.assertEqual(len(set(colors[:len(core.LABEL_PALETTE)])), len(core.LABEL_PALETTE))
        self.assertEqual(len(colors), len(core.LABEL_PALETTE) + 2)  # палитра закончилась — по кругу

    def test_unassign_and_empty_cleanup(self):
        core.add_label(self.root, "A")
        core.set_account_label(self.root, ["x"], "A", True)
        data = core.set_account_label(self.root, ["x"], "A", False)
        self.assertEqual(data["accounts"], {})

    def test_assign_is_idempotent(self):
        core.add_label(self.root, "A")
        core.set_account_label(self.root, ["x"], "A", True)
        data = core.set_account_label(self.root, ["x"], "A", True)
        self.assertEqual(data["accounts"]["x"], ["A"])

    def test_name_validation(self):
        core.add_label(self.root, "Work")
        for bad in ("", "   ", "work", "WORK", "x" * (core.LABEL_MAX_LEN + 1)):
            with self.assertRaises(core.SwitcherError, msg=repr(bad)):
                core.add_label(self.root, bad)
        data = core.add_label(self.root, "  Two   words ")
        self.assertIn("Two words", data["labels"])

    def test_unknown_label_errors(self):
        for fn in (lambda: core.remove_label(self.root, "nope"),
                   lambda: core.recolor_label(self.root, "nope", "#fff"),
                   lambda: core.rename_label(self.root, "nope", "x"),
                   lambda: core.set_account_label(self.root, ["a"], "nope", True)):
            with self.assertRaises(core.SwitcherError):
                fn()

    def test_remove_label_unassigns_everywhere(self):
        core.add_label(self.root, "A")
        core.add_label(self.root, "B")
        core.set_account_label(self.root, ["x", "y"], "A", True)
        core.set_account_label(self.root, ["y"], "B", True)
        data = core.remove_label(self.root, "A")
        self.assertEqual(data["accounts"], {"y": ["B"]})
        self.assertEqual(list(data["labels"]), ["B"])

    def test_rename_keeps_order_color_and_assignments(self):
        core.add_label(self.root, "A", "#111111")
        core.add_label(self.root, "B", "#222222")
        core.set_account_label(self.root, ["x"], "A", True)
        data = core.rename_label(self.root, "A", "Alpha")
        self.assertEqual(list(data["labels"]), ["Alpha", "B"])
        self.assertEqual(data["labels"]["Alpha"], "#111111")
        self.assertEqual(data["accounts"]["x"], ["Alpha"])
        with self.assertRaises(core.SwitcherError):  # занято другой меткой (без учёта регистра)
            core.rename_label(self.root, "Alpha", "b")
        data = core.rename_label(self.root, "Alpha", "ALPHA")  # смена регистра своей метки — можно
        self.assertIn("ALPHA", data["labels"])

    def test_recolor(self):
        core.add_label(self.root, "A", "#111111")
        data = core.recolor_label(self.root, "A", "#F00")
        self.assertEqual(data["labels"]["A"], "#ff0000")

    def test_normalize_color_and_contrast(self):
        self.assertEqual(core.normalize_color("ABC"), "#aabbcc")
        self.assertEqual(core.normalize_color(" #AABBCC "), "#aabbcc")
        for bad in ("", "red", "#12", "#1234567", "#gggggg"):
            with self.assertRaises(core.SwitcherError, msg=bad):
                core.normalize_color(bad)
        self.assertEqual(core.text_color_for("#ffffff"), "#000000")
        self.assertEqual(core.text_color_for("#000000"), "#ffffff")
        self.assertEqual(core.text_color_for("#e3b341"), "#000000")  # жёлтый — тёмный текст
        self.assertEqual(core.text_color_for("#986ee2"), "#ffffff")  # фиолетовый — светлый

    def test_corrupt_file_is_tolerated(self):
        p = core.labels_path(self.root)
        for junk in ("not json", "[]", '{"labels": 5, "accounts": 7}', ""):
            p.write_text(junk, encoding="utf-8")
            self.assertEqual(core.load_labels(self.root), {"labels": {}, "accounts": {}})

    def test_partially_bad_data_is_cleaned(self):
        core.labels_path(self.root).write_text(
            '{"labels": {"A": "#abc", "B": "not-a-color", "": "#fff", "7": 1},'
            ' "accounts": {"x": ["A", "A", "ghost", 3], "y": ["ghost"], "z": "A"}}',
            encoding="utf-8")
        data = core.load_labels(self.root)
        self.assertEqual(data["labels"]["A"], "#aabbcc")
        self.assertIn(data["labels"]["B"], core.LABEL_PALETTE)  # битый цвет заменён
        self.assertNotIn("", data["labels"])
        self.assertEqual(data["accounts"], {"x": ["A"]})  # дубли и неизвестные метки убраны

    def test_find_accounts_ignores_labels_file(self):
        make_tdata(self.root / "a" / "tdata")
        core.add_label(self.root, "A")
        self.assertEqual([n for n, _ in core.find_accounts(self.root)], ["a"])

    def test_atomic_save_leaves_no_tmp(self):
        core.add_label(self.root, "A")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["labels.json"])


if __name__ == "__main__":
    unittest.main()
