// Copyright 2025 Dual Tachyon
// https://github.com/DualTachyon
//
// Web Serial interface by Joel Calado
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
//     Unless required by applicable law or agreed to in writing, software
//     distributed under the License is distributed on an "AS IS" BASIS,
//     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//     See the License for the specific language governing permissions and
//     limitations under the License.

'use strict';

// ----------------------------------------------------------------------------
// Flashing protocol (UPDATE / WRITE)
// ----------------------------------------------------------------------------
const BASE_ADDRESS = 0x08004000;
const BAUDRATE = 4000000;
const CHUNK = 32;
const FLASH_TIMEOUT = 1000;      // ms

// Firmware size sanity bounds (see the Python GUI for rationale).
const FW_MIN_SIZE = 16 * 1024;
const FW_MAX_SIZE = 4 * 1024 * 1024;

// ----------------------------------------------------------------------------
// Calibration backup protocol (PROGRAM / READ)
// ----------------------------------------------------------------------------
const CALIB_BAUD = 921600;
const CALIB_TIMEOUT = 3000;      // ms, reset whenever data arrives
const READ_CHUNK = 128;
const EXPECTED_DEVICE_ID = new Uint8Array([
  ...new TextEncoder().encode('ID168UV'), 0x00,
  ...new TextEncoder().encode('V100'), 0x00, 0x00, 0x06,
]);
const READ_REGIONS = [
  [0x02F60000, 2048, 'calibration'],
  [0x02FA0000, 512, 'device-info'],
];

// ----------------------------------------------------------------------------
// License install protocol (PROGRAM / WRITE)
// ----------------------------------------------------------------------------
const LICENSE_BAUD = 4000000;
const LICENSE_SIZE = 96;
const LICENSE_ADDRESS = 0x2EC0000;
const LICENSE_CHUNK = 16;
const LICENSE_TIMEOUT = 1000;    // ms

const ACK = 0x06;

class Cancelled extends Error {}
class Timeout extends Error {}

// ----------------------------------------------------------------------------
// Shared state
// ----------------------------------------------------------------------------
const state = {
  port: null,
  firmware: null,     // {name, data: Uint8Array}
  license: null,      // {name, data: Uint8Array}
  busy: false,
  cancel: false,      // set by any Cancel button; checked by the running job
};

const $ = (id) => document.getElementById(id);

