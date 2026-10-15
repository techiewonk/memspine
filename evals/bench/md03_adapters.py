"""MD-03: list DXGI adapters (index = DirectML device_id) via ctypes. Windows only."""

from __future__ import annotations

import ctypes
from ctypes import (
    POINTER,
    WINFUNCTYPE,
    Structure,
    byref,
    c_long,
    c_size_t,
    c_uint,
    c_void_p,
    c_wchar,
)


class GUID(Structure):
    _fields_ = [
        ("a", ctypes.c_uint32),
        ("b", ctypes.c_uint16),
        ("c", ctypes.c_uint16),
        ("d", ctypes.c_ubyte * 8),
    ]


class LUID(Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", c_long)]


class DESC1(Structure):
    _fields_ = [
        ("Description", c_wchar * 128),
        ("VendorId", c_uint),
        ("DeviceId", c_uint),
        ("SubSysId", c_uint),
        ("Revision", c_uint),
        ("DedicatedVideoMemory", c_size_t),
        ("DedicatedSystemMemory", c_size_t),
        ("SharedSystemMemory", c_size_t),
        ("Luid", LUID),
        ("Flags", c_uint),
    ]


def list_adapters() -> list[dict]:
    dxgi = ctypes.windll.dxgi
    # IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
    iid = GUID(
        0x770AAE78,
        0xF26F,
        0x4DBA,
        (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
    )
    fac = c_void_p()
    if dxgi.CreateDXGIFactory1(byref(iid), byref(fac)) != 0:
        return []

    def vt(obj, idx, restype, *argt):
        tbl = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
        return WINFUNCTYPE(restype, c_void_p, *argt)(tbl[idx])

    out = []
    i = 0
    while True:
        ad = c_void_p()
        if (
            vt(fac, 12, ctypes.c_long, c_uint, POINTER(c_void_p))(fac, i, byref(ad)) != 0
        ):  # EnumAdapters1
            break
        d = DESC1()
        vt(ad, 10, ctypes.c_long, POINTER(DESC1))(ad, byref(d))  # GetDesc1
        out.append(
            {
                "device_id": i,
                "name": d.Description,
                "vendor": hex(d.VendorId),
                "dedicated_mb": d.DedicatedVideoMemory // 2**20,
                "shared_mb": d.SharedSystemMemory // 2**20,
            }
        )
        i += 1
    return out


def find_amd() -> int | None:
    for a in list_adapters():
        if a["vendor"] == "0x1002":
            return a["device_id"]
    return None


if __name__ == "__main__":
    for a in list_adapters():
        print(a)
