import os
import sys

# Глушим спам декодера в консоль ("Could not find ref with POC",
# "cu_qp_delta ... outside the valid range", "moov atom not found").
# Должно стоять ДО import cv2. os.environ тут не помогает: ffmpeg-dll
# из opencv читает переменные через msvcrt, поэтому ставим их и там.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
if os.name == "nt":
    try:
        import ctypes
        for _k in ("OPENCV_LOG_LEVEL", "OPENCV_FFMPEG_LOGLEVEL"):
            ctypes.cdll.msvcrt._putenv(f"{_k}={os.environ[_k]}".encode())
    except Exception:
        pass

import cv2
import json
import re
import shutil
import time
from pathlib import Path
from datetime import datetime, timedelta

# Символы ✓ → ✗ не должны ронять скрипт, если вывод перенаправлен в файл
# или консоль не в UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


# ============================================================
# НАСТРОЙКИ
# ============================================================

INPUT_FOLDER = Path(r"D:\recordings")

# Куда класть скрины.
#  - SCREENSHOTS_IN_PLACE = False → зеркальная структура в OUTPUT_FOLDER
#  - SCREENSHOTS_IN_PLACE = True  → рядом с видео (в тех же папках)
SCREENSHOTS_IN_PLACE = False
OUTPUT_FOLDER = Path(r"D:\screenshots")

# Куда проводник (gui.py) перекладывает разобранные скрины. Структура
# вложенных папок там та же, что в папке со скринами. Скрин, который
# лежит в одной из этих папок, заново не создаётся.
APPROVED_FOLDER = Path(r"D:\screenshots_approved")
DELETED_FOLDER = Path(r"D:\screenshots_deleted")

# Формат скриншотов.
#   True  → PNG (lossless, без потери качества)
#   False → JPEG с JPEG_QUALITY=100
SAVE_AS_PNG = True
JPEG_QUALITY = 100

# Интервал между кадрами (в секундах)
INTERVAL_SECONDS = 3

# Минимальный размер готового файла
MIN_FILE_SIZE_BYTES = 100

# Останавливаемся, если на диске со скринами осталось меньше этого (ГБ)
MIN_FREE_GB = 20

# Файл, изменённый менее N секунд назад, проверяем на "ещё пишется"
GROWING_CHECK_AGE_SECONDS = 120

# Какие папки внутри INPUT_FOLDER обрабатывать: пути относительно неё,
# например ["2026-10-09/cam3"]. Пустой список — вся INPUT_FOLDER.
# Скрины ложатся по тому же пути внутри папки со скринами.
ONLY_FOLDERS = []

# В папке со скринами: список кадров, которые не декодируются из-за
# повреждения записи (серый экран). При повторном запуске они
# пропускаются. Удалите файл, чтобы попробовать их заново.
DAMAGED_LIST_NAME = "_damaged_frames.txt"

# Папка с логами
LOGS_FOLDER = Path(r"D:\screenshots_logs")
ERRORS_LOG_NAME = "errors.txt"
RUN_LOG_NAME    = "run.txt"

# Настройки из интерфейса (gui.py) лежат в settings.json рядом со скриптом
# и перекрывают значения выше. Нет файла — работают значения выше.
SETTINGS_FILE = Path(os.environ.get("SCREEN_SETTINGS_FILE")
                     or Path(__file__).with_name("settings.json"))
SETTINGS_KEYS = (
    "INPUT_FOLDER", "SCREENSHOTS_IN_PLACE", "OUTPUT_FOLDER",
    "APPROVED_FOLDER", "DELETED_FOLDER", "SAVE_AS_PNG",
    "JPEG_QUALITY", "INTERVAL_SECONDS", "MIN_FILE_SIZE_BYTES", "MIN_FREE_GB",
    "GROWING_CHECK_AGE_SECONDS", "DAMAGED_LIST_NAME",
    "LOGS_FOLDER", "ERRORS_LOG_NAME", "RUN_LOG_NAME", "ONLY_FOLDERS",
)


