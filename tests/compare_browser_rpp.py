#!/usr/bin/env python3
"""Compare PTX2RPP desktop and browser output, ignoring path-only differences.

Usage: python compare_browser_rpp.py DESKTOP.rpp BROWSER.rpp

Normalizes only:
- REAPER project header timestamp
- audio media path prefixes (keeps the original filename)
- audio item/take GUIDs, which PTX2RPP derives from absolute media paths

Does NOT normalize clip positions, lengths, offsets, names, track structure,
MIDI events, tempo, markers, or other project content.
"""

from pathlib import Path
import difflib
import re
import sys

AUDIO_ITEM = re.compile(r'^  <ITEM\r?\n.*?^  >(?=\r?\n|$)', re.MULTILINE | re.DOTALL)


def normalize(text: str) -> str:
    text = text.replace('\r\n', '\n')
    text = re.sub(
        r'(<REAPER_PROJECT\s+0\.1\s+"[^"]+"\s+)\d+',
        r'\g<1><TIMESTAMP>',
        text,
        count=1,
    )

    def audio_item(match: re.Match[str]) -> str:
        block = match.group(0)
        if '<SOURCE WAVE' not in block:
            return block
        # The audio item's GUIDs are derived from the absolute media path,
        # which differs between a Windows session and the browser filesystem.
        return re.sub(
            r'(?m)^(\s*(?:IGUID|GUID) )\{[^}\n]+\}',
            r'\g<1>{AUDIO_PATH_GUID}',
            block,
        )

    text = AUDIO_ITEM.sub(audio_item, text)

    def media_path(match: re.Match[str]) -> str:
        raw_path = match.group(2)
        filename = raw_path.replace('\\', '/').rsplit('/', 1)[-1]
        return match.group(1) + filename + match.group(3)

    text = re.sub(
        r'(?m)^(\s*FILE ")([^"]+)(")$',
        media_path,
        text,
    )
    return text


def stats(text: str) -> str:
    def count(pattern: str) -> int:
        return len(re.findall(pattern, text, re.MULTILINE))
    return (
        f"tracks={count(r'^  <TRACK$')}, "
        f"audio items={count(r'^    <SOURCE WAVE$')}, "
        f"MIDI items={count(r'^    <SOURCE MIDI$')}, "
        f"markers={count(r'^  MARKER ')}"
    )


def main() -> int:
    if len(sys.argv) != 3:
        print('Usage: python compare_browser_rpp.py DESKTOP.rpp BROWSER.rpp')
        return 2
    paths = [Path(arg) for arg in sys.argv[1:]]
    for path in paths:
        if not path.is_file():
            print(f'Cannot find: {path}')
            return 2
    desktop_raw, browser_raw = (path.read_text(encoding='utf-8-sig') for path in paths)
    desktop, browser = normalize(desktop_raw), normalize(browser_raw)
    print('Desktop:', stats(desktop_raw))
    print('Browser:', stats(browser_raw))
    if desktop == browser:
        print('\nPASS — Conversion data matches after normalizing media paths, audio GUIDs and header timestamp.')
        return 0
    print('\nDIFFERENCES FOUND — First 60 lines of unified diff:')
    diff = difflib.unified_diff(
        desktop.splitlines(), browser.splitlines(),
        fromfile='desktop', tofile='browser', lineterm='',
    )
    for _, line in zip(range(60), diff):
        print(line)
    print('\nNote: media path prefixes, path-derived audio GUIDs and timestamps were ignored.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
