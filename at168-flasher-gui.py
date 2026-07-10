#!/usr/bin/env python3

# Copyright 2025 Dual Tachyon
# https://github.com/DualTachyon
#
# DearPyGui interface by Joel Calado
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

import os
import re
import threading
import time
from struct import pack

import dearpygui.dearpygui as dpg
import serial
import serial.tools.list_ports

# ----------------------------------------------------------------------------
# Flashing protocol (UPDATE / WRITE)
# ----------------------------------------------------------------------------
BASE_ADDRESS = 0x08004000
BAUDRATE = 4000000
CHUNK = 32

# Firmware size sanity bounds. The image is written into the STM32 flash
# starting at BASE_ADDRESS, so anything tiny is almost certainly the wrong
# file and anything huge won't fit the expected region.
FW_MIN_SIZE = 16 * 1024          # 16 KiB
FW_MAX_SIZE = 4 * 1024 * 1024    # 4 MiB

# ----------------------------------------------------------------------------
# Calibration backup protocol (PROGRAM / READ)
# ----------------------------------------------------------------------------
CALIB_BAUD = 921600
CALIB_TIMEOUT = 3.0
READ_CHUNK = 128
EXPECTED_DEVICE_ID = b"ID168UV\x00V100\x00\x00\x06"
READ_REGIONS = [
    (0x02F60000, 2048, 'calibration'),
    (0x02FA0000,  512, 'device-info'),
]

# ----------------------------------------------------------------------------
# License install protocol (PROGRAM / WRITE)
# ----------------------------------------------------------------------------
LICENSE_BAUD = 4000000
LICENSE_SIZE = 96               # license blobs are exactly 96 bytes
LICENSE_ADDRESS = 0x2EC0000
LICENSE_CHUNK = 16


class _Cancelled(Exception):
    """Raised inside the backup worker when the user cancels."""


# ----------------------------------------------------------------------------
# Shared state between the UI and the worker threads.
# ----------------------------------------------------------------------------
state = {
    'firmware_path': None,
    'firmware': None,
    'license_path': None,
    'license': None,
    'busy': False,        # any long operation is running (locks shared controls)
    'flashing': False,    # flash loop active    (also the cancel flag)
    'backuping': False,   # backup loop active   (also the cancel flag)
    'licensing': False,   # license loop active  (also the cancel flag)
    'calib_dir': os.getcwd(),
    'worker': None,
}

# Maps a serial device path to its human-readable description.
port_info = {}


def log(msg, level='info'):
    """Append a timestamped line to the log window (thread-safe enough for dpg)."""
    colors = {
        'info': (220, 220, 220),
        'ok': (120, 220, 120),
        'warn': (230, 200, 90),
        'error': (240, 110, 110),
    }
    stamp = time.strftime('%H:%M:%S')
    dpg.add_text(f'[{stamp}] {msg}', color=colors.get(level, colors['info']),
                 parent='log_window', wrap=0)
    # Auto-scroll to the bottom.
    dpg.set_y_scroll('log_window', -1.0)


# ----------------------------------------------------------------------------
# Serial port discovery
# ----------------------------------------------------------------------------
def list_ports():
    """Return the candidate serial ports, preferring real USB serial devices.

    On Linux, comports() also reports the phantom /dev/ttyS0..31 legacy UARTs
    that the kernel always enumerates whether or not anything is attached. USB
    serial adapters expose a USB VID, so we prefer those; otherwise we still
    drop the legacy /dev/ttyS* noise so an empty list means "nothing plugged in".
    """
    ports = serial.tools.list_ports.comports()
    usb = [p for p in ports if p.vid is not None]
    if usb:
        return usb
    return [p for p in ports if not re.match(r'/dev/ttyS\d+$', p.device)]


