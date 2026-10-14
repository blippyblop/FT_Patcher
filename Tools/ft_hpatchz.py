#!/usr/bin/env python3
"""ft_hpatchz.py — pure-Python HDiffPatch patch applier (drop-in replacement
for the HDiffPatch ``hpatchz`` binary, v4.5.2).

Applies an ``.hdiff`` binary diff (the format written by ``hdiffz``) to an
original file to produce the patched file, in pure Python (standard library
only, no external dependencies). It is byte-for-byte equivalent to running:

    hpatchz  oldPath  diffFile  outNewPath

The GUI / ``ft_transplant.py`` / ``ft_patch.py`` import this module and call
:func:`apply_patch_file` instead of spawning the ``hpatchz`` executable, so
the packaged tool no longer ships a platform ``hpatchz`` binary — only Blender
itself is still required at run time.

Patch format (``hdiffz`` "diffz" files, magic ``HDIFF13``):

    "HDIFF13" &            version tag, terminated by '&'
    <compressType> \\0      empty for uncompressed diffs
    newDataSize           packed uint
    oldDataSize           packed uint
    coverCount            packed uint
    cover_buf_size        packed uint   (followed by its compress_*_size)
    rle_ctrlBuf_size      packed uint   (the RLE ctrl stream, no leading varint)
    rle_codeBuf_size      packed uint
    newDataDiff_size      packed uint
    ----------------  data sections (all raw for uncompressed)  ----------------
    cover buffer          coverCount entries, each:
                            inc_oldPos   packed uint, 1 sign bit
                            inc_newPos   packed uint   (gap = verbatim copy len)
                            length       packed uint   (region added from old)
    rle_ctrl stream       packed run-control entries (high 2 bits = run type,
                            tag-2 packed uint = run length); no leading size
    rle_code stream       literal bytes pulled for rle/unrle runs
    newDataDiff           raw verbatim bytes

Application: the output starts as the RLE-decoded base; for every cover the
gap region is overwritten with verbatim ``newDataDiff`` bytes and the covered
region is byte-wise ``old[oldPos : +length]`` added (mod 256) on top of the
base. Any remaining tail is copied from ``newDataDiff``.

Compressed diffs (a non-empty ``compressType`` such as LZMA/ZLIB) are not
supported here — the Hash Edits package ships uncompressed diffs; a clear
error is raised if one is encountered.
"""

import os
import sys

__all__ = [
    "apply_patch_bytes",
    "apply_patch_file",
    "HdiffPatchError",
    "main",
]


class HdiffPatchError(Exception):
    """Raised when a patch cannot be applied (bad header, bad base, ...)."""


# ---------------------------------------------------------------------------
# packed-uint (varint) codec  — mirrors hpatch_packUIntWithTag/unpackUIntWithTag
# ---------------------------------------------------------------------------

def _unpack_uint(data, pos, tagbit=0):
    """Read one HDiffPatch packed uint starting at ``pos``.

    ``tagbit`` high bits of the first byte are a reserved tag (sign bit for
    ``inc_oldPos``); the next bit is the "more bytes follow" bit. Continuation
    bytes carry 7 value bits plus a "more" bit. Returns ``(value, new_pos)``.
    """
    code = data[pos]
    pos += 1
    low = (1 << (7 - tagbit)) - 1
    more_bit = 1 << (7 - tagbit)
    value = code & low
    if (code & more_bit) != 0:
        while True:
            code = data[pos]
            pos += 1
            value = (value << 7) | (code & 0x7F)
            if (code & 0x80) == 0:
                return value, pos
    return value, pos


def _read_type_end(data, pos, endtag):
    start = pos
    while data[pos] != endtag:
        pos += 1
    return bytes(data[start:pos]), pos + 1


# ---------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------

class _Header(object):
    __slots__ = (
        "compress_type", "new_data_size", "old_data_size", "cover_count",
        "cover_buf_size", "rle_ctrl_buf_size", "rle_code_buf_size",
        "new_data_diff_size", "head_end",
    )


