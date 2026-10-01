# PTX2RPP

**Convert Pro Tools `.ptx` sessions into REAPER `.rpp` projects.**

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

## Installation

PTX2RPP is currently installed directly from the source repository.

Clone the repository:

```bash
git clone <repository-url>
cd PTX2RPP
```

Create a virtual environment:

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install PTX2RPP:

```bash
python -m pip install -e .
```

You can then check the installation with:

```bash
ptx2rpp --help
```

and:

```bash
ptx2rpp --version
```

---

## Basic usage

Convert a Pro Tools session by supplying either the `.ptx` file itself:

```bash
ptx2rpp "/path/to/My Session.ptx"
```

or the Pro Tools session directory:

```bash
ptx2rpp "/path/to/My Session"
```

PTX2RPP will create a `.rpp` project alongside the source session unless another output location is specified.

Paths containing spaces should be placed inside quotes.

---

## Example

On Windows:

```powershell
ptx2rpp "C:\Sessions\My Song"
```

With playlist conversion and audio gap healing enabled:

```powershell
ptx2rpp "C:\Sessions\My Song" --max-gap-heal-ms 1000 --playlists-to-lanes
```

Specify a different output file:

```powershell
ptx2rpp "C:\Sessions\My Song" --output "C:\REAPER Projects\My Song.rpp"
```

---

## Command-line options

```text
ptx2rpp [input] [ptx_name] [options]
```

### `--output PATH`

Override the output `.rpp` file location.

```bash
ptx2rpp "My Session" --output "Converted Session.rpp"
```

### `--audio-dir PATH`

Override the location of the session's audio files.

```bash
ptx2rpp "My Session.ptx" --audio-dir "/path/to/Audio Files"
```

### `--playlists-to-lanes`

Attempt to convert detected Pro Tools audio playlists into REAPER fixed item lanes.

```bash
ptx2rpp "My Session" --playlists-to-lanes
```

### `--max-gap-heal-ms N`

Extend short audio items to the next edit when the gap is no greater than the specified number of milliseconds and sufficient source media is available.

For example:

```bash
ptx2rpp "My Session" --max-gap-heal-ms 1000
```

Use:

```bash
--max-gap-heal-ms 0
```

to disable gap healing.

The default is 250 ms.

### `--verbose`

Print detailed PTX parser diagnostics.

```bash
ptx2rpp "My Session" --verbose
```

This is particularly useful when investigating a session that does not convert correctly.

### `--strict`

Return a failure exit code if active audio or MIDI placements cannot be written.

### `--version`

Display the installed PTX2RPP version.

### `--help`

Display the complete command-line help.

---

## Recommended conversion

For sessions containing edited audio and Pro Tools playlists, a useful starting command is:

```bash
ptx2rpp "My Session" --max-gap-heal-ms 1000 --playlists-to-lanes
```

After conversion, open the generated `.rpp` file in REAPER and verify the project against the original Pro Tools session.

---

## Audio files

PTX2RPP does not duplicate your audio.

The generated REAPER project references the existing source media associated with the Pro Tools session.

For the most reliable conversion, keep the original Pro Tools session folder structure intact, including its audio files.

If the audio is stored elsewhere, use:

```bash
--audio-dir "/path/to/audio"
```

---

## Pro Tools playlists

PTX2RPP can experimentally translate detected Pro Tools audio playlist families into REAPER fixed item lanes.

Enable this with:

```bash
--playlists-to-lanes
```

The active Pro Tools playlist is retained as the active lane, while detected alternate playlists are represented as additional lanes where possible.

Playlist detection is still considered experimental and may not behave correctly with every PTX session.

---

## Memory Locations

Point-based Pro Tools Memory Locations are imported as REAPER project markers where they can be identified from the PTX session.

Not every type of Pro Tools Memory Location is currently supported.

---

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
