import os
import random
import struct
import subprocess

import pytest

from video_renamer.core.phash import (
    PhashError,
    PhashToolError,
    build_montage,
    decode_bmp,
    dct_top_left_8x8,
    extract_frame,
    format_phash,
    frame_times,
    phash_from_path,
    quick_select_median,
)


def make_bmp_24(width, height, pixels):
    """BMP 24-bit, строки снизу вверх; pixels — список строк сверху вниз из (r, g, b)."""
    row_size = ((width * 3 + 3) // 4) * 4
    body = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            r, g, b = pixels[y][x]
            row += bytes([b, g, r])
        row += b"\x00" * (row_size - len(row))
        body += row
    header = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                          len(body), 0, 0, 0, 0)
    return header + bytes(body)


def fake_completed(args, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def test_frame_times_match_stash_formula():
    times = frame_times(100.0)
    assert len(times) == 25
    assert times[0] == pytest.approx(5.0)
    assert times[-1] == pytest.approx(5.0 + 24 * 3.6)


def test_decode_bmp_reads_24bit_bottom_up_into_top_down_gray():
    pixels = [[(255, 0, 0), (0, 255, 0)], [(0, 0, 255), (255, 255, 255)]]
    width, height, gray = decode_bmp(make_bmp_24(2, 2, pixels))
    assert (width, height) == (2, 2)
    assert gray[0][0] == pytest.approx(0.299 * 255)
    assert gray[0][1] == pytest.approx(0.587 * 255)
    assert gray[1][0] == pytest.approx(0.114 * 255)
    assert gray[1][1] == pytest.approx(255.0)


def test_build_montage_places_cells_row_major():
    frames = [(1, 1, [[float(i)]]) for i in range(25)]
    canvas = build_montage(frames)
    assert len(canvas) == 5 and len(canvas[0]) == 5
    assert canvas[0][4] == 4.0
    assert canvas[1][0] == 5.0


def test_quick_select_median_stays_near_true_median():
    values = [float(v) for v in random.Random(1).sample(range(1000), 64)]
    ordered = sorted(values)
    result = quick_select_median(list(values), 0, 63, 32)
    assert ordered[30] <= result <= ordered[33]


def test_dct_top_left_zero_coefficient_is_sum_of_all_pixels():
    pixels = [[float((r * 7 + c * 3) % 11) for c in range(64)] for r in range(64)]
    flat = dct_top_left_8x8(pixels)
    assert len(flat) == 64
    assert flat[0] == pytest.approx(sum(sum(row) for row in pixels))


def test_format_phash_is_16_lowercase_hex():
    assert format_phash(0xDC75C0F03795438E) == "dc75c0f03795438e"


def test_extract_frame_uses_fast_seek_args_and_no_shell():
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return fake_completed(args, stdout=b"BM")

    extract_frame("C:\\v.mp4", 12.5, accurate=False, ffmpeg="ffmpeg", run=fake_run)
    args, kwargs = calls[0]
    assert isinstance(args, list)
    assert args.index("-ss") < args.index("-i")
    assert "scale=160:-2" in args
    assert kwargs.get("shell") is not True


def test_extract_frame_accurate_seek_puts_ss_after_input():
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return fake_completed(args, stdout=b"BM")

    extract_frame("C:\\v.mp4", 12.5, accurate=True, ffmpeg="ffmpeg", run=fake_run)
    assert calls[0].index("-ss") > calls[0].index("-i")


def test_extract_frame_empty_output_is_phash_error():
    def fake_run(args, **kwargs):
        return fake_completed(args, returncode=1, stderr=b"no frame")

    with pytest.raises(PhashError):
        extract_frame("C:\\v.mp4", 1.0, accurate=False, ffmpeg="ffmpeg", run=fake_run)


def test_missing_tool_is_phash_tool_error():
    def fake_run(args, **kwargs):
        raise FileNotFoundError(args[0])

    with pytest.raises(PhashToolError):
        phash_from_path("C:\\v.mp4", ffmpeg="ffmpeg", ffprobe="ffprobe", run=fake_run)


def test_phash_from_path_fails_on_missing_duration():
    def fake_run(args, **kwargs):
        return fake_completed(args, stdout=b"N/A\n")

    with pytest.raises(PhashError):
        phash_from_path("C:\\v.mp4", ffmpeg="ffmpeg", ffprobe="ffprobe", run=fake_run)


REFERENCE_FILE = r"D:\Videos\PH\brazzersexxtra.26.10.03.ryan.reid.poking.glory.mp4"
REFERENCE_PHASH = 0xDC75C0F03795438E


def _ffmpeg_binary(name):
    explicit = os.environ.get(f"{name.upper()}_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    winget = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "Microsoft", "WinGet", "Links", f"{name}.exe")
    return winget if os.path.exists(winget) else None


@pytest.mark.skipif(not os.path.exists(REFERENCE_FILE), reason="local sample file")
@pytest.mark.skipif(_ffmpeg_binary("ffmpeg") is None, reason="ffmpeg not installed")
def test_phash_matches_stashdb_reference_within_threshold():
    value = phash_from_path(
        REFERENCE_FILE,
        ffmpeg=_ffmpeg_binary("ffmpeg"),
        ffprobe=_ffmpeg_binary("ffprobe"),
    )
    assert bin(value ^ REFERENCE_PHASH).count("1") <= 4


def test_phash_from_path_retries_failed_fast_seek_with_accurate_seek():
    frame = make_bmp_24(2, 2, [[(10, 20, 30), (40, 50, 60)], [(70, 80, 90), (1, 2, 3)]])
    accurate_calls = []

    def fake_run(args, **kwargs):
        if args[0] == "ffprobe":
            return fake_completed(args, stdout=b"10.0\n")
        if args.index("-ss") > args.index("-i"):
            accurate_calls.append(args)
            return fake_completed(args, stdout=frame)
        return fake_completed(args, returncode=1, stderr=b"seek failed")

    value = phash_from_path("C:\\v.mp4", ffmpeg="ffmpeg", ffprobe="ffprobe", run=fake_run)
    assert isinstance(value, int)
    assert len(accurate_calls) == 25
