# PTX2RPP

**Convert Pro Tools `.ptx` sessions into REAPER `.rpp` projects.**

**Latest release: [PTX2RPP v1.2.0](https://github.com/bpcrutchfield/PTX2RPP/releases/tag/v1.2.0)**

PTX2RPP is an open-source command-line tool for transferring session data from Avid Pro Tools to REAPER.

The aim is not to reproduce an entire Pro Tools session exactly, but to recover as much useful project structure as possible — including audio edits, MIDI, markers, timing information and playlists — so that a session can be opened and continued in REAPER.

> **Status:** PTX2RPP is currently under active development.  
> Always keep your original Pro Tools session and check converted projects before relying on them.

---

## Features

PTX2RPP currently supports:

- Pro Tools `.ptx` session parsing
- Audio track names
- Audio clip placement
- Audio clip lengths
- Source offsets within audio files
- Registered audio-file matching
- Active/inactive audio placement filtering
- Mono audio media
- MIDI track names
- MIDI region placement
- MIDI note pitch
- MIDI note velocity
- MIDI note start position and duration
- Pro Tools Memory Locations as REAPER markers
- Constant session tempo
- Pro Tools session timecode origin handling
- Optional Pro Tools playlist conversion to REAPER fixed item lanes
- Optional healing of short gaps between audio edits
- Non-pooled MIDI items for compatibility across REAPER versions

The converter generates a standard REAPER `.rpp` project file and references the original session audio files rather than copying or modifying them.

---

## Current limitations

PTX2RPP does **not currently convert**:

- Plugins
- Plugin settings
- Sends
- Bus routing
- Track routing
- Automation
- Fades
- Mixer settings
- Tempo-map changes

Constant session tempo is supported, but sessions containing tempo changes are not yet fully reproduced.

Stereo Pro Tools audio channels are currently represented as separate mono REAPER tracks.

PTX is a proprietary session format and support is based on the session structures that have been identified and tested so far. Sessions created with different versions of Pro Tools may contain structures that PTX2RPP does not yet understand.

---

## Requirements

- Python 3.10 or newer
- REAPER
- A Pro Tools `.ptx` session
- Access to the session's original audio files

PTX2RPP does **not** require Pro Tools to perform the conversion.

---

### Installation

Download `ptx2rpp-1.2.0-py3-none-any.whl` from the release assets below.

Install it with:

```bash
python -m pip install ptx2rpp-1.2.0-py3-none-any.whl
```

Then run:

```bash
python -m ptx2rpp "path/to/session"
```

For edited sessions with playlists:

```bash
python -m ptx2rpp "path/to/session" --max-gap-heal-ms 1000 --playlists-to-lanes
```

See the README for complete installation instructions, options and current limitations.

### Current limitations

Plugins, sends, routing, automation, fades, mixer state and tempo-map changes are not currently converted.

Stereo Pro Tools audio channels are currently represented as separate mono REAPER tracks.

PTX is a proprietary format and session structures may differ between Pro Tools versions, so converted projects should always be checked against the original session.

## Reporting problems

PTX files can differ significantly depending on the version of Pro Tools and the features used in a session.

If you find a session that converts incorrectly, useful information to include in a bug report is:

- PTX2RPP version
- Pro Tools version, if known
- REAPER version
- Session sample rate
- What was expected
- What appeared in the converted project
- PTX2RPP terminal output
- Output from `--verbose`, where appropriate

Please do **not** publicly upload copyrighted session audio or other material you do not have permission to share.

---

## Project structure

```text
ptx2rpp/
├── audio.py       Audio extraction and placement
├── converter.py   Command-line interface and conversion orchestration
├── markers.py     Pro Tools Memory Location extraction
├── midi.py        MIDI extraction and placement
├── models.py      Shared data structures
├── ptx.py         PTX decryption and binary parsing
├── reaper.py      REAPER project generation
├── session.py     Session metadata extraction
├── timing.py      Timing conversion utilities
├── __init__.py
└── __main__.py
```

---

## Development status

PTX2RPP has been developed by comparing converted REAPER projects against known Pro Tools sessions and incrementally identifying PTX structures.

The current converter has been regression-tested against multiple real-world sessions containing audio, MIDI, session timecode, Memory Locations and edited audio.

More PTX session variants still need testing, so feedback and reproducible test cases are welcome.

---

## License

PTX2RPP is released under the MIT License.

See [LICENSE](LICENSE) for details.

---

## Disclaimer

PTX2RPP is an independent project and is not affiliated with, endorsed by, or supported by Avid or Cockos.

Pro Tools is a trademark of Avid Technology, Inc.  
REAPER is a product of Cockos Incorporated.