function log(msg, level = 'info') {
  const stamp = new Date().toTimeString().slice(0, 8);
  const line = document.createElement('div');
  line.className = level;
  line.textContent = `[${stamp}] ${msg}`;
  const el = $('log');
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

const hex8 = (n) => '0x' + (n >>> 0).toString(16).toUpperCase().padStart(8, '0');
const hex2 = (n) => '0x' + n.toString(16).toUpperCase().padStart(2, '0');

function fmtEta(seconds) {
  seconds = Math.floor(seconds);
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`;
  return `${seconds}s`;
}

const sum = (bytes) => bytes.reduce((a, b) => a + b, 0);
const ascii = (s) => new TextEncoder().encode(s);

// ----------------------------------------------------------------------------
// Serial link: wraps a Web Serial port with a buffered, timeout-aware reader.
//
// Web Serial only gives us a stream of arbitrarily sized chunks, so a
// background loop drains it into a byte buffer and recv(n) waits for enough
// bytes to arrive (or the timeout / cancel flag to fire).
// ----------------------------------------------------------------------------
class Link {
  constructor(port) {
    this.port = port;
    this.buf = new Uint8Array(0);
    this.waiter = null;
    this.error = null;
  }

  async open(baudRate) {
    await this.port.open({ baudRate, dataBits: 8, parity: 'none', stopBits: 1, bufferSize: 65536 });
    this.writer = this.port.writable.getWriter();
    this.reader = this.port.readable.getReader();
    this.loop = this.readLoop();
  }

  async readLoop() {
    try {
      for (;;) {
        const { value, done } = await this.reader.read();
        if (done) break;
        if (value && value.length) {
          const merged = new Uint8Array(this.buf.length + value.length);
          merged.set(this.buf);
          merged.set(value, this.buf.length);
          this.buf = merged;
          this.wake();
        }
      }
    } catch (e) {
      this.error = e;
      this.wake();
    }
  }

  wake() {
    if (this.waiter) {
      const w = this.waiter;
      this.waiter = null;
      w();
    }
  }

  async send(bytes) {
    await this.writer.write(bytes);
  }

  // Read exactly n bytes. The timeout restarts whenever new data arrives,
  // matching the Python backup worker's behaviour.
  async recv(n, timeoutMs) {
    let deadline = performance.now() + timeoutMs;
    let have = this.buf.length;
    while (this.buf.length < n) {
      if (state.cancel) throw new Cancelled();
      if (this.error) throw this.error;
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new Timeout(`Timeout waiting for ${n} bytes; got ${this.buf.length}`);
      await new Promise((resolve) => {
        const t = setTimeout(() => { this.waiter = null; resolve(); }, Math.min(remaining, 100));
        this.waiter = () => { clearTimeout(t); resolve(); };
      });
      if (this.buf.length > have) {
        have = this.buf.length;
        deadline = performance.now() + timeoutMs;
      }
    }
    const out = this.buf.slice(0, n);
    this.buf = this.buf.slice(n);
    return out;
  }

  // Like recv(), but returns null instead of throwing on timeout.
  async tryRecv(n, timeoutMs) {
    try {
      return await this.recv(n, timeoutMs);
    } catch (e) {
      if (e instanceof Timeout) return null;
      throw e;
    }
  }

  async close() {
    try { await this.reader.cancel(); } catch (e) { /* ignore */ }
    try { await this.loop; } catch (e) { /* ignore */ }
    try { this.reader.releaseLock(); } catch (e) { /* ignore */ }
    try { this.writer.releaseLock(); } catch (e) { /* ignore */ }
    try { await this.port.close(); } catch (e) { /* ignore */ }
  }
}

const equalBytes = (a, b) => a && a.length === b.length && a.every((v, i) => v === b[i]);

// ----------------------------------------------------------------------------
// Port selection
// ----------------------------------------------------------------------------
// Common USB-serial bridge vendors, for the description under the combo.
const USB_VENDORS = {
  0x0403: 'FTDI',
  0x067b: 'Prolific',
  0x10c4: 'Silicon Labs CP210x',
  0x1a86: 'WCH CH34x',
  0x0483: 'STMicroelectronics',
};

// Granted ports, in the order shown in the combo.
let ports = [];

function describePort(port) {
  const info = port.getInfo();
  if (info.usbVendorId === undefined) return 'Serial port';
  const h = (n) => n.toString(16).padStart(4, '0');
  return `USB serial ${h(info.usbVendorId)}:${h(info.usbProductId)}`;
}

function portDetail(port) {
  const info = port.getInfo();
  if (info.usbVendorId === undefined) return 'Bluetooth or built-in serial port';
  const vendor = USB_VENDORS[info.usbVendorId];
  return vendor ? `${vendor} USB serial adapter` : 'USB serial adapter';
}

function setPort(port) {
  state.port = port;
  $('port-select').value = port ? String(ports.indexOf(port)) : '';
  $('port-desc').textContent = port ? portDetail(port) : '';
  updateControls();
}

// Rebuilds the combo from the ports this site has been granted, keeping the
// current selection when it is still present.
async function refreshPorts() {
  ports = await navigator.serial.getPorts();
  const select = $('port-select');
  select.replaceChildren();
  if (!ports.length) {
    select.add(new Option('(no port selected)', ''));
  }
  ports.forEach((port, i) => {
    const dupes = ports.filter((p) => describePort(p) === describePort(port));
    const suffix = dupes.length > 1 ? ` #${dupes.indexOf(port) + 1}` : '';
    select.add(new Option(describePort(port) + suffix, String(i)));
  });
  if (state.busy) return;
  setPort(ports.includes(state.port) ? state.port : ports[0] || null);
}

