"""Webfoot detection + .wbf container round-trip. Builds a minimal but
structurally real Webfoot ROM image in memory, converts it, and
checks the .wbf parses back to the same song/instrument/sample data the
engine will read."""

import struct

from gba_audio.native import build_wbf, detect_webfoot


def _luts() -> bytes:
    b = bytearray()
    for n in range(120):  # note periods (0x200,0x21E,...)
        b += struct.pack("<I", round(16384 * 2 ** ((n - 60) / 12)))
    for r in range(256):  # tup starts 16384,16443,16503
        b += struct.pack("<H", min(65535, round(16384 * 2 ** (r / 192))))
    for r in range(256):  # tdn starts 65535,65300,65065
        b += struct.pack("<H", min(65535, round(65536 * 2 ** (-r / 192))))
    return bytes(b)


def _webfoot_rom() -> bytes:
    """Assemble a tiny ROM carrying the three table signatures and one BGM
    entry with a shared instrument table + flag LUT."""
    rom = bytearray(0x40000)
    rom[0xB2] = 0x96  # GBA header fixed byte

    luts = _luts()
    note_off = 0x1000
    rom[note_off : note_off + len(luts)] = luts  # note@+0, tup@+480, tdn@+992

    # one 8-byte PCM sample
    sample_off = 0x8000
    rom[sample_off : sample_off + 8] = bytes([10, 20, 30, 40, 50, 40, 30, 20])

    # instrument table (2 records) then flag LUT immediately after
    inst_off = 0x9000
    struct.pack_into("<IBBHHH", rom, inst_off, 0x08000000 + sample_off, 0x01, 0, 0, 8, 16000)
    struct.pack_into("<IBBHHH", rom, inst_off + 12, 0x08000000 + sample_off, 0x00, 0, 0, 8, 16000)
    flag_off = inst_off + 24
    rom[flag_off : flag_off + 14] = bytes(range(14))

    # pattern: 1 row, ch0 literal-flag (byte 0x01 = sel0|ch+1) note+ins+vol,
    # terminator
    pat_off = 0xA000
    rom[pat_off] = 1
    rom[pat_off + 1 : pat_off + 1 + 6] = bytes([0x01, 0x07, 60, 0, 48, 0x00])

    pat_tbl_off = 0xB000
    struct.pack_into("<I", rom, pat_tbl_off, 0x08000000 + pat_off)
    order_off = 0xB100
    rom[order_off : order_off + 2] = bytes([0, 0xFF])

    # BGM table: 8 identical entries (min run the detector accepts)
    bgm_off = 0xC000
    for i in range(8):
        struct.pack_into(
            "<IIIIBBH",
            rom,
            bgm_off + i * 20,
            0x08000000 + pat_tbl_off,
            0x08000000 + order_off,
            0x08000000 + inst_off,
            0x08000000 + flag_off,
            120,
            6,
            0,
        )
    return bytes(rom)


def test_detect_locates_tables():
    rom = _webfoot_rom()
    w = detect_webfoot(rom)
    assert w is not None
    # the consolidated detect exposes counts, not internal ROM offsets
    assert w.n_songs == 8
    assert w.n_insts == 2


def test_detect_rejects_non_webfoot():
    plain = bytearray(0x20000)
    plain[0xB2] = 0x96
    assert detect_webfoot(bytes(plain)) is None


def test_per_song_metadata():
    w = detect_webfoot(_webfoot_rom())
    assert w is not None
    assert len(w.songs) == w.n_songs == 8
    s = w.songs[0]
    assert s.index == 0
    # 1 row at tempo 120 / speed 6: 6 * (40000/120) / (2**24/1050) s
    assert abs(s.duration_sec - 6 * (40000 / 120) / (2**24 / 1050)) < 1e-3
    assert s.channels == 1  # one channel plays a note
    assert s.loops is False  # order list ends, no backward jump


def test_build_wbf_roundtrip(tmp_path):
    rom = _webfoot_rom()
    out = tmp_path / "game.wbf"
    stats = build_wbf(rom, str(out))
    assert stats["songs"] == 8

    data = out.read_bytes()
    assert data[:4] == b"WBF1"
    n_songs, n_insts = data[4], data[5]
    assert (n_songs, n_insts) == (8, 2)

    off_luts, off_flag, off_songs, off_insts = struct.unpack_from("<IIII", data, 8)
    file_size = struct.unpack_from("<I", data, 0x18)[0]
    assert file_size == len(data)

    # note LUT copied verbatim (note 60 == 0x4000)
    assert struct.unpack_from("<I", data, off_luts + 60 * 4)[0] == 0x4000
    # flag LUT copied
    assert data[off_flag : off_flag + 14] == bytes(range(14))

    # instrument record points inside the file and carries the loop end
    soff, flags, _pad, ls, le, bf = struct.unpack_from("<IBBHHH", data, off_insts)
    assert 0 < soff < file_size and le == 8 and bf == 16000
    assert data[soff : soff + 8] == bytes([10, 20, 30, 40, 50, 40, 30, 20])

    # song record: order + pattern-table offsets resolve in-file
    off_orders, off_pat_tbl, n_pats, tempo, speed, _ = struct.unpack_from(
        "<IIHBBI", data, off_songs
    )
    assert (n_pats, tempo, speed) == (1, 120, 6)
    assert data[off_orders] == 0 and data[off_orders + 1] == 0xFF
    pat_ptr = struct.unpack_from("<I", data, off_pat_tbl)[0]
    assert data[pat_ptr] == 1  # one row
