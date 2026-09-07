

from __future__ import annotations

from pathlib import Path
import argparse
import re
import struct
import wave
import subprocess
import tempfile
import shutil
import collections

FSB4 = b"FSB4"
FSB_HEADER_SIZE = 0x30
FSB_ENTRY_SIZE = 0x50


def u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def p16(value):
    return struct.pack("<H", value)


def p32(value):
    return struct.pack("<I", value)


def find_fsb4(data):
    hits = [m.start() for m in re.finditer(re.escape(FSB4), data)]
    if not hits:
        raise ValueError("FSB4 signature not found")
    if len(hits) != 1:
        raise ValueError(f"Expected one FSB4 signature, found {len(hits)}")
    return hits[0]


def parse_fsb4(data):
    if len(data) < FSB_HEADER_SIZE or data[:4] != FSB4:
        raise ValueError("Invalid FSB4")

    count = u32(data, 4)
    directory_size = u32(data, 8)
    data_size = u32(data, 12)
    flags = u32(data, 0x14)

    directory_start = FSB_HEADER_SIZE
    data_start = FSB_HEADER_SIZE + directory_size

    minimum_directory_size = count * FSB_ENTRY_SIZE
    if directory_size < minimum_directory_size:
        raise ValueError(
            f"Directory size 0x{directory_size:X} is smaller than "
            f"count*entry_size 0x{minimum_directory_size:X}"
        )

    # Some Reflex banks contain extra zero padding inside the directory
    # region. It belongs to the FSB4 container and must remain before the
    # logical audio payload.
    directory_padding = data[FSB_HEADER_SIZE + minimum_directory_size:data_start]

    # The decompressed .soundbnk allocation may be larger than the logical
    # FSB4 resource. Bytes after this point are not part of the audio bank
    # and are intentionally discarded on rebuilt output.
    expected_end = data_start + data_size

    if expected_end > len(data):
        raise ValueError(
            f"FSB data exceeds file: header ends at "
            f"0x{expected_end:X}, file is 0x{len(data):X}"
        )

    trailing_data = data[expected_end:]

    entries = []
    payload_offset = data_start

    for i in range(count):
        o = directory_start + i * FSB_ENTRY_SIZE
        raw = data[o:o + FSB_ENTRY_SIZE]

        name = raw[2:32].split(b"\0", 1)[0].decode(
            "ascii", "replace"
        )

        frames = u32(data, o + 32)
        size = u32(data, o + 36)
        loop_start = u32(data, o + 40)
        loop_end = u32(data, o + 44)
        mode = u32(data, o + 48)
        frequency = u32(data, o + 52)

        # Preserve the original entry exactly so unknown FSB flags/fields
        # are not silently lost during a rebuild.
        channels = u16(data, o + 62)

        pcm_size = frames * channels * 2
        is_mpeg = bool(mode & 0x0200)

        if payload_offset + size > expected_end:
            raise ValueError(f"Entry {i} payload exceeds logical FSB data")

        entries.append({
            "index": i,
            "name": name or f"sound_{i:03d}.wav",
            "frames": frames,
            "size": size,
            "loop_start": loop_start,
            "loop_end": loop_end,
            "mode": mode,
            "frequency": frequency,
            "channels": channels,
            "offset": payload_offset,
            "pcm_size": pcm_size,
            "is_mpeg": is_mpeg,
            "raw_entry": raw,
        })

        payload_offset += size

    if payload_offset > expected_end:
        raise ValueError("FSB payload exceeds logical data size")

    return {
        "count": count,
        "directory_size": directory_size,
        "flags": flags,
        "data_size": data_size,
        "directory_padding": directory_padding,
        "entries": entries,
        "trailing_data": trailing_data,
        "logical_size": expected_end,
        "allocated_size": len(data),
    }


