"""Fayl amallari va yo'l qorovuli — qurilmada (Windows/Termux) ishlaydi.

Tarmoqdan butunlay mustaqil: har funksiya qator qaytaradi yoki `Denied` /
OSError ko'taradi. Xavfsizlik shu yerda — `Sandbox.resolve()` ildizdan
tashqariga chiqishning barcha yo'llarini uzadi, agentdagi chaqiruvchi
qatlam faqat natijani Telegramga uzatadi.
"""

import fnmatch
import os
import shutil
import stat as stat_module
from pathlib import Path

MAX_LIST_ENTRIES = 60
MAX_CAT_CHARS = 3800
MAX_FIND_RESULTS = 200
MAX_TREE_LINES = 200
MAX_TREE_DEPTH = 3
# Fayl base64 ko'rinishida bitta JSON kadrda uzatiladi (kadr ~1.37 barobar
# kengayadi), shuning uchun chegara Telegramning 50 MB'idan ancha past.
MAX_GET_BYTES = 8 * 1024 * 1024

# Rad etilgan yo'llar `*` bilan yoziladi: `*` Python'ning fnmatch'ida katalog
# bo'lgichlaridan ham o'tadi, shuning uchun bir andoza katalogning o'zini va
# ichidagilarni birga beradi.
DEFAULT_DENY = (
    "*/.ssh", "*/.ssh/*",
    "*/.aws", "*/.aws/*",
    "*/User Data", "*/User Data/*",
    "*/NTUSER.DAT", "*/ntuser.dat.LOG*",
)


class Denied(Exception):
    """So'rov ruxsat etilgan ildizdan tashqariga chiqmoqchi edi."""


def _inside(root: Path, target: Path) -> bool:
    left = os.path.normcase(str(root)).rstrip("\\/")
    right = os.path.normcase(str(target))
    return right == left or right.startswith(left + os.sep)


class Sandbox:
    def __init__(self, roots, deny=DEFAULT_DENY, cwd=None):
        resolved = []
        for raw in roots:
            path = Path(raw).expanduser()
            if not path.is_dir():
                raise ValueError(f"ildiz mavjud emas yoki katalog emas: {raw}")
            resolved.append(path.resolve())
        if not resolved:
            raise ValueError("kamida bitta ruxsat etilgan ildiz kerak")
        self.roots = resolved
        self.deny = tuple(deny)
        base = Path(cwd).expanduser() if cwd is not None else resolved[0]
        if not any(_inside(root, base) for root in resolved):
            raise ValueError(f"joriy katalog ildizlardan birida emas: {base}")
        self.cwd = base.resolve()

    def _reject_denied(self, path: Path) -> None:
        form = os.path.normcase(path.as_posix())
        for pattern in self.deny:
            if fnmatch.fnmatch(form, os.path.normcase(pattern)):
                raise Denied(f"ruxsat yo'q: bu yo'l himoyalangan ({pattern})")

    def resolve(self, raw) -> Path:
        """Xom yo'lni ildiz ichidagi haqiqiy yo'lga aylantiradi yoki Denied beradi.

        `path.resolve()` symlink va junction'larni ochgach tekshiriladi — shuning
        uchun havola qayerga borsa ham ildizdan chiqish aniqlanadi.
        """
        text = str(raw or "").strip().strip('"').strip("'")
        if not text:
            raise ValueError("yo'l bo'sh")
        path = Path(os.path.expandvars(os.path.expanduser(text)))
        if not path.is_absolute():
            path = self.cwd / path
        path = Path(os.path.normpath(str(path)))
        for pattern_root in self.roots:
            if not _inside(pattern_root, path):
                continue
            # Mavjudlik tekshiruvidan oldin: aks holda /ls orqali tashqi
            # kataloglarning bor-yo'qligini so'rab bo'lardi.
            self._reject_denied(path)
            target = path.resolve(strict=False)
            if not any(_inside(root, target) for root in self.roots):
                raise Denied(f"ruxsat yo'q: {target} ildizdan tashqarida")
            self._reject_denied(target)
            return target
        allowed = ", ".join(str(root) for root in self.roots)
        raise Denied(f"ruxsat yo'q: {path} ruxsat etilgan ildizdan tashqarida ({allowed})")


def _entry(path: Path) -> dict:
    info = path.stat()
    return {
        "name": path.name,
        "dir": stat_module.S_ISDIR(info.st_mode),
        "size": info.st_size,
        "mtime": int(info.st_mtime),
    }


def _sorted_children(directory: Path):
    children = [(child.is_dir(), child.name.lower(), child) for child in directory.iterdir()]
    return [child for _, _, child in sorted(children, key=lambda item: (not item[0], item[1]))]


def _base(sb: Sandbox, path=None) -> Path:
    return sb.cwd if path is None or str(path).strip() == "" else sb.resolve(path)


def _need(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"bunday yo'l yo'q: {path}")
    return path


def ls(sb: Sandbox, path=None) -> dict:
    directory = _need(_base(sb, path))
    if not directory.is_dir():
        raise NotADirectoryError(f"bu fayl, katalog emas: {directory}")
    children = _sorted_children(directory)
    return {
        "cwd": str(directory),
        "entries": [_entry(child) for child in children[:MAX_LIST_ENTRIES]],
        "truncated": len(children) > MAX_LIST_ENTRIES,
    }


