"""Captura PNG de uma única janela pelo PID, sem incluir a área de trabalho."""

import argparse
import ctypes as ct
from ctypes import wintypes as wt
from pathlib import Path
import struct
import time
import zlib


def capture(pid, destination):
    user = ct.WinDLL('user32', use_last_error=True)
    gdi = ct.WinDLL('gdi32', use_last_error=True)
    try:
        ct.WinDLL('shcore').SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        user.SetProcessDPIAware()
    callback_type = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wt.LPARAM]
    user.IsWindowVisible.argtypes = [wt.HWND]
    user.GetWindowThreadProcessId.argtypes = [wt.HWND, ct.POINTER(wt.DWORD)]
    user.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ct.c_int]
    user.GetWindowRect.argtypes = [wt.HWND, ct.POINTER(wt.RECT)]
    user.GetWindowDC.argtypes = [wt.HWND]
    user.GetWindowDC.restype = wt.HDC
    user.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    gdi.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi.CreateCompatibleDC.restype = wt.HDC
    gdi.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
    gdi.SelectObject.restype = wt.HANDLE
    gdi.DeleteObject.argtypes = [wt.HANDLE]
    gdi.DeleteDC.argtypes = [wt.HDC]

    windows = []
    @callback_type
    def visit(hwnd, _):
        owner = wt.DWORD()
        user.GetWindowThreadProcessId(hwnd, ct.byref(owner))
        if owner.value == pid and user.IsWindowVisible(hwnd):
            title = ct.create_unicode_buffer(512)
            user.GetWindowTextW(hwnd, title, len(title))
            if 'Mine Pirata' in title.value:
                windows.append((hwnd, title.value))
        return True
    deadline = time.monotonic() + 15
    while not windows and time.monotonic() < deadline:
        user.EnumWindows(visit, 0)
        if not windows:
            time.sleep(0.1)
    if len(windows) != 1:
        raise RuntimeError(f'Esperava uma janela do aplicativo no PID {pid}; encontrei {len(windows)}')
    hwnd, title = windows[0]
    rect = wt.RECT()
    if not user.GetWindowRect(hwnd, ct.byref(rect)):
        raise ct.WinError(ct.get_last_error())
    width, height = rect.right - rect.left, rect.bottom - rect.top

    class BitmapInfoHeader(ct.Structure):
        _fields_ = [('size', wt.DWORD), ('width', wt.LONG), ('height', wt.LONG),
                    ('planes', wt.WORD), ('bits', wt.WORD), ('compression', wt.DWORD),
                    ('image_size', wt.DWORD), ('x', wt.LONG), ('y', wt.LONG),
                    ('used', wt.DWORD), ('important', wt.DWORD)]
    info = BitmapInfoHeader(ct.sizeof(BitmapInfoHeader), width, -height, 1, 32, 0,
                            width * height * 4, 0, 0, 0, 0)
    gdi.CreateDIBSection.argtypes = [wt.HDC, ct.POINTER(BitmapInfoHeader), wt.UINT,
                                   ct.POINTER(ct.c_void_p), wt.HANDLE, wt.DWORD]
    gdi.CreateDIBSection.restype = wt.HBITMAP
    window_dc = user.GetWindowDC(hwnd)
    memory_dc = gdi.CreateCompatibleDC(window_dc)
    bits = ct.c_void_p()
    bitmap = gdi.CreateDIBSection(window_dc, ct.byref(info), 0, ct.byref(bits), None, 0)
    if not bitmap:
        raise ct.WinError(ct.get_last_error())
    previous = gdi.SelectObject(memory_dc, bitmap)
    try:
        if not user.PrintWindow(hwnd, memory_dc, 2):
            raise ct.WinError(ct.get_last_error())
        pixels = ct.string_at(bits, width * height * 4)
    finally:
        gdi.SelectObject(memory_dc, previous)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory_dc)
        user.ReleaseDC(hwnd, window_dc)

    # Exclui apenas a borda de redimensionamento invisível do Windows.
    bounds = wt.RECT()
    dwm = ct.WinDLL('dwmapi')
    dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ct.c_void_p, wt.DWORD]
    result = dwm.DwmGetWindowAttribute(hwnd, 9, ct.byref(bounds), ct.sizeof(bounds))
    left, top, right, bottom = 0, 0, width, height
    if result == 0:
        left, top = max(0, bounds.left - rect.left), max(0, bounds.top - rect.top)
        right, bottom = min(width, bounds.right - rect.left), min(height, bounds.bottom - rect.top)
    cropped_width, cropped_height = right - left, bottom - top
    rows = []
    for y in range(top, bottom):
        row = pixels[(y * width + left) * 4:(y * width + right) * 4]
        rgb = bytearray(cropped_width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        rows.append(b'\0' + rgb)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', cropped_width,
           cropped_height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows), 9)) + chunk(b'IEND', b''))
    destination.write_bytes(png)
    print(f'{title}: {cropped_width}x{cropped_height} -> {destination.resolve()}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('pid', type=int)
    parser.add_argument('destination', type=Path)
    options = parser.parse_args()
    capture(options.pid, options.destination)