def refresh_ports(sender=None, app_data=None, quiet=False):
    global port_info
    ports = list_ports()
    port_info = {p.device: (p.description or 'Unknown device') for p in ports}
    devices = list(port_info.keys())

    if devices:
        dpg.configure_item('port_combo', items=devices)
        current = dpg.get_value('port_combo')
        if current not in devices:
            dpg.set_value('port_combo', devices[0])
        update_port_desc()
        if not quiet:
            log(f'Found {len(devices)} serial port(s).')
    else:
        dpg.configure_item('port_combo', items=[])
        dpg.set_value('port_combo', '')
        dpg.set_value('port_desc_label', '')
        log('No serial ports found.', 'warn')
    update_controls()


def update_port_desc(sender=None, app_data=None):
    dev = dpg.get_value('port_combo')
    desc = port_info.get(dev, '')
    dpg.set_value('port_desc_label', desc)
    update_controls()


# ----------------------------------------------------------------------------
# Control enable/disable (shared lock)
# ----------------------------------------------------------------------------
def update_controls():
    """Reflect the current busy/readiness state onto every interactive item."""
    busy = state['busy']
    port_ok = bool(dpg.get_value('port_combo'))

    # Shared controls
    dpg.configure_item('refresh_button', enabled=not busy)
    dpg.configure_item('port_combo', enabled=not busy)

    # Flash tab
    dpg.configure_item('browse_button', enabled=not busy)
    dpg.configure_item('flash_button', enabled=(
        not busy and state['firmware'] is not None and port_ok))

    # Backup tab
    dpg.configure_item('calib_folder_button', enabled=not busy)
    dpg.configure_item('calib_button', enabled=(not busy and port_ok))

    # License tab
    dpg.configure_item('license_browse_button', enabled=not busy)
    dpg.configure_item('license_button', enabled=(
        not busy and state['license'] is not None and port_ok))


def show_file_dialog(tag):
    """Open one of the internal file dialogs, sized to fit the current window.

    DearPyGui file dialogs are drawn *inside* the viewport rather than as a
    native OS dialog, so a fixed size can overflow the app window - notably on
    high-DPI displays (e.g. 200-250% scaling), where the dialog's own controls
    (the OK/Cancel buttons, path bar) end up clipped and unreachable. Fit the
    dialog to the live viewport client area every time it opens so it works at
    any scale and after the user resizes the window.
    """
    vw = dpg.get_viewport_client_width()
    vh = dpg.get_viewport_client_height()
    dpg.configure_item(tag, width=max(320, int(vw * 0.92)),
                       height=max(240, int(vh * 0.85)))
    dpg.show_item(tag)


# ----------------------------------------------------------------------------
# Firmware selection
# ----------------------------------------------------------------------------
def check_firmware(data):
    """Return a list of (message, level) sanity warnings for the firmware."""
    warnings = []
    size = len(data)
    if size == 0:
        warnings.append(('Firmware file is empty - cannot flash.', 'error'))
    elif size < FW_MIN_SIZE:
        warnings.append(
            (f'Firmware is only {size} bytes - is this the right file?', 'warn'))
    if size > FW_MAX_SIZE:
        warnings.append(
            (f'Firmware is {size} bytes - larger than expected, check the file.',
             'warn'))
    return warnings


def on_firmware_selected(sender, app_data):
    # app_data['file_path_name'] usually holds the chosen file, but depending
    # on how the dialog is dismissed (double-click vs. OK) it can come back as
    # just the directory. Fall back to the 'selections' map, which holds the
    # actual highlighted file(s) as {name: full_path}.
    path = app_data.get('file_path_name')
    if not path or not os.path.isfile(path):
        selections = app_data.get('selections') or {}
        path = next(iter(selections.values()), None)
    if not path or not os.path.isfile(path):
        log('Could not determine the selected firmware file.', 'warn')
        return
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception as e:
        log(f'Error loading firmware: {e}', 'error')
        return

    warnings = check_firmware(data)
    for msg, level in warnings:
        log(msg, level)

    # An empty file is unusable; refuse to arm the flash button for it.
    if len(data) == 0:
        state['firmware_path'] = None
        state['firmware'] = None
        dpg.set_value('firmware_label', '(invalid file)')
        update_controls()
        return

    state['firmware_path'] = path
    state['firmware'] = data
    dpg.set_value('firmware_label', f'{os.path.basename(path)}  ({len(data)} bytes)')
    if not warnings:
        log(f'Loaded firmware "{os.path.basename(path)}" ({len(data)} bytes).', 'ok')
    update_controls()


