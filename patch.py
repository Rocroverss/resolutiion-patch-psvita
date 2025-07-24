#!/usr/bin/env python3
"""
patch.py – Extract, merge, repack Godot 3 .pck archives and optionally build
a full VPK with 0777 permissions on all entries.
"""

import os
import sys
import argparse
import struct
import shutil
import hashlib
import zipfile

# -------------------------------------------------------------------
# Reading the header-based index of a Godot 3 .pck
# -------------------------------------------------------------------
def read_header_index(pck_path):
    index = {}
    with open(pck_path, 'rb') as f:
        magic = f.read(4)
        if magic != b'GDPC':
            sys.exit(f"Error: Not a Godot 3 PCK (bad magic: {magic!r})")

        version = struct.unpack('<4I', f.read(16))
        reserved = f.read(64)
        file_count = struct.unpack('<i', f.read(4))[0]

        for _ in range(file_count):
            path_len = struct.unpack('<i', f.read(4))[0]
            raw = f.read(path_len)
            path = raw.decode('utf-8').rstrip('\x00')
            offset, size = struct.unpack('<q q', f.read(16))
            f.read(16)  # skip stored MD5
            index[path] = (offset, size)

    return version, reserved, index

# -------------------------------------------------------------------
# --extract: unpack every blob into out_dir
# -------------------------------------------------------------------
def extract_pck(pck_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    _, _, index = read_header_index(pck_path)

    with open(pck_path, 'rb') as f:
        for virt_path, (offset, size) in index.items():
            rel = virt_path.replace('res://', '')
            dest = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            f.seek(offset)
            data = f.read(size)
            with open(dest, 'wb') as out:
                out.write(data)
            print(f"Extracted: {virt_path} → {dest}")

# -------------------------------------------------------------------
# --merge: copy everything from patch_dir into extracted_dir
# -------------------------------------------------------------------
def merge_patch(extracted_dir, patch_dir):
    if not os.path.isdir(patch_dir):
        sys.exit(f"Error: No patch folder at '{patch_dir}'")
    for root, _, files in os.walk(patch_dir):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, patch_dir)
            dst = os.path.join(extracted_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            print(f"Patched: {rel}")

# -------------------------------------------------------------------
# --repack: build a new Godot 3 .pck from raw files in extracted_dir
# -------------------------------------------------------------------
def repack_pck(extracted_dir, new_pck_path, src_pck=None):
    if src_pck:
        version, reserved, _ = read_header_index(src_pck)
    else:
        version, reserved = (0,0,0,0), b'\x00'*64

    items = []
    for root, _, files in os.walk(extracted_dir):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, extracted_dir).replace(os.sep, '/')
            virt = f"res://{rel}"
            size = os.path.getsize(full)
            md5  = hashlib.md5(open(full, 'rb').read()).digest()
            items.append((virt, full, size, md5))

    header_size = 4 + 16 + 64 + 4
    idx_entries_size = sum(
        4 + len(virt.encode('utf-8')) + 8 + 8 + 16
        for virt, _, _, _ in items
    )
    base_offset = header_size + idx_entries_size

    offsets, cur = [], base_offset
    for _, _, size, _ in items:
        offsets.append(cur)
        cur += size

    with open(new_pck_path, 'wb') as out:
        out.write(b'GDPC')
        out.write(struct.pack('<4I', *version))
        out.write(reserved)
        out.write(struct.pack('<i', len(items)))

        for (virt, _, size, md5), off in zip(items, offsets):
            vb = virt.encode('utf-8')
            out.write(struct.pack('<i', len(vb)))
            out.write(vb)
            out.write(struct.pack('<q', off))
            out.write(struct.pack('<q', size))
            out.write(md5)

        for _, full, _, _ in items:
            with open(full, 'rb') as f:
                out.write(f.read())

    print(f"Repacked → {new_pck_path}")

