"""
Интерфейс для screen_script.py: настройки, запуск и журнал — в браузере.

Запуск:  python gui.py   (или start_gui.bat), либо docker compose up -d
Настройки сохраняются в settings.json рядом со скриптом; screen_script.py
читает этот файл при старте. Сторонние библиотеки не нужны.

Переменные окружения (нужны в основном для Docker):
  SCREEN_GUI_HOST, SCREEN_GUI_PORT  — адрес и порт сервера
  SCREEN_SETTINGS_FILE              — где хранить settings.json
  SCREEN_INPUT_FOLDER, SCREEN_OUTPUT_FOLDER, SCREEN_APPROVED_FOLDER,
  SCREEN_DELETED_FOLDER, SCREEN_LOGS_FOLDER
                                    — папки по умолчанию
  SCREEN_BROWSE_ROOT                — корень для выбора папок в браузере
  SCREEN_DRIVES_ROOT                — куда в Docker подключены диски
                                      компьютера (/drives/c, /drives/d)
  SCREEN_PICKER_HOST                — адрес компьютера изнутри Docker, где
                                      запущено окно выбора папок
  SCREEN_AUTH                       — 1/0: вход по коду из Google
                                      Authenticator (по умолчанию включён,
                                      когда сервер доступен из сети)

Подключить Google Authenticator:  python gui.py --auth
(в Docker:  docker exec screen-gui python gui.py --auth)

Окно выбора папок Windows для версии в Docker:  pythonw gui.py --picker
(его запускает start_docker.bat)
"""

import base64
import errno
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
SCRIPT = BASE / "screen_script.py"
SETTINGS_FILE = Path(os.environ.get("SCREEN_SETTINGS_FILE")
                     or BASE / "settings.json")
PAGE_FILE = BASE / "gui.html"
LOGIN_FILE = BASE / "login.html"
BROWSE_FILE = BASE / "browse.html"
# Уменьшенные копии для сетки проводника
THUMBS_FOLDER = SETTINGS_FILE.with_name("thumbs")
THUMB_WIDTH = 360

HOST = os.environ.get("SCREEN_GUI_HOST") or "127.0.0.1"
PORT = int(os.environ.get("SCREEN_GUI_PORT") or 8765)
# Сервер слушает не только этот компьютер (так в Docker): браузер и
# проводник на стороне сервера открывать некому, папки выбираются
# прямо на странице, в пределах BROWSE_ROOT.
REMOTE = HOST not in ("127.0.0.1", "localhost")
BROWSE_ROOT = Path(os.environ.get("SCREEN_BROWSE_ROOT") or "/").resolve()

# В Docker диски компьютера подключены как DRIVES_ROOT/<буква>. Человек
# видит и вводит обычные пути Windows (D:\recordings), а программа и
# settings.json работают с путями контейнера (/drives/d/recordings).
DRIVES_ROOT = (os.environ.get("SCREEN_DRIVES_ROOT") or "").rstrip("/")
DRIVE_RE = re.compile(re.escape(DRIVES_ROOT) + r"/([a-z])(?=/|$)")

# Окно выбора папок Windows. Из контейнера его не открыть, поэтому на
# самом компьютере работает помощник (python gui.py --picker), а
# контейнер обращается к нему. Общий ключ лежит в файле рядом с настройками.
PICKER_HOST = os.environ.get("SCREEN_PICKER_HOST") or ""
PICKER_PORT = int(os.environ.get("SCREEN_PICKER_PORT") or 8766)
PICKER_TOKEN_FILE = (SETTINGS_FILE.with_name("picker.token") if REMOTE
                     else BASE / "docker-config" / "picker.token")

# Вход по одноразовому коду (TOTP, Google Authenticator)
AUTH = (os.environ.get("SCREEN_AUTH") or ("1" if REMOTE else "0")) == "1"
AUTH_FILE = SETTINGS_FILE.with_name("auth.json")
AUTH_NAME = "ScreenScript"
MAX_FAILS, FAIL_WINDOW = 5, 60

# Страница получает токен при загрузке и шлёт его с каждым запросом:
# посторонний сайт, открытый в том же браузере, не сможет дёргать API.
TOKEN = secrets.token_urlsafe(24)

# Значения по умолчанию — те же, что в блоке НАСТРОЙКИ в screen_script.py
DEFAULTS = {
    "INPUT_FOLDER": os.environ.get("SCREEN_INPUT_FOLDER") or r"D:\recordings",
    # Какие папки внутри INPUT_FOLDER обрабатывать (пути относительно неё,
    # через "/"); пусто — всю
    "ONLY_FOLDERS": [],
    # Сколько скриптов запускать разом, когда выбрано несколько папок
    "PARALLEL_RUNS": 1,
    "SCREENSHOTS_IN_PLACE": False,
    "OUTPUT_FOLDER": (os.environ.get("SCREEN_OUTPUT_FOLDER")
                      or r"D:\screenshots"),
    "APPROVED_FOLDER": (os.environ.get("SCREEN_APPROVED_FOLDER")
                        or r"D:\screenshots_approved"),
    "DELETED_FOLDER": (os.environ.get("SCREEN_DELETED_FOLDER")
                       or r"D:\screenshots_deleted"),
    "SAVE_AS_PNG": True,
    "JPEG_QUALITY": 100,
    "INTERVAL_SECONDS": 3,
    "MIN_FILE_SIZE_BYTES": 100,
    "MIN_FREE_GB": 20,
    "GROWING_CHECK_AGE_SECONDS": 120,
    "DAMAGED_LIST_NAME": "_damaged_frames.txt",
    "LOGS_FOLDER": (os.environ.get("SCREEN_LOGS_FOLDER")
                    or r"D:\screenshots_logs"),
    "ERRORS_LOG_NAME": "errors.txt",
    "RUN_LOG_NAME": "run.txt",
}