async function selectPort() {
  try {
    const port = await navigator.serial.requestPort();
    await refreshPorts();
    setPort(port);
    log(`Selected ${describePort(port)}.`);
  } catch (e) {
    if (e.name !== 'NotFoundError') log(`Could not select port: ${e.message}`, 'error');
  }
}

// ----------------------------------------------------------------------------
// Control enable/disable (shared lock)
// ----------------------------------------------------------------------------
function updateControls() {
  const busy = state.busy;
  const portOk = !!state.port;
  $('port-button').disabled = busy;
  $('port-select').disabled = busy || !ports.length;
  $('fw-file').disabled = busy;
  $('fw-button').disabled = busy || !portOk || !state.firmware;
  $('calib-prefix').disabled = busy;
  $('calib-button').disabled = busy || !portOk;
  $('lic-file').disabled = busy;
  $('lic-button').disabled = busy || !portOk || !state.license;
}

// Wraps a long-running job: takes the shared lock, toggles the job's cancel
// button, and always releases everything when the job finishes.
async function runJob(cancelId, job) {
  if (state.busy) return;
  state.busy = true;
  state.cancel = false;
  $(cancelId).disabled = false;
  updateControls();
  try {
    await job();
  } finally {
    state.busy = false;
    state.cancel = false;
    $(cancelId).disabled = true;
    updateControls();
  }
}

async function openLink(baud) {
  const link = new Link(state.port);
  try {
    await link.open(baud);
  } catch (e) {
    log(`Error opening serial port: ${e.message}`, 'error');
    return null;
  }
  return link;
}

// ----------------------------------------------------------------------------
// File selection
// ----------------------------------------------------------------------------
async function readFile(input) {
  const file = input.files[0];
  if (!file) return null;
  return { name: file.name, data: new Uint8Array(await file.arrayBuffer()) };
}

async function onFirmwareSelected() {
  let fw;
  try {
    fw = await readFile($('fw-file'));
  } catch (e) {
    log(`Error loading firmware: ${e.message}`, 'error');
    return;
  }
  state.firmware = null;
  $('fw-label').textContent = '(none selected)';
  if (!fw) { updateControls(); return; }

  const size = fw.data.length;
  let warned = false;
  if (size === 0) {
    log('Firmware file is empty - cannot flash.', 'error');
    $('fw-label').textContent = '(invalid file)';
    updateControls();
    return;
  }
  if (size < FW_MIN_SIZE) { log(`Firmware is only ${size} bytes - is this the right file?`, 'warn'); warned = true; }
  if (size > FW_MAX_SIZE) { log(`Firmware is ${size} bytes - larger than expected, check the file.`, 'warn'); warned = true; }

  state.firmware = fw;
  $('fw-label').textContent = `${fw.name} (${size} bytes)`;
  if (!warned) log(`Loaded firmware "${fw.name}" (${size} bytes).`, 'ok');
  updateControls();
}

async function onLicenseSelected() {
  let lic;
  try {
    lic = await readFile($('lic-file'));
  } catch (e) {
    log(`Error loading license: ${e.message}`, 'error');
    return;
  }
  state.license = null;
  $('lic-label').textContent = '(none selected)';
  if (!lic) { updateControls(); return; }

  if (lic.data.length !== LICENSE_SIZE) {
    log(`Invalid license file: ${lic.data.length} bytes (expected ${LICENSE_SIZE}).`, 'error');
    $('lic-label').textContent = '(invalid file)';
    updateControls();
    return;
  }
  state.license = lic;
  $('lic-label').textContent = `${lic.name} (${lic.data.length} bytes)`;
  log(`Loaded license "${lic.name}" (${lic.data.length} bytes).`, 'ok');
  updateControls();
}

// Shows the in-page confirmation modal; resolves true when "Flash" is chosen.
function confirmDialog(text) {
  const dialog = $('confirm-dialog');
  $('confirm-text').textContent = text;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
  });
}

