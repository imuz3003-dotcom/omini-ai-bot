"""companion/ops.py — fayl amallari va yo'l qorovuli DARAJASIDA tekshiriladi.

Nima uchun kerak: agent foydalanuvchining haqiqiy kompyuterida ishlaydi. Bitta
noto'g'ri yo'l (`../../`, symlink, `.ssh`) barcha hujjatlarni ochib berishi yoki
yo'qotishi mumkin, shuning uchun bu fayldagi har bir rad etish testi xavfsizlik
cheginasi hisoblanadi. Testlar `tmp_path`dagi HAQIQIY fayllar bilan ishlaydi —
hech narsa mock qilinmaydi, chunki mock qilingan filesystem xatoni yashiradi.

Ishga tushirish:  python -m unittest discover -s . -p "test_*.py"   (butun loyiha)
                python -m unittest discover -s companion -v        (faqat shu bo'lim)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion import ops  # noqa: E402

MAX_LS = 60          # /ls bir chaqiruvda nechta joy ko'rsatadi
MAX_CAT = 3800       # /cat nechta belgi
MAX_TREE = 3         # /tree nechta chuqurlik
MAX_GET = 25 * 1024 * 1024


def sandbox(root, **kwargs):
    return ops.Sandbox(roots=[Path(root)], **kwargs)


class SandboxEscape(unittest.TestCase):
    """Ildizdan tashqariga chiqishning har bir yo'li rad etilishi kerak."""

    def setUp(self):
        self.base = Path(self.mkdtemp())
        self.root = self.base / "home"
        (self.root / "Documents").mkdir(parents=True)
        (self.root / "Documents" / "notes.txt").write_text("salom", encoding="utf-8")
        self.sb = sandbox(self.root)

    def mkdtemp(self):
        return Path(tempfile.mkdtemp(prefix="omini_ops_"))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_parent_escape_is_denied(self):
        with self.assertRaises(ops.Denied):
            self.sb.resolve("../outside.txt")

    def test_deep_parent_escape_is_denied(self):
        with self.assertRaises(ops.Denied):
            self.sb.resolve("Documents/../../etc/passwd")

    def test_absolute_path_outside_root_is_denied(self):
        outside = self.base / "secret.txt"
        outside.write_text("kalit", encoding="utf-8")
        with self.assertRaises(ops.Denied):
            self.sb.resolve(str(outside))

    def test_denied_before_missing(self):
        """Ildizdan tashqaridagi yo'l 'yo'q' deb emas, 'rad etildi' deb javob beradi.

        Aks holda hujumchi /ls orqali tashqi kataloglar mavjudligini so'ray oladi.
        """
        outside = self.base / "no-such-dir"
        with self.assertRaises(ops.Denied):
            ops.ls(self.sb, str(outside))

    def test_existing_file_inside_root_resolves(self):
        path = self.sb.resolve("Documents/notes.txt")
        self.assertEqual(path.name, "notes.txt")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink yo'q")
    def test_symlink_escape_is_denied(self):
        target = self.base / "outside"
        target.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("bu tizimda symlink yaratib bo'lmaydi (Windows: admin huquqi)")
        with self.assertRaises(ops.Denied):
            self.sb.resolve("link/any.txt")

    @unittest.skipUnless(os.name == "nt", "junction faqat Windows'da yasaladi")
    def test_junction_escape_is_denied(self):
        """Windows junction'ini `os.path.islink` symlink deb bermaydi — yagona
        himoya `resolve()` dan keyin ildizni qayta tekshirish."""
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("sir", encoding="utf-8")
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.root / "jlink"), str(outside)],
            capture_output=True,
        )
        if made.returncode != 0:
            self.skipTest("junction yaratib bo'lmadi: " + made.stderr.decode("utf-8", "replace"))
        with self.assertRaises(ops.Denied):
            self.sb.resolve("jlink/secret.txt")

    @unittest.skipUnless(os.name == "nt", "junction faqat Windows'da yasaladi")
    def test_write_cannot_plant_file_through_junction(self):
        outside = self.base / "outside"
        outside.mkdir()
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.root / "jlink"), str(outside)],
            capture_output=True,
        )
        if made.returncode != 0:
            self.skipTest("junction yaratib bo'lmadi")
        with self.assertRaises(ops.Denied):
            ops.write(self.sb, "jlink/planted.txt", "zarar")
        self.assertFalse((outside / "planted.txt").exists())

    def test_deny_glob_blocks_dotssh_inside_root(self):
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_ed25519").write_text("priv", encoding="utf-8")
        with self.assertRaises(ops.Denied):
            self.sb.resolve(".ssh/id_ed25519")

    def test_deny_glob_blocks_browser_profile(self):
        profile = self.root / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
        profile.mkdir(parents=True)
        with self.assertRaises(ops.Denied):
            self.sb.resolve("AppData/Local/Google/Chrome/User Data/Default")

    def test_similar_name_is_not_blocked_by_deny_glob(self):
        """`.ssh` rad etiladi, lekin `.ssh_backup` — yo'q: rad ro'yxati ortiqcha
        bloklamasin, aks holda foydalanuvchi o'z fayllarini yo'qotadi."""
        (self.root / ".ssh_backup").mkdir()
        path = self.sb.resolve(".ssh_backup")
        self.assertEqual(path.name, ".ssh_backup")

    @unittest.skipUnless(os.name == "nt", "Windows'ning yo'l registri")
    def test_root_case_mismatch_is_accepted_on_windows(self):
        upper = sandbox(str(self.root).upper())
        path = upper.resolve("documents/notes.txt")
        self.assertTrue(path.exists())

    def test_empty_roots_is_rejected(self):
        with self.assertRaises(ValueError):
            ops.Sandbox(roots=[])

    def test_file_as_root_is_rejected(self):
        with self.assertRaises(ValueError):
            ops.Sandbox(roots=[self.root / "Documents" / "notes.txt"])


