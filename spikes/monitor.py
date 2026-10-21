# Live monitor: sets pendant clock (msg 6, what omi does on every connect), then polls
# GetDeviceStatus every 3s and logs button events. Never sends msg 7 (ACK) or msg 8.
import asyncio, time
from bleak import BleakClient, BleakScanner
from probe import SVC, TX, RX, dvarint, get_status, set_time, storage_state

NAMES = {0: "none", 1: "SHORT", 2: "LONG", 3: "DOUBLE", 4: "?4"}

def button(d):
    for i in range(len(d) - 5):
        if d[i] == 0x22:
            ln, p = dvarint(d, i + 1)
            if p < len(d) and d[p] == 0x42:
                bl, q = dvarint(d, p + 1)
                for k in range(q, q + bl - 1):
                    if d[k] == 0x08:
                        return dvarint(d, k + 1)[0]
            return None
    return None

async def main():
    t0 = time.time()
    ts = lambda: f"[{time.time() - t0:5.1f}s]"
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: SVC in [u.lower() for u in a.service_uuids] or (a.local_name or "") == "Pendant", timeout=20)
    if not dev:
        raise SystemExit("pendant not found (another client connected?)")
    last = {}
    def on_rx(_, data):
        d = bytes(data)
        b = button(d)
        if b:
            print(ts(), "BUTTON", NAMES.get(b, b), flush=True)
        st = storage_state(d)
        if st:
            last.update(st)
    async with BleakClient(dev, timeout=30) as c:
        await c.start_notify(RX, on_rx)
        idx = rid = 0
        await c.write_gatt_char(TX, set_time(idx, rid := rid + 1, int(time.time() * 1000)), response=True); idx += 1
        print(ts(), "clock set; polling", flush=True)
        prev = None
        while time.time() - t0 < 170:
            await c.write_gatt_char(TX, get_status(idx, rid := rid + 1), response=True); idx += 1
            await asyncio.sleep(3)
            n = last.get("newest")
            if n != prev:
                print(ts(), f"newest page {n}  used {last.get('total_pages', 0) - last.get('free_pages', 0)}", flush=True)
                prev = n
        await c.stop_notify(RX)
    print(ts(), "done", flush=True)

asyncio.run(main())
