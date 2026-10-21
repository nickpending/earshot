import asyncio
from bleak import BleakScanner
U = "632de001-604c-446b-a80f-7963e950f3fb"
async def main():
    found = await BleakScanner.discover(timeout=20, return_adv=True)
    rows = sorted(found.values(), key=lambda da: -da[1].rssi)
    print(f"devices seen: {len(rows)}")
    for d, a in rows:
        lim = U in [s.lower() for s in a.service_uuids] or any(k in (a.local_name or d.name or "").lower() for k in ("limitless", "pendant"))
        print(f"{d.address} rssi {a.rssi} {(a.local_name or d.name)!r}{'  <== LIMITLESS' if lim else ''}")
asyncio.run(main())