// ----------------------------------------------------------------------------
// Flashing
// ----------------------------------------------------------------------------
async function flash() {
  const fw = state.firmware;
  const ok = await confirmDialog(
    `This will OVERWRITE the firmware on:\n\n` +
    `    Device:    ${describePort(state.port)}\n` +
    `    Firmware:  ${fw.name}\n` +
    `    Size:      ${fw.data.length} bytes\n\n` +
    `A failed or interrupted flash can brick the radio.\n` +
    `Keep the cable connected until it finishes.`);
  if (!ok) return;

  await runJob('fw-cancel', async () => {
    const progress = $('fw-progress');
    const status = $('fw-status');
    progress.value = 0;
    status.textContent = '';
    log(`Starting flash at ${BAUDRATE} baud...`);

    const link = await openLink(BAUDRATE);
    if (!link) return;
    try {
      log('Entering update mode...');
      await link.send(ascii('UPDATE'));
      const r = await link.tryRecv(1, FLASH_TIMEOUT);
      if (!r || r[0] !== ACK) {
        log('Failed to start update mode!', 'error');
        return;
      }
      log('Update mode acknowledged.', 'ok');

      // Pad to a multiple of the chunk size.
      const total = Math.ceil(fw.data.length / CHUNK) * CHUNK;
      const firmware = new Uint8Array(total);
      firmware.set(fw.data);

      const start = performance.now();
      let lastUi = 0;
      for (let offset = 0; offset < total; offset += CHUNK) {
        if (state.cancel) {
          log('Flashing cancelled by user.', 'warn');
          return;
        }
        const address = BASE_ADDRESS + offset;

        // 0x01 | addr (LE u32) | 32 data bytes | checksum (LE u16) | 0x06
        const packet = new Uint8Array(1 + 4 + CHUNK + 2 + 1);
        const view = new DataView(packet.buffer);
        packet[0] = 0x01;
        view.setUint32(1, address, true);
        packet.set(firmware.subarray(offset, offset + CHUNK), 5);
        view.setUint16(5 + CHUNK, sum(packet.subarray(1, 5 + CHUNK)), true);
        packet[packet.length - 1] = ACK;

        await link.send(packet);
        const ack = await link.tryRecv(1, FLASH_TIMEOUT);
        if (!ack || ack[0] !== ACK) {
          log(`Failed to update at ${hex8(address)}!`, 'error');
          return;
        }

        // Throttle UI updates.
        const done = offset + CHUNK;
        const now = performance.now();
        if (now - lastUi >= 100 || done === total) {
          lastUi = now;
          const elapsed = (now - start) / 1000;
          const rate = elapsed > 0 ? done / elapsed : 0;
          const eta = rate > 0 ? (total - done) / rate : 0;
          progress.value = done / total;
          status.textContent =
            `${(done / total * 100).toFixed(1)}% (${done}/${total})  ` +
            `${hex8(BASE_ADDRESS + done)}  ${(rate / 1024).toFixed(1)} KB/s  ETA ${fmtEta(eta)}`;
        }
      }

      // Leave update mode.
      await link.send(new Uint8Array([0x18]));

      const elapsed = (performance.now() - start) / 1000;
      status.textContent = 'Complete';
      log(`Update complete! Wrote ${total} bytes in ${fmtEta(elapsed)}.`, 'ok');
    } catch (e) {
      if (e instanceof Cancelled) log('Flashing cancelled by user.', 'warn');
      else log(`Flash failed: ${e.message}`, 'error');
    } finally {
      await link.close();
    }
  });
}