class RelativeAndCwd(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.mkdtemp())
        (self.root / "Documents").mkdir()
        (self.root / "Documents" / "notes.txt").write_text("salom", encoding="utf-8")
        (self.root / "top.txt").write_text("yuqori", encoding="utf-8")

    def mkdtemp(self):
        return Path(tempfile.mkdtemp(prefix="omini_ops_"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_relative_path_uses_cwd(self):
        sb = sandbox(self.root, cwd=self.root / "Documents")
        path = sb.resolve("notes.txt")
        self.assertEqual(path.parent.name, "Documents")

    def test_ls_without_path_lists_cwd(self):
        sb = sandbox(self.root, cwd=self.root / "Documents")
        result = ops.ls(sb)
        self.assertEqual([e["name"] for e in result["entries"]], ["notes.txt"])
        self.assertEqual(Path(result["cwd"]).name, "Documents")

    def test_dotdot_inside_root_is_allowed(self):
        sb = sandbox(self.root, cwd=self.root / "Documents")
        result = ops.ls(sb, "..")
        names = [e["name"] for e in result["entries"]]
        self.assertIn("top.txt", names)


class ListOperations(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.mkdtemp())
        (self.root / "b_dir").mkdir()
        (self.root / "a.txt").write_text("salom", encoding="utf-8")
        (self.root / "empty.bin").write_bytes(b"\x00\x01\x02")

    def mkdtemp(self):
        return Path(tempfile.mkdtemp(prefix="omini_ops_"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_ls_reports_name_dir_and_size(self):
        result = ops.ls(sandbox(self.root))
        by_name = {e["name"]: e for e in result["entries"]}
        self.assertEqual(sorted(by_name), ["a.txt", "b_dir", "empty.bin"])
        self.assertTrue(by_name["b_dir"]["dir"])
        self.assertFalse(by_name["a.txt"]["dir"])
        self.assertEqual(by_name["a.txt"]["size"], 5)
        self.assertFalse(result["truncated"])

    def test_ls_caps_entries_and_marks_truncated(self):
        for index in range(MAX_LS + 5):
            (self.root / f"f{index:03d}.txt").write_text("x", encoding="utf-8")
        result = ops.ls(sandbox(self.root))
        self.assertEqual(len(result["entries"]), MAX_LS)
        self.assertTrue(result["truncated"])

    def test_ls_on_file_raises_not_a_directory(self):
        with self.assertRaises(NotADirectoryError):
            ops.ls(sandbox(self.root), "a.txt")

    def test_cat_returns_full_text(self):
        result = ops.cat(sandbox(self.root), "a.txt")
        self.assertEqual(result["text"], "salom")
        self.assertEqual(result["size"], 5)
        self.assertFalse(result["truncated"])
        self.assertFalse(result["binary"])

    def test_cat_truncates_and_offset_continues(self):
        digits = "".join(str(index % 10) for index in range(4000))
        (self.root / "big.txt").write_text(digits, encoding="utf-8")
        sb = sandbox(self.root)
        first = ops.cat(sb, "big.txt")
        self.assertEqual(len(first["text"]), MAX_CAT)
        self.assertTrue(first["truncated"])
        second = ops.cat(sb, "big.txt", offset=MAX_CAT)
        self.assertEqual(second["text"], "0123456789" * 20)
        self.assertFalse(second["truncated"])

    def test_cat_negative_offset_is_value_error(self):
        with self.assertRaises(ValueError):
            ops.cat(sandbox(self.root), "a.txt", offset=-1)

    def test_cat_next_lands_on_a_character_boundary(self):
        # Har biri 3 bayt bo'lgan belgi: 3800 baytlik oynaning oxiri belgini ikki bo'lakka
        # bo'lib, keyingi `/cat` o'rtadan boshlanib ketmasin — offset.shart shu.
        text = "’" * 2000
        (self.root / "kop.txt").write_text(text, encoding="utf-8")
        sb = sandbox(self.root)
        first = ops.cat(sb, "kop.txt")
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next"], 3798)  # 1266 ta to'liq belgi x 3 bayt
        rest = ops.cat(sb, "kop.txt", offset=first["next"])
        self.assertEqual(rest["text"], "’" * 734)
        self.assertFalse(rest["truncated"])

    def test_cat_detects_binary(self):
        result = ops.cat(sandbox(self.root), "empty.bin")
        self.assertTrue(result["binary"])
        self.assertEqual(result["text"], "")

    def test_find_matches_recursively(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "c.txt").write_text("x", encoding="utf-8")
        (self.root / "skip.log").write_text("x", encoding="utf-8")
        result = ops.find(sandbox(self.root), "*.txt")
        names = sorted(Path(match).name for match in result["matches"])
        self.assertEqual(names, ["a.txt", "c.txt"])
        self.assertEqual(result["count"], 2)

    def test_find_caps_at_two_hundred(self):
        for index in range(250):
            (self.root / f"n{index:03d}.dat").write_text("x", encoding="utf-8")
        result = ops.find(sandbox(self.root), "*.dat")
        self.assertEqual(result["count"], 200)
        self.assertTrue(result["truncated"])

    def test_tree_lists_three_levels_only(self):
        (self.root / "d1" / "d2" / "d3").mkdir(parents=True)
        (self.root / "d1" / "y.txt").write_text("x", encoding="utf-8")
        (self.root / "d1" / "d2" / "z.txt").write_text("x", encoding="utf-8")
        (self.root / "d1" / "d2" / "d3" / "deep.txt").write_text("x", encoding="utf-8")
        result = ops.tree(sandbox(self.root))
        joined = "\n".join(result["lines"])
        self.assertIn("y.txt", joined)
        self.assertIn("z.txt", joined)
        self.assertNotIn("deep.txt", joined)

    def test_disk_reports_each_root(self):
        result = ops.disk(sandbox(self.root))
        volume = result["volumes"][0]
        self.assertGreater(volume["total"], 0)
        self.assertGreaterEqual(volume["total"], volume["free"])

    def test_stat_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            ops.stat(sandbox(self.root), "nope.txt")

    def test_stat_reports_size_and_dir_flag(self):
        sb = sandbox(self.root)
        file_stat = ops.stat(sb, "a.txt")
        dir_stat = ops.stat(sb, "b_dir")
        self.assertEqual(file_stat["size"], 5)
        self.assertFalse(file_stat["dir"])
        self.assertTrue(dir_stat["dir"])
        self.assertGreater(file_stat["mtime"], 0)


class WriteOperations(unittest.TestCase):
    def setUp(self):
        # base — root'ning OTA katalogi: ildizdan tashqariga yozib yuborilgan
        # fayl aynan shu yerdan izlanadi (umumiy %TEMP% emas).
        self.base = Path(tempfile.mkdtemp(prefix="omini_ops_"))
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "a.txt").write_text("salom", encoding="utf-8")
        self.sb = sandbox(self.root)

    def mkdtemp(self):
        return Path(tempfile.mkdtemp(prefix="omini_ops_"))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_mkdir_creates_parents(self):
        result = ops.mkdir(self.sb, "x/y/z")
        self.assertTrue((self.root / "x" / "y" / "z").is_dir())
        self.assertEqual(Path(result["path"]).name, "z")

    def test_mkdir_existing_is_not_an_error(self):
        ops.mkdir(self.sb, "a_dir")
        ops.mkdir(self.sb, "a_dir")

    def test_write_then_cat_roundtrip(self):
        ops.write(self.sb, "notes/todo.md", "birinchi vazifa")
        result = ops.cat(self.sb, "notes/todo.md")
        self.assertEqual(result["text"], "birinchi vazifa")

    def test_write_overwrites_and_reports_bytes(self):
        result = ops.write(self.sb, "a.txt", "salom salom")
        self.assertEqual(result["bytes"], 11)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "salom salom")

    def test_write_outside_root_is_denied(self):
        with self.assertRaises(ops.Denied):
            ops.write(self.sb, "../evil.txt", "zarar")
        self.assertFalse((self.root.parent / "evil.txt").exists())

    def test_mv_moves_within_root(self):
        ops.mkdir(self.sb, "done")
        result = ops.mv(self.sb, "a.txt", "done/moved.txt")
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual((self.root / "done" / "moved.txt").read_text(encoding="utf-8"), "salom")
        self.assertEqual(Path(result["dst"]).name, "moved.txt")

    def test_mv_destination_outside_root_is_denied(self):
        with self.assertRaises(ops.Denied):
            ops.mv(self.sb, "a.txt", "../stolen.txt")
        self.assertTrue((self.root / "a.txt").exists())

    def test_mv_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            ops.mv(self.sb, "nope.txt", "ok.txt")

    def test_cp_keeps_source(self):
        result = ops.cp(self.sb, "a.txt", "copy.txt")
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "salom")
        self.assertEqual((self.root / "copy.txt").read_text(encoding="utf-8"), "salom")
        self.assertEqual(result["bytes"], 5)

    def test_rm_removes_single_file(self):
        result = ops.rm(self.sb, "a.txt")
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual(result["size"], 5)

    def test_rm_refuses_directory(self):
        (self.root / "dir").mkdir()
        with self.assertRaises(ops.Denied):
            ops.rm(self.sb, "dir")
        self.assertTrue((self.root / "dir").exists())

    def test_rm_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            ops.rm(self.sb, "nope.txt")

    def test_get_returns_bytes(self):
        result = ops.get(self.sb, "a.txt")
        self.assertEqual(result["data"], b"salom")
        self.assertEqual(result["size"], 5)

    def test_get_refuses_oversized_file(self):
        big = self.root / "big.bin"
        with big.open("wb") as handle:
            handle.truncate(MAX_GET + 1)
        with self.assertRaises(ops.Denied):
            ops.get(self.sb, "big.bin")

    def test_put_writes_bytes(self):
        result = ops.put(self.sb, "in.bin", b"\x00\x01\x02")
        self.assertEqual((self.root / "in.bin").read_bytes(), b"\x00\x01\x02")
        self.assertEqual(result["bytes"], 3)

    def test_put_outside_root_is_denied(self):
        with self.assertRaises(ops.Denied):
            ops.put(self.sb, "../evil.bin", b"zarar")
        self.assertFalse((self.base / "evil.bin").exists())


if __name__ == "__main__":
    unittest.main()
