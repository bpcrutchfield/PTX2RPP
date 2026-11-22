const $ = id => document.getElementById(id);
const ptxInput = $('ptxInput');
const audioInput = $('audioInput');
const dropzone = $('dropzone');
const convertBtn = $('convertBtn');
const statusText = $('statusText');
const progressBar = $('progressBar');
const results = $('results');
const errorBox = $('errorBox');
const logBox = $('logBox');
let selectedPtx = null;
let converting = false;
const worker = new Worker('./worker.js');

function setStatus(text, progress) {
  statusText.textContent = text;
  if (progress != null) progressBar.style.width = `${Math.round(progress * 100)}%`;
}
function setError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  setStatus('Conversion stopped.', 0);
  converting = false;
  convertBtn.disabled = !selectedPtx;
}
function selectPtx(file) {
  if (!file) return;
  if (!/\.ptx$/i.test(file.name)) return setError('Please choose a Pro Tools .ptx file.');
  if (file.size > 128 * 1024 * 1024) return setError('This prototype supports PTX files up to 128 MB.');
  selectedPtx = file;
  $('selectedFile').textContent = `${file.name} · ${formatBytes(file.size)}`;
  $('selectedFile').hidden = false;
  errorBox.hidden = true;
  results.hidden = true;
  convertBtn.disabled = converting;
  setStatus('Session selected. Ready to convert.', 0);
}
function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 ** 2)).toFixed(1)} MB`;
}
ptxInput.addEventListener('change', () => selectPtx(ptxInput.files[0]));
for (const eventName of ['dragenter', 'dragover']) {
  dropzone.addEventListener(eventName, event => { event.preventDefault(); dropzone.classList.add('dragging'); });
}
for (const eventName of ['dragleave', 'drop']) {
  dropzone.addEventListener(eventName, event => { event.preventDefault(); dropzone.classList.remove('dragging'); });
}
dropzone.addEventListener('drop', event => selectPtx([...event.dataTransfer.files].find(file => /\.ptx$/i.test(file.name))));
audioInput.addEventListener('change', () => {
  const files = [...audioInput.files].filter(file => /\.(wav|aif|aiff)$/i.test(file.name));
  $('audioSummary').textContent = files.length ? `${files.length} audio files identified (filenames and headers only)` : 'No audio files identified';
});

// Read just the first 64 KiB of each WAV, not its actual audio sample data.
async function wavMetadata(file) {
  if (!/\.wav$/i.test(file.name)) return null;
  const bytes = await file.slice(0, 65536).arrayBuffer();
  const v = new DataView(bytes);
  if (v.byteLength < 44) return null;
  const tag = off => String.fromCharCode(...new Uint8Array(bytes, off, 4));
  if (tag(0) !== 'RIFF' || tag(8) !== 'WAVE') return null;
  let off = 12, formatCode = 0, channels = 0, rate = 0, bits = 0, blockAlign = 0, dataBytes = null;
  while (off + 8 <= v.byteLength) {
    const kind = tag(off), length = v.getUint32(off + 4, true);
    if (kind === 'fmt ' && off + 24 <= v.byteLength) {
      formatCode = v.getUint16(off + 8, true);
      channels = v.getUint16(off + 10, true);
      rate = v.getUint32(off + 12, true);
      blockAlign = v.getUint16(off + 20, true);
      bits = v.getUint16(off + 22, true);
    }
    if (kind === 'data') { dataBytes = length; break; }
    const next = off + 8 + length + (length % 2);
    if (next <= off || next > v.byteLength) break;
    off = next;
  }
  if (!formatCode || !channels || !rate || !blockAlign || !bits || dataBytes === null || dataBytes === 0xffffffff) return null;
  return { formatCode, channels, rate, bits, frames: Math.floor(dataBytes / blockAlign) };
}

async function readAudioMetadata() {
  const files = [...audioInput.files].filter(file => /\.(wav|aif|aiff)$/i.test(file.name));
  const entries = [];
  let missing = 0;
  const names = new Set();
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    if (names.has(file.name.toLowerCase())) throw new Error(`Duplicate audio filename: ${file.name}. This prototype expects unique filenames.`);
    names.add(file.name.toLowerCase());
    const info = await wavMetadata(file);
    if (!info) missing++;
    entries.push({ name: file.name, wavInfo: info });
    if (i % 25 === 0) setStatus(`Reading audio metadata ${i + 1}/${files.length}…`, 0.02);
  }
  return { entries, missing };
}

worker.onmessage = event => {
  const msg = event.data;
  if (msg.type === 'progress') setStatus(msg.message, msg.fraction);
  if (msg.type === 'error') setError(msg.message);
  if (msg.type === 'result') {
    const blob = new Blob([msg.buffer], { type: 'application/octet-stream' });
    const url = URL.createObjectURL(blob);
    const link = $('downloadLink');
    if (link.dataset.objectUrl) URL.revokeObjectURL(link.dataset.objectUrl);
    link.href = url;
    link.dataset.objectUrl = url;
    link.download = msg.filename;
    $('resultSummary').textContent = `${msg.filename} · ${formatBytes(msg.outputBytes)} · ${msg.audioCount} audio filenames mapped`;
    logBox.textContent = msg.log || '(No diagnostic output)';
    results.hidden = false;
    converting = false;
    convertBtn.disabled = false;
    setStatus('Your REAPER project is ready.', 1);
  }
};
worker.onerror = event => setError(event.message || 'The conversion worker stopped unexpectedly.');

convertBtn.addEventListener('click', async () => {
  if (!selectedPtx || converting) return;
  converting = true;
  convertBtn.disabled = true;
  results.hidden = true;
  errorBox.hidden = true;
  try {
    setStatus('Reading session locally…', 0.01);
    const { entries, missing } = await readAudioMetadata();
    if (missing && entries.length) {
      $('metadataWarning').textContent = `${missing} file(s) have no usable WAV metadata. Short-gap healing may be limited for those files.`;
      $('metadataWarning').hidden = false;
    } else $('metadataWarning').hidden = true;
    const data = await selectedPtx.arrayBuffer();
    worker.postMessage({
      type: 'convert', name: selectedPtx.name, data, audio: entries,
      gapMs: Number($('gapHealing').value), playlists: $('playlists').checked,
      verbose: $('verbose').checked
    }, [data]);
  } catch (err) { setError(err.message || String(err)); }
});