// ----------------------------------------------------------------------------
// Calibration backup
// ----------------------------------------------------------------------------
async function calibReadBlock(link, addr, length) {
  const cmd = new Uint8Array(6);
  cmd[0] = 0x52; // 'R'
  new DataView(cmd.buffer).setUint32(1, addr, false);
  cmd[5] = length & 0xFF;
  await link.send(cmd);

  // 'W' | addr (BE u32) | len | data | checksum | 0x06
  const resp = await link.recv(8 + length, CALIB_TIMEOUT);
  if (resp[0] !== 0x57) throw new Error(`Expected 'W' (0x57), got ${hex2(resp[0])}`);
  const respAddr = new DataView(resp.buffer).getUint32(1, false);
  const respLen = resp[5];
  if (respAddr !== addr || respLen !== length) {
    throw new Error(`Address/length mismatch: expected ${hex8(addr)}/${length}, got ${hex8(respAddr)}/${respLen}`);
  }
  if (resp[resp.length - 1] !== ACK) throw new Error('Missing ACK after data block');
  const cksum = sum(resp.subarray(1, resp.length - 2)) & 0xFF;
  if (cksum !== resp[resp.length - 2]) {
    throw new Error(`Checksum mismatch at ${hex8(addr)}: got ${hex2(cksum)}, expected ${hex2(resp[resp.length - 2])}`);
  }
  return resp.slice(6, 6 + length);
}

function download(name, data) {
  const url = URL.createObjectURL(new Blob([data], { type: 'application/octet-stream' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function backup() {
  await runJob('calib-cancel', async () => {
    const progress = $('calib-progress');
    const status = $('calib-status');
    progress.value = 0;
    status.textContent = '';

    const link = await openLink(CALIB_BAUD);
    if (!link) return;
    const regions = {};
    try {
      log('Entering programming mode...');
      await link.send(ascii('PROGRAM'));
      if (!equalBytes(await link.recv(3, CALIB_TIMEOUT), [0x51, 0x58, ACK])) {
        log('Failed to start programming mode!', 'error');
        return;
      }
      log('Programming mode acknowledged.', 'ok');

      // Verify we are talking to the expected radio before trusting any data.
      await link.send(new Uint8Array([0x02]));
      const idbuf = await link.recv(16, CALIB_TIMEOUT);
      const deviceId = new Uint8Array([...idbuf.subarray(0, 8), ...idbuf.subarray(9)]);
      if (!equalBytes(deviceId, EXPECTED_DEVICE_ID)) {
        log(`Device ID mismatch, aborting. Got ${JSON.stringify(String.fromCharCode(...deviceId))}`, 'error');
        return;
      }
      log('Device ID verified.', 'ok');

      const total = READ_REGIONS.reduce((a, [, size]) => a + size, 0);
      let done = 0;
      let lastUi = 0;
      for (const [base, size, name] of READ_REGIONS) {
        log(`Reading ${name} region ${hex8(base)} (${size} bytes)...`);
        const buf = new Uint8Array(size);
        for (let off = 0; off < size;) {
          if (state.cancel) throw new Cancelled();
          const n = Math.min(READ_CHUNK, size - off);
          buf.set(await calibReadBlock(link, base + off, n), off);
          off += n;
          done += n;

          const now = performance.now();
          if (now - lastUi >= 100 || done === total) {
            lastUi = now;
            progress.value = done / total;
            status.textContent = `${(done / total * 100).toFixed(1)}%  ${name}: ${off}/${size} bytes`;
          }
        }
        regions[name] = buf;
      }

      await link.send(ascii('END'));
      const r = await link.tryRecv(1, CALIB_TIMEOUT);
      if (!r || r[0] !== ACK) log('Unexpected response ending session.', 'warn');
    } catch (e) {
      if (e instanceof Cancelled) log('Backup cancelled by user.', 'warn');
      else log(`Backup failed: ${e.message}`, 'error');
      return;
    } finally {
      await link.close();
    }

    // Download one file per region.
    const prefix = $('calib-prefix').value.trim() || 'calibration';
    for (const [, , name] of READ_REGIONS) {
      const file = `${prefix}-${name}.bin`;
      download(file, regions[name]);
      log(`Saved ${file}`, 'ok');
    }
    status.textContent = `Backup complete, ${READ_REGIONS.length} file(s) saved.`;
    log('Calibration backup complete.', 'ok');
  });
}

// ----------------------------------------------------------------------------
// License install
// ----------------------------------------------------------------------------
async function installLicense() {
  const lic = state.license;
  await runJob('lic-cancel', async () => {
    const progress = $('lic-progress');
    const status = $('lic-status');
    progress.value = 0;
    status.textContent = '';

    const link = await openLink(LICENSE_BAUD);
    if (!link) return;
    try {
      log('Entering programming mode...');
      await link.send(ascii('PROGRAM'));
      if (!equalBytes(await link.tryRecv(3, LICENSE_TIMEOUT), [0x51, 0x58, ACK])) {
        log('Failed to start programming mode!', 'error');
        return;
      }
      log('Programming mode acknowledged.', 'ok');

      log('Writing license...');
      const total = lic.data.length;
      const start = performance.now();
      for (let offset = 0; offset < total;) {
        if (state.cancel) {
          log('License install cancelled by user.', 'warn');
          return;
        }
        const chunk = Math.min(total - offset, LICENSE_CHUNK);
        const address = LICENSE_ADDRESS + offset;

        // 'W' | addr (BE u32) | len | data | checksum | 0x06
        const packet = new Uint8Array(1 + 4 + 1 + chunk + 2);
        packet[0] = 0x57;
        new DataView(packet.buffer).setUint32(1, address, false);
        packet[5] = chunk;
        packet.set(lic.data.subarray(offset, offset + chunk), 6);
        packet[6 + chunk] = sum(packet.subarray(1, 6 + chunk)) & 0xFF;
        packet[7 + chunk] = ACK;

        await link.send(packet);
        const ack = await link.tryRecv(1, LICENSE_TIMEOUT);
        if (!ack || ack[0] !== ACK) {
          log(`Failed to write license at ${hex8(address)}!`, 'error');
          return;
        }

        offset += chunk;
        progress.value = offset / total;
        status.textContent = `${(offset / total * 100).toFixed(1)}% (${offset}/${total})`;
      }

      await link.send(ascii('END'));

      const elapsed = (performance.now() - start) / 1000;
      status.textContent = 'Complete';
      log(`License install complete! Wrote ${total} bytes in ${fmtEta(elapsed)}.`, 'ok');
    } catch (e) {
      if (e instanceof Cancelled) log('License install cancelled by user.', 'warn');
      else log(`License install failed: ${e.message}`, 'error');
    } finally {
      await link.close();
    }
  });
}

// ----------------------------------------------------------------------------
// Wiring
// ----------------------------------------------------------------------------
// Tab bar: click or arrow keys switch panes.
function initTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (tab) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute('aria-selected', on);
      t.tabIndex = on ? 0 : -1;
      $(t.getAttribute('aria-controls')).hidden = !on;
    }
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step) return;
      const next = tabs[(i + step + tabs.length) % tabs.length];
      select(next);
      next.focus();
    });
  });
}