def _apply_settings_file():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    g = globals()
    for key in SETTINGS_KEYS:
        if key not in data:
            continue
        value, default = data[key], g[key]
        if isinstance(default, Path):
            if isinstance(value, str) and value.strip():
                g[key] = Path(value.strip())
        elif isinstance(default, bool):
            if isinstance(value, bool):
                g[key] = value
        elif isinstance(default, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                g[key] = value
        elif isinstance(default, list):
            if isinstance(value, list):
                g[key] = [v.strip() for v in value
                          if isinstance(v, str) and v.strip()]
        elif isinstance(value, str) and value.strip():
            g[key] = value.strip()


_apply_settings_file()

# Интерфейс, запуская несколько скриптов разом, даёт каждому свою папку
if os.environ.get("SCREEN_ONLY_FOLDERS"):
    try:
        ONLY_FOLDERS = [str(v) for v in
                        json.loads(os.environ["SCREEN_ONLY_FOLDERS"])]
    except (ValueError, TypeError):
        pass

ERRORS_LOG_FILE = LOGS_FOLDER / ERRORS_LOG_NAME
RUN_LOG_FILE    = LOGS_FOLDER / RUN_LOG_NAME

# Корень папки со скринами
SCREENS_ROOT = INPUT_FOLDER if SCREENSHOTS_IN_PLACE else OUTPUT_FOLDER

# !!! Только .mp4 — остальное пропускается моментально !!!
VIDEO_EXTENSIONS = {".mp4"}

# Имя скрина: <stem видео>_HH-MM-SS.<ext>
SCREEN_EXT = ".png" if SAVE_AS_PNG else ".jpg"

# Метка "Nшт" в имени папки. Новые папки её не получают, но у папок
# от прежних запусков она осталась (см. resolve_dst_folder)
COUNT_SUFFIX_RE = re.compile(r"^(.*?)\s+(\d+)шт$")

# Unix timestamp в имени (для fallback определения времени)
UNIX_TS_RE = re.compile(r"_(\d{9,11})_")

# Дата/время в имени вида cam11_20261005_1600 (тоже fallback)
NAME_DT_RE = re.compile(r"_(\d{8})_(\d{4})(\d{2})?(?:\D|$)")


class LowDiskSpace(Exception):
    pass


# ============================================================
# ЛОГГЕРЫ
# ============================================================

class FileLogger:
    def __init__(self, path: Path):
        self.path = path
        self.enabled = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.enabled = False

    def _now(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _write_line(self, line: str):
        if not self.enabled:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            self.enabled = False

    def session_header(self, title: str):
        sep = "=" * 78
        self._write_line("")
        self._write_line(sep)
        self._write_line(f"{title}: {self._now()}")
        self._write_line(sep)

    def log(self, category: str, message: str):
        self._write_line(f"[{self._now()}] [{category:<13}] {message}")


class ErrorLogger(FileLogger):
    def __init__(self, path: Path):
        super().__init__(path)
        if self.enabled:
            self.session_header("SESSION STARTED (errors)")

    def error(self, msg): self.log("ERROR", msg)
    def warn(self, msg):  self.log("WARN",  msg)
    def info(self, msg):  self.log("INFO",  msg)


class RunLogger(FileLogger):
    def __init__(self, path: Path):
        super().__init__(path)
        if self.enabled:
            self.session_header("RUN STARTED")

    def folder_started(self, folder: Path):
        self._write_line(f"[{self._now()}] НАЧАЛО ПАПКИ: {folder}")

    def folder_finished(self, folder: Path, created: int,
                        skipped: int, failed: int):
        self._write_line(
            f"[{self._now()}] ЗАВЕРШЕНА ПАПКА: {folder} | "
            f"создано={created}, уже_было={skipped}, ошибок={failed}")

    def summary(self, text: str):
        self._write_line("")
        self._write_line(f"[{self._now()}] ИТОГ: {text}")


ERRLOG: ErrorLogger = None   # type: ignore
RUNLOG: RunLogger = None     # type: ignore


# ============================================================
# СОХРАНЕНИЕ КАДРА БЕЗ ПОТЕРИ КАЧЕСТВА
# ============================================================

def save_frame(path: Path, frame) -> bool:
    # cv2.imwrite на Windows не умеет пути с кириллицей ("Вечер", "День"):
    # кодируем в память и пишем файл средствами Python.
    # Пишем во временный файл и переименовываем — чтобы при обрыве
    # не оставался недописанный скрин, который потом сочтётся готовым.
    tmp = path.with_name(path.name + ".tmp")
    try:
        if SAVE_AS_PNG:
            params = [cv2.IMWRITE_PNG_COMPRESSION, 1]
        else:
            params = [
                cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY,
                cv2.IMWRITE_JPEG_OPTIMIZE, 0,
                cv2.IMWRITE_JPEG_PROGRESSIVE, 0,
            ]
        ok, buf = cv2.imencode(SCREEN_EXT, frame, params)
        if not ok:
            if ERRLOG:
                ERRLOG.error(f"{path}: imencode вернул False")
            return False
        with open(tmp, "wb") as f:
            f.write(buf.tobytes())
        os.replace(tmp, path)
        return True
    except Exception as e:
        if ERRLOG:
            ERRLOG.error(f"{path}: исключение при сохранении ({e})")
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def is_blank_frame(frame) -> bool:
    """
    Ровный серый кадр (яркость ≈128, почти без деталей) — так декодер
    отдаёт картинку, когда ключевой кадр в записи потерян/повреждён.
    """
    sample = frame[::24, ::24]
    mean, std = cv2.meanStdDev(cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY))
    return abs(mean[0][0] - 128) < 6 and std[0][0] < 12


def read_good_frame(cap, offset: float, interval: float):
    """
    Кадр на момент offset. Если он повреждён (серый), читаем дальше
    до первого нормального кадра, но не дальше следующего момента.
    Возвращает (frame, status): "ok" | "damaged" | "read_fail".
    """
    limit_ms = (offset + interval) * 1000.0
    cap.set(cv2.CAP_PROP_POS_MSEC, offset * 1000.0)
    ok, frame = cap.read()
    got_any = False
    read_fails = 0
    for _ in range(600):
        if ok and frame is not None:
            got_any = True
            if not is_blank_frame(frame):
                return frame, "ok"
        else:
            read_fails += 1
            if read_fails > 10:
                break
        if not cap.grab():
            ok, frame = False, None
            continue
        if cap.get(cv2.CAP_PROP_POS_MSEC) >= limit_ms:
            break
        ok, frame = cap.retrieve()
    return None, ("damaged" if got_any else "read_fail")


def load_damaged_list(out_dir: Path) -> set:
    try:
        with open(out_dir / DAMAGED_LIST_NAME, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def append_damaged_list(out_dir: Path, names):
    try:
        with open(out_dir / DAMAGED_LIST_NAME, "a", encoding="utf-8") as f:
            for name in names:
                f.write(name + "\n")
    except OSError as e:
        if ERRLOG:
            ERRLOG.error(f"не записать {out_dir / DAMAGED_LIST_NAME}: {e}")


def check_free_space(folder: Path):
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if free < MIN_FREE_GB * 1024**3:
        raise LowDiskSpace(
            f"на диске {folder.anchor} осталось {free / 1024**3:.1f} ГБ "
            f"(< {MIN_FREE_GB} ГБ)"
        )


# ============================================================
# ОПРЕДЕЛЕНИЕ СТАРТА ВИДЕО
# ============================================================

def parse_unix_ts_from_name(name: str):
    m = UNIX_TS_RE.search(name)
    if not m:
        return None
    try:
        ts = int(m.group(1))
    except ValueError:
        return None
    if 10**9 <= ts <= 2 * 10**9:
        return datetime.fromtimestamp(ts)
    return None


def parse_datetime_from_name(name: str):
    m = NAME_DT_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(
            m.group(1) + m.group(2) + (m.group(3) or "00"), "%Y%m%d%H%M%S"
        )
    except ValueError:
        return None


def detect_start(video_path: Path, duration: float):
    try:
        mtime = datetime.fromtimestamp(video_path.stat().st_mtime)
    except OSError as e:
        mtime = None
        if ERRLOG:
            ERRLOG.warn(f"{video_path}: mtime недоступен ({e})")

    if mtime is not None and 2000 <= mtime.year <= 2100:
        return mtime - timedelta(seconds=duration), f"mtime={mtime}"

    ts = parse_unix_ts_from_name(video_path.name)
    if ts is not None:
        return ts, f"unix-ts={ts}"

    dt = parse_datetime_from_name(video_path.name)
    if dt is not None:
        return dt, f"дата-в-имени={dt}"

    return None, "ни mtime, ни время в имени не сработали"


# ============================================================
# ОТКРЫТИЕ ВИДЕО
# ============================================================

def open_video_any_backend(path: Path):
    # Только FFMPEG: MSMF на битом файле висит ~60 секунд и всё равно
    # не открывает, GSTREAMER в сборке opencv-python отсутствует.
    try:
        cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    except Exception:
        return None
    if cap.isOpened():
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        fc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps > 0 and fc > 0:
            return cap
    cap.release()
    return None


def file_is_growing(path: Path, delay=0.4) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    s1 = st.st_size
    if s1 <= 0:
        return False
    # Давно не менявшийся файл точно не пишется — не тратим 0.4 с на каждый
    if time.time() - st.st_mtime > GROWING_CHECK_AGE_SECONDS:
        return False
    time.sleep(delay)
    try:
        s2 = path.stat().st_size
    except OSError:
        return False
    return s1 != s2


def build_video_info(video_path: Path):
    if file_is_growing(video_path):
        return None, "файл прямо сейчас пишется (растёт)"

    cap = open_video_any_backend(video_path)
    if cap is None:
        return None, ("не открылся в OpenCV/FFMPEG (файл битый или "
                      "недописан — нет индекса moov)")

    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        fc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()

    if not fps or fps <= 0:
        return None, f"FPS={fps}"
    if fc <= 0:
        return None, f"кадров={fc}"

    duration = fc / fps
    start, src = detect_start(video_path, duration)
    if start is None:
        return None, src

    return {
        "path": video_path,
        "start": start,
        "duration": duration,
        "frame_count": fc,
        "fps": fps,
        "stem": video_path.stem,
        "time_source": src,
    }, None


# ============================================================
# РАСЧЁТ МОМЕНТОВ КАДРОВ
# ============================================================

def max_frame_index(frame_count: int, fps: float, interval: int) -> int:
    if frame_count <= 0 or fps <= 0:
        return -1
    # Момент последнего реально существующего кадра; дальше него
    # seek + read гарантированно не сработает
    last_ts = (frame_count - 1) / fps
    return int(last_ts // interval)


def frame_times(info, interval):
    start = info["start"]
    max_n = max_frame_index(info["frame_count"], info["fps"], interval)
    if max_n < 0:
        return
    for n in range(0, max_n + 1):
        offset = n * interval
        yield start + timedelta(seconds=offset), offset


# ============================================================
# СУФФИКС "Nшт" В ИМЕНИ ПАПКИ
# ============================================================

def resolve_dst_folder(parent: Path, name: str) -> Path:
    """
    Папка со скринами для исходной папки `name`. Раньше такие папки
    переименовывались в "<name> Nшт"; если такая осталась — возвращаем
    её, иначе скрипт не увидит готовые скрины и начнёт всё заново.
    """
    try:
        for p in parent.iterdir():
            if p.name == name:
                continue
            m = COUNT_SUFFIX_RE.match(p.name)
            if m and m.group(1) == name and p.is_dir():
                return p
    except OSError:
        pass
    return parent / name


# ============================================================
# ОБРАБОТКА ОДНОГО ВИДЕО
# ============================================================

def sorted_dirs(out_dir: Path):
    """Папки одобренных и удалённых скринов, парные к out_dir."""
    try:
        rel = out_dir.relative_to(SCREENS_ROOT)
    except ValueError:
        return []
    return [APPROVED_FOLDER / rel, DELETED_FOLDER / rel]


def process_video(info, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = SCREEN_EXT

    times = list(frame_times(info, INTERVAL_SECONDS))
    if not times:
        return {"created": 0, "skipped": 0, "failed": 0, "damaged": 0,
                "total": 0}

    known_damaged = load_damaged_list(out_dir)

    # Читаем существующие скрины ОДИН раз
    existing_sizes = {}
    try:
        for p in out_dir.iterdir():
            name = p.name
            if not name.endswith(ext):
                continue
            try:
                existing_sizes[name] = p.stat().st_size
            except OSError:
                existing_sizes[name] = 0
    except OSError as e:
        if ERRLOG:
            ERRLOG.error(f"не прочитать {out_dir}: {e}")

    # Скрины, разобранные в проводнике, лежат в папках одобренных и
    # удалённых — считаем их готовыми
    for sorted_dir in sorted_dirs(out_dir):
        try:
            with os.scandir(sorted_dir) as entries:
                for entry in entries:
                    if entry.name.endswith(ext):
                        existing_sizes[entry.name] = max(
                            existing_sizes.get(entry.name, 0),
                            MIN_FILE_SIZE_BYTES)
        except OSError:
            pass

    todo = []
    skipped = damaged = 0
    for t, offset in times:
        fname = f"{info['stem']}_{t.strftime('%H-%M-%S')}{ext}"
        if existing_sizes.get(fname, 0) >= MIN_FILE_SIZE_BYTES:
            skipped += 1
            continue
        if fname in known_damaged:
            damaged += 1
            continue
        todo.append((t, offset, out_dir / fname))

    if not todo:
        note = f", повреждённых в записи {damaged}" if damaged else ""
        print(f"        ✓ {info['stem']}: "
              f"{skipped}/{len(times)} — уже готово{note}")
        return {"created": 0, "skipped": skipped, "failed": 0,
                "damaged": damaged, "total": len(times)}

    print(f"        → {info['stem']}: ожидается {len(times)}; "
          f"есть {skipped}; досоздаём {len(todo)}")

    cap = open_video_any_backend(info["path"])
    if cap is None:
        if ERRLOG:
            ERRLOG.error(f"видео перестало открываться: {info['path']}")
        return {"created": 0, "skipped": skipped, "failed": len(todo),
                "damaged": damaged, "total": len(times)}

    created = failed = 0
    failed_list = []
    new_damaged = []
    try:
        for i, (t, offset, path) in enumerate(todo):
            if i % 20 == 0:
                check_free_space(out_dir)
            frame, status = read_good_frame(cap, offset, INTERVAL_SECONDS)
            if status == "damaged":
                new_damaged.append(path.name)
                continue
            if frame is None:
                failed += 1
                failed_list.append((path.name, offset))
                continue
            if save_frame(path, frame):
                created += 1
            else:
                failed += 1
                failed_list.append((path.name, offset))
    finally:
        cap.release()
        if new_damaged:
            append_damaged_list(out_dir, new_damaged)

    damaged += len(new_damaged)
    if new_damaged and ERRLOG:
        ERRLOG.warn(
            f"{info['path']}: запись повреждена, {len(new_damaged)} из "
            f"{len(times)} кадров не декодируются (серый экран) — пропущены"
        )

    if failed_list and ERRLOG:
        ERRLOG.warn(
            f"{info['path']}: не создано {len(failed_list)} кадров"
        )
        for name, off in failed_list[:20]:
            ERRLOG.warn(f"  кадр не сохранён: {name} (offset={off:.1f}s)")
        if len(failed_list) > 20:
            ERRLOG.warn(f"  ... ещё {len(failed_list) - 20} кадров")

    note = f", повреждённых в записи {damaged}" if damaged else ""
    print(f"        ✓ {info['stem']}: создано {created}, "
          f"ошибок {failed}{note}, итого {skipped + created}/{len(times)}")
    return {"created": created, "skipped": skipped, "failed": failed,
            "damaged": damaged, "total": len(times)}


# ============================================================
# СБОР ВИДЕО (ТОЛЬКО .mp4)
# ============================================================

def collect_mp4_in_folder(folder: Path):
    """
    Возвращает отсортированный список .mp4-файлов непосредственно
    в этой папке. Никаких is_file() для остальных расширений —
    сначала suffix-фильтр, только потом stat.
    """
    result = []
    try:
        for p in folder.iterdir():
            # 1) Сначала дешёвая проверка по расширению
            if p.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            # 2) Только теперь дёргаем ФС
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            result.append(p)
    except OSError as e:
        if ERRLOG:
            ERRLOG.error(f"не прочитать {folder}: {e}")
        return []

    result.sort(key=lambda p: p.name.lower())
    return result


# ============================================================
# ОБРАБОТКА ЛИСТОВОЙ ПАПКИ (только mp4)
# ============================================================

def process_leaf_folder(src_folder: Path, dst_folder: Path):
    vids = collect_mp4_in_folder(src_folder)
    if not vids:
        return None

    if RUNLOG:
        RUNLOG.folder_started(src_folder)

    print(f"\n  >>> ПАПКА: {src_folder}")
    print(f"      mp4: {len(vids)}")

    stats = {"created": 0, "skipped": 0, "failed": 0, "damaged": 0,
             "total": 0, "videos_ok": 0, "videos_broken": 0,
             "src": src_folder, "dst": dst_folder, "real_count": 0}

    for v in vids:
        info, reason = build_video_info(v)
        if info is None:
            stats["videos_broken"] += 1
            if ERRLOG:
                ERRLOG.error(f"видео не обработается: {v} — {reason}")
            print(f"        ✗ {v.name}: {reason}")
            continue

        stats["videos_ok"] += 1
        rep = process_video(info, dst_folder)
        stats["created"] += rep["created"]
        stats["skipped"] += rep["skipped"]
        stats["failed"]  += rep["failed"]
        stats["damaged"] += rep["damaged"]
        stats["total"]   += rep["total"]

    # Сколько реально файлов скринов в папке
    real_count = 0
    try:
        for p in dst_folder.iterdir():
            if p.suffix == SCREEN_EXT:
                real_count += 1
    except OSError:
        pass
    stats["real_count"] = real_count

    print(f"      итог: создано={stats['created']}, "
          f"уже_было={stats['skipped']}, ошибок={stats['failed']}, "
          f"повреждено_в_записи={stats['damaged']}, "
          f"файлов_в_папке={real_count}")

    return stats


# ============================================================
# РЕКУРСИВНЫЙ ОБХОД
# ============================================================

def selected_starts(src_root: Path):
    """
    Откуда начинать обход: [(папка с видео, её путь внутри src_root)].
    Без ONLY_FOLDERS — одна запись: сама src_root.
    """
    chosen = set()
    for raw in ONLY_FOLDERS:
        parts = tuple(p for p in raw.replace("\\", "/").split("/")
                      if p and p != ".")
        if parts and ".." not in parts:
            chosen.add(parts)
    # Папка внутри уже выбранной обработается вместе с ней
    chosen = sorted(c for c in chosen
                    if not any(c[:n] in chosen for n in range(1, len(c))))
    if not chosen:
        return [(src_root, ())]
    return [(src_root.joinpath(*parts), parts) for parts in chosen]


def walk_and_process(src_root: Path, dst_root: Path, starts):
    all_stats = []

    def _walk(src: Path, dst: Path):
        try:
            subdirs = sorted(
                [p for p in src.iterdir() if p.is_dir()],
                key=lambda p: p.name.lower()
            )
        except OSError as e:
            if ERRLOG:
                ERRLOG.error(f"не прочитать {src}: {e}")
            return

        st = process_leaf_folder(src, dst)
        if st is not None:
            all_stats.append((src, st))

        in_place = dst == src
        for sd in subdirs:
            _walk(sd, sd if in_place else resolve_dst_folder(dst, sd.name))

        if st is not None and RUNLOG:
            RUNLOG.folder_finished(src, st["created"], st["skipped"],
                                   st["failed"])

    dst_root.mkdir(parents=True, exist_ok=True)
    for src, parts in starts:
        if dst_root == src_root:
            dst = src
        else:
            # Тот же путь внутри папки со скринами
            dst = dst_root
            for part in parts:
                dst = resolve_dst_folder(dst, part)
        _walk(src, dst)
    return all_stats


# ============================================================
# MAIN
# ============================================================

def main():
    global ERRLOG, RUNLOG

    print("=" * 72)
    print(f"INPUT_FOLDER          = {INPUT_FOLDER}")
    print(f"ONLY_FOLDERS          = {ONLY_FOLDERS or 'вся папка'}")
    print(f"OUTPUT_FOLDER         = {OUTPUT_FOLDER}")
    print(f"SCREENSHOTS_IN_PLACE  = {SCREENSHOTS_IN_PLACE}")
    print(f"SAVE_AS_PNG           = {SAVE_AS_PNG}")
    print(f"INTERVAL_SECONDS      = {INTERVAL_SECONDS}")
    print(f"VIDEO_EXTENSIONS      = {sorted(VIDEO_EXTENSIONS)}")
    print(f"LOGS_FOLDER           = {LOGS_FOLDER}")
    print("=" * 72)

    if not INPUT_FOLDER.exists():
        print(f"ОШИБКА: нет папки: {INPUT_FOLDER}")
        return

    LOGS_FOLDER.mkdir(parents=True, exist_ok=True)
    ERRLOG = ErrorLogger(ERRORS_LOG_FILE)
    RUNLOG = RunLogger(RUN_LOG_FILE)

    print(f"Логи ошибок:   {ERRORS_LOG_FILE}")
    print(f"Лог запуска:   {RUN_LOG_FILE}")

    if SCREENSHOTS_IN_PLACE:
        src_root = INPUT_FOLDER
        dst_root = INPUT_FOLDER
    else:
        src_root = INPUT_FOLDER
        dst_root = OUTPUT_FOLDER
        dst_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Сканирование — только .mp4
    # ------------------------------------------------------------------
    starts = []
    for start, parts in selected_starts(src_root):
        if start.is_dir():
            starts.append((start, parts))
        else:
            print(f"  ✗ выбранной папки нет: {start}")
            ERRLOG.error(f"выбранной папки нет: {start}")
    if not starts:
        print("Ни одной из выбранных папок нет на месте.")
        RUNLOG.summary("выбранных папок нет на месте")
        return

    print("\n### СКАНИРОВАНИЕ ДЕРЕВА (только .mp4)")
    tree = []
    for start, _parts in starts:
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames.sort()
            # Дешёвый фильтр: endswith вместо Path().suffix на каждом файле
            mp4s = [f for f in filenames
                    if f.lower().endswith(".mp4")]
            if mp4s:
                tree.append((Path(dirpath), len(mp4s)))

    if not tree:
        print("Видео .mp4 не найдено.")
        RUNLOG.summary("mp4 не найдено")
        return

    print(f"Папок с .mp4: {len(tree)}")
    total_mp4 = sum(n for _, n in tree)
    print(f"Всего .mp4: {total_mp4}")
    for f, n in tree:
        try:
            rel = f.relative_to(src_root)
        except ValueError:
            rel = f
        print(f"  {rel}  → {n} mp4")

    # ------------------------------------------------------------------
    # Обработка
    # ------------------------------------------------------------------
    print("\n### ОБРАБОТКА")
    all_stats = walk_and_process(src_root, dst_root, starts)

    # ------------------------------------------------------------------
    # Итог
    # ------------------------------------------------------------------
    total_created = sum(st["created"] for _, st in all_stats)
    total_skipped = sum(st["skipped"] for _, st in all_stats)
    total_failed  = sum(st["failed"]  for _, st in all_stats)
    total_damaged = sum(st["damaged"] for _, st in all_stats)
    total_videos_ok = sum(st["videos_ok"] for _, st in all_stats)
    total_videos_broken = sum(st["videos_broken"] for _, st in all_stats)

    print("\n" + "=" * 72)
    print("ИТОГО")
    print("=" * 72)
    print(f"  папок обработано:    {len(all_stats)}")
    print(f"  видео ок:            {total_videos_ok}")
    print(f"  видео битых:         {total_videos_broken}")
    print(f"  кадров создано:      {total_created}")
    print(f"  кадров уже было:     {total_skipped}")
    print(f"  кадров с ошибкой:    {total_failed}")
    print(f"  повреждено в записи: {total_damaged}")
    print(f"  скриншоты:           "
          f"{INPUT_FOLDER if SCREENSHOTS_IN_PLACE else OUTPUT_FOLDER}")
    print(f"  лог ошибок:          {ERRORS_LOG_FILE}")
    print(f"  лог запуска:         {RUN_LOG_FILE}")

    RUNLOG.summary(
        f"папок={len(all_stats)}, видео_ок={total_videos_ok}, "
        f"видео_битых={total_videos_broken}, "
        f"создано={total_created}, уже_было={total_skipped}, "
        f"ошибок={total_failed}, повреждено_в_записи={total_damaged}"
    )


def _watch_stdin_for_stop():
    """
    Запуск из интерфейса (gui.py): кнопка «Остановить» шлёт строку в stdin
    (или закрывает его). Прерываем главный поток так же, как Ctrl+C.
    """
    import threading
    import _thread

    def _wait():
        try:
            sys.stdin.readline()
        except Exception:
            pass
        _thread.interrupt_main()

    threading.Thread(target=_wait, daemon=True).start()


if __name__ == "__main__":
    if os.environ.get("SCREEN_SCRIPT_GUI"):
        _watch_stdin_for_stop()
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nПрервано. Запустите снова — продолжит с места остановки.")
        if ERRLOG:
            ERRLOG.warn("скрипт прерван пользователем (Ctrl+C)")
        if RUNLOG:
            RUNLOG.summary("прервано пользователем")
    except LowDiskSpace as e:
        print(f"\n\nОСТАНОВЛЕНО: {e}. Освободите место и запустите снова — "
              f"продолжит с места остановки.")
        if ERRLOG:
            ERRLOG.error(f"остановлено: {e}")
        if RUNLOG:
            RUNLOG.summary(f"остановлено: {e}")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("\n\nНЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:\n" + tb)
        if ERRLOG:
            ERRLOG.error(f"необработанное исключение: {e}")
            for line in tb.splitlines():
                ERRLOG.error("  " + line)
        raise
