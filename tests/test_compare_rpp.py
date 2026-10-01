"""Tests for the REAPER project regression comparison helper."""

from pathlib import Path

from tests.compare_rpp import normalise_rpp


def test_normalise_rpp_ignores_generation_timestamp(tmp_path: Path) -> None:
    """Generated RPP timestamps should not affect regression comparisons."""

    first = tmp_path / "first.rpp"
    second = tmp_path / "second.rpp"

    first.write_text(
        '<REAPER_PROJECT 0.1 "7.0" 1000000000\n'
        '  RIPPLE 0\n'
        '>\n',
        encoding="utf-8",
    )

    second.write_text(
        '<REAPER_PROJECT 0.1 "7.0" 2000000000\n'
        '  RIPPLE 0\n'
        '>\n',
        encoding="utf-8",
    )

    assert normalise_rpp(first) == normalise_rpp(second)


def test_normalise_rpp_preserves_project_changes(tmp_path: Path) -> None:
    """Real project differences must remain visible after normalisation."""

    first = tmp_path / "first.rpp"
    second = tmp_path / "second.rpp"

    first.write_text(
        '<REAPER_PROJECT 0.1 "7.0" 1000000000\n'
        '  TEMPO 120 4 4\n'
        '>\n',
        encoding="utf-8",
    )

    second.write_text(
        '<REAPER_PROJECT 0.1 "7.0" 2000000000\n'
        '  TEMPO 86 4 4\n'
        '>\n',
        encoding="utf-8",
    )

    assert normalise_rpp(first) != normalise_rpp(second)