# (минимум, максимум, только целое)
NUMBER_LIMITS = {
    "JPEG_QUALITY": (1, 100, True),
    "INTERVAL_SECONDS": (1, 86400, True),
    "MIN_FILE_SIZE_BYTES": (0, 10**9, True),
    "MIN_FREE_GB": (0, 10**6, False),
    "PARALLEL_RUNS": (1, 16, True),
    "GROWING_CHECK_AGE_SECONDS": (0, 86400, True),
}
FOLDER_KEYS = ("INPUT_FOLDER", "OUTPUT_FOLDER", "APPROVED_FOLDER",
               "DELETED_FOLDER", "LOGS_FOLDER")
FILE_NAME_KEYS = ("DAMAGED_LIST_NAME", "ERRORS_LOG_NAME", "RUN_LOG_NAME")
BAD_NAME_CHARS = set('\\/:*?"<>|')

MAX_LOG_LINES = 5000


# ============================================================
# НАСТРОЙКИ
# ============================================================

def to_host(path: str) -> str:
    """/drives/d/recordings → D:\\recordings (вне Docker — без изменений)."""
    m = DRIVE_RE.match(path) if DRIVES_ROOT else None
    if not m:
        return path
    return m.group(1).upper() + ":" + (path[m.end():].replace("/", "\\")
                                       or "\\")


def from_host(path: str) -> str:
    """D:\\recordings → /drives/d/recordings (вне Docker — без изменений)."""
    m = re.match(r"^\s*([A-Za-z]):[\\/]*(.*)$", path) if DRIVES_ROOT else None
    if not m:
        return path
    rest = m.group(2).strip().replace("\\", "/").strip("/")
    return f"{DRIVES_ROOT}/{m.group(1).lower()}" + (f"/{rest}" if rest else "")


def host_text(text: str) -> str:
    """То же для строки журнала или сообщения, в которой встретился путь."""
    m = DRIVE_RE.search(text) if DRIVES_ROOT else None
    if not m:
        return text
    return (text[:m.start()] + m.group(1).upper() + ":"
            + text[m.end():].replace("/", "\\"))


def folders_to_host(settings: dict) -> dict:
    return {k: to_host(v) if k in FOLDER_KEYS else v
            for k, v in settings.items()}


def folders_from_host(data: dict) -> dict:
    return {k: from_host(v) if k in FOLDER_KEYS and isinstance(v, str) else v
            for k, v in data.items()}


def inner_folders(base: str, items: list):
    """
    Приводит выбранные папки к путям относительно папки с видео.
    Принимает и полные пути (так их отдаёт окно выбора папки).
    Возвращает (список, текст ошибки или None).
    """
    chosen = set()
    for raw in items:
        full = Path(from_host(raw))
        if full.is_absolute():
            try:
                parts = full.relative_to(Path(base)).parts
            except ValueError:
                return [], f"Папка {raw} лежит не внутри папки с видео."
        else:
            parts = tuple(p for p in raw.replace("\\", "/").split("/")
                          if p and p != ".")
        if ".." in parts:
            return [], f"Папка {raw} лежит не внутри папки с видео."
        if not parts:
            return [], None          # выбрана сама папка с видео — значит, вся
        chosen.add(tuple(parts))
    # Папка внутри уже выбранной обработается вместе с ней
    chosen = sorted(c for c in chosen
                    if not any(c[:n] in chosen for n in range(1, len(c))))
    return ["/".join(parts) for parts in chosen], None


def validate(data: dict):
    """Возвращает (чистые настройки, {ключ: текст ошибки})."""
    clean, errors = {}, {}
    for key, default in DEFAULTS.items():
        value = data.get(key, default)

        if isinstance(default, bool):
            if not isinstance(value, bool):
                errors[key] = "Ожидается да или нет."
                continue

        elif isinstance(default, list):
            if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value):
                errors[key] = "Ожидается список папок."
                continue
            value = [v.strip() for v in value if v.strip()]

        elif key in NUMBER_LIMITS:
            lo, hi, whole = NUMBER_LIMITS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors[key] = "Введите число."
                continue
            if whole and value != int(value):
                errors[key] = "Нужно целое число."
                continue
            if not lo <= value <= hi:
                errors[key] = f"Допустимо от {lo} до {hi}."
                continue
            value = int(value) if value == int(value) else float(value)

        else:
            if not isinstance(value, str) or not value.strip():
                errors[key] = "Поле не может быть пустым."
                continue
            value = value.strip()
            if key in FILE_NAME_KEYS:
                if BAD_NAME_CHARS & set(value) or value in (".", ".."):
                    errors[key] = ('Только имя файла, без пути и '
                                   'символов \\ / : * ? " < > |')
                    continue
            elif not Path(value).is_absolute():
                errors[key] = ("Укажите полный путь, например "
                               + ("/data/папка." if os.name != "nt"
                                  and not DRIVES_ROOT else r"D:\папка."))
                continue
            elif DRIVES_ROOT:
                m = DRIVE_RE.match(value)
                if not m:
                    errors[key] = r"Укажите полный путь, например D:\папка."
                    continue
                if not os.path.isdir(f"{DRIVES_ROOT}/{m.group(1)}"):
                    errors[key] = (f"Диск {m.group(1).upper()}: не подключён "
                                   "к программе. Добавьте его в "
                                   "docker-compose.yml.")
                    continue

        clean[key] = value

    if "INPUT_FOLDER" in clean and "ONLY_FOLDERS" in clean:
        chosen, error = inner_folders(clean["INPUT_FOLDER"],
                                      clean["ONLY_FOLDERS"])
        if error:
            errors["ONLY_FOLDERS"] = error
        clean["ONLY_FOLDERS"] = chosen

    # Проводник перекладывает скрины между этими тремя папками с тем же
    # относительным путём, поэтому они не должны пересекаться
    if not errors:
        keys = ("INPUT_FOLDER" if clean["SCREENSHOTS_IN_PLACE"]
                else "OUTPUT_FOLDER", "APPROVED_FOLDER", "DELETED_FOLDER")
        for i, first in enumerate(keys):
            for second in keys[i + 1:]:
                a, b = Path(clean[first]), Path(clean[second])
                if a == b or a in b.parents or b in a.parents:
                    errors[second] = ("Папки скринов, одобренных и удалённых "
                                      "должны быть разными и не лежать одна "
                                      "в другой.")
    return clean, errors


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    clean, _errors = validate(data)
    return {**DEFAULTS, **clean}


