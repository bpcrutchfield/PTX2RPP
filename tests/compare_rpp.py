#!/usr/bin/env python3

"""Compare two PTX2RPP-generated REAPER projects.

The REAPER project header contains a generation timestamp, so that
field is normalised before comparison. Everything else must match.
"""

from pathlib import Path
import re
import sys


def normalise_rpp(path: Path) -> str:
    text = path.read_text(encoding="utf-8")

    text = re.sub(
        r'(<REAPER_PROJECT\s+0\.1\s+"[^"]+"\s+)\d+',
        r'\1<TIMESTAMP>',
        text,
        count=1,
    )

    return text


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python compare_rpp.py REFERENCE.rpp TEST.rpp")
        return 2

    reference = Path(sys.argv[1])
    test = Path(sys.argv[2])

    if not reference.exists():
        print(f"Reference file not found: {reference}")
        return 2

    if not test.exists():
        print(f"Test file not found: {test}")
        return 2

    reference_text = normalise_rpp(reference)
    test_text = normalise_rpp(test)

    if reference_text == test_text:
        print("PASS - Conversion data is identical")
        return 0

    print("FAIL - Conversion data differs")

    ref_lines = reference_text.splitlines()
    test_lines = test_text.splitlines()

    limit = max(len(ref_lines), len(test_lines))

    differences = 0

    for i in range(limit):
        ref_line = ref_lines[i] if i < len(ref_lines) else "<MISSING>"
        test_line = test_lines[i] if i < len(test_lines) else "<MISSING>"

        if ref_line != test_line:
            differences += 1

            if differences <= 10:
                print()
                print(f"Line {i + 1}:")
                print(f"  REF : {ref_line}")
                print(f"  TEST: {test_line}")

    print()
    print(f"Different lines: {differences}")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