async function init() {
  initTabs();
  $('log-clear').addEventListener('click', () => { $('log').textContent = ''; });

  if (!('serial' in navigator)) {
    $('unsupported').hidden = false;
    $('port-button').disabled = true;
    $('port-select').disabled = true;
    log('Web Serial is not available. Use Chrome or Edge over https or localhost.', 'error');
    return;
  }

  $('port-button').addEventListener('click', selectPort);
  $('port-select').addEventListener('change', (e) => {
    const port = ports[Number(e.target.value)];
    setPort(port || null);
    if (port) log(`Selected ${describePort(port)}.`);
  });
  $('fw-file').addEventListener('change', onFirmwareSelected);
  $('lic-file').addEventListener('change', onLicenseSelected);
  $('fw-button').addEventListener('click', flash);
  $('calib-button').addEventListener('click', backup);
  $('lic-button').addEventListener('click', installLicense);
  for (const id of ['fw-cancel', 'calib-cancel', 'lic-cancel']) {
    $(id).addEventListener('click', () => { state.cancel = true; });
  }

  // Granted ports come and go as cables are plugged in and out.
  navigator.serial.addEventListener('connect', () => refreshPorts());
  navigator.serial.addEventListener('disconnect', (e) => {
    if (e.target === state.port) log('Serial port disconnected.', 'warn');
    refreshPorts();
  });

  await refreshPorts();
  log('Ready. Select a serial port, then flash firmware or back up calibration.');
  updateControls();
}

init();