def save_settings(settings: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def screens_folder(settings: dict) -> Path:
    key = "INPUT_FOLDER" if settings["SCREENSHOTS_IN_PLACE"] else "OUTPUT_FOLDER"
    return Path(settings[key])


def free_gb(folder: Path):
    """Свободное место на диске папки (её может ещё не быть)."""
    for p in (folder, *folder.parents):
        try:
            return round(shutil.disk_usage(p).free / 1024**3, 1)
        except OSError:
            continue
    return None


def describe(settings: dict) -> dict:
    return {
        "settings": folders_to_host(settings),
        "free_gb": free_gb(screens_folder(settings)),
        "input_exists": Path(settings["INPUT_FOLDER"]).is_dir(),
    }


# ============================================================
# ВХОД ПО КОДУ ИЗ GOOGLE AUTHENTICATOR
# ============================================================

def load_secret() -> str:
    """Ключ из auth.json; при первом запуске создаётся новый."""
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            secret = json.load(f)["secret"]
        base64.b32decode(secret)
        return secret
    except (OSError, ValueError, KeyError, TypeError):
        pass
    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_name(AUTH_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"secret": secret}, f)
    os.replace(tmp, AUTH_FILE)
    return secret


def seconds_to_midnight() -> int:
    """Вход действует до 00:00 по местному времени: каждый день — заново."""
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def totp(secret: str, step: int) -> str:
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", step),
                      hashlib.sha1).digest()
    pos = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[pos:pos + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**6:06d}"


