#!/usr/bin/env python3
"""PHASH — перцептивный хэш видео, совместимый со Stash и StashDB.

Повторяет `pkg/hash/videophash` (Stash) и `PerceptionHash` (goimagehash):

- 25 кадров на отрезке 5–95 % длительности, каждый масштабируется до ширины
  160 px (`scale=160:-2`) и отдаётся как BMP;
- кадры собираются в сетку 5×5 (серый: 0.299R + 0.587G + 0.114B);
- сетка уменьшается до 64×64, считается DCT-II по строкам и столбцам, берутся
  коэффициенты 8×8, бит ставится при `p > медиана`.

Ресемплинг — треугольный фильтр; побитовой копии весов `nfnt/resize` нет.
Проверено на файлах StashDB: расстояние Хэмминга до сохранённого значения
0–2 бита, порог StashDB — 4 бита.

ffmpeg и ffprobe вызываются списками аргументов без оболочки. Зависимостей
нет — только стандартная библиотека.
"""
import math
import os
import struct
import subprocess

__all__ = [
    "PhashError",
    "PhashToolError",
    "build_montage",
    "check_phash_tools",
    "dct_top_left_8x8",
    "decode_bmp",
    "extract_frame",
    "format_phash",
    "frame_times",
    "phash_from_path",
    "probe_duration",
    "quick_select_median",
    "resize_bilinear",
]

FRAME_COUNT = 25
GRID = 5
FRAME_WIDTH = 160
HASH_SIZE = 64
DCT_SIZE = 8
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROBE_TIMEOUT_SECONDS = 30
FRAME_TIMEOUT_SECONDS = 60


class PhashError(ValueError):
    """Ошибка файла: нет длительности, ffmpeg не вернул кадр, BMP не разобран."""


class PhashToolError(RuntimeError):
    """ffmpeg или ffprobe не найдены или не запускаются."""


def frame_times(duration: float) -> list[float]:
    """25 отметок времени: отступ 5 % с каждого конца, равный шаг."""
    offset = 0.05 * duration
    step = (0.9 * duration) / FRAME_COUNT
    return [offset + index * step for index in range(FRAME_COUNT)]