def _parse_header(diff):
    pos = 0
    version, pos = _read_type_end(diff, pos, 0x26)      # up to '&'
    if version != b"HDIFF13":
        raise HdiffPatchError(
            "not an HDiffPatch (hdiffz) patch file: bad version tag %r"
            % (bytes(version),))
    compress_type, pos = _read_type_end(diff, pos, 0x00)  # up to '\0'
    if compress_type:
        raise HdiffPatchError(
            "compressed diff (compressType %r) is not supported by the Python "
            "patcher; the Hash Edits package ships uncompressed diffs — use the "
            "original hpatchz binary for this file" % (bytes(compress_type),))

    h = _Header()
    h.new_data_size, pos = _unpack_uint(diff, pos)
    h.old_data_size, pos = _unpack_uint(diff, pos)
    h.cover_count,   pos = _unpack_uint(diff, pos)
    h.cover_buf_size, pos = _unpack_uint(diff, pos)
    compress_cover,   pos = _unpack_uint(diff, pos)
    h.rle_ctrl_buf_size, pos = _unpack_uint(diff, pos)
    compress_rle_ctrl,  pos = _unpack_uint(diff, pos)
    h.rle_code_buf_size, pos = _unpack_uint(diff, pos)
    compress_rle_code,  pos = _unpack_uint(diff, pos)
    h.new_data_diff_size, pos = _unpack_uint(diff, pos)
    compress_new_diff,  pos = _unpack_uint(diff, pos)
    h.head_end = pos

    if any(s != 0 for s in (compress_cover, compress_rle_ctrl,
                            compress_rle_code, compress_new_diff)):
        raise HdiffPatchError("diff contains compressed sections that the "
                              "Python patcher does not support")
    return h


# ---------------------------------------------------------------------------
# byte-RLE decoder  — mirrors _bytesRle_load
# ---------------------------------------------------------------------------

_RLE0, _RLE255, _RLE, _UNRLE = 0, 1, 2, 3


def _decode_rle(rle_ctrl, rle_code):
    """Decode the RLE section into the full base byte string
    (length == newDataSize).

    ``rle_ctrl`` is the run-control stream and ``rle_code`` the literal stream;
    they are two separate sections in an HDIFF13 file (sized by the header).
    Unlike the in-memory HDIFF1 layout there is NO leading ctrl-size varint.
    Each ctrl entry carries its run type in the high 2 bits and the run length
    (stored as ``len - 1``) as a tag-2 packed uint.
    """
    out = bytearray()
    cp = 0
    cc = 0
    code_end = len(rle_code)
    ctrl_end = len(rle_ctrl)
    while cp < ctrl_end:
        b = rle_ctrl[cp]
        rtype = b >> 6                       # high 2 bits
        length, cp = _unpack_uint(rle_ctrl, cp, tagbit=2)
        length += 1                          # stored as (len - 1)
        if rtype == _RLE0:
            out += b"\x00" * length
        elif rtype == _RLE255:
            out += b"\xff" * length
        elif rtype == _RLE:
            if cc >= code_end:
                raise HdiffPatchError("RLE data underrun (rle code)")
            v = rle_code[cc]
            cc += 1
            out += bytes((v,)) * length
        else:                                # _UNRLE
            if cc + length > code_end:
                raise HdiffPatchError("RLE data underrun (unrle)")
            out += rle_code[cc:cc + length]
            cc += length
    return bytes(out)


# ---------------------------------------------------------------------------
# cover decoding + patch application
# ---------------------------------------------------------------------------

