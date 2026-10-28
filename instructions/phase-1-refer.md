# Smart Home – Phase 1: Foundation

**Goal of this phase:** a 24/7 server that can (1) send IR commands to the ACs and fans through Broadlink, and (2) read temperature/humidity from Zigbee sensors locally. No voice yet. Voice comes in Phase 2, on top of this.

**Done when:**

- [ ] You have tested Vietnamese speech-to-text on your own voice (Step 0)
- [ ] The server runs Ubuntu + Docker and survives a reboot with everything starting automatically
- [ ] `python ir.py send living_ac_off` turns the AC off
- [ ] Sensor readings arrive on MQTT and are logged to a file

---

## Step 0 – Test Vietnamese speech recognition first (no purchase needed)

Do this on your current Windows PC before buying anything. It tells you how much the microphone distance matters in your real rooms.

1. With your phone's voice recorder, record the same 5 commands at **0.5 m, 2 m and 4 m**, once with the AC/fan off and once with them running. Example commands:
   - "Tắt điều hòa"
   - "Bật quạt số hai"
   - "Hôm nay thời tiết thế nào"
   - "Mười lăm phút nữa nhắc tôi tắt bếp"
   - "Bật điều hòa hai mươi sáu độ"
2. Copy the files to `F:\Working\projects\SmartHome\tests\audio\` (name them e.g. `2m_ac_on_01.m4a`).
3. Install and run:

```powershell
cd F:\Working\projects\SmartHome
python -m venv .venv
.venv\Scripts\activate
pip install transformers torch librosa soundfile
```

`tests\stt_test.py`:

```python
import sys
from pathlib import Path
import librosa
from transformers import pipeline

model = sys.argv[1] if len(sys.argv) > 1 else "vinai/PhoWhisper-small"
asr = pipeline("automatic-speech-recognition", model=model)

for f in sorted(Path("tests/audio").glob("*")):
    audio, _ = librosa.load(f, sr=16000)  # PhoWhisper expects 16 kHz mono
    text = asr({"raw": audio, "sampling_rate": 16000})["text"]
    print(f"{f.name:30} -> {text}")
```

```powershell
python tests\stt_test.py
python tests\stt_test.py vinai/PhoWhisper-medium
```

**What to look for:** if 0.5 m is near-perfect but 4 m with the AC on is bad, the fix is better microphones (ESP32-S3 mic-array boards), not a bigger GPU. Note the results; they decide your Phase 2 hardware.

> Reading `.m4a` files needs ffmpeg on Windows (`winget install ffmpeg`). Or record/convert to `.wav`.

---

## Step 1 – Shopping list for Phase 1

| Item | Notes |
|---|---|
| Server PC | Recommended tier: Ryzen 5 / Core i5, 32 GB RAM, RTX 3060 12 GB, 512 GB NVMe + 2–4 TB HDD (HDD can wait for Phase 3 cameras) |
| Zigbee USB coordinator | e.g. Sonoff ZBDongle-E or SMLIGHT SLZB-07 (adapter type `ember`), or Sonoff ZBDongle-P (adapter type `zstack`). Zigbee2MQTT itself is free software; this dongle is the hardware it needs |
| USB 2.0 extension cable, 0.5–1 m | Keeps the dongle away from USB 3 ports and the PC case, which interfere with Zigbee |
| Zigbee temp/humidity sensors | Check each model on the Zigbee2MQTT supported devices list before buying |
| Small UPS | Protects against power cuts corrupting the disk |
| Broadlink IR blasters | You already have these; one per room with IR devices |

---

## Step 2 – Install the server

1. Install **Ubuntu Server 24.04 LTS**. During install, enable **OpenSSH server**.
2. After first boot:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3-venv mosquitto-clients
sudo timedatectl set-timezone Asia/Ho_Chi_Minh
```

3. NVIDIA driver:

```bash
sudo ubuntu-drivers install
sudo reboot
nvidia-smi   # should show the RTX card
```