def _run(args: list[str], run, timeout: float) -> subprocess.CompletedProcess:
    """Запуск без оболочки. Отсутствие бинарника — `PhashToolError`, таймаут
    пробрасывается вызывающему коду, который решает, какая ошибка уместна."""
    try:
        return run(
            args,
            capture_output=True,
            check=False,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise PhashToolError(f"cannot run {args[0]}: {exc}") from exc


def check_phash_tools(ffmpeg: str, ffprobe: str, run=subprocess.run) -> None:
    """Проверяет, что ffmpeg и ffprobe запускаются; иначе `PhashToolError`."""
    for name, binary in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        try:
            result = _run([binary, "-version"], run, PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise PhashToolError(f"{name} timed out: {binary}") from exc
        if result.returncode != 0:
            raise PhashToolError(f"{name} failed to run: {binary}")


def probe_duration(path: str, ffprobe: str, run=subprocess.run) -> float:
    """Длительность файла в секундах по `format=duration`."""
    args = [
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", path,
    ]
    try:
        result = _run(args, run, PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise PhashError("ffprobe timed out reading duration") from exc
    if result.returncode != 0:
        raise PhashError("ffprobe failed to read duration")
    text = (result.stdout or b"").decode("utf-8", "replace").strip()
    try:
        duration = float(text)
    except ValueError as exc:
        raise PhashError(f"no duration in ffprobe output: {text!r}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise PhashError(f"invalid duration: {duration}")
    return duration


def extract_frame(path: str, t: float, *, accurate: bool, ffmpeg: str,
                  run=subprocess.run) -> bytes:
    """Один кадр в момент `t` как BMP. `accurate` — seek после `-i`."""
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if not accurate:
        args += ["-ss", f"{t:.3f}"]
    args += ["-i", path]
    if accurate:
        args += ["-ss", f"{t:.3f}"]
    args += [
        "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2",
        "-c:v", "bmp", "-f", "rawvideo", "-",
    ]
    try:
        result = _run(args, run, FRAME_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise PhashError(f"ffmpeg timed out at {t:.3f}s") from exc
    if result.returncode != 0 or not result.stdout:
        raise PhashError(f"ffmpeg returned no frame at {t:.3f}s")
    return result.stdout


def decode_bmp(data: bytes) -> tuple[int, int, list[list[float]]]:
    """BMP 24/32-bit → (ширина, высота, серые строки сверху вниз)."""
    if len(data) < 54 or data[:2] != b"BM":
        raise PhashError("frame is not a BMP")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    if bpp not in (24, 32):
        raise PhashError(f"unsupported BMP bit depth: {bpp}")
    top_down = height < 0
    height = abs(height)
    bytes_per_pixel = bpp // 8
    stride = ((width * bytes_per_pixel + 3) // 4) * 4
    rows: list[list[float]] = []
    for row in range(height):
        source_row = row if top_down else height - 1 - row
        base = offset + source_row * stride
        line = []
        for x in range(width):
            p = base + x * bytes_per_pixel
            blue, green, red = data[p], data[p + 1], data[p + 2]
            line.append(0.299 * red + 0.587 * green + 0.114 * blue)
        rows.append(line)
    return width, height, rows


def build_montage(frames) -> list[list[float]]:
    """Сетка 5×5 из кадров; размер ячейки берётся из первого кадра."""
    cell_w, cell_h = frames[0][0], frames[0][1]
    canvas = [[0.0] * (cell_w * GRID) for _ in range(cell_h * GRID)]
    for index, (width, height, gray) in enumerate(frames):
        x0 = cell_w * (index % GRID)
        y0 = cell_h * (index // GRID)
        for y in range(min(height, cell_h)):
            target = canvas[y0 + y]
            source = gray[y]
            for x in range(min(width, cell_w)):
                target[x0 + x] = source[x]
    return canvas


def resize_bilinear(gray: list[list[float]], out_w: int, out_h: int) -> list[list[float]]:
    """Треугольный фильтр с масштабом `in/out`, сначала по строкам, затем по столбцам."""
    in_h = len(gray)
    in_w = len(gray[0])
    scale_x = in_w / out_w
    scale_y = in_h / out_h

    def weights(in_len: int, out_len: int, scale: float):
        table = []
        for i in range(out_len):
            center = (i + 0.5) * scale - 0.5
            lo = max(0, math.floor(center - scale))
            hi = min(in_len - 1, math.ceil(center + scale))
            taps = []
            total = 0.0
            for j in range(lo, hi + 1):
                w = max(0.0, 1.0 - abs(j - center) / scale)
                if w > 0:
                    taps.append((j, w))
                    total += w
            table.append([(j, w / total) for j, w in taps])
        return table

    wx = weights(in_w, out_w, scale_x)
    wy = weights(in_h, out_h, scale_y)
    horizontal = [
        [sum(row[j] * w for j, w in taps) for taps in wx] for row in gray
    ]
    result = []
    for taps in wy:
        result.append([
            sum(horizontal[j][x] * w for j, w in taps) for x in range(out_w)
        ])
    return result


def dct_top_left_8x8(pixels64: list[list[float]]) -> list[float]:
    """Левый верхний блок 8×8 DCT-II без нормировки; `flat[8*j+i]`."""
    n = HASH_SIZE
    cos_table = [
        [math.cos(math.pi / n * (i + 0.5) * k) for i in range(n)]
        for k in range(DCT_SIZE)
    ]
    rows_k = []
    for row in pixels64:
        rows_k.append([
            sum(row[i] * cos_table[k][i] for i in range(n))
            for k in range(DCT_SIZE)
        ])
    flat = [0.0] * (DCT_SIZE * DCT_SIZE)
    for col in range(DCT_SIZE):
        column = [rows_k[r][col] for r in range(n)]
        for j in range(DCT_SIZE):
            flat[DCT_SIZE * j + col] = sum(
                column[r] * cos_table[j][r] for r in range(n)
            )
    return flat


def quick_select_median(seq: list[float], low: int, hi: int, k: int) -> float:
    """Порт `quickSelectMedian` из goimagehash (в том же порядке операций)."""
    if low == hi:
        return seq[k]
    while low < hi:
        pivot = low // 2 + hi // 2
        pivot_value = seq[pivot]
        store = low
        seq[pivot], seq[hi] = seq[hi], seq[pivot]
        for i in range(low, hi):
            if seq[i] < pivot_value:
                seq[store], seq[i] = seq[i], seq[store]
                store += 1
        seq[hi], seq[store] = seq[store], seq[hi]
        if k <= store:
            hi = store
        else:
            low = store + 1
    if len(seq) % 2 == 0:
        return seq[k - 1] / 2 + seq[k] / 2
    return seq[k]


def format_phash(value: int) -> str:
    """64-битное значение как 16 hex-символов в нижнем регистре."""
    return f"{value:016x}"


def phash_from_path(path: str, *, ffmpeg: str, ffprobe: str,
                    run=subprocess.run) -> int:
    """PHASH видеофайла как 64-битное целое."""
    path = os.path.abspath(path)
    duration = probe_duration(path, ffprobe, run)
    frames = []
    accurate = False
    for t in frame_times(duration):
        try:
            data = extract_frame(path, t, accurate=accurate, ffmpeg=ffmpeg, run=run)
        except PhashError:
            if accurate:
                raise
            accurate = True
            data = extract_frame(path, t, accurate=True, ffmpeg=ffmpeg, run=run)
        frames.append(decode_bmp(data))
    montage = build_montage(frames)
    small = resize_bilinear(montage, HASH_SIZE, HASH_SIZE)
    flat = dct_top_left_8x8(small)
    median = quick_select_median(list(flat), 0, len(flat) - 1, len(flat) // 2)
    value = 0
    for index, p in enumerate(flat):
        if p > median:
            value |= 1 << (63 - index)
    return value