# ----------------------------------------------------------------------------
# License selection
# ----------------------------------------------------------------------------
def on_license_selected(sender, app_data):
    # See on_firmware_selected for why we fall back to the 'selections' map.
    path = app_data.get('file_path_name')
    if not path or not os.path.isfile(path):
        selections = app_data.get('selections') or {}
        path = next(iter(selections.values()), None)
    if not path or not os.path.isfile(path):
        log('Could not determine the selected license file.', 'warn')
        return
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception as e:
        log(f'Error loading license: {e}', 'error')
        return

    if len(data) != LICENSE_SIZE:
        log(f'Invalid license file: {len(data)} bytes '
            f'(expected {LICENSE_SIZE}).', 'error')
        state['license_path'] = None
        state['license'] = None
        dpg.set_value('license_label', '(invalid file)')
        update_controls()
        return

    state['license_path'] = path
    state['license'] = data
    dpg.set_value('license_label', f'{os.path.basename(path)}  ({len(data)} bytes)')
    log(f'Loaded license "{os.path.basename(path)}" ({len(data)} bytes).', 'ok')
    update_controls()


# ----------------------------------------------------------------------------
# Flashing worker
# ----------------------------------------------------------------------------
def fmt_eta(seconds):
    seconds = int(seconds)
    if seconds >= 60:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    return f'{seconds}s'


def flash_worker(port, firmware):
    try:
        try:
            s = serial.Serial(port=port, baudrate=BAUDRATE, timeout=1)
        except Exception as e:
            log(f'Error opening serial port {port}: {e}', 'error')
            return

        log('Entering update mode...')
        s.write(b'UPDATE')
        if s.read() != b'\x06':
            log('Failed to start update mode!', 'error')
            s.close()
            return
        log('Update mode acknowledged.', 'ok')

        # Pad to a multiple of the chunk size.
        if len(firmware) % CHUNK > 0:
            firmware += b'\x00' * (CHUNK - (len(firmware) % CHUNK))

        total = len(firmware)
        offset = 0
        start = time.time()
        last_ui = 0.0
        while offset != total:
            if not state['flashing']:
                log('Flashing cancelled by user.', 'warn')
                s.close()
                return

            body = pack('<I', BASE_ADDRESS + offset) + firmware[offset:offset + CHUNK]
            checksum = sum(body)
            packet = b'\x01' + body + pack('<H', checksum) + b'\x06'
            s.write(packet)
            if s.read() != b'\x06':
                log(f'Failed to update at {BASE_ADDRESS + offset:08X}!', 'error')
                s.close()
                return

            offset += CHUNK

            # Throttle UI updates so we don't spend the flash redrawing.
            now = time.time()
            if now - last_ui >= 0.1 or offset == total:
                last_ui = now
                frac = offset / total
                elapsed = now - start
                rate = offset / elapsed if elapsed > 0 else 0          # bytes/s
                eta = (total - offset) / rate if rate > 0 else 0       # seconds
                dpg.set_value('progress_bar', frac)
                dpg.configure_item('progress_bar',
                                   overlay=f'{frac * 100:0.1f}%  ({offset}/{total})')
                dpg.set_value(
                    'addr_label',
                    f'0x{BASE_ADDRESS + offset:08X}   '
                    f'{rate / 1024:0.1f} KB/s   ETA {fmt_eta(eta)}')

        # Leave update mode.
        s.write(b'\x18')
        s.close()

        elapsed = time.time() - start
        dpg.configure_item('progress_bar', overlay='Complete')
        dpg.set_value('addr_label', '')
        log(f'Update complete! Wrote {total} bytes in {fmt_eta(elapsed)}.', 'ok')

    finally:
        state['flashing'] = False
        state['busy'] = False
        dpg.configure_item('flash_button', label='Flash Firmware')
        dpg.configure_item('cancel_button', enabled=False)
        update_controls()


