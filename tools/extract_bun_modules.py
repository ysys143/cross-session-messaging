#!/usr/bin/env python3
"""Extract the embedded JS modules from a Bun standalone Claude Code binary.

Bun stores a module table in the executable. Each 52-byte record starts with
two StringPointers, (name offset, name len) and (contents offset, contents
len), both relative to one base address. Module names are stored back to back
as NUL-terminated "/$bunfs/root/..." strings.

This script:
  1. finds the run of NUL-separated module names,
  2. finds the table record that points at the first of those names, which
     yields the base address (name_abs - name_rel),
  3. walks the table and writes every module under its real name.

Usage:
    extract_bun_modules.py BINARY OUT_DIR

Writes OUT_DIR/<name> for every module and OUT_DIR/module_table.json with
{name, abs, len} per record, where abs is the absolute byte offset in BINARY.
"""
import json
import os
import re
import struct
import sys

RECORD_STRIDE = 52
NAME_PREFIX = b"/$bunfs/root/"
NAME_RUN = re.compile(rb"(?:/\$bunfs/root/[\x21-\x7e]{1,200}\x00){50,}")
MIN_CONSECUTIVE = 8


def is_record(data, base, rec, strict=False):
    if rec < 0 or rec + 16 > len(data):
        return False
    name_rel, name_len, body_rel, body_len = struct.unpack_from("<4I", data, rec)
    start = base + name_rel
    if not 0 < name_len < 300 or start < 0 or start + name_len > len(data):
        return False
    name = data[start:start + name_len]
    if not name.startswith(NAME_PREFIX):
        return False
    body = base + body_rel
    if body < 0 or body + body_len > len(data):
        return False
    # Used only when locating the table: bundled chunks carry a "// @bun"
    # banner, while a stray name list does not point at real module bodies.
    return not strict or not name.endswith(b".js") or data[body:body + 7] == b"// @bun"


def find_table(data):
    """Return (base, first_record_offset)."""
    for run in NAME_RUN.finditer(data):
        name_abs = run.start()
        name_len = data.index(b"\x00", name_abs) - name_abs
        pattern = re.compile(rb"(?s)....(?=" + re.escape(struct.pack("<I", name_len)) + rb")")
        for hit in pattern.finditer(data):
            rec = hit.start()
            base = name_abs - struct.unpack_from("<I", data, rec)[0]
            if all(is_record(data, base, rec + k * RECORD_STRIDE, strict=True)
                   for k in range(MIN_CONSECUTIVE)):
                while is_record(data, base, rec - RECORD_STRIDE):
                    rec -= RECORD_STRIDE
                return base, rec
    raise SystemExit("module table not found")


def main():
    binary, out_dir = sys.argv[1], sys.argv[2]
    data = open(binary, "rb").read()
    base, rec = find_table(data)
    os.makedirs(out_dir, exist_ok=True)
    table = []
    while is_record(data, base, rec):
        name_rel, name_len, body_rel, body_len = struct.unpack_from("<4I", data, rec)
        name = data[base + name_rel:base + name_rel + name_len].decode()
        body_abs = base + body_rel
        table.append({"name": name, "abs": body_abs, "len": body_len})
        with open(os.path.join(out_dir, os.path.basename(name)), "wb") as f:
            f.write(data[body_abs:body_abs + body_len])
        rec += RECORD_STRIDE
    with open(os.path.join(out_dir, "module_table.json"), "w") as f:
        json.dump(table, f, indent=1)
    print(f"base={base} records={len(table)} -> {out_dir}")


if __name__ == "__main__":
    main()