4. Docker:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out and back in, then:
docker run --rm hello-world
```

5. NVIDIA Container Toolkit (lets Docker containers use the GPU; needed from Phase 2). Follow the official guide: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>

6. In BIOS, set **"Restore on AC power loss" → Power On**, so the server restarts by itself after a power cut.

**Tip:** develop from your Windows PC with VS Code's **Remote - SSH** extension. You edit files directly on the server, and Claude Code works there too.

---

## Step 3 – Fix IP addresses on your router

In your router's DHCP settings, reserve a fixed IP for:

- the server
- every Broadlink device
- (later) the Imou cameras and room voice devices

Write them down in `docs/network.md`. Everything later depends on these addresses not changing.

---

## Step 4 – Project structure

Create a Git repo (GitHub/GitLab private, or just local) and use the same structure on the dev PC and the server:

```
SmartHome/
├── instructions/          # these docs
├── docs/network.md
├── infra/
│   ├── docker-compose.yml
│   ├── mosquitto/config/mosquitto.conf
│   └── zigbee2mqtt/data/configuration.yaml
├── devices/
│   ├── ir.py              # Broadlink learn/send
│   └── ir_codes.json      # learned codes (generated)
├── sensors/
│   └── logger.py          # MQTT -> CSV logger
└── tests/
    ├── stt_test.py
    └── audio/
```

---

## Step 5 – Broadlink: learn and send IR codes

```bash
cd ~/SmartHome
python3 -m venv .venv && source .venv/bin/activate
pip install broadlink paho-mqtt
```

`devices/ir.py`:

```python
"""Usage:
  python ir.py list
  python ir.py learn <broadlink_ip> <code_name>
  python ir.py send  <code_name>
"""
import json, sys, time
from pathlib import Path
import broadlink
from broadlink.exceptions import ReadError, StorageError

CODES = Path(__file__).with_name("ir_codes.json")

def load_codes():
    return json.loads(CODES.read_text()) if CODES.exists() else {}

def connect(ip):
    for d in broadlink.discover(timeout=5):
        if d.host[0] == ip:
            d.auth()
            return d
    sys.exit(f"Broadlink {ip} not found")

def cmd_list():
    for d in broadlink.discover(timeout=5):
        print(d.host[0], d.type, d.model, d.name)

def cmd_learn(ip, name):
    d = connect(ip)
    d.enter_learning()
    print(f"Press the remote button for '{name}' now (30 s)...")
    for _ in range(30):
        time.sleep(1)
        try:
            packet = d.check_data()
            break
        except (ReadError, StorageError):
            continue
    else:
        sys.exit("Timed out, nothing received")
    codes = load_codes()
    codes[name] = {"ip": ip, "data": packet.hex()}
    CODES.write_text(json.dumps(codes, indent=2))
    print(f"Saved '{name}'")

def cmd_send(name):
    code = load_codes()[name]
    connect(code["ip"]).send_data(bytes.fromhex(code["data"]))
    print(f"Sent '{name}'")

if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["list"]: cmd_list()
    elif a[:1] == ["learn"] and len(a) == 3: cmd_learn(a[1], a[2])
    elif a[:1] == ["send"] and len(a) == 2: cmd_send(a[1])
    else: print(__doc__)
```

Steps:

1. `python devices/ir.py list` → all Broadlink devices with their IPs.
2. Learn codes, naming them `<room>_<device>_<action>`:

```bash
python devices/ir.py learn 192.168.1.50 bedroom_ac_off
python devices/ir.py learn 192.168.1.50 bedroom_ac_cool_26
python devices/ir.py learn 192.168.1.50 bedroom_fan_toggle
```

3. Test: `python devices/ir.py send bedroom_ac_off`

**Notes:**

- **AC remotes send their full state every press** (mode + temperature + fan). Start with a small set of useful states, e.g. `off`, `cool_25`, `cool_26`, `cool_27`, `cool_28`. Set the remote to that exact state, then press the power/temp button to capture it.
- Many fan remotes have a single toggle button for power, so the server can't know on/off from the code alone. This is what the smart plug with power metering will fix later.
- If `list` finds nothing and the server has several network interfaces, use `broadlink.discover(timeout=5, local_ip_address="<server LAN IP>")`.

---

## Step 6 – Zigbee: Mosquitto + Zigbee2MQTT

1. Plug the dongle in (via the extension cable) and find its stable path:

```bash
ls -l /dev/serial/by-id/
```

2. `infra/mosquitto/config/mosquitto.conf`:

```
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
```

> `allow_anonymous` is OK while the broker is only reachable inside your home LAN. Add a username/password before exposing anything.

3. `infra/docker-compose.yml` (replace the `by-id` path with yours):

```yaml
services:
  mosquitto:
    image: eclipse-mosquitto:2
    restart: unless-stopped
    ports:
      - "1883:1883"
    volumes:
      - ./mosquitto/config:/mosquitto/config
      - ./mosquitto/data:/mosquitto/data

  zigbee2mqtt:
    image: ghcr.io/koenkk/zigbee2mqtt:latest
    restart: unless-stopped
    depends_on:
      - mosquitto
    ports:
      - "8080:8080"
    environment:
      - TZ=Asia/Ho_Chi_Minh
    volumes:
      - ./zigbee2mqtt/data:/app/data
      - /run/udev:/run/udev:ro
    devices:
      - /dev/serial/by-id/usb-XXXXXXXX-if00:/dev/ttyACM0