def cat(sb: Sandbox, path, offset: int = 0) -> dict:
    if int(offset) < 0:
        raise ValueError("offset manfiy bo'lishi mumkin emas")
    target = _need(sb.resolve(path))
    if target.is_dir():
        raise IsADirectoryError(f"bu katalog, `/ls` bilan ko'ring: {target}")
    size = target.stat().st_size
    with target.open("rb") as handle:
        handle.seek(int(offset))
        block = handle.read(MAX_CAT_CHARS + 1)
    binary = b"\x00" in block
    truncated = len(block) > MAX_CAT_CHARS or int(offset) + len(block) < size
    if binary:
        return {"text": "", "offset": int(offset), "size": size, "truncated": truncated, "binary": True}
    return {
        "text": block[:MAX_CAT_CHARS].decode("utf-8", errors="replace"),
        "offset": int(offset),
        "size": size,
        "truncated": truncated,
        "binary": False,
    }


def find(sb: Sandbox, pattern: str, path=None) -> dict:
    base = _need(_base(sb, path))
    if not base.is_dir():
        raise NotADirectoryError(f"bu fayl, katalog emas: {base}")
    matches = []
    for current, dirnames, filenames in os.walk(str(base), followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(current, name))]
        for name in sorted(dirnames + filenames):
            if not fnmatch.fnmatch(name, pattern):
                continue
            candidate = Path(current) / name
            try:
                sb.resolve(str(candidate))
            except Denied:
                continue
            matches.append(str(candidate))
            if len(matches) >= MAX_FIND_RESULTS:
                return {"matches": matches, "count": len(matches), "truncated": True}
    return {"matches": matches, "count": len(matches), "truncated": False}


def tree(sb: Sandbox, path=None) -> dict:
    base = _need(_base(sb, path))
    if not base.is_dir():
        raise NotADirectoryError(f"bu fayl, katalog emas: {base}")
    lines = [base.name + "/"]
    state = {"truncated": False}

    def walk(directory: Path, prefix: str, depth: int) -> None:
        children = _sorted_children(directory)
        for index, child in enumerate(children):
            if len(lines) >= MAX_TREE_LINES:
                state["truncated"] = True
                return
            last = index == len(children) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{child.name}{'/' if child.is_dir() else ''}")
            if child.is_dir() and not child.is_symlink() and depth < MAX_TREE_DEPTH:
                walk(child, prefix + ("    " if last else "│   "), depth + 1)

    walk(base, "", 1)
    return {"lines": lines, "truncated": state["truncated"]}


def disk(sb: Sandbox) -> dict:
    volumes = []
    for root in sb.roots:
        usage = shutil.disk_usage(str(root))
        volumes.append({"root": str(root), "total": usage.total, "used": usage.used, "free": usage.free})
    return {"volumes": volumes}


def stat(sb: Sandbox, path) -> dict:
    target = sb.resolve(path)
    info = target.stat()
    return {
        "path": str(target),
        "dir": stat_module.S_ISDIR(info.st_mode),
        "size": info.st_size,
        "mtime": int(info.st_mtime),
        "mode": oct(stat_module.S_IMODE(info.st_mode)),
    }


def mkdir(sb: Sandbox, path) -> dict:
    target = sb.resolve(path)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": str(target)}


def write(sb: Sandbox, path, text: str) -> dict:
    target = sb.resolve(path)
    data = str(text or "").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(target), "bytes": len(data)}


def mv(sb: Sandbox, source, destination) -> dict:
    src, dst = sb.resolve(source), sb.resolve(destination)
    _need(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"src": str(src), "dst": str(dst)}


def cp(sb: Sandbox, source, destination) -> dict:
    src, dst = sb.resolve(source), sb.resolve(destination)
    _need(src)
    if src.is_dir():
        raise IsADirectoryError(f"katalog nusxalanmaydi: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    return {"src": str(src), "dst": str(dst), "bytes": dst.stat().st_size}


def rm(sb: Sandbox, path) -> dict:
    target = _need(sb.resolve(path))
    if target.is_dir():
        raise Denied(f"katalog o'chirilmaydi (faqat bitta fayl): {target}")
    size = target.stat().st_size
    target.unlink()
    return {"removed": str(target), "size": size}


def get(sb: Sandbox, path) -> dict:
    target = _need(sb.resolve(path))
    if target.is_dir():
        raise IsADirectoryError(f"bu katalog, `/get` fayl uchun: {target}")
    size = target.stat().st_size
    # Tekshiruv o'qishdan oldin: 25 MB'dan katta fayl xotiraga sig'maydi.
    if size > MAX_GET_BYTES:
        raise Denied(f"ruxsat yo'q: fayl juda katta ({size} bayt, chegarasi {MAX_GET_BYTES})")
    return {"path": str(target), "size": size, "data": target.read_bytes()}


def put(sb: Sandbox, path, data: bytes) -> dict:
    target = sb.resolve(path)
    payload = bytes(data or b"")
    if len(payload) > MAX_GET_BYTES:
        raise Denied(f"ruxsat yo'q: fayl juda katta ({len(payload)} bayt)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return {"path": str(target), "bytes": len(payload)}
