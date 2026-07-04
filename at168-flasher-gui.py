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

BASE_ADDRESS = 0x08004000
BAUDRATE = 4000000
CHUNK = 32

# Firmware size sanity bounds. The image is written into the STM32 flash
# starting at BASE_ADDRESS, so anything tiny is almost certainly the wrong
# file and anything huge won't fit the expected region.
FW_MIN_SIZE = 16 * 1024          # 16 KiB
FW_MAX_SIZE = 4 * 1024 * 1024    # 4 MiB

# ----------------------------------------------------------------------------
# Shared state between the UI and the worker thread.
# ----------------------------------------------------------------------------
state = {
    'firmware_path': None,
    'firmware': None,
    'flashing': False,
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
    update_flash_button()


def update_port_desc(sender=None, app_data=None):
    dev = dpg.get_value('port_combo')
    desc = port_info.get(dev, '')
    dpg.set_value('port_desc_label', desc)
    update_flash_button()


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
    # app_data['file_path_name'] holds the chosen file.
    path = app_data.get('file_path_name')
    if not path or not os.path.isfile(path):
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
        update_flash_button()
        return

    state['firmware_path'] = path
    state['firmware'] = data
    dpg.set_value('firmware_label', f'{os.path.basename(path)}  ({len(data)} bytes)')
    if not warnings:
        log(f'Loaded firmware "{os.path.basename(path)}" ({len(data)} bytes).', 'ok')
    update_flash_button()


def update_flash_button():
    ready = (
        not state['flashing']
        and state['firmware'] is not None
        and bool(dpg.get_value('port_combo'))
    )
    dpg.configure_item('flash_button', enabled=ready)


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
        dpg.configure_item('flash_button', label='Flash Firmware')
        dpg.configure_item('cancel_button', enabled=False)
        # Re-enable the controls that were locked during flashing.
        dpg.configure_item('refresh_button', enabled=True)
        dpg.configure_item('browse_button', enabled=True)
        dpg.configure_item('port_combo', enabled=True)
        update_flash_button()


# ----------------------------------------------------------------------------
# Confirmation + flash start
# ----------------------------------------------------------------------------
def confirm_flash(sender=None, app_data=None):
    """Validate selection and pop the confirmation modal."""
    if state['flashing']:
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
    if state['flashing']:
        return
    port = dpg.get_value('port_combo')

    state['flashing'] = True
    dpg.set_value('progress_bar', 0.0)
    dpg.configure_item('progress_bar', overlay='0.0%')
    dpg.configure_item('flash_button', enabled=False, label='Flashing...')
    dpg.configure_item('cancel_button', enabled=True)
    # Lock the controls that must not change mid-flash.
    dpg.configure_item('refresh_button', enabled=False)
    dpg.configure_item('browse_button', enabled=False)
    dpg.configure_item('port_combo', enabled=False)
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
# UI construction
# ----------------------------------------------------------------------------
def build_ui():
    dpg.create_context()

    with dpg.file_dialog(
        directory_selector=False, show=False, callback=on_firmware_selected,
        tag='firmware_dialog', width=600, height=400):
        dpg.add_file_extension('.*')
        dpg.add_file_extension('Firmware (*.bin){.bin}', color=(120, 220, 120))

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
        dpg.add_text('Anytone D168UV raw firmware flasher',
                     color=(140, 190, 255))
        dpg.add_text('Copyright 2025 Dual Tachyon - UI by Joel Calado',
                     color=(150, 150, 150))
        dpg.add_separator()

        # Serial port row
        with dpg.group(horizontal=True):
            dpg.add_text('Serial port:')
            dpg.add_combo([], tag='port_combo', width=260,
                          callback=update_port_desc)
            dpg.add_button(label='Refresh', tag='refresh_button',
                           callback=refresh_ports)
        dpg.add_text('', tag='port_desc_label', color=(150, 150, 150),
                     indent=90)

        # Firmware row
        with dpg.group(horizontal=True):
            dpg.add_text('Firmware:')
            dpg.add_button(label='Browse...', tag='browse_button',
                           callback=lambda: dpg.show_item('firmware_dialog'))
            dpg.add_text('(none selected)', tag='firmware_label',
                         color=(200, 200, 120))

        dpg.add_separator()

        # Action buttons
        with dpg.group(horizontal=True):
            dpg.add_button(label='Flash Firmware', tag='flash_button',
                           callback=confirm_flash, enabled=False, width=150,
                           height=32)
            dpg.add_button(label='Cancel', tag='cancel_button',
                           callback=cancel_flash, enabled=False, height=32)

        dpg.add_progress_bar(tag='progress_bar', default_value=0.0,
                             width=-1, overlay='')
        dpg.add_text('', tag='addr_label', color=(150, 150, 150))

        dpg.add_separator()
        dpg.add_text('Log:')
        with dpg.child_window(tag='log_window', height=-1, width=-1,
                              border=True):
            pass

    dpg.create_viewport(title='AT168 Flasher', width=640, height=580,
                        min_width=520, min_height=460)
    dpg.setup_dearpygui()
    dpg.set_primary_window('primary', True)
    dpg.show_viewport()

    log('Ready. Select a serial port and firmware, then click Flash.')
    refresh_ports(quiet=True)

    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == '__main__':
    build_ui()