# ----------------------------------------------------------------------------
# Calibration backup worker  (ported from at-calib.py)
# ----------------------------------------------------------------------------
def _calib_send(s, data):
    s.write(data)
    s.flush()


def _calib_recv(s, n):
    buf = b''
    deadline = time.monotonic() + CALIB_TIMEOUT
    while len(buf) < n:
        if not state['backuping']:
            raise _Cancelled()
        if time.monotonic() > deadline:
            raise TimeoutError(f'Timeout waiting for {n} bytes; got {len(buf)}')
        chunk = s.read(n - len(buf))
        if chunk:
            buf += chunk
            deadline = time.monotonic() + CALIB_TIMEOUT
    return buf


def _calib_checksum(data):
    return sum(data) & 0xFF


def _calib_read_block(s, addr, length):
    cmd = bytes([
        0x52,
        (addr >> 24) & 0xFF, (addr >> 16) & 0xFF,
        (addr >> 8) & 0xFF, addr & 0xFF,
        length & 0xFF,
    ])
    _calib_send(s, cmd)

    resp = _calib_recv(s, 8 + length)
    if resp[0] != 0x57:
        raise RuntimeError(f"Expected 'W' (0x57), got 0x{resp[0]:02X}")
    resp_addr = int.from_bytes(resp[1:5], 'big')
    resp_len = resp[5]
    if resp_addr != addr or resp_len != length:
        raise RuntimeError(
            f'Address/length mismatch: expected {addr:#010x}/{length}, '
            f'got {resp_addr:#010x}/{resp_len}')
    if resp[-1] != 0x06:
        raise RuntimeError('Missing ACK after data block')
    cksum = _calib_checksum(resp[1:-2])
    if cksum != resp[-2]:
        raise RuntimeError(
            f'Checksum mismatch at {addr:#010x}: '
            f'got 0x{cksum:02X}, expected 0x{resp[-2]:02X}')
    return resp[6:6 + length]


def backup_worker(port, out_dir, prefix):
    s = None
    try:
        try:
            s = serial.Serial(port=port, baudrate=CALIB_BAUD,
                              bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                              stopbits=serial.STOPBITS_ONE, timeout=0.1)
        except Exception as e:
            log(f'Error opening serial port {port}: {e}', 'error')
            return

        log('Entering programming mode...')
        _calib_send(s, b'PROGRAM')
        if _calib_recv(s, 3) != bytes.fromhex('515806'):
            log('Failed to start programming mode!', 'error')
            return
        log('Programming mode acknowledged.', 'ok')

        # Verify we are talking to the expected radio before trusting any data.
        _calib_send(s, bytes([0x02]))
        idbuf = _calib_recv(s, 16)
        device_id = idbuf[:8] + idbuf[9:]
        if device_id != EXPECTED_DEVICE_ID:
            log(f'Device ID mismatch, aborting. Got {device_id!r}', 'error')
            return
        log('Device ID verified.', 'ok')

        total = sum(size for _, size, _ in READ_REGIONS)
        done = 0
        start = time.time()
        last_ui = 0.0
        regions = {}
        for base, size, name in READ_REGIONS:
            log(f'Reading {name} region 0x{base:08X} ({size} bytes)...')
            buf = bytearray()
            addr = base
            off = 0
            while off < size:
                if not state['backuping']:
                    raise _Cancelled()
                n = min(READ_CHUNK, size - off)
                buf += _calib_read_block(s, addr, n)
                off += n
                addr += n
                done += n

                now = time.time()
                if now - last_ui >= 0.1 or done == total:
                    last_ui = now
                    frac = done / total
                    dpg.set_value('calib_progress', frac)
                    dpg.configure_item('calib_progress',
                                       overlay=f'{frac * 100:0.1f}%')
                    dpg.set_value('calib_status', f'{name}: {off}/{size} bytes')
            regions[name] = bytes(buf)

        _calib_send(s, b'END')
        if _calib_recv(s, 1) != b'\x06':
            log('Unexpected response ending session.', 'warn')
        s.close()
        s = None

        # Write one file per region.
        safe_prefix = prefix.strip() or 'calibration'
        written = 0
        for _, _, name in READ_REGIONS:
            path = os.path.join(out_dir, f'{safe_prefix}-{name}.bin')
            with open(path, 'wb') as f:
                f.write(regions[name])
            log(f'Wrote {path}', 'ok')
            written += 1

        dpg.configure_item('calib_progress', overlay='Complete')
        dpg.set_value('calib_status', f'Backup complete, {written} file(s) written.')
        log('Calibration backup complete.', 'ok')

    except _Cancelled:
        log('Backup cancelled by user.', 'warn')
    except Exception as e:
        log(f'Backup failed: {e}', 'error')
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        state['backuping'] = False
        state['busy'] = False
        dpg.configure_item('calib_button', label='Backup calibration')
        dpg.configure_item('calib_cancel_button', enabled=False)
        update_controls()