def load_soundbnk(path):
    data = Path(path).read_bytes()
    offset = find_fsb4(data)
    fsb = data[offset:]
    info = parse_fsb4(fsb)
    info["soundbnk_prefix"] = data[:offset]
    info["cro_prefix"] = info["soundbnk_prefix"]  # v10 compatibility
    info["fsb_offset"] = offset
    return data, fsb, info


# Backward-compatible API name for code written against v10.
load_cro = load_soundbnk


def safe_name(name, fallback):
    name = Path(name).name
    cleaned = "".join(
        c if c.isalnum() or c in " ._()-"
        else "_"
        for c in name
    )
    return cleaned or fallback


def require_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "MPEG audio requires ffmpeg.exe in PATH. "
            "Install FFmpeg or place ffmpeg.exe next to soundbnk_tool.py."
        )
    return ffmpeg


def strip_id3v2(data):
    """Remove an optional ID3v2 tag before parsing raw MPEG frames."""
    if len(data) < 10 or data[:3] != b"ID3":
        return data

    # Synchsafe 28-bit size.
    size = (
        ((data[6] & 0x7F) << 21)
        | ((data[7] & 0x7F) << 14)
        | ((data[8] & 0x7F) << 7)
        | (data[9] & 0x7F)
    )

    end = 10 + size

    # ID3 footer, when present.
    if data[5] & 0x10:
        end += 10

    return data[end:]



def _mpeg_header_info(data, p):
    """Return (frame_length, padding) for a valid MPEG-1 Layer III header."""
    if p + 4 > len(data):
        return None

    b0, b1, b2, _ = data[p:p + 4]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None

    version_id = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    bitrate_index = (b2 >> 4) & 0x0F
    sample_index = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01

    if version_id != 0b11 or layer != 0b01:
        return None

    bitrates = [
        None, 32, 40, 48, 56, 64, 80, 96,
        112, 128, 160, 192, 224, 256, 320, None,
    ]
    sample_rates = [44100, 48000, 32000, None]

    bitrate = bitrates[bitrate_index]
    sample_rate = sample_rates[sample_index]
    if bitrate is None or sample_rate is None:
        return None

    frame_len = (144 * bitrate * 1000) // sample_rate + padding
    if p + frame_len > len(data):
        return None

    return frame_len, padding


def infer_mpeg_bitrate_kbps(raw_mpeg):
    """Read the bitrate from the first valid MPEG-1 Layer III frame."""
    data = strip_id3v2(raw_mpeg)
    parsed = _mpeg_header_info(data, 0)
    if parsed is None:
        raise ValueError("Unable to determine MPEG bitrate from reference sample")

    _, _padding = parsed
    bitrate_index = (data[2] >> 4) & 0x0F
    bitrates = [
        None, 32, 40, 48, 56, 64, 80, 96,
        112, 128, 160, 192, 224, 256, 320, None,
    ]
    bitrate = bitrates[bitrate_index]
    if bitrate is None:
        raise ValueError("Unsupported MPEG bitrate in reference sample")
    return bitrate


def infer_reflex_mpeg_separator(reference_mpeg, expected_frames):
    """
    Infer the one-byte inter-frame convention used by the original sample.

    Reflex soundbanks seen here use either:
      - no inserted byte, or
      - one 0x00/other byte after frames with a specific padding-bit value.

    The decision is made from the actual source bank rather than hard-coding
    one bitrate's behavior.
    """
    data = strip_id3v2(reference_mpeg)
    pos = 0
    observations = []

    for _ in range(expected_frames):
        parsed = _mpeg_header_info(data, pos)
        if parsed is None:
            break

        frame_len, padding = parsed
        next_pos = pos + frame_len
        if next_pos >= len(data):
            break

        if _mpeg_header_info(data, next_pos) is not None:
            pos = next_pos
            continue

        if next_pos + 1 < len(data) and _mpeg_header_info(data, next_pos + 1) is not None:
            observations.append((padding, data[next_pos]))
            pos = next_pos + 1
            continue

        break

    if not observations:
        return None

    by_padding = {}
    for padding, value in observations:
        by_padding.setdefault(padding, []).append(value)

    # Prefer a consistent rule; otherwise use the most frequent padding class.
    padding_value = max(by_padding, key=lambda k: len(by_padding[k]))
    values = by_padding[padding_value]
    byte_value = collections.Counter(values).most_common(1)[0][0]

    return {
        "after_padding": padding_value,
        "byte": byte_value,
        "count": len(observations),
    }


