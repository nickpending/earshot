# Read-only Limitless pendant probe. Sends ONLY GetDeviceStatus (msg 21) and, if the
# pendant stays silent, SetCurrentTime (msg 6). Never sends msg 7 (ACK deletes flash
# pages) or msg 8 (mode switch). Wire format from omi LimitlessProtocol.swift (MIT).
import asyncio, sys, time
from bleak import BleakClient, BleakScanner

SVC = "632de001-604c-446b-a80f-7963e950f3fb"
TX = "632de002-604c-446b-a80f-7963e950f3fb"
RX = "632de003-604c-446b-a80f-7963e950f3fb"


def varint(v):
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def dvarint(b, p):
    r = s = 0
    while p < len(b):
        x = b[p]; p += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            break
        s += 7
    return r, p


def f_int(n, v): return varint(n << 3) + varint(v)
def f_bytes(n, d): return varint((n << 3) | 2) + varint(len(d)) + d
def request_data(rid): return f_bytes(30, f_int(1, rid) + varint(2 << 3) + b"\x00")
def wrapper(idx, payload): return f_int(1, idx) + f_int(2, 0) + f_int(3, 1) + f_bytes(4, payload)
def get_status(idx, rid): return wrapper(idx, f_bytes(21, b"") + request_data(rid))
def set_time(idx, rid, ms): return wrapper(idx, f_bytes(6, f_int(1, ms)) + request_data(rid))


def storage_state(data):
    # mirrors parseDeviceStatus: 0x22 wrapper payload -> 0x2a status -> 0x2a storage
    for i in range(len(data) - 5):
        if data[i] != 0x22:
            continue
        ln, p = dvarint(data, i + 1)
        end = p + ln
        for j in range(p, end - 5):
            if data[j] == 0x2A:
                sl, q = dvarint(data, j + 1)
                for k in range(q, q + sl - 1):
                    if data[k] == 0x2A:
                        stl, r = dvarint(data, k + 1)
                        st, keys = {}, {0x08: "oldest", 0x10: "newest", 0x18: "session", 0x20: "free_pages", 0x28: "total_pages"}
                        while r < k + 1 + stl + 1 and r < len(data) - 1:
                            m = data[r]; r += 1
                            if m in keys:
                                v, r = dvarint(data, r); st[keys[m]] = v
                        return st or None
                return None
        return None
    return None


async def main():
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: SVC in [u.lower() for u in a.service_uuids] or (a.local_name or "") == "Pendant", timeout=20)
    if not dev:
        sys.exit("pendant not found in 20s scan")
    print(f"found {dev.name} {dev.address}")
    rx, got = [], asyncio.Event()

    def on_rx(_, data: bytearray):
        rx.append(bytes(data))
        if storage_state(bytes(data)):
            got.set()

    async with BleakClient(dev, timeout=30) as c:
        print(f"connected, mtu={c.mtu_size}")
        svc = c.services.get_service(SVC)
        print("characteristics:", [(ch.uuid[4:8], ch.properties) for ch in svc.characteristics])
        await c.start_notify(RX, on_rx)
        idx = rid = 0
        await c.write_gatt_char(TX, get_status(idx, rid := rid + 1), response=True); idx += 1
        try:
            await asyncio.wait_for(got.wait(), 6)
        except asyncio.TimeoutError:
            print("no status after GetDeviceStatus; sending SetCurrentTime then retrying")
            await c.write_gatt_char(TX, set_time(idx, rid := rid + 1, int(time.time() * 1000)), response=True); idx += 1
            await asyncio.sleep(1)
            await c.write_gatt_char(TX, get_status(idx, rid := rid + 1), response=True); idx += 1
            try:
                await asyncio.wait_for(got.wait(), 8)
            except asyncio.TimeoutError:
                pass
        await asyncio.sleep(2)
        await c.stop_notify(RX)

    print(f"rx notifications: {len(rx)}")
    for d in rx[:8]:
        print(f"  {len(d):4d}B {d[:48].hex()}{'...' if len(d) > 48 else ''}")
    st = next((s for s in map(storage_state, rx) if s), None)
    print("storage state:", st)
    if st and st.get("total_pages"):
        used = st["total_pages"] - st.get("free_pages", 0)
        print(f"pages buffered: {st.get('newest', 0) - st.get('oldest', 0)}  used {used}/{st['total_pages']} ({100 * used / st['total_pages']:.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())