# ----------------------------------------------------------------------------
# License install worker  (ported from at168-lic.py)
# ----------------------------------------------------------------------------
def license_worker(port, licbin):
    s = None
    try:
        try:
            s = serial.Serial(port=port, baudrate=LICENSE_BAUD, timeout=1)
        except Exception as e:
            log(f'Error opening serial port {port}: {e}', 'error')
            return

        log('Entering programming mode...')
        s.write(b'PROGRAM')
        if s.read(3) != b'QX\x06':
            log('Failed to start programming mode!', 'error')
            return
        log('Programming mode acknowledged.', 'ok')

        log('Writing license...')
        total = len(licbin)
        address = LICENSE_ADDRESS
        offset = 0
        start = time.time()
        while offset < total:
            if not state['licensing']:
                log('License install cancelled by user.', 'warn')
                return

            chunk = min(total - offset, LICENSE_CHUNK)
            packet = b'W' + pack('>IB', address, chunk) + licbin[offset:offset + chunk]
            packet += bytes([sum(packet[1:]) & 0xFF])
            packet += b'\x06'
            s.write(packet)
            if s.read() != b'\x06':
                log(f'Failed to write license at 0x{address:08X}!', 'error')
                return

            offset += chunk
            address += chunk

            frac = offset / total
            dpg.set_value('license_progress', frac)
            dpg.configure_item('license_progress',
                               overlay=f'{frac * 100:0.1f}%  ({offset}/{total})')

        s.write(b'END')

        elapsed = time.time() - start
        dpg.configure_item('license_progress', overlay='Complete')
        log(f'License install complete! Wrote {total} bytes in '
            f'{fmt_eta(elapsed)}.', 'ok')

    except Exception as e:
        log(f'License install failed: {e}', 'error')
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        state['licensing'] = False
        state['busy'] = False
        dpg.configure_item('license_button', label='Install License')
        dpg.configure_item('license_cancel_button', enabled=False)
        update_controls()


# ----------------------------------------------------------------------------
# Confirmation + flash start
# ----------------------------------------------------------------------------
def confirm_flash(sender=None, app_data=None):
    """Validate selection and pop the confirmation modal."""
    if state['busy']:
        return
    port = dpg.get_value('port_combo')
    if not port:
        log('No serial port selected.', 'warn')
        return
    if state['firmware'] is None:
        log('No firmware loaded.', 'warn')
        return

    name = os.path.basename(state['firmware_path'])
    size = len(state['firmware'])
    desc = port_info.get(port, '')
    dpg.set_value(
        'confirm_text',
        f'This will OVERWRITE the firmware on:\n\n'
        f'    Port:      {port}\n'
        f'    Device:    {desc}\n'
        f'    Firmware:  {name}\n'
        f'    Size:      {size} bytes\n\n'
        f'A failed or interrupted flash can brick the radio.\n'
        f'Keep the cable connected until it finishes.\n\n'
        f'Continue?')
    dpg.configure_item('confirm_dialog', show=True)


