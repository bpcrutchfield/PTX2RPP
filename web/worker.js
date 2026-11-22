/* PTX2RPP Web prototype — all session processing occurs in this Web Worker. */
const PYODIDE_VERSION = '0.27.7';
const PYODIDE_ROOT = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const PACKAGE_FILES = [
  '__init__.py', '__main__.py', 'audio.py', 'converter.py', 'markers.py',
  'midi.py', 'models.py', 'ptx.py', 'reaper.py', 'session.py', 'timing.py'
];
let pythonReady = null;
let pyodide = null;

function progress(message, fraction = null) {
  postMessage({ type: 'progress', message, fraction });
}

async function init() {
  if (pythonReady) return pythonReady;
  pythonReady = (async () => {
    progress('Loading Python runtime…', 0.06);
    importScripts(`${PYODIDE_ROOT}pyodide.js`);
    pyodide = await loadPyodide({ indexURL: PYODIDE_ROOT });
    pyodide.FS.mkdirTree('/app/ptx2rpp');
    pyodide.FS.mkdirTree('/workspace/Audio Files');
    progress('Loading the PTX2RPP v1.2.0 engine…', 0.20);
    for (let i = 0; i < PACKAGE_FILES.length; i++) {
      const filename = PACKAGE_FILES[i];
      const response = await fetch(`./ptx2rpp/${filename}`);
      if (!response.ok) throw new Error(`Could not load ${filename} (HTTP ${response.status}). Run the preparation script first.`);
      pyodide.FS.writeFile(`/app/ptx2rpp/${filename}`, new Uint8Array(await response.arrayBuffer()));
      progress(`Loading converter (${i + 1}/${PACKAGE_FILES.length})…`, 0.20 + 0.20 * (i + 1) / PACKAGE_FILES.length);
    }
    pyodide.runPython('import sys; sys.path.insert(0, "/app"); import ptx2rpp');
    progress('Converter ready.', 0.42);
  })().catch(err => { pythonReady = null; throw err; });
  return pythonReady;
}

function safeName(value) {
  return String(value).replace(/[\\/\x00-\x1f]/g, '_').replace(/^\.+$/, '_');
}

function wavHeader(info) {
  // A 44-byte *metadata-only* WAV stub. The converter uses the WAV header
  // to determine frame counts; it does not need actual audio sample data.
  const channels = Math.max(1, Math.min(32, info.channels | 0));
  const rate = Math.max(1, Math.min(384000, info.rate | 0));
  const bits = [8, 16, 24, 32].includes(info.bits) ? info.bits : 16;
  // Keep the source format: 1=PCM, 3=IEEE float, 65534=WAVE_FORMAT_EXTENSIBLE.
  // Python's wave module rejects unsupported formats; the browser must do likewise.
  const formatCode = Number.isInteger(info.formatCode) && info.formatCode >= 1 && info.formatCode <= 65535
    ? info.formatCode : 0;
  const blockAlign = channels * (bits / 8);
  const frames = Math.max(0, Math.floor(info.frames || 0));
  const dataBytes = Math.min(0xffffff00, frames * blockAlign);
  const buf = new ArrayBuffer(44);
  const view = new DataView(buf);
  const ascii = (pos, str) => { for (let i = 0; i < str.length; i++) view.setUint8(pos + i, str.charCodeAt(i)); };
  ascii(0, 'RIFF'); view.setUint32(4, 36 + dataBytes, true); ascii(8, 'WAVE');
  ascii(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, formatCode, true);
  view.setUint16(22, channels, true); view.setUint32(24, rate, true);
  view.setUint32(28, rate * blockAlign, true); view.setUint16(32, blockAlign, true);
  view.setUint16(34, bits, true); ascii(36, 'data'); view.setUint32(40, dataBytes, true);
  return new Uint8Array(buf);
}