# -------------------------------------------------------------------
# --build-vpk: assemble a full VPK using a template directory,
#             giving every entry 0777 permissions.
# -------------------------------------------------------------------
def build_vpk(patched_pck_path, template_dir, content_dir, output_vpk):
    # 1) Copy entire template_dir → content_dir
    if not os.path.isdir(template_dir):
        sys.exit(f"Error: VPK template folder '{template_dir}' not found")
    if os.path.isdir(content_dir):
        shutil.rmtree(content_dir)
    shutil.copytree(template_dir, content_dir)
    print(f"Copied template → {content_dir}/")

    # 2) Replace game_data/game.pck
    gd = os.path.join(content_dir, 'game_data')
    if os.path.isdir(gd):
        shutil.rmtree(gd)
    os.makedirs(gd, exist_ok=True)
    dst = os.path.join(gd, 'game.pck')
    shutil.copy2(patched_pck_path, dst)
    print(f"Copied patched PCK → {dst}")

    # 3) Zip up content_dir → .vpk with 0777 perms on all entries
    with zipfile.ZipFile(output_vpk, 'w', compression=zipfile.ZIP_STORED) as zf:
        for root, dirs, files in os.walk(content_dir):
            # directories
            for d in dirs:
                full = os.path.join(root, d)
                rel  = os.path.relpath(full, content_dir).replace(os.sep, '/')
                if not rel.endswith('/'):
                    rel += '/'
                zi = zipfile.ZipInfo(rel)
                # UNIX mode 0777 + directory flag
                zi.external_attr = (0o777 << 16) | 0x10
                zf.writestr(zi, b'')
            # files
            for f in files:
                full = os.path.join(root, f)
                rel  = os.path.relpath(full, content_dir).replace(os.sep, '/')
                zi = zipfile.ZipInfo(rel)
                # UNIX mode 0777 for file
                zi.external_attr = (0o777 << 16)
                with open(full, 'rb') as fp:
                    data = fp.read()
                zf.writestr(zi, data)

    print(f"Built VPK: {output_vpk}")

# -------------------------------------------------------------------
# --all: run extract → merge → repack (no VPK)
# -------------------------------------------------------------------
def run_all(args):
    extract_pck(args.pck, 'extracted')
    merge_patch('extracted', 'patch')
    repack_pck('extracted', args.output, src_pck=args.pck)

# -------------------------------------------------------------------
# Command-line interface
# -------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        prog='patch',
        description='Extract → merge → repack Godot 3 .pck and optionally build a full VPK.'
    )
    p.add_argument('--pck', '-i',  required=True,
                   help='Input .pck file (for extract/repack/all/build-vpk)')
    p.add_argument('--output', '-o', default='game_patched.pck',
                   help='Output .pck on repack')
    p.add_argument('--vpk-template', '-t', default='vpk_template',
                   help='Folder containing full VPK structure')
    p.add_argument('--vpk-content', '-c', default='vpk_content',
                   help='Temp folder to assemble the VPK')
    p.add_argument('--vpk-output', '-v', default='game.vpk',
                   help='Final .vpk filename')

    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--extract', action='store_true',
                       help='Only extract to ./extracted/')
    group.add_argument('--merge', action='store_true',
                       help='Only merge patch/ into ./extracted/')
    group.add_argument('--repack', action='store_true',
                       help='Only repack ./extracted/ into a .pck')
    group.add_argument('--all', action='store_true',
                       help='extract, merge, then repack')

    p.add_argument('--build-vpk', action='store_true',
                   help='Assemble a full VPK after repacking (or from an existing .pck)')

    args = p.parse_args()

    if args.extract:
        extract_pck(args.pck, 'extracted')
    elif args.merge:
        merge_patch('extracted', 'patch')
    elif args.repack:
        repack_pck('extracted', args.output, src_pck=args.pck)
    elif args.all:
        run_all(args)

    if args.build_vpk:
        source_pck = args.output if (args.all or args.repack) else args.pck
        build_vpk(
            source_pck,
            template_dir=args.vpk_template,
            content_dir=args.vpk_content,
            output_vpk=args.vpk_output
        )

if __name__ == '__main__':
    main()