def confirm_yes(sender=None, app_data=None):
    dpg.configure_item('confirm_dialog', show=False)
    start_flash()


def confirm_no(sender=None, app_data=None):
    dpg.configure_item('confirm_dialog', show=False)


def start_flash():
    if state['busy']:
        return
    port = dpg.get_value('port_combo')

    state['busy'] = True
    state['flashing'] = True
    dpg.set_value('progress_bar', 0.0)
    dpg.configure_item('progress_bar', overlay='0.0%')
    dpg.configure_item('flash_button', label='Flashing...')
    dpg.configure_item('cancel_button', enabled=True)
    update_controls()
    log(f'Starting flash on {port} at {BAUDRATE} baud...', 'info')

    worker = threading.Thread(
        target=flash_worker, args=(port, state['firmware']), daemon=True)
    state['worker'] = worker
    worker.start()


def cancel_flash(sender=None, app_data=None):
    if state['flashing']:
        state['flashing'] = False
        log('Cancelling...', 'warn')


# ----------------------------------------------------------------------------
# Backup start / cancel
# ----------------------------------------------------------------------------
def on_calib_dir(sender, app_data):
    path = app_data.get('file_path_name') or app_data.get('current_path')
    if path and os.path.isdir(path):
        state['calib_dir'] = path
        dpg.set_value('calib_dir_label', path)


def start_backup(sender=None, app_data=None):
    if state['busy']:
        return
    port = dpg.get_value('port_combo')
    if not port:
        log('No serial port selected.', 'warn')
        return
    out_dir = state['calib_dir']
    if not out_dir or not os.path.isdir(out_dir):
        log('Choose a valid output folder first.', 'warn')
        return
    prefix = dpg.get_value('calib_prefix')

    state['busy'] = True
    state['backuping'] = True
    dpg.set_value('calib_progress', 0.0)
    dpg.configure_item('calib_progress', overlay='0.0%')
    dpg.set_value('calib_status', 'Starting...')
    dpg.configure_item('calib_button', label='Backing up...')
    dpg.configure_item('calib_cancel_button', enabled=True)
    update_controls()
    log(f'Starting calibration backup on {port} at {CALIB_BAUD} baud...', 'info')

    worker = threading.Thread(
        target=backup_worker, args=(port, out_dir, prefix), daemon=True)
    state['worker'] = worker
    worker.start()


def cancel_backup(sender=None, app_data=None):
    if state['backuping']:
        state['backuping'] = False
        log('Cancelling...', 'warn')


# ----------------------------------------------------------------------------
# License install start / cancel
# ----------------------------------------------------------------------------
def start_license(sender=None, app_data=None):
    if state['busy']:
        return
    port = dpg.get_value('port_combo')
    if not port:
        log('No serial port selected.', 'warn')
        return
    if state['license'] is None:
        log('No license file loaded.', 'warn')
        return

    state['busy'] = True
    state['licensing'] = True
    dpg.set_value('license_progress', 0.0)
    dpg.configure_item('license_progress', overlay='0.0%')
    dpg.configure_item('license_button', label='Installing...')
    dpg.configure_item('license_cancel_button', enabled=True)
    update_controls()
    log(f'Starting license install on {port} at {LICENSE_BAUD} baud...', 'info')

    worker = threading.Thread(
        target=license_worker, args=(port, state['license']), daemon=True)
    state['worker'] = worker
    worker.start()


def cancel_license(sender=None, app_data=None):
    if state['licensing']:
        state['licensing'] = False
        log('Cancelling...', 'warn')