def apply_patch_bytes(old, diff):
    """Apply ``diff`` (bytes) to ``old`` (bytes); return the new data (bytes)."""
    h = _parse_header(diff)

    if len(old) != h.old_data_size:
        raise HdiffPatchError(
            "base file size %d does not match what the diff was built against "
            "(expected %d) — point at the exact original file"
            % (len(old), h.old_data_size))

    pos = h.head_end
    cover = diff[pos:pos + h.cover_buf_size]
    pos += h.cover_buf_size
    rle_ctrl = diff[pos:pos + h.rle_ctrl_buf_size]
    pos += h.rle_ctrl_buf_size
    rle_code = diff[pos:pos + h.rle_code_buf_size]
    pos += h.rle_code_buf_size
    new_data_diff = diff[pos:pos + h.new_data_diff_size]

    base = _decode_rle(rle_ctrl, rle_code)
    if len(base) != h.new_data_size:
        raise HdiffPatchError(
            "RLE base length %d != newDataSize %d (corrupt diff?)"
            % (len(base), h.new_data_size))

    new_data = bytearray(base)
    old_len = len(old)

    cp = 0
    cover_end = len(cover)
    old_pos_back = 0
    new_pos_back = 0
    ndp = 0                                  # newDataDiff cursor
    nd_len = len(new_data_diff)

    for _i in range(h.cover_count):
        # inc_oldPos: 1 sign bit (high bit of the first byte)
        if cp >= cover_end:
            raise HdiffPatchError("cover buffer underrun (inc_oldPos)")
        sign = cover[cp] >> 7
        inc_old_pos, cp = _unpack_uint(cover, cp, tagbit=1)
        if sign == 0:
            old_pos = old_pos_back + inc_old_pos
        else:
            old_pos = old_pos_back - inc_old_pos
        copy_length, cp = _unpack_uint(cover, cp)      # inc_newPos (the gap)
        length, cp = _unpack_uint(cover, cp)           # region added from old
        new_pos = new_pos_back + copy_length

        if copy_length > 0:
            if ndp + copy_length > nd_len:
                raise HdiffPatchError("newDataDiff underrun (copy region)")
            new_data[new_pos_back:new_pos] = new_data_diff[ndp:ndp + copy_length]
            ndp += copy_length

        if length > 0:
            if old_pos > old_len or old_pos + length > old_len:
                raise HdiffPatchError(
                    "cover oldPos %d + length %d exceeds old data (%d)"
                    % (old_pos, length, old_len))
            if new_pos + length > h.new_data_size:
                raise HdiffPatchError(
                    "cover newPos %d + length %d exceeds newData (%d)"
                    % (new_pos, length, h.new_data_size))
            seg_old = old[old_pos:old_pos + length]
            base_seg = new_data[new_pos:new_pos + length]
            for j in range(length):
                base_seg[j] = (base_seg[j] + seg_old[j]) & 0xFF
            new_data[new_pos:new_pos + length] = base_seg

        new_pos_back = new_pos + length
        old_pos_back = old_pos + length

    if new_pos_back < h.new_data_size:
        tail = h.new_data_size - new_pos_back
        if ndp + tail > nd_len:
            raise HdiffPatchError("newDataDiff underrun (tail)")
        new_data[new_pos_back:h.new_data_size] = new_data_diff[ndp:ndp + tail]
        ndp += tail

    if ndp != nd_len:
        raise HdiffPatchError(
            "newDataDiff not fully consumed (%d of %d used)" % (ndp, nd_len))

    return bytes(new_data)


def apply_patch_file(old_path, diff_path, out_path):
    """Apply ``diff_path`` to ``old_path``, writing the result to ``out_path``.

    Returns the number of bytes written. Raises :class:`HdiffPatchError` on any
    failure (the output file is left untouched on error).
    """
    with open(old_path, "rb") as f:
        old = f.read()
    with open(diff_path, "rb") as f:
        diff = f.read()
    new = apply_patch_bytes(old, diff)
    with open(out_path, "wb") as f:
        f.write(new)
    return len(new)


# ---------------------------------------------------------------------------
# command line (mirrors: hpatchz [-f] oldPath diffFile outNewPath)
# ---------------------------------------------------------------------------

_USAGE = (
    "HDiffPatch::hpatchz (pure-Python patcher) v4.5.2\n"
    "\n"
    "patch usage: ft_hpatchz.py [options] oldPath diffFile outNewPath\n"
    "  -f  Force overwrite, ignore write path already exists;\n"
)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    force = False
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-f":
            force = True
        elif a.startswith("-"):
            sys.stderr.write("warning: ignoring unknown option %r\n" % a)
        else:
            args.append(a)
        i += 1

    if len(args) < 3:
        sys.stderr.write(_USAGE)
        return 0 if not args else 1

    old_path, diff_path, out_path = args[0], args[1], args[2]

    if not os.path.isfile(old_path):
        sys.stderr.write("error: old file not found: %s\n" % old_path)
        return 1
    if not os.path.isfile(diff_path):
        sys.stderr.write("error: diff file not found: %s\n" % diff_path)
        return 1
    if not force and out_path not in (old_path, diff_path) and os.path.exists(out_path):
        sys.stderr.write(
            "error: output already exists: %s (use -f to overwrite)\n" % out_path)
        return 1

    try:
        size = apply_patch_file(old_path, diff_path, out_path)
    except HdiffPatchError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1

    print("old : %s" % old_path)
    print("diff: %s" % diff_path)
    print("out : %s" % out_path)
    print("  patch ok!")
    return 0


if __name__ == "__main__":
    sys.exit(main())