```

> Once everything works, replace `latest` with the exact version you're running, so an automatic update can't break the setup.

4. `infra/zigbee2mqtt/data/configuration.yaml`:

```yaml
homeassistant:
  enabled: false
frontend:
  enabled: true
  port: 8080
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://mosquitto:1883
serial:
  port: /dev/ttyACM0
  adapter: ember      # ZBDongle-E / SLZB-07 = ember, ZBDongle-P = zstack
advanced:
  network_key: GENERATE
  pan_id: GENERATE
  ext_pan_id: GENERATE
  channel: 25         # 15, 20 or 25 overlap least with 2.4 GHz WiFi
```

5. Start it:

```bash
cd ~/SmartHome/infra
docker compose up -d
docker compose logs -f zigbee2mqtt
```

6. Open `http://<server-ip>:8080`, click **Permit join (All)**, then put the sensor in pairing mode (usually hold its button ~5 s until the LED blinks). Rename it in the UI, e.g. `bedroom_climate`. Turn **Permit join off** afterwards.

7. Check readings:

```bash
mosquitto_sub -h localhost -t 'zigbee2mqtt/#' -v
```

You should see JSON like `{"temperature":27.4,"humidity":68,"battery":100,...}`.

> Back up `zigbee2mqtt/data/` (especially `configuration.yaml` with the generated keys and `coordinator_backup.json`). Losing it means re-pairing every device.

---

## Step 7 – Log sensor data

Start collecting data now. A few weeks of readings will show what your rooms look like with the AC on vs off, which you'll need later to detect the AC's state.

`sensors/logger.py`:

```python
import csv, json
from datetime import datetime
from pathlib import Path
import paho.mqtt.client as mqtt

LOG = Path(__file__).with_name("climate_log.csv")

def on_connect(client, userdata, flags, reason_code, properties):
    client.subscribe("zigbee2mqtt/+")

def on_message(client, userdata, msg):
    device = msg.topic.split("/", 1)[1]
    if device == "bridge":
        return
    try:
        data = json.loads(msg.payload)
    except ValueError:
        return
    if "temperature" not in data:
        return
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "device", "temperature", "humidity"])
        w.writerow([datetime.now().isoformat(timespec="seconds"),
                    device, data.get("temperature"), data.get("humidity")])

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.connect("localhost", 1883)
client.loop_forever()
```

Run it permanently as a systemd service, `/etc/systemd/system/climate-logger.service` (replace `trung` with your Linux username):

```ini
[Unit]
Description=Climate logger
After=network-online.target docker.service

[Service]
User=trung
WorkingDirectory=/home/trung/SmartHome
ExecStart=/home/trung/SmartHome/.venv/bin/python sensors/logger.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now climate-logger
```

While logging, write down in a note when you turn the AC on/off by hand. Later you can line those times up with the CSV.

---

## Step 8 – Final check

Reboot the server (`sudo reboot`) and confirm without touching anything:

- [ ] `docker ps` shows `mosquitto` and `zigbee2mqtt` running
- [ ] `systemctl status climate-logger` is active and the CSV keeps growing
- [ ] `python devices/ir.py send <code>` still works

---

## Next: Phase 2 preview

- One room voice device (ESP32-S3 mic board or a test phone), chosen based on Step 0 results
- Speech-to-text service on the GPU (PhoWhisper or Zipformer)
- A small server API: text in → fixed command table → `ir.py send` → spoken reply via VieNeu-TTS
- Then add the language model for natural sentences and timers