# ----------------------------------------------------------------------------
# UI construction
# ----------------------------------------------------------------------------
def build_ui():
    dpg.create_context()

    with dpg.file_dialog(
        directory_selector=False, show=False, callback=on_firmware_selected,
        tag='firmware_dialog', width=600, height=400):
        dpg.add_file_extension('.*')
        dpg.add_file_extension('Firmware (*.bin){.bin}', color=(120, 220, 120))

    with dpg.file_dialog(
        directory_selector=True, show=False, callback=on_calib_dir,
        tag='calib_dir_dialog', width=600, height=400):
        pass

    with dpg.file_dialog(
        directory_selector=False, show=False, callback=on_license_selected,
        tag='license_dialog', width=600, height=400):
        dpg.add_file_extension('.*')
        dpg.add_file_extension('License (*.bin){.bin}', color=(120, 220, 120))

    # Confirmation modal (hidden until a flash is requested).
    with dpg.window(label='Confirm flash', modal=True, show=False,
                    tag='confirm_dialog', no_resize=True, no_move=False,
                    width=460, pos=(90, 150)):
        dpg.add_text('', tag='confirm_text')
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label='Flash', width=140, height=30,
                           callback=confirm_yes)
            dpg.add_button(label='Cancel', width=140, height=30,
                           callback=confirm_no)

    with dpg.window(tag='primary'):
        # Title block
        dpg.add_text('Anytone D168UV firmware toolkit',
                     color=(140, 190, 255))
        dpg.add_text('Copyright 2025 Dual Tachyon  -  UI by Joel Calado',
                     color=(150, 150, 150))
        dpg.add_spacer(height=2)
        dpg.add_separator()
        dpg.add_spacer(height=6)

        # Shared device selection
        with dpg.table(header_row=False, borders_innerH=False,
                       borders_outerH=False, borders_innerV=False,
                       borders_outerV=False,
                       policy=dpg.mvTable_SizingStretchProp):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=82)
            dpg.add_table_column()

            with dpg.table_row():
                dpg.add_text('Serial port')
                with dpg.group(horizontal=True):
                    dpg.add_combo([], tag='port_combo', width=-104,
                                  callback=update_port_desc)
                    dpg.add_button(label='Refresh', tag='refresh_button',
                                   width=94, callback=refresh_ports)

            with dpg.table_row():
                dpg.add_text('')
                dpg.add_text('', tag='port_desc_label', color=(150, 150, 150))

        dpg.add_spacer(height=6)

        # Operation tabs
        with dpg.tab_bar():
            # -- Flash firmware --------------------------------------------
            with dpg.tab(label='Flash firmware'):
                dpg.add_spacer(height=4)
                with dpg.table(header_row=False, borders_innerH=False,
                               borders_outerH=False, borders_innerV=False,
                               borders_outerV=False,
                               policy=dpg.mvTable_SizingStretchProp):
                    dpg.add_table_column(width_fixed=True, init_width_or_weight=82)
                    dpg.add_table_column()
                    with dpg.table_row():
                        dpg.add_text('Firmware')
                        with dpg.group(horizontal=True):
                            dpg.add_button(
                                label='Browse...', tag='browse_button', width=94,
                                callback=lambda: show_file_dialog('firmware_dialog'))
                            dpg.add_text('(none selected)', tag='firmware_label',
                                         color=(200, 200, 120))

                dpg.add_spacer(height=6)
                with dpg.group(horizontal=True):
                    dpg.add_button(label='Flash Firmware', tag='flash_button',
                                   callback=confirm_flash, enabled=False,
                                   width=150, height=34)
                    dpg.add_button(label='Cancel', tag='cancel_button',
                                   callback=cancel_flash, enabled=False,
                                   width=90, height=34)

                dpg.add_spacer(height=4)
                dpg.add_progress_bar(tag='progress_bar', default_value=0.0,
                                     width=-1, overlay='')
                dpg.add_text('', tag='addr_label', color=(150, 150, 150))

            # -- Backup calibration ----------------------------------------
            with dpg.tab(label='Backup calibration'):
                dpg.add_spacer(height=4)
                dpg.add_text('Reads the calibration and device-info regions to '
                             '.bin files.', color=(180, 180, 180))
                dpg.add_text('Recommended before flashing custom firmware.',
                             color=(150, 150, 150))
                dpg.add_spacer(height=4)

                with dpg.table(header_row=False, borders_innerH=False,
                               borders_outerH=False, borders_innerV=False,
                               borders_outerV=False,
                               policy=dpg.mvTable_SizingStretchProp):
                    dpg.add_table_column(width_fixed=True, init_width_or_weight=82)
                    dpg.add_table_column()

                    with dpg.table_row():
                        dpg.add_text('Save to')
                        with dpg.group(horizontal=True):
                            dpg.add_button(
                                label='Folder...', tag='calib_folder_button',
                                width=94,
                                callback=lambda: show_file_dialog('calib_dir_dialog'))
                            dpg.add_text(state['calib_dir'], tag='calib_dir_label',
                                         color=(150, 150, 150))

                    with dpg.table_row():
                        dpg.add_text('Prefix')
                        dpg.add_input_text(tag='calib_prefix',
                                           default_value='at168-calib', width=-1)

                dpg.add_spacer(height=6)
                with dpg.group(horizontal=True):
                    dpg.add_button(label='Backup calibration', tag='calib_button',
                                   callback=start_backup, enabled=False,
                                   width=170, height=34)
                    dpg.add_button(label='Cancel', tag='calib_cancel_button',
                                   callback=cancel_backup, enabled=False,
                                   width=90, height=34)

                dpg.add_spacer(height=4)
                dpg.add_progress_bar(tag='calib_progress', default_value=0.0,
                                     width=-1, overlay='')
                dpg.add_text('', tag='calib_status', color=(150, 150, 150))

            # -- Install license -------------------------------------------
            with dpg.tab(label='Install license'):
                dpg.add_spacer(height=4)
                dpg.add_text('Writes a license blob to the radio.',
                             color=(180, 180, 180))
                dpg.add_spacer(height=4)

                with dpg.table(header_row=False, borders_innerH=False,
                               borders_outerH=False, borders_innerV=False,
                               borders_outerV=False,
                               policy=dpg.mvTable_SizingStretchProp):
                    dpg.add_table_column(width_fixed=True, init_width_or_weight=82)
                    dpg.add_table_column()
                    with dpg.table_row():
                        dpg.add_text('License')
                        with dpg.group(horizontal=True):
                            dpg.add_button(
                                label='Browse...', tag='license_browse_button',
                                width=94,
                                callback=lambda: show_file_dialog('license_dialog'))
                            dpg.add_text('(none selected)', tag='license_label',
                                         color=(200, 200, 120))

                dpg.add_spacer(height=6)
                with dpg.group(horizontal=True):
                    dpg.add_button(label='Install License', tag='license_button',
                                   callback=start_license, enabled=False,
                                   width=150, height=34)
                    dpg.add_button(label='Cancel', tag='license_cancel_button',
                                   callback=cancel_license, enabled=False,
                                   width=90, height=34)

                dpg.add_spacer(height=4)
                dpg.add_progress_bar(tag='license_progress', default_value=0.0,
                                     width=-1, overlay='')

        dpg.add_spacer(height=6)
        dpg.add_separator()
        dpg.add_spacer(height=4)

        # Shared log
        dpg.add_text('Log')
        with dpg.child_window(tag='log_window', height=-1, width=-1,
                              border=True):
            pass

    dpg.create_viewport(title='AT168 Flasher', width=600, height=640,
                        min_width=520, min_height=520)
    dpg.setup_dearpygui()
    dpg.set_primary_window('primary', True)
    dpg.show_viewport()

    log('Ready. Select a serial port, then flash firmware or back up calibration.')
    refresh_ports(quiet=True)

    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == '__main__':
    build_ui()
