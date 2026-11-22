#!/usr/bin/env python3
"""Diagnose audio item length differences between PTX2RPP desktop and browser RPPs.

Usage: python diagnose_greyscale_lengths.py DESKTOP.rpp BROWSER.rpp
Reads original audio WAV headers through desktop RPP media paths; no files are changed.
"""
from __future__ import annotations
import collections
import pathlib
import re
import struct
import sys
import wave

ITEM_RE = re.compile(r'(?ms)^  <ITEM\s*\n(.*?)^  >\s*$', re.MULTILINE)

def field(block, key):
    m = re.search(r'(?m)^\s*' + re.escape(key) + r'\s+(.+?)\s*$', block)
    return m.group(1).strip() if m else None

def load(path):
    text = path.read_text(encoding='utf-8-sig')
    result = []
    for m in ITEM_RE.finditer(text):
        block = m.group(1)
        if '<SOURCE WAVE' not in block:
            continue
        filename = field(block, 'FILE')
        if filename:
            filename = filename.strip('"')
        result.append({
            'position': field(block, 'POSITION'),
            'length': field(block, 'LENGTH'),
            'offset': field(block, 'SOFFS'),
            'name': (field(block, 'NAME') or '').strip('"'),
            'file': filename,
        })
    return result

def inspect_wav(path):
    if not path:
        return 'no media path'
    if not path.is_file():
        return 'media file not found'
    try:
        with path.open('rb') as f:
            header = f.read(128)
        kind = header[:4].decode('ascii', 'replace')
        tag = '?'
        if kind in ('RIFF', 'RF64') and header[8:12] == b'WAVE':
            pos = 12
            while pos + 8 <= len(header):
                size = struct.unpack_from('<I', header, pos+4)[0]
                if header[pos:pos+4] == b'fmt ' and pos+10 <= len(header):
                    fmt = struct.unpack_from('<H', header, pos+8)[0]
                    tag = {1:'PCM', 3:'IEEE_FLOAT', 65534:'EXTENSIBLE'}.get(fmt, str(fmt))
                    break
                pos += 8 + size + (size % 2)
        try:
            with wave.open(str(path), 'rb') as w:
                frames = w.getnframes()
                return f'WAV={kind}/{tag}; wave.open=OK; frames={frames}'
        except Exception as e:
            return f'WAV={kind}/{tag}; wave.open=ERROR {type(e).__name__}: {e}'
    except Exception as e:
        return f'cannot inspect media: {type(e).__name__}: {e}'

def main():
    if len(sys.argv) != 3:
        print('Usage: python diagnose_greyscale_lengths.py DESKTOP.rpp BROWSER.rpp')
        return 2
    desktop_path, browser_path = map(pathlib.Path, sys.argv[1:])
    a, b = load(desktop_path), load(browser_path)
    print(f'Desktop audio items: {len(a)} | Browser audio items: {len(b)}')
    if len(a) != len(b):
        print('WARNING: different audio item counts; comparison by index may not align.')
    mismatches = []
    for i, (x, y) in enumerate(zip(a,b), 1):
        if x['position'] != y['position'] or x['name'] != y['name']:
            print(f'WARNING: item {i} position or name differs; check item ordering.')
            break
        if x['length'] != y['length']:
            mismatches.append((i,x,y))
    print(f'Audio length mismatches: {len(mismatches)}')
    if not mismatches:
        return 0
    media = collections.Counter((x['file'] or '').replace('\\','/').split('/')[-1] for _,x,_ in mismatches)
    print('\nMost affected source files:')
    for name, n in media.most_common(12):
        print(f'  {n:4d}  {name}')
    print('\nFirst 12 differing clips:')
    for i, x, y in mismatches[:12]:
        print(f"  #{i}: {x['position']}s  desktop={x['length']}s browser={y['length']}s  {x['name']}")
    print('\nDesktop WAV header checks (affected files):')
    seen = set()
    for _, x, _ in mismatches:
        raw = x['file']
        if not raw or raw in seen:
            continue
        seen.add(raw)
        # RPP FILE paths are relative to the desktop RPP directory.
        media_path = pathlib.Path(raw.replace('/', '\\') if sys.platform == 'win32' else raw)
        if not media_path.is_absolute():
            media_path = desktop_path.resolve().parent / media_path
        print(f'  {media_path.name}: {inspect_wav(media_path)}')
        if len(seen) >= 12:
            break
    return 1

if __name__ == '__main__':
    raise SystemExit(main())
