"""no_songtable_message: the converter should say *what* the file is when
the MP2K scan fails, not just shrug."""

from gba_audio.cli import no_songtable_message


def gba_rom(extra: bytes = b"") -> bytearray:
    data = bytearray(0x200)
    data[0xB2] = 0x96  # GBA header fixed byte
    return data + extra


def snes_rom(header_at: int) -> bytearray:
    data = bytearray(max(0x10000, header_at + 4))
    # checksum-complement pair: complement ^ checksum == 0xFFFF
    data[header_at : header_at + 2] = (0x5AA5).to_bytes(2, "little")
    data[header_at + 2 : header_at + 4] = (0xA55A).to_bytes(2, "little")
    return data


def test_spc_file_recognized():
    data = b"SNES-SPC700 Sound File Data v0.30" + bytes(0x100)
    assert ".spc" in no_songtable_message("song.bin", data)


def test_snes_rom_by_checksum_lorom():
    msg = no_songtable_message("game.bin", bytes(snes_rom(0x7FDC)))
    assert "SNES ROM" in msg and ".spc" in msg


def test_snes_rom_by_checksum_hirom_with_copier_header():
    assert "SNES ROM" in no_songtable_message("game.bin", bytes(snes_rom(0xFFDC + 512)))


def test_snes_rom_by_extension():
    assert "SNES ROM" in no_songtable_message("game.sfc", bytes(0x1000))


def test_not_a_gba_rom():
    assert "doesn't look like a GBA ROM" in no_songtable_message("x.bin", bytes(0x1000))


def test_gba_header_beats_snes_checksum_coincidence():
    # a valid GBA ROM must never be reported as SNES, even if 4 bytes at a
    # SNES header offset happen to xor to 0xFFFF
    data = gba_rom(bytes(0x8000))
    data[0x7FDC:0x7FE0] = bytes((0xA5, 0x5A, 0x5A, 0xA5))
    assert "SNES" not in no_songtable_message("game.gba", bytes(data))


def test_gba_rom_with_gax_signature():
    msg = no_songtable_message("game.gba", bytes(gba_rom(b"GAX Sound Engine v3.05")))
    assert "GAX" in msg


def test_gba_rom_custom_driver_fallback():
    msg = no_songtable_message("game.gba", bytes(gba_rom()))
    assert "custom sound driver" in msg
