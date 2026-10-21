# Non-destructive flash pull: msg 8 {1,0} (download mode), collect pages, then msg 8 {0,0}
# (back to record-to-flash). NEVER sends msg 7 — un-ACKed pages stay on the pendant.
# Writes frames.bin: per frame [u32 page][u64 ts_ms][u16 len][opus bytes].
import asyncio, struct, sys, time
from bleak import BleakClient, BleakScanner
from probe import SVC, TX, RX, dvarint, f_int, f_bytes, wrapper, request_data, get_status, set_time, storage_state

TOC = {0xB8, 0x78, 0xF8, 0xB0, 0x70, 0xF0}


def set_mode(idx, rid, batch, realtime):
    msg = f_int(1, int(batch)) + f_int(2, int(realtime))
    return wrapper(idx, f_bytes(8, msg) + request_data(rid))


def fields(b, s=0, e=None):
    e = len(b) if e is None else e
    p = s
    while p < e:
        tag, p = dvarint(b, p)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, p = dvarint(b, p); yield fn, wt, v
        elif wt == 2:
            ln, p = dvarint(b, p)
            if p + ln > e: return
            yield fn, wt, b[p:p + ln]; p += ln
        elif wt == 1: p += 8
        elif wt == 5: p += 4
        else: return


def opus_frames(blob, out):
    for fn, wt, v in fields(blob):
        if wt != 2 or not v: continue
        if 10 <= len(v) <= 200 and v[0] in TOC: out.append(v)
        elif len(v) > 10: opus_frames(v, out)


def page_frames(page):
    ts, frames = 0, []
    for fn, wt, v in fields(page):
        if fn == 1 and wt == 0: ts = v
        if fn == 3 and wt == 2:
            for f2, w2, v2 in fields(v):
                if f2 == 2 and w2 == 2: opus_frames(v2, frames)
    return ts, frames


async def main():
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: SVC in [u.lower() for u in a.service_uuids] or (a.local_name or "") == "Pendant", timeout=20)
    if not dev: sys.exit("pendant not found")
    frags, pages, st = {}, {}, {}
    last_rx = [time.time()]

    def on_rx(_, data):
        d = bytes(data); last_rx[0] = time.time()
        s = storage_state(d)
        if s: st.update(s)
        f = {fn: v for fn, wt, v in fields(d)}
        if not {1, 3, 4} <= f.keys(): return
        idx, seq, n = f[1], f.get(2, 0), f[3]
        frags.setdefault(idx, {})[seq] = f[4]
        if len(frags[idx]) < n: return
        parts = frags.pop(idx)
        payload = b"".join(parts[k] for k in sorted(parts))
        for fn, wt, v in fields(payload):
            if fn == 2 and wt == 2:
                sb = {f2: v2 for f2, w2, v2 in fields(v)}
                if 5 in sb and 6 in sb: pages[sb[5]] = sb[6]

    async with BleakClient(dev, timeout=30) as c:
        await c.start_notify(RX, on_rx)
        i = r = 0
        async def send(b):
            nonlocal i; await c.write_gatt_char(TX, b, response=True); i += 1
        await send(set_time(i, r := r + 1, int(time.time() * 1000)))
        await send(get_status(i, r := r + 1))
        await asyncio.sleep(2)
        print("storage before:", st, flush=True)
        newest = st.get("newest", 0)
        await send(set_mode(i, r := r + 1, True, False))
        t0 = time.time()
        while time.time() - t0 < 120:
            await asyncio.sleep(1)
            if pages and max(pages) >= newest: break
            if time.time() - last_rx[0] > 10: break
        await send(set_mode(i, r := r + 1, False, False))
        await asyncio.sleep(1)
        await send(get_status(i, r := r + 1))
        await asyncio.sleep(2)
        print("storage after:", st, flush=True)
        await c.stop_notify(RX)

    n = 0
    with open("frames.bin", "wb") as out:
        for pi in sorted(pages):
            ts, frames = page_frames(pages[pi])
            for fr in frames:
                out.write(struct.pack("<IQH", pi, ts, len(fr)) + fr); n += 1
    print(f"pages pulled {len(pages)} ({min(pages) if pages else '-'}..{max(pages) if pages else '-'}), opus frames {n}", flush=True)


asyncio.run(main())
