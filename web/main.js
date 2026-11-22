/* PTX2RPP Web UI — presentation refresh only; conversion worker is unchanged. */
const $ = id => document.getElementById(id);
const ptxInput = $('ptxInput');
const audioInput = $('audioInput');
const dropzone = $('dropzone');
const convertBtn = $('convertBtn');
const statusText = $('statusText');
const progressBar = $('progressBar');
const progressTrack = $('progressTrack');
const results = $('results');
const errorBox = $('errorBox');
const logBox = $('logBox');
let selectedPtx = null;
let converting = false;
const worker = new Worker('./worker.js');

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 ** 2)).toFixed(1)} MB`;
}
function setStatus(message, progress = null) {
  statusText.textContent = message;
  if (progress != null) {
    const percentage = Math.max(0, Math.min(100, Math.round(progress * 100)));
    progressBar.style.width = `${percentage}%`;
    progressTrack.setAttribute('aria-valuenow', String(percentage));
    $('progressPercent').textContent = `${percentage}%`;
  }
}
function setError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  $('statusLabel').textContent = 'NEEDS ATTENTION';
  setStatus('Conversion stopped.', 0);
  converting = false;
  convertBtn.disabled = !selectedPtx;
  convertBtn.querySelector('span:first-child').textContent = 'Convert to REAPER';
}
function selectPtx(file) {
  if (converting || !file) return;
  if (!/\.ptx$/i.test(file.name)) return setError('Please choose a Pro Tools .ptx file.');
  if (file.size > 128 * 1024 * 1024) return setError('PTX files must be 128 MB or smaller.');
  selectedPtx = file;
  $('selectedFileName').textContent = file.name;
  $('selectedFileSize').textContent = formatBytes(file.size);
  $('selectedFile').hidden = false;
  errorBox.hidden = true;
  results.hidden = true;
  $('metadataWarning').hidden = true;
  convertBtn.disabled = false;
  $('statusLabel').textContent = 'READY TO CONVERT';
  setStatus('Session selected. You can start the conversion.', 0);
}
ptxInput.addEventListener('change', () => selectPtx(ptxInput.files[0]));
for (const name of ['dragenter', 'dragover']) {
  dropzone.addEventListener(name, event => {
    event.preventDefault();
    if (!converting) dropzone.classList.add('dragging');
  });
}
for (const name of ['dragleave', 'drop']) {
  dropzone.addEventListener(name, event => {
    event.preventDefault();
    dropzone.classList.remove('dragging');
  });
}
dropzone.addEventListener('drop', event => {
  if (converting) return;
  const file = [...event.dataTransfer.files].find(item => /\.ptx$/i.test(item.name));
  if (file) selectPtx(file);
  else setError('Drop a Pro Tools .ptx file to continue.');
});
audioInput.addEventListener('change', () => {
  const files = [...audioInput.files].filter(file => /\.(wav|aif|aiff)$/i.test(file.name));
  $('audioSummary').classList.toggle('has-files', files.length > 0);
  $('audioSummaryText').textContent = files.length
    ? `${files.length.toLocaleString()} audio files selected (metadata only)`
    : 'No audio folder selected';
});

// Read at most the first 64 KiB of each WAV file; never load the audio samples.
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
    if (names.has(file.name.toLowerCase())) throw new Error(`Duplicate audio filename: ${file.name}. This version expects unique filenames.`);
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
  if (msg.type === 'progress') {
    $('statusLabel').textContent = 'CONVERTING LOCALLY';
    setStatus(msg.message, msg.fraction);
  }
  if (msg.type === 'error') setError(msg.message);
  if (msg.type === 'result') {
    const blob = new Blob([msg.buffer], { type: 'application/octet-stream' });
    const url = URL.createObjectURL(blob);
    const link = $('downloadLink');
    if (link.dataset.objectUrl) URL.revokeObjectURL(link.dataset.objectUrl);
    link.href = url;
    link.dataset.objectUrl = url;
    link.download = msg.filename;
    $('resultSummary').textContent = `${msg.filename} · ${formatBytes(msg.outputBytes)} · ${Number(msg.audioCount).toLocaleString()} audio filenames indexed`;
    logBox.textContent = msg.log || '(No diagnostic output)';
    results.hidden = false;
    converting = false;
    convertBtn.disabled = false;
    convertBtn.querySelector('span:first-child').textContent = 'Convert again';
    $('statusLabel').textContent = 'CONVERSION COMPLETE';
    setStatus('Your REAPER project is ready to download.', 1);
    results.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
  }
};
worker.onerror = event => setError(event.message || 'The conversion worker stopped unexpectedly.');

convertBtn.addEventListener('click', async () => {
  if (!selectedPtx || converting) return;
  converting = true;
  convertBtn.disabled = true;
  convertBtn.querySelector('span:first-child').textContent = 'Converting…';
  results.hidden = true;
  errorBox.hidden = true;
  $('metadataWarning').hidden = true;
  $('statusLabel').textContent = 'PREPARING FILES';
  try {
    setStatus('Reading session locally…', 0.01);
    const { entries, missing } = await readAudioMetadata();
    if (missing && entries.length) {
      $('metadataWarning').textContent = `${missing} file(s) have no usable WAV metadata. Short-gap healing may be limited for those files.`;
      $('metadataWarning').hidden = false;
    }
    const data = await selectedPtx.arrayBuffer();
    worker.postMessage({
      type: 'convert', name: selectedPtx.name, data, audio: entries,
      gapMs: Number($('gapHealing').value), playlists: $('playlists').checked,
      verbose: $('verbose').checked
    }, [data]);
  } catch (err) { setError(err.message || String(err)); }
});

$('copyLogBtn').addEventListener('click', async () => {
  const button = $('copyLogBtn');
  try {
    await navigator.clipboard.writeText(logBox.textContent);
    button.textContent = 'Copied!';
  } catch {
    button.textContent = 'Copy unavailable';
  }
});
