import fcntl, socket, struct, sys, time

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 20
LIMITLESS_UUID = bytes.fromhex("632DE001604C446BA80F7963E950F3FB")[::-1]
HCIDEVUP = 0x400448C9

s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
was_down = False
try:
    fcntl.ioctl(s.fileno(), HCIDEVUP, 0)
    was_down = True
except OSError as e:
    if e.errno != 114:  # EALREADY: already up
        raise
s.bind((0,))
# HCI filter: pass all event packets
s.setsockopt(socket.SOL_HCI, socket.HCI_FILTER, struct.pack("<IIIH2x", 1 << 4, 0xFFFFFFFF, 0xFFFFFFFF, 0))

def cmd(ocf, params=b""):
    op = (0x08 << 10) | ocf
    s.send(struct.pack("<BHB", 1, op, len(params)) + params)
    time.sleep(0.1)

cmd(0x000C, b"\x00\x00")                              # scan disable
cmd(0x000B, struct.pack("<BHHBB", 1, 0x10, 0x10, 0, 0))  # active scan params
cmd(0x000C, b"\x01\x00")                              # scan enable, no dup filter

seen = {}
s.settimeout(1)
end = time.time() + DUR
while time.time() < end:
    try:
        pkt = s.recv(300)
    except socket.timeout:
        continue
    if len(pkt) < 4 or pkt[0] != 4 or pkt[1] != 0x3E or pkt[3] != 0x02:
        continue
    n, i = pkt[4], 5
    for _ in range(n):
        addr = ":".join(f"{b:02X}" for b in pkt[i + 2:i + 8][::-1])
        ln = pkt[i + 8]
        data = pkt[i + 9:i + 9 + ln]
        rssi = struct.unpack("b", pkt[i + 9 + ln:i + 10 + ln])[0]
        i += 10 + ln
        name, lim = "", LIMITLESS_UUID in data
        j = 0
        while j < len(data) - 1:
            l, t = data[j], data[j + 1]
            if t in (8, 9):
                name = data[j + 2:j + 1 + l].decode(errors="replace")
            j += l + 1
        e = seen.setdefault(addr, {"name": "", "rssi": [], "lim": False})
        e["name"] = e["name"] or name
        e["rssi"].append(rssi)
        e["lim"] = e["lim"] or lim or "limitless" in name.lower() or "pendant" in name.lower()

cmd(0x000C, b"\x00\x00")
if was_down:
    fcntl.ioctl(s.fileno(), 0x400448CA, 0)  # HCIDEVDOWN: leave radio as found

print(f"devices seen: {len(seen)}")
for a, e in sorted(seen.items(), key=lambda kv: -max(kv[1]["rssi"])):
    tag = "  <== LIMITLESS" if e["lim"] else ""
    print(f"{a} rssi max {max(e['rssi'])} avg {sum(e['rssi'])//len(e['rssi'])} n={len(e['rssi'])} {e['name']!r}{tag}")