def build_reflex_mpeg(raw_mpeg, reference_mpeg=None, expected_reference_frames=None,
                       fsb_padding=0):
    """
    Convert a standard MPEG stream into Reflex FSB MPEG layout.

    FSB MPEG padding is independent of the MPEG frame header padding bit.
    For stereo banks with MPEG_PADDED (0x20), frames are padded to 2-byte
    boundaries. For MPEG_PADDED4 (0x40), they are padded to 4-byte boundaries.
    """
    data = strip_id3v2(raw_mpeg)

    if fsb_padding not in (0, 2, 4, 16):
        fsb_padding = 0

    out = bytearray()
    pos = 0
    frame_count = 0

    while pos < len(data):
        parsed = _mpeg_header_info(data, pos)
        if parsed is None:
            break

        frame_len, _mpeg_padding_bit = parsed
        out.extend(data[pos:pos + frame_len])
        pos += frame_len
        frame_count += 1

        if pos >= len(data):
            break

        if fsb_padding:
            remainder = frame_len % fsb_padding
            if remainder:
                out.extend(b"\x00" * (fsb_padding - remainder))

    if frame_count == 0:
        raise ValueError("No valid MPEG frames found in encoded stream")

    return bytes(out), frame_count


def _fsb_mpeg_padding(flags, channels, frame_len):
    """
    Return the number of FMOD/FSB bytes inserted after one MPEG frame.

    This is container-level padding and is completely independent of the
    MPEG header's own padding bit.

    FSB_SOURCE_MPEG_PADDED  (0x20):
        2-byte alignment for normal/stereo samples, 16-byte alignment for
        multichannel samples.

    FSB_SOURCE_MPEG_PADDED4 (0x40):
        4-byte alignment for normal samples, 16-byte alignment for
        multichannel samples.
    """
    if channels > 2:
        alignment = 16
    elif flags & 0x40:
        alignment = 4
    elif flags & 0x20:
        alignment = 2
    else:
        return 0

    remainder = frame_len % alignment
    return (alignment - remainder) if remainder else 0


def normalize_reflex_mpeg(raw_mpeg, fsb_flags=0, channels=2, expected_frames=None):
    """
    Remove FMOD FSB MPEG frame padding without guessing from the byte values.

    The previous implementation tried to detect a one-byte separator by
    looking for the next MPEG header. That is unsafe for FSBs with
    MPEG_PADDED: the padding byte can itself contain 0xFF or other data that
    looks like an MPEG sync/header.

    When FSB padding is enabled, the exact padding length is derived from the
    MPEG frame length, FSB flags, and channel count. This makes the parser
    advance to the real next frame even when padding contains garbage or a
    byte sequence resembling an MPEG header.

    If no FSB padding flag is supplied, the legacy one-byte separator
    detection is retained for the older Reflex banks already supported.
    """
    data = strip_id3v2(raw_mpeg)
    out = bytearray()
    pos = 0
    frame_count = 0

    while pos < len(data):
        parsed = _mpeg_header_info(data, pos)
        if parsed is None:
            break

        frame_len, _mpeg_padding_bit = parsed
        out.extend(data[pos:pos + frame_len])
        pos += frame_len
        frame_count += 1

        if expected_frames is not None and frame_count >= expected_frames:
            break

        if pos >= len(data):
            break

        fsb_pad = _fsb_mpeg_padding(fsb_flags, channels, frame_len)
        if fsb_pad:
            if pos + fsb_pad > len(data):
                raise ValueError(
                    "MPEG FSB padding extends beyond the sample payload"
                )
            pos += fsb_pad
            continue

        # Legacy Reflex variant: one byte between MPEG frames.
        if _mpeg_header_info(data, pos) is not None:
            continue

        if _mpeg_header_info(data, pos + 1) is not None:
            pos += 1
            continue

        break

    if not out:
        raise ValueError("No valid MPEG frames found in FSB payload")

    if expected_frames is not None and frame_count != expected_frames:
        raise ValueError(
            f"MPEG frame count mismatch: expected {expected_frames}, "
            f"found {frame_count}"
        )

    return bytes(out)