class Auth:
    def __init__(self):
        self.lock = threading.Lock()
        self.secret = load_secret() if AUTH else ""
        self.sessions = {}           # идентификатор → когда истекает
        self.fails = []              # время неудачных попыток
        self.last_step = 0           # код нельзя использовать дважды

    def login(self, code: str):
        """Возвращает (идентификатор сессии, сколько секунд она живёт,
        текст ошибки)."""
        code = "".join(ch for ch in str(code) if ch.isdigit())
        now = time.time()
        with self.lock:
            self.fails = [t for t in self.fails if now - t < FAIL_WINDOW]
            if len(self.fails) >= MAX_FAILS:
                return None, 0, "Слишком много попыток. Подождите минуту."
            current = int(now // 30)
            # Соседние интервалы — на случай, если часы телефона спешат
            # или отстают
            for step in (current - 1, current, current + 1):
                if step > self.last_step and hmac.compare_digest(
                        totp(self.secret, step), code):
                    self.last_step = step
                    sid = secrets.token_urlsafe(32)
                    self.sessions = {k: v for k, v in self.sessions.items()
                                     if v > now}
                    lifetime = seconds_to_midnight()
                    self.sessions[sid] = now + lifetime
                    return sid, lifetime, None
            self.fails.append(now)
            return (None, 0,
                    "Код не подошёл. Введите текущий код из приложения.")

    def valid(self, sid: str) -> bool:
        with self.lock:
            return self.sessions.get(sid, 0) > time.time()

    def logout(self, sid: str):
        with self.lock:
            self.sessions.pop(sid, None)


AUTHENTICATOR = Auth()


def show_auth_setup():
    """python gui.py --auth: ключ и QR-код для Google Authenticator."""
    secret = load_secret()
    uri = (f"otpauth://totp/{AUTH_NAME}?secret={secret}"
           f"&issuer={AUTH_NAME}")
    print("Google Authenticator → «+» → «Сканировать QR-код»:\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(uri)
        qr.print_ascii(invert=True)
    except Exception:
        print("  (QR-код недоступен: не установлен пакет qrcode)\n")
    print("Или «Ввести ключ настройки», тип «По времени»:")
    print(f"  аккаунт: {AUTH_NAME}")
    print(f"  ключ:    {secret}\n")
    print(f"Ключ хранится в {AUTH_FILE}. Чтобы выпустить новый, удалите "
          "этот файл и перезапустите программу.")


# ============================================================
# ПРОВОДНИК: ПРОСМОТР И РАЗБОР СКРИНОВ
# ============================================================

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
ROOT_NAMES = ("screens", "approved", "deleted")

# Декодирование 4K-кадра занимает заметную память — не больше 4 разом
THUMB_SLOTS = threading.BoundedSemaphore(4)


def root_folder(root: str):
    settings = load_settings()
    if root == "screens":
        return screens_folder(settings)
    if root == "approved":
        return Path(settings["APPROVED_FOLDER"])
    if root == "deleted":
        return Path(settings["DELETED_FOLDER"])
    return None


def safe_path(root: str, rel: str):
    """Путь внутри одной из трёх папок; None, если он выходит за неё."""
    base = root_folder(root)
    if base is None:
        return None
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p]
    if any(p in (".", "..") or ":" in p for p in parts):
        return None
    return base.joinpath(*parts)


def media_kind(name: str):
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return "i"
    if ext in VIDEO_EXTENSIONS:
        return "v"
    return None


def list_files(root: str, rel: str) -> dict:
    folder = safe_path(root, rel)
    if folder is None:
        return {"error": "Неизвестная папка."}
    folders, files = [], []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        folders.append(entry.name)
                        continue
                except OSError:
                    continue
                kind = media_kind(entry.name)
                if kind:
                    files.append([entry.name, kind])
    except FileNotFoundError:
        return {"missing": to_host(str(folder)), "folders": [], "files": []}
    except OSError as e:
        return {"error": host_text(
            f"Не удалось прочитать папку {folder}: {e}")}
    folders.sort(key=str.lower)
    files.sort(key=lambda f: f[0].lower())
    return {"folders": folders, "files": files}


def count_tree(folder):
    """(папок прямо внутри, картинок и видео на всех уровнях)."""
    dirs = files = 0
    stack = [(folder, 1)]
    while stack:
        current, top = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        dirs += top
                        stack.append((entry.path, 0))
                    elif media_kind(entry.name):
                        files += 1
        except OSError:
            pass
    return dirs, files


def count_folders(root: str, rel: str) -> dict:
    """Счётчики для плиток папок: {имя: [папок, файлов]}."""
    folder = safe_path(root, rel)
    result = {}
    if folder is None:
        return result
    try:
        with os.scandir(folder) as entries:
            subfolders = [e for e in entries
                          if e.is_dir(follow_symlinks=False)]
    except OSError:
        return result
    for entry in subfolders:
        result[entry.name] = list(count_tree(entry.path))
    return result


class Locks:
    """
    Кто в какой папке работает. Папку с картинками занимает тот, кто
    открыл её первым; остальным она закрыта, пока он не уйдёт. Страница
    подтверждает, что человек ещё здесь, каждые несколько секунд; без
    подтверждений папка освобождается сама через LIFETIME.
    """
    LIFETIME = 40

    def __init__(self):
        self.lock = threading.Lock()
        self.owners = {}             # (корень, путь) → (кто, до какого времени)
        self.places = {}             # кто → (корень, путь)

    @staticmethod
    def key(root: str, rel: str):
        return root, "/".join(
            p for p in str(rel).replace("\\", "/").split("/") if p)

    def _owner(self, key):
        entry = self.owners.get(key)
        if entry and entry[1] > time.time():
            return entry[0]
        if entry:
            del self.owners[key]
            if self.places.get(entry[0]) == key:
                del self.places[entry[0]]
        return None

    def _leave(self, who: str):
        key = self.places.pop(who, None)
        if key and self.owners.get(key, ("",))[0] == who:
            del self.owners[key]

    def take(self, who: str, key) -> bool:
        with self.lock:
            owner = self._owner(key)
            if owner and owner != who:
                return False
            if self.places.get(who) != key:
                self._leave(who)     # у человека одна папка за раз
            self.owners[key] = (who, time.time() + self.LIFETIME)
            self.places[who] = key
            return True

    def leave(self, who: str):
        with self.lock:
            self._leave(who)

    def taken_by_other(self, who: str, key) -> bool:
        with self.lock:
            owner = self._owner(key)
            return bool(owner) and owner != who

    def busy_folders(self, who: str, key) -> list:
        """Имена папок прямо внутри key, занятых другими."""
        root, rel = key
        prefix = rel + "/" if rel else ""
        with self.lock:
            return [k[1][len(prefix):] for k in list(self.owners)
                    if k[0] == root and k[1].startswith(prefix)
                    and "/" not in k[1][len(prefix):] and k[1] != rel
                    and self._owner(k) not in (None, who)]


LOCKS = Locks()
BUSY_TEXT = "В этой папке сейчас работает другой пользователь."


class Totals:
    """Сколько всего файлов в каждой из трёх папок (для вкладок)."""
    LIFETIME = 30                    # секунд; обход сотен тысяч файлов небыстрый

    def __init__(self):
        self.lock = threading.Lock()
        self.values, self.folders, self.expires = None, None, 0

    def get(self) -> dict:
        folders = {name: root_folder(name) for name in ROOT_NAMES}
        with self.lock:
            if (self.values is None or folders != self.folders
                    or time.time() > self.expires):
                self.values = {name: count_tree(folder)[1]
                               for name, folder in folders.items()}
                self.folders = folders
                self.expires = time.time() + self.LIFETIME
            return dict(self.values)

    def moved(self, source: str, target: str):
        with self.lock:
            if self.values is not None:
                self.values[source] -= 1
                self.values[target] += 1


TOTALS = Totals()


def make_thumb(path: Path):
    """JPEG-миниатюра (bytes) или None, если её не получить."""
    try:
        st = path.stat()
    except OSError:
        return None
    # Без пути в ключе: после переноса в другую папку миниатюра та же
    key = hashlib.sha1(
        f"{path.name}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
    cached = THUMBS_FOLDER / key[:2] / f"{key}.jpg"
    try:
        return cached.read_bytes()
    except OSError:
        pass

    try:
        os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
        os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
        import cv2
        import numpy as np
    except ImportError:
        return None

    with THUMB_SLOTS:
        try:
            if media_kind(path.name) == "v":
                cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
                try:
                    ok, image = cap.read()
                finally:
                    cap.release()
                if not ok:
                    return None
            else:
                # imread на Windows не умеет пути с кириллицей
                image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8),
                                     cv2.IMREAD_COLOR)
            if image is None:
                return None
            height, width = image.shape[:2]
            if width > THUMB_WIDTH:
                image = cv2.resize(
                    image, (THUMB_WIDTH, max(1, height * THUMB_WIDTH // width)),
                    interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", image,
                                   [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                return None
            data = buf.tobytes()
        except Exception:
            return None

    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_name(f"{cached.name}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, cached)
    except OSError:
        pass
    return data


# Форматы без сжатия с потерями: для просмотра их стоит облегчать
HEAVY_EXTENSIONS = {".png", ".bmp"}
VIEW_JPEG_QUALITY = 85
# Ширина копии для просмотра: кадр 4K (3840) уменьшается до 2K
VIEW_WIDTH = 2560
# Копии для просмотра создаются по запросу и нигде не хранятся;
# одновременно — не больше, чем ядер (каждая держит кадр 4K в памяти)
VIEW_SLOTS = threading.BoundedSemaphore(max(2, min(8, os.cpu_count() or 2)))


def make_view(path: Path):
    """
    Облегчённая JPEG-копия для показа в проводнике, не шире VIEW_WIDTH
    (PNG кадра 4K весит около 7 МБ). Оригинал не меняется. None — если
    копию не сделать или она не нужна: тогда отдаётся сам файл.
    """
    heavy = path.suffix.lower() in HEAVY_EXTENSIONS
    if path.suffix.lower() == ".gif":
        return None                  # может быть анимацией
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    with VIEW_SLOTS:
        try:
            image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8),
                                 cv2.IMREAD_COLOR)
            if image is None:
                return None
            height, width = image.shape[:2]
            if width > VIEW_WIDTH:
                image = cv2.resize(
                    image, (VIEW_WIDTH, max(1, height * VIEW_WIDTH // width)),
                    interpolation=cv2.INTER_AREA)
            elif not heavy:
                return None          # небольшой JPEG отдаём как есть
            ok, buf = cv2.imencode(
                ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, VIEW_JPEG_QUALITY])
            return buf.tobytes() if ok else None
        except Exception:
            return None


def patiently(action, *args):
    """
    Windows не даёт перенести файл, пока он открыт, а открыт он бывает
    на мгновение: сервер как раз отдаёт его браузеру. Ждём до 2 секунд.
    """
    for _ in range(40):
        try:
            return action(*args)
        except PermissionError:
            time.sleep(0.05)
    return action(*args)


def move_file(root: str, rel: str, to: str):
    """Переносит картинку в другую из трёх папок с тем же путём внутри."""
    if to not in ROOT_NAMES or to == root:
        return "Неизвестная папка."
    src, dst = safe_path(root, rel), safe_path(to, rel)
    if src is None or dst is None:
        return "Неизвестная папка."
    if media_kind(src.name) != "i":
        return "Переносить можно только картинки."
    if not src.is_file():
        return "Файла уже нет на месте — обновите страницу."
    if dst.exists():
        return f"В папке назначения уже есть такой файл: {to_host(str(dst))}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            patiently(os.rename, src, dst)
        except OSError as e:
            if e.errno != errno.EXDEV and getattr(e, "winerror", 0) != 17:
                raise
            # Папки на разных дисках (или разных томах Docker): копируем
            # под временным именем, чтобы не оставить половину файла
            tmp = dst.with_name(dst.name + ".tmp")
            try:
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
            try:
                patiently(os.unlink, src)
            except OSError:
                # Иначе файл остался бы в обеих папках сразу
                dst.unlink(missing_ok=True)
                raise
    except OSError as e:
        return host_text(f"Не удалось перенести файл: {e}")
    TOTALS.moved(root, to)
    return None


# ============================================================
# ЗАПУСК СКРИПТА
# ============================================================

class Job:
    """Один запущенный (или ждущий очереди) скрипт."""

    def __init__(self, folder):
        self.folder = folder         # путь внутри папки с видео; None — всё
        self.label = (folder.replace("/", "\\")
                      if folder and (os.name == "nt" or DRIVES_ROOT)
                      else folder or "")
        self.state = "queued"        # queued | running | done | cancelled
        self.proc = None
        self.exit_code = None
        self.total = self.done = self.broken = 0


class Runner:
    """
    Запускает screen_script.py. Если выбрано несколько папок и в настройках
    разрешено больше одного скрипта разом, на каждую папку идёт отдельный
    скрипт; одновременно работает не больше PARALLEL_RUNS, остальные ждут.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = []
        self.limit = 1
        self.run_id = 0
        self.state = "idle"          # idle | running | stopping
        self.exit_code = None
        self.lines = []
        self.base = 0                # номер первой строки, оставшейся в lines

    def start(self):
        settings = load_settings()
        folders = settings["ONLY_FOLDERS"]
        limit = settings["PARALLEL_RUNS"]
        with self.lock:
            if self.state != "idle":
                return "Обработка уже идёт."
            if limit > 1 and len(folders) > 1:
                self.jobs = [Job(folder) for folder in folders]
            else:
                self.jobs = [Job(None)]
            self.limit = limit
            self.run_id += 1
            self.state = "running"
            self.exit_code = None
            self.lines, self.base = [], 0
            self._fill()
        return None

    def _say(self, job, text):
        """Строка в общий журнал (self.lock уже взят)."""
        prefix = f"[{job.label}] " if len(self.jobs) > 1 else ""
        self.lines.append(prefix + text)
        extra = len(self.lines) - MAX_LOG_LINES
        if extra > 0:
            del self.lines[:extra]
            self.base += extra

    def _fill(self):
        """Запускает ждущие скрипты, пока есть свободные места (под lock)."""
        while self.state == "running":
            running = sum(1 for j in self.jobs if j.state == "running")
            job = next((j for j in self.jobs if j.state == "queued"), None)
            if job is None or running >= self.limit:
                break
            env = dict(os.environ, PYTHONIOENCODING="utf-8",
                       PYTHONUNBUFFERED="1", SCREEN_SCRIPT_GUI="1")
            if job.folder:
                env["SCREEN_ONLY_FOLDERS"] = json.dumps([job.folder])
            try:
                job.proc = subprocess.Popen(
                    [sys.executable, "-u", str(SCRIPT)], cwd=str(BASE),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    text=True, encoding="utf-8", errors="replace",
                )
            except OSError as e:
                job.state, job.exit_code = "done", 1
                self._say(job, f"ОШИБКА: не удалось запустить скрипт: {e}")
                continue
            job.state = "running"
            threading.Thread(target=self._read, args=(job,),
                             daemon=True).start()
        if not any(j.state in ("queued", "running") for j in self.jobs):
            codes = [j.exit_code for j in self.jobs if j.exit_code]
            self.exit_code = codes[0] if codes else 0
            self.state = "idle"

    def _read(self, job):
        proc = job.proc
        for raw in proc.stdout:
            line = host_text(raw.rstrip("\r\n"))
            mark = line.lstrip()[:2]
            with self.lock:
                if line.startswith("Всего .mp4:"):
                    try:
                        job.total = int(line.split(":")[1])
                    except ValueError:
                        pass
                elif mark == "✓ ":
                    job.done += 1
                elif mark == "✗ " and "выбранной папки нет" not in line:
                    job.done += 1
                    job.broken += 1
                self._say(job, line)
        code = proc.wait()
        with self.lock:
            job.exit_code = code
            job.state = "done"
            job.proc = None
            if self.state == "stopping":
                for other in self.jobs:
                    if other.state == "queued":
                        other.state = "cancelled"
            self._fill()

    def stop(self):
        with self.lock:
            if self.state != "running":
                return
            self.state = "stopping"
            procs = []
            for job in self.jobs:
                if job.state == "queued":
                    job.state = "cancelled"
                elif job.state == "running" and job.proc is not None:
                    procs.append(job.proc)
        # Скрипт следит за stdin и по этой команде завершается так же,
        # как по Ctrl+C: дописывает текущий кадр и закрывает логи.
        for proc in procs:
            try:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

        def _kill_if_stuck(proc):
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        for proc in procs:
            threading.Thread(target=_kill_if_stuck, args=(proc,),
                             daemon=True).start()

    def wait(self, timeout: float):
        """Ждёт завершения всех скриптов (при выходе из программы)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                procs = [j.proc for j in self.jobs if j.proc is not None]
            if not procs:
                return
            time.sleep(0.2)
        for proc in procs:
            proc.kill()

    def snapshot(self, start: int) -> dict:
        with self.lock:
            start = max(start, self.base)
            return {
                "run_id": self.run_id,
                "state": self.state,
                "exit_code": self.exit_code,
                "total": sum(j.total for j in self.jobs),
                "done": sum(j.done for j in self.jobs),
                "broken": sum(j.broken for j in self.jobs),
                "jobs": [{"label": j.label, "state": j.state,
                          "exit_code": j.exit_code, "total": j.total,
                          "done": j.done, "broken": j.broken}
                         for j in self.jobs] if len(self.jobs) > 1 else [],
                "from": start,
                "lines": self.lines[start - self.base:],
            }


RUNNER = Runner()


# ============================================================
# ДИАЛОГ ВЫБОРА ПАПКИ
# ============================================================

PICK_CODE = """
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(initialdir=sys.argv[1] or None, parent=root)
sys.stdout.buffer.write(path.encode("utf-8"))
"""


def pick_folder(initial: str) -> str:
    # Отдельным процессом: tkinter не любит жить в потоке HTTP-сервера
    start = ""
    if initial:
        for p in (Path(initial), *Path(initial).parents):
            if p.is_dir():
                start = str(p)
                break
    try:
        out = subprocess.run(
            [sys.executable, "-c", PICK_CODE, start],
            capture_output=True, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return str(Path(out)) if out else ""


class PickerHandler(BaseHTTPRequestHandler):
    """Помощник на самом компьютере: открывает окно выбора папок Windows."""
    busy = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            token = PICKER_TOKEN_FILE.read_text(encoding="ascii").strip()
        except OSError:
            token = ""
        if not token or self.path != "/pick" or not secrets.compare_digest(
                self.headers.get("X-Token", ""), token):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            initial = str(json.loads(self.rfile.read(size)).get("initial", ""))
        except (ValueError, AttributeError):
            initial = ""
        with self.busy:              # одно окно за раз
            path = pick_folder(initial)
        body = json.dumps({"path": path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_picker():
    """python gui.py --picker"""
    if not PICKER_TOKEN_FILE.exists():
        PICKER_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        PICKER_TOKEN_FILE.write_text(secrets.token_urlsafe(32),
                                     encoding="ascii")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PICKER_PORT),
                                     PickerHandler)
    except OSError:
        return                       # уже запущен
    print(f"Окно выбора папок ждёт запросов на 127.0.0.1:{PICKER_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def pick_on_host(initial: str):
    """Путь из окна Windows; None, если помощник на компьютере не запущен."""
    if not PICKER_HOST:
        return None
    try:
        token = PICKER_TOKEN_FILE.read_text(encoding="ascii").strip()
        request = urllib.request.Request(
            f"http://{PICKER_HOST}:{PICKER_PORT}/pick",
            data=json.dumps({"initial": initial}).encode("utf-8"),
            headers={"X-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=900) as response:
            return str(json.loads(response.read()).get("path", ""))
    except (OSError, ValueError):
        return None


def list_folders(raw: str) -> dict:
    """Вложенные папки для выбора на странице (не выше BROWSE_ROOT)."""
    raw = from_host(raw)
    folder = BROWSE_ROOT
    if raw:
        for p in (Path(raw), *Path(raw).parents):
            if p.is_dir():
                folder = p.resolve()
                break
    if folder != BROWSE_ROOT and BROWSE_ROOT not in folder.parents:
        folder = BROWSE_ROOT
    try:
        names = sorted((p.name for p in folder.iterdir() if p.is_dir()),
                       key=str.lower)
    except OSError:
        names = []
    drives = bool(DRIVES_ROOT) and str(folder) == DRIVES_ROOT
    return {
        "path": "Этот компьютер" if drives else to_host(str(folder)),
        "parent": (None if folder == BROWSE_ROOT
                   else to_host(str(folder.parent))),
        "folders": [[to_host(str(folder / name)) if drives else name,
                     to_host(str(folder / name))] for name in names],
    }


# ============================================================
# HTTP
# ============================================================

# Что доступно при входе не с этого компьютера: только проводник скринов
OUTSIDE_GET = ("/", "/browse", "/file", "/thumb", "/view", "/api/files",
               "/api/counts", "/api/totals")
OUTSIDE_POST = ("/login", "/api/logout", "/api/move", "/api/lock",
                "/api/unlock")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


# Если на этом же адресе раньше работал другой сайт со «служебным
# сценарием» (service worker), браузер продолжает показывать его из своей
# памяти, не обращаясь к серверу. Браузер сам проверяет обновление этого
# сценария — отдаём ему такой, который удаляет себя и старые данные.
OLD_WORKER_NAMES = ("service-worker.js", "sw.js", "serviceworker.js",
                    "ngsw-worker.js", "firebase-messaging-sw.js",
                    "registersw.js", "workbox-sw.js")
OLD_WORKER_REMOVER = b"""
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil((async () => {
  for (const key of await caches.keys()) await caches.delete(key);
  await self.registration.unregister();
  for (const client of await self.clients.matchAll({ type: "window" })) {
    client.navigate(client.url);
  }
})()));
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ScreenScriptGUI"

    def log_message(self, *args):
        pass

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _session(self) -> str:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get("sid")
        except Exception:
            return ""
        return morsel.value if morsel else ""

    def _local(self) -> bool:
        """
        Открыт ли сайт с этого же компьютера (http://localhost:...).
        За пробросом порта Docker адрес клиента не виден — у всех он
        адрес шлюза, — поэтому смотрим, по какому адресу открыт сайт.
        """
        if not REMOTE:
            return True              # сервер и так слушает только localhost
        host = self.headers.get("Host", "")
        if not host.endswith("]"):   # [::1] без порта
            host = host.rpartition(":")[0] or host
        return host.lower() in LOCAL_HOSTS

    def _who(self) -> str:
        """Метка браузера: по ней различаются люди в проводнике."""
        return self.headers.get("X-Client", "")[:64] or self._session() or "?"

    def _signed_in(self) -> bool:
        return not AUTH or AUTHENTICATOR.valid(self._session())

    def _allowed(self) -> bool:
        port = self.server.server_address[1]
        # За пробросом порта Docker адрес в Host заранее неизвестен
        if not REMOTE and self.headers.get("Host") not in (
                f"{HOST}:{port}", f"localhost:{port}"):
            return False
        if self.path.startswith("/api/"):
            return self._signed_in() and secrets.compare_digest(
                self.headers.get("X-Token", ""), TOKEN)
        return True

    def _page(self, file: Path):
        try:
            page = file.read_text(encoding="utf-8")
        except OSError as e:
            return self._send(500, f"Нет файла {file.name}: {e}".encode(),
                              "text/plain; charset=utf-8")
        body = page.replace("__TOKEN__", TOKEN).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if file == LOGIN_FILE:
            # Заодно стираем данные сайта, работавшего на этом адресе раньше
            self.send_header("Clear-Site-Data", '"cache", "storage"')
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        """Отдаёт файл; с Range, чтобы видео можно было перематывать."""
        try:
            size = path.stat().st_size
            f = open(path, "rb")
        except OSError:
            return self._json({"error": "not found"}, 404)
        with f:
            start, end, code = 0, size - 1, 200
            spec = self.headers.get("Range", "")
            if spec.startswith("bytes=") and size:
                first, _, last = spec[6:].partition("-")
                try:
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    else:
                        start = max(0, size - int(last))
                    if not 0 <= start <= end:
                        raise ValueError
                    code = 206
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            length = end - start + 1 if size else 0
            self.send_response(code)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0]
                             or "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=600")
            if code == 206:
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{size}")
            self.end_headers()
            f.seek(start)
            # Картинку читаем целиком и сразу закрываем файл: открытый
            # файл Windows не даёт перенести в другую папку
            data = None
            if length <= 64 * 1024 * 1024:
                data = f.read(length)
                f.close()
            try:
                if data is not None:
                    self.wfile.write(data)
                while data is None and length > 0:
                    chunk = f.read(min(length, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    length -= len(chunk)
            except OSError:
                pass                 # вкладку закрыли или видео перемотали

    def _media(self, path: str, query: str):
        if not self._signed_in():
            return self._json({"error": "forbidden"}, 403)
        params = parse_qs(query)
        target = safe_path(params.get("root", [""])[0],
                           params.get("path", [""])[0])
        if target is None or not media_kind(target.name):
            return self._json({"error": "not found"}, 404)
        if path == "/file":
            return self._file(target)
        data = make_view(target) if path == "/view" else make_thumb(target)
        if data is None:
            if path == "/view" or media_kind(target.name) == "i":
                return self._file(target)
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=600"
                         if path == "/view" else "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _login(self, data: dict):
        sid, lifetime, error = AUTHENTICATOR.login(data.get("code", ""))
        if error:
            return self._json({"error": error})
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie",
                         f"sid={sid}; Path=/; Max-Age={lifetime}; "
                         "HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(size) or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def do_GET(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        path, _, query = self.path.partition("?")
        if path.rpartition("/")[2].lower() in OLD_WORKER_NAMES:
            return self._send(200, OLD_WORKER_REMOVER,
                              "text/javascript; charset=utf-8")
        local = self._local()
        if not local and path not in OUTSIDE_GET:
            return self._json({"error": "not found"}, 404)

        if path == "/":
            if not self._signed_in():
                return self._page(LOGIN_FILE)
            if local:
                return self._page(PAGE_FILE)
            self.send_response(302)
            self.send_header("Location", "/browse")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if path == "/browse":
            return self._page(BROWSE_FILE if self._signed_in()
                              else LOGIN_FILE)

        if path in ("/file", "/thumb", "/view"):
            return self._media(path, query)

        if path == "/api/files":
            params = parse_qs(query)
            key = LOCKS.key(params.get("root", [""])[0],
                            params.get("path", [""])[0])
            listing = list_files(*key)
            who = self._who()
            if listing.get("files"):
                # папку с картинками занимает тот, кто открыл её первым
                if not LOCKS.take(who, key):
                    listing = {"locked": True, "folders": [], "files": []}
            else:
                LOCKS.leave(who)
            return self._json({
                **listing,
                "busy": LOCKS.busy_folders(who, key),
                "auth": AUTH,
                "local": local,
                "view_width": VIEW_WIDTH,
            })

        if path == "/api/counts":
            params = parse_qs(query)
            return self._json(count_folders(params.get("root", [""])[0],
                                            params.get("path", [""])[0]))

        if path == "/api/totals":
            return self._json(TOTALS.get())

        if path == "/api/state":
            return self._json({**describe(load_settings()),
                               "defaults": folders_to_host(DEFAULTS),
                               "remote": REMOTE, "auth": AUTH})

        if path == "/api/log":
            try:
                start = int(query.partition("from=")[2] or 0)
            except ValueError:
                start = 0
            return self._json(RUNNER.snapshot(start))

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        if not self._local() and self.path not in OUTSIDE_POST:
            return self._json({"error": "not found"}, 404)
        data = self._body()

        if self.path == "/login" and AUTH:
            return self._login(data)

        if self.path == "/api/logout":
            AUTHENTICATOR.logout(self._session())
            return self._json({})

        if self.path == "/api/settings":
            clean, errors = validate(folders_from_host(data))
            if errors:
                return self._json({"errors": errors})
            try:
                save_settings(clean)
            except OSError as e:
                return self._json({"error": f"Не удалось сохранить: {e}"})
            return self._json(describe(clean))

        if self.path == "/api/move":
            root, rel = str(data.get("root", "")), str(data.get("path", ""))
            folder = LOCKS.key(root, LOCKS.key("", rel)[1].rpartition("/")[0])
            if LOCKS.taken_by_other(self._who(), folder):
                return self._json({"error": BUSY_TEXT})
            return self._json({"error": move_file(root, rel,
                                                  str(data.get("to", "")))})

        if self.path == "/api/lock":
            # страница подтверждает: человек всё ещё в этой папке
            key = LOCKS.key(str(data.get("root", "")),
                            str(data.get("path", "")))
            who = self._who()
            held = bool(data.get("hold")) and LOCKS.take(who, key)
            return self._json({"held": held,
                               "busy": LOCKS.busy_folders(who, key)})

        if self.path == "/api/unlock":
            LOCKS.leave(self._who())
            return self._json({})

        if self.path == "/api/pick":
            initial = str(data.get("initial", ""))
            if not REMOTE:
                return self._json({"path": pick_folder(initial)})
            path = pick_on_host(initial)
            # Помощник не запущен — папку выбирают на самой странице
            return self._json({"fallback": True} if path is None
                              else {"path": path})

        if self.path == "/api/ls":
            return self._json(list_folders(str(data.get("path", ""))))

        if self.path == "/api/open":
            key = data.get("key")
            if REMOTE or key not in FOLDER_KEYS:
                return self._json({"error": "Неизвестная папка."})
            folder = Path(load_settings()[key])
            if not folder.is_dir():
                return self._json({"error": f"Папки ещё нет: {folder}"})
            os.startfile(folder)
            return self._json({})

        if self.path == "/api/run":
            settings = load_settings()
            if not Path(settings["INPUT_FOLDER"]).is_dir():
                return self._json({"error": "Папка с видео не найдена: "
                                   + to_host(settings["INPUT_FOLDER"])})
            # Скрипт читает настройки только из файла
            if not SETTINGS_FILE.exists():
                try:
                    save_settings(settings)
                except OSError as e:
                    return self._json(
                        {"error": f"Не удалось сохранить настройки: {e}"})
            return self._json({"error": RUNNER.start()})

        if self.path == "/api/stop":
            RUNNER.stop()
            return self._json({})

        self._json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    # Проводник запрашивает десятки картинок разом; с очередью по
    # умолчанию (5) часть соединений отбрасывалась бы
    request_queue_size = 128


def main():
    # docker stop шлёт SIGTERM: останавливаем обработку так же, как Ctrl+C
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    server = None
    for port in range(PORT, PORT + (1 if REMOTE else 10)):
        try:
            server = Server((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"Не удалось занять порт {PORT}.")
        return

    url = f"http://{HOST}:{server.server_address[1]}/"
    if AUTH:
        print("Вход по коду из Google Authenticator включён. Подключить "
              "приложение: python gui.py --auth "
              "(в Docker: docker exec screen-gui python gui.py --auth)",
              flush=True)
    if REMOTE:
        print(f"Сервер слушает {url}", flush=True)
    else:
        print(f"Интерфейс открыт: {url}")
        print("Закройте это окно (или Ctrl+C), чтобы выйти.")
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNER.stop()
        RUNNER.wait(20)


if __name__ == "__main__":
    if "--auth" in sys.argv[1:]:
        show_auth_setup()
    elif "--picker" in sys.argv[1:]:
        run_picker()
    else:
        main()
