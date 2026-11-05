import pytest

from video_renamer.core.oshash import OshashError, oshash_from_path

CHUNK_SIZE = 64 * 1024


def make_byte_array(base: bytes, mag: int) -> bytes:
    """Mirrors Go's makeByteArray: doubles the buffer `mag` times."""
    ret = base
    for _ in range(mag):
        ret = ret + ret
    return ret


def make_tail_array(base: bytes, tail: bytes) -> bytes:
    """Mirrors Go's makeTailArray: appends a chunk-sized buffer ending with `tail`."""
    buf = bytearray(CHUNK_SIZE)
    buf[len(buf) - len(tail):] = tail
    return base + bytes(buf)


def test_oshash_known_vectors(tmp_path):
    p = tmp_path / "hello.bin"
    p.write_bytes(b"hello world")
    assert oshash_from_path(p) == "d3e392dee38cd4df"

    p.write_bytes(make_byte_array(b"this is a test", 15))
    assert oshash_from_path(p) == "6a0eba04654d0b9b"


def test_oshash_rejects_tiny_file(tmp_path):
    p = tmp_path / "tiny.bin"
    p.write_bytes(b"hello")
    with pytest.raises(OshashError):
        oshash_from_path(p)


def test_oshash_head_and_tail_vectors(tmp_path):
    """Go's 'identical #1/#2': head and tail both matter."""
    p = tmp_path / "big.bin"
    p.write_bytes(make_tail_array(bytes(CHUNK_SIZE), b"this is dumb"))
    assert oshash_from_path(p) == "d5d6ddd820756920"

    p.write_bytes(make_tail_array(bytes(CHUNK_SIZE), b"dumb is this"))
    assert oshash_from_path(p) == "d5d6ddd820756920"


def test_oshash_is_16_lowercase_hex(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"0123456789abcdef")
    value = oshash_from_path(p)
    assert len(value) == 16
    assert value == value.lower()
    assert int(value, 16) >= 0


def test_oshash_error_is_value_error():
    assert issubclass(OshashError, ValueError)


def test_oshash_rejects_file_of_exactly_eight_bytes(tmp_path):
    p = tmp_path / "eight.bin"
    p.write_bytes(b"01234567")
    with pytest.raises(OshashError):
        oshash_from_path(p)


def test_oshash_accepts_str_path(tmp_path):
    p = tmp_path / "str.bin"
    p.write_bytes(b"hello world")
    assert oshash_from_path(str(p)) == "d3e392dee38cd4df"


def test_oshash_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        oshash_from_path(tmp_path / "nope.bin")