def decode_mpeg_to_wav(raw_mpeg, output_path, frequency, channels, fsb_flags=0, expected_frames=None):
    """
    Decode a raw MPEG Layer III payload to PCM WAV.

    The FSB entry name remains the output WAV name. FFMPEG is only used
    as the codec decoder; the FSB container is handled by this script.
    """
    ffmpeg = require_ffmpeg()

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "input.mpg"
        src.write_bytes(normalize_reflex_mpeg(raw_mpeg, fsb_flags=fsb_flags, channels=channels, expected_frames=expected_frames))

        cmd = [
            ffmpeg, "-y",
            "-f", "mp3",
            "-i", str(src),
            "-ac", str(channels),
            "-ar", str(frequency),
            "-c:a", "pcm_s16le",
            str(output_path),
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        if result.returncode != 0:
            raise RuntimeError(
                "FFmpeg failed to decode MPEG audio:\n"
                + result.stderr[-2000:]
            )


def encode_wav_to_mpeg(wav_path, output_path, frequency, channels, bitrate_kbps):
    """
    Encode replacement WAV to MPEG Layer III using the original sample's
    MPEG bitrate. Reflex UI banks are sensitive to the original bitrate.
    """
    ffmpeg = require_ffmpeg()

    cmd = [
        ffmpeg, "-y",
        "-i", str(wav_path),
        "-ac", str(channels),
        "-ar", str(frequency),
        "-c:a", "libmp3lame",
        "-b:a", f"{bitrate_kbps}k",
        "-write_id3v1", "0",
        "-f", "mp3",
        str(output_path),
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg failed to encode MPEG audio:\n"
            + result.stderr[-2000:]
        )



def extract_wav(fsb, info, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)

    for e in info["entries"]:
        raw = fsb[e["offset"]:e["offset"] + e["size"]]

        filename = safe_name(
            e["name"],
            f"sound_{e['index']:03d}.wav"
        )
        if not filename.lower().endswith(".wav"):
            filename += ".wav"

        output_path = output / filename

        if e["is_mpeg"]:
            # The FSB entry can be named .wav even though its payload is
            # MPEG Layer III. Decode that payload to the requested WAV.
            decode_mpeg_to_wav(
                raw,
                output_path,
                e["frequency"],
                e["channels"],
                fsb_flags=info.get("flags", 0),
                expected_frames=max(1, e["frames"] // 1152),
            )
            continue

        pcm_size = e["pcm_size"]

        if e["size"] < pcm_size:
            raise ValueError(
                f"{e['name']}: data smaller than PCM size"
            )

        pcm = raw[:pcm_size]

        with wave.open(str(output_path), "wb") as w:
            w.setnchannels(e["channels"])
            w.setsampwidth(2)
            w.setframerate(e["frequency"])
            w.writeframes(pcm)


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        frequency = w.getframerate()
        frames = w.getnframes()
        pcm = w.readframes(frames)

    if width != 2:
        raise ValueError(
            f"{path}: only 16-bit WAV is supported"
        )

    expected = frames * channels * 2
    if len(pcm) != expected:
        raise ValueError(f"{path}: unexpected PCM length")

    return {
        "channels": channels,
        "frequency": frequency,
        "frames": frames,
        "pcm": pcm,
    }


def _wav_file_bytes(path):
    """Return the complete WAV file bytes for exact change detection."""
    return Path(path).read_bytes()


def build_fsb(original_fsb, wav_dir):
    """
    Rebuild the FSB while changing only samples whose working WAV differs
    from the WAV extracted from the original FSB.

    Unchanged samples are copied byte-for-byte from the original FSB,
    including their original entry metadata. This is important for Reflex:
    rebuilding an untouched MPEG sample can produce a different MPEG stream
    and may break other sounds in the bank.
    """
    old = parse_fsb4(original_fsb)
    wav_dir = Path(wav_dir)
    directory_padding = old["directory_padding"]

    # Extract the original samples to a temporary directory once. The WAV
    # representation is exactly the same representation used by the editor's
    # working directory, so byte comparison reliably detects user changes.
    with tempfile.TemporaryDirectory() as td:
        original_wav_dir = Path(td) / "original"
        extract_wav(original_fsb, old, original_wav_dir)

        entries = []
        payloads = []

        for e in old["entries"]:
            wav_name = safe_name(
                e["name"],
                f"sound_{e['index']:03d}.wav"
            )
            if not wav_name.lower().endswith(".wav"):
                wav_name += ".wav"

            wav_path = wav_dir / wav_name
            original_wav_path = original_wav_dir / wav_name

            if not wav_path.exists():
                raise FileNotFoundError(
                    f"Missing replacement WAV: {wav_path}"
                )

            if not original_wav_path.exists():
                raise FileNotFoundError(
                    f"Original extracted WAV is missing: {original_wav_path}"
                )

            old_raw = original_fsb[
                e["offset"]:e["offset"] + e["size"]
            ]

            # IMPORTANT: unchanged samples are copied verbatim.
            if _wav_file_bytes(wav_path) == _wav_file_bytes(original_wav_path):
                entries.append(e["raw_entry"])
                payloads.append(old_raw)
                continue

            wav = read_wav(wav_path)

            if e["is_mpeg"]:
                # Only a genuinely modified MPEG sample is re-encoded.
                with tempfile.TemporaryDirectory() as encode_td:
                    encoded = Path(encode_td) / "replacement.mp3"
                    bitrate_kbps = infer_mpeg_bitrate_kbps(old_raw)
                    encode_wav_to_mpeg(
                        wav_path,
                        encoded,
                        e["frequency"],
                        e["channels"],
                        bitrate_kbps,
                    )
                    encoded_mpeg = encoded.read_bytes()

                fsb_padding = 0
                fsb_flags = u32(original_fsb, 0x14)
                if e["channels"] > 2:
                    if fsb_flags & (0x20 | 0x40):
                        fsb_padding = 16
                elif fsb_flags & 0x40:
                    fsb_padding = 4
                elif fsb_flags & 0x20:
                    fsb_padding = 2

                new_raw, mpeg_frames = build_reflex_mpeg(
                    encoded_mpeg,
                    reference_mpeg=old_raw,
                    expected_reference_frames=max(1, e["frames"] // 1152),
                    fsb_padding=fsb_padding,
                )

                raw_entry = bytearray(e["raw_entry"])

                # MPEG-1 Layer III has 1152 decoded PCM samples per frame.
                new_frames = mpeg_frames * 1152
                struct.pack_into("<I", raw_entry, 32, new_frames)
                struct.pack_into("<I", raw_entry, 36, len(new_raw))

                entries.append(bytes(raw_entry))
                payloads.append(new_raw)
                continue

            if wav["channels"] != e["channels"]:
                raise ValueError(
                    f"{wav_path.name}: channels changed "
                    f"({wav['channels']} != {e['channels']})"
                )

            if wav["frequency"] != e["frequency"]:
                raise ValueError(
                    f"{wav_path.name}: frequency changed "
                    f"({wav['frequency']} != {e['frequency']})"
                )

            pcm = wav["pcm"]
            if wav["frames"] != len(pcm) // (wav["channels"] * 2):
                raise ValueError(f"{wav_path}: invalid frame count")

            # Preserve the original entry's unknown trailing data.
            old_pcm_size = e["pcm_size"]
            old_extra = old_raw[old_pcm_size:]
            new_raw = pcm + old_extra

            raw_entry = bytearray(e["raw_entry"])

            # Frames and data size change with the replacement audio.
            struct.pack_into("<I", raw_entry, 32, wav["frames"])
            struct.pack_into("<I", raw_entry, 36, len(new_raw))

            entries.append(bytes(raw_entry))
            payloads.append(new_raw)

    count = len(entries)
    # Keep the original directory allocation, including any zero padding.
    directory_size = old["directory_size"]
    minimum_directory_size = count * FSB_ENTRY_SIZE
    if directory_size < minimum_directory_size:
        raise ValueError(
            f"Original directory size 0x{directory_size:X} is too small "
            f"for {count} entries"
        )

    data_size = sum(len(x) for x in payloads)

    header = bytearray(FSB_HEADER_SIZE)
    header[:] = original_fsb[:FSB_HEADER_SIZE]

    header[0:4] = FSB4
    struct.pack_into("<I", header, 4, count)
    struct.pack_into("<I", header, 8, directory_size)
    struct.pack_into("<I", header, 12, data_size)

    return (
        bytes(header)
        + b"".join(entries)
        + directory_padding
        + b"".join(payloads)
    )


def command_info(path):
    data, fsb, info = load_soundbnk(path)

    print(f"SOUNDBNK size:   {len(data):,}")
    print(f"FSB4 offset:    0x{info['fsb_offset']:X}")
    print(f"FSB4 size:      {len(fsb):,}")
    print(f"Sounds:         {info['count']}")
    print(f"Directory:      0x{info['directory_size']:X}")
    print(f"Audio data:     0x{info['data_size']:X}")
    print(f"Directory pad:  0x{len(info['directory_padding']):X}")
    print(f"Logical end:    0x{info['logical_size']:X}")
    print(f"Allocated size: 0x{info['allocated_size']:X}")
    print(f"Trimmed bytes:  {len(info['trailing_data']):,}")


def command_list(path):
    _, _, info = load_soundbnk(path)

    for e in info["entries"]:
        print(
            f"{e['index']:02d} "
            f"{e['name']} | "
            f"{e['frequency']} Hz | "
            f"{e['channels']} ch | "
            f"{e['frames']} frames | "
            f"{e['size']} bytes"
        )


def command_analyze(path):
    data, fsb, info = load_soundbnk(path)

    print(f"File:            {Path(path).name}")
    print(f"Container size:  {len(data):,}")
    print(f"FSB4 offset:     0x{info['fsb_offset']:X}")
    print(f"FSB4 logical end:0x{info['fsb_offset'] + info['logical_size']:X}")
    print(f"FSB4 logical size:{info['logical_size']:,}")
    print(f"Physical FSB4:   {len(fsb):,}")
    print(f"Trailing padding:{len(info['trailing_data']):,}")
    print(f"Samples:         {info['count']}")
    print(f"Directory size:  0x{info['directory_size']:X}")
    print(f"Directory pad:   0x{len(info['directory_padding']):X}")

    for e in info["entries"]:
        codec = "MPEG Layer III" if e["is_mpeg"] else "PCM"
        print(
            f"[{e['index']:02d}] {e['name']} | {codec} | "
            f"{e['frequency']} Hz | {e['channels']} ch | "
            f"frames={e['frames']:,} | bytes={e['size']:,}"
        )


def command_extract(path, output):
    _, fsb, info = load_soundbnk(path)
    extract_wav(fsb, info, output)
    print(f"Extracted {info['count']} WAV files to: {output}")


def command_extract_fsb(path, output):
    _, fsb, info = load_soundbnk(path)
    # Export only the logical FSB4; never export the decompressed allocation tail.
    Path(output).write_bytes(fsb[:info["logical_size"]])
    print(f"Extracted logical FSB4: {output}")
    print(f"Size: {info['logical_size']:,} bytes")
    if info["trailing_data"]:
        print(f"Skipped trailing padding: {len(info['trailing_data']):,} bytes")


def command_trim(path, output):
    original = Path(path).read_bytes()
    offset = find_fsb4(original)
    fsb = original[offset:]
    info = parse_fsb4(fsb)

    # Keep the SOUNDBNK prefix and only the logical FSB4 bytes.
    trimmed_fsb = fsb[:info["logical_size"]]
    trimmed = original[:offset] + trimmed_fsb
    Path(output).write_bytes(trimmed)

    print(f"Trimmed SOUNDBNK: {output}")
    print(f"Original size:    {len(original):,}")
    print(f"New size:         {len(trimmed):,}")
    print(f"Removed padding:  {len(original) - len(trimmed):,} bytes")


def command_build_fsb(cro, wav_dir, output):
    _, original_fsb, _ = load_soundbnk(cro)
    new_fsb = build_fsb(original_fsb, wav_dir)
    Path(output).write_bytes(new_fsb)
    print(f"Built FSB4: {output}")
    print(f"Size: {len(new_fsb):,} bytes")


def command_rebuild(cro, wav_dir, output):
    original = Path(cro).read_bytes()
    fsb_offset = find_fsb4(original)
    original_fsb = original[fsb_offset:]

    new_fsb = build_fsb(original_fsb, wav_dir)

    # Preserve the entire SOUNDBNK prefix exactly.
    rebuilt = original[:fsb_offset] + new_fsb
    Path(output).write_bytes(rebuilt)

    print(f"Rebuilt SOUNDBNK: {output}")
    print(f"Original size: {len(original):,}")
    print(f"New size:      {len(rebuilt):,}")
    print(f"FSB4 offset:   0x{fsb_offset:X}")


def main():
    parser = argparse.ArgumentParser(
        description="MX vs ATV Reflex SOUNDBNK/FSB4 audio tool"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info")
    p.add_argument("file")

    p = sub.add_parser("analyze")
    p.add_argument("file")

    p = sub.add_parser("list")
    p.add_argument("file")

    p = sub.add_parser("extract")
    p.add_argument("file")
    p.add_argument("-o", "--output", required=True)

    p = sub.add_parser("extract-fsb")
    p.add_argument("file")
    p.add_argument("-o", "--output", required=True)

    p = sub.add_parser("trim")
    p.add_argument("file")
    p.add_argument("-o", "--output", required=True)

    p = sub.add_parser("build-fsb")
    p.add_argument("cro")
    p.add_argument("wav_dir")
    p.add_argument("-o", "--output", required=True)

    p = sub.add_parser("rebuild")
    p.add_argument("cro")
    p.add_argument("wav_dir")
    p.add_argument("-o", "--output", required=True)

    args = parser.parse_args()

    if args.command == "info":
        command_info(Path(args.file))
    elif args.command == "analyze":
        command_analyze(Path(args.file))
    elif args.command == "list":
        command_list(Path(args.file))
    elif args.command == "extract":
        command_extract(Path(args.file), Path(args.output))
    elif args.command == "extract-fsb":
        command_extract_fsb(Path(args.file), Path(args.output))
    elif args.command == "trim":
        command_trim(Path(args.file), Path(args.output))
    elif args.command == "build-fsb":
        command_build_fsb(
            Path(args.cro),
            Path(args.wav_dir),
            Path(args.output)
        )
    elif args.command == "rebuild":
        command_rebuild(
            Path(args.cro),
            Path(args.wav_dir),
            Path(args.output)
        )


if __name__ == "__main__":
    main()