async function convert(message) {
  await init();
  const { name, data, audio, gapMs, playlists, verbose } = message;
  const inputName = safeName(name);
  if (!/\.ptx$/i.test(inputName)) throw new Error('Please select a .ptx session file.');
  if (!data || data.byteLength < 20) throw new Error('The selected PTX file is empty or too small.');
  progress('Preparing session in browser memory…', 0.50);
  const fs = pyodide.FS;
  const inputPath = `/workspace/${inputName}`;
  const outputName = inputName.replace(/\.ptx$/i, '.rpp');
  const outputPath = `/workspace/${outputName}`;
  // Remove previous conversion files to avoid stale outputs or media matches.
  for (const entry of fs.readdir('/workspace')) {
    if (entry === '.' || entry === '..') continue;
    const path = `/workspace/${entry}`;
    if (entry === 'Audio Files') {
      for (const old of fs.readdir(path)) {
        if (old !== '.' && old !== '..') fs.unlink(`${path}/${old}`);
      }
    } else if (fs.stat(path).mode & 0o40000) {
      // No nested directories are created by this prototype.
    } else {
      fs.unlink(path);
    }
  }
  fs.writeFile(inputPath, new Uint8Array(data));
  let metadataCount = 0;
  let audioCount = 0;
  for (const file of audio || []) {
    if (!/\.(wav|aif|aiff)$/i.test(file.name)) continue;
    const filename = safeName(file.name);
    const path = `/workspace/Audio Files/${filename}`;
    const header = file.wavInfo && /\.wav$/i.test(filename) ? wavHeader(file.wavInfo) : new Uint8Array(0);
    if (header.length) metadataCount++;
    fs.writeFile(path, header);
    audioCount++;
  }
  progress('Starting Python conversion (stage diagnostics enabled)…', 0.65);
  const args = [inputPath, '--output', outputPath, '--audio-dir', '/workspace/Audio Files',
    '--max-gap-heal-ms', String(Math.max(0, Math.min(10000, Number(gapMs) || 0)))];
  if (playlists) args.push('--playlists-to-lanes');
  if (verbose) args.push('--verbose');
  fs.writeFile('/workspace/options.json', JSON.stringify(args));
  const rawResult = await pyodide.runPythonAsync(`
import io, json, contextlib, time, functools
from js import postMessage, JSON
import ptx2rpp.converter as converter

# Browser-only optimisation for the existing PTX structure parser.
# The original implementation tries every byte and repeatedly reparses
# overlapping candidate blocks.  A large edited session can take minutes.
# This retains its tuple layout and boundary rules, but skips directly to
# candidate 0x5A bytes and caches candidate results within one conversion.
def _web_fast_find_top(data):
    from functools import lru_cache
    from ptx2rpp.ptx import r2, r4
    data_length = len(data)

    @lru_cache(maxsize=250000)
    def parse_at(pos, parent_end=None):
        if pos >= data_length or data[pos] != 0x5A:
            return None
        end = parent_end if parent_end is not None else data_length
        if pos + 9 > end:
            return None
        bt = r2(data, pos + 1)
        bs = r4(data, pos + 3)
        ct = r2(data, pos + 7)
        if bt & 0xFF00 or bs > 0x4000000:
            return None
        be = pos + 7 + bs
        if be > data_length:
            return None
        children = []
        cursor = pos + 1
        limit = pos + bs
        while cursor < limit:
            candidate = data.find(b'Z', cursor, limit)
            if candidate < 0:
                break
            child = parse_at(candidate, be)
            if child:
                children.append(child)
                cursor = candidate + child[2] + 7
            else:
                cursor = candidate + 1
        return (bt, ct, bs, pos + 7, children)

    blocks = []
    cursor = 0x14
    while cursor < data_length:
        candidate = data.find(b'Z', cursor)
        if candidate < 0:
            break
        block = parse_at(candidate)
        if block:
            blocks.append(block)
            cursor = candidate + block[2] + 7
        else:
            cursor = candidate + 1
    return blocks

converter.find_top = _web_fast_find_top

# Instrument the existing converter at its main stage boundaries.
# This does not modify the actual parsing or writing algorithms.
_STAGES = [
    ('decrypt_ptx', 'Decrypting PTX', .66),
    ('find_top', 'Reading session structure', .68),
    ('detect_session_sample_rate', 'Detecting sample rate', .70),
    ('extract_audio_files', 'Finding registered audio files', .72),
    ('extract_audio_tracks', 'Finding audio tracks', .74),
    ('extract_regions', 'Extracting audio regions', .76),
    ('build_wav_index', 'Indexing audio files', .78),
    ('build_registered_media_map', 'Matching registered media', .80),
    ('match_regions_to_wavs', 'Linking audio regions', .82),
    ('extract_session_timecode_origin_samples', 'Reading timecode origin', .84),
    ('assign_regions_to_tracks', 'Placing audio clips', .86),
    ('dedupe_redundant_same_track_audio', 'Removing duplicate clips', .88),
    ('prune_spurious_cross_track_audio', 'Pruning audio aliases', .89),
    ('resolve_midi_placements_direct', 'Extracting MIDI', .90),
    ('extract_session_tempo', 'Reading session tempo', .92),
    ('extract_memory_locations', 'Reading markers', .94),
    ('write_rpp', 'Writing REAPER project', .96),
]

def _report(message, fraction):
    postMessage(JSON.parse(json.dumps({'type': 'progress', 'message': message, 'fraction': fraction})))

if not hasattr(converter, '_web_original_functions'):
    converter._web_original_functions = {}
    for _name, _label, _fraction in _STAGES:
        _original = getattr(converter, _name)
        converter._web_original_functions[_name] = _original
        def _make_wrapper(original, label, fraction):
            @functools.wraps(original)
            def wrapper(*args, **kwargs):
                _report(label + '…', fraction)
                started = time.monotonic()
                try:
                    return original(*args, **kwargs)
                finally:
                    elapsed = time.monotonic() - started
                    _report(label + ' finished (' + format(elapsed, '.1f') + 's)', fraction)
            return wrapper
        setattr(converter, _name, _make_wrapper(_original, _label, _fraction))

from ptx2rpp.converter import main
buf_out, buf_err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
    exit_code = main(json.loads(open('/workspace/options.json', encoding='utf-8').read()))
json.dumps({'code': exit_code, 'stdout': buf_out.getvalue(), 'stderr': buf_err.getvalue()})
`);
  const result = JSON.parse(rawResult);
  if (result.code !== 0 || !fs.analyzePath(outputPath).exists) {
    throw new Error(`Conversion failed (exit code ${result.code}).\n${result.stderr || result.stdout}`);
  }
  const output = fs.readFile(outputPath);
  progress('Conversion complete.', 1);
  postMessage({
    type: 'result', filename: outputName, buffer: output.buffer,
    log: [result.stdout, result.stderr].filter(Boolean).join('\n'),
    audioCount, metadataCount, outputBytes: output.byteLength
  }, [output.buffer]);
}

onmessage = async event => {
  const msg = event.data;
  try {
    if (msg.type === 'init') await init();
    else if (msg.type === 'convert') await convert(msg);
  } catch (err) {
    postMessage({ type: 'error', message: String(err?.message || err), stack: err?.stack || '' });
  }
};
