# Smart Home MVP: Server + Android Mic App

**Goal:** speak into an Android phone, the server understands Vietnamese, controls a device, and answers back through the phone. The server can also discover devices on the home network and let you add them, similar to Home Assistant.

**Environment:** Windows PC with a GTX 1660 (Super) for now, moving to Linux on the new home server later.

---

## 0. Rules that make the Windows → Linux move painless

Follow these from day one, and the move to Linux later is mostly copying a folder and installing services.

- **One language for the server:** Python, kept cross-platform. Use `pathlib` for paths, no Windows-only libraries, no hard-coded `C:\` or `F:\` paths.
- **All settings in one config file plus environment variables:** IP addresses, ports, model paths, tokens.
- **All data in one folder** (`data/`): the database, learned IR codes, logs, downloaded models. Moving servers means moving this folder.
- **Git from the first commit.** Develop on Windows, and later pull the same repo on Linux.
- **Run the server natively on Windows (Python venv), not inside Docker, during the test phase.** On Windows, Docker containers sit behind a virtual network, so the broadcast and multicast traffic used for device discovery doesn't reach them reliably. On Linux later, Docker with host networking works fine.
- **Write a short `docs/setup-windows.md` as you go.** The Linux version will follow the same steps.

---

## 1. Prepare the Windows machine

1. Install the latest NVIDIA driver and check that `nvidia-smi` shows the GTX 1660.
2. Install Python (3.11 or 3.12), Git, VS Code, and ffmpeg.
3. Give the PC a fixed IP in your router's DHCP settings, and do the same for each Broadlink device, Tuya device, and camera.
4. Set your home WiFi/LAN as a **Private** network in Windows, and allow the server's ports in Windows Defender Firewall. Otherwise phones can't connect and discovery replies get blocked.
5. Set Windows power options so the PC never sleeps while testing.
6. If the 1660 also drives your monitor, remember that Windows keeps some of its 6 GB of video memory for the desktop.

---

## 2. Install and benchmark the AI models (each one on its own first)

Test each model alone with your own recordings before connecting anything. Record how long each one takes and how much GPU memory it uses (shown by `nvidia-smi`).

| Role | Recommended option | Runs on | Approx. GPU memory |
|---|---|---|---|
| Voice activity detection (detects when you stop talking) | Silero VAD | CPU | none |
| Speech-to-text, fast | Zipformer-vi via sherpa-onnx | CPU (or GPU) | ~0 if on CPU |
| Speech-to-text, most accurate | PhoWhisper-small or -medium, converted for faster-whisper | GPU | ~1–2 GB |
| Language model (understands commands, calls tools) | A current ~4B model with good Vietnamese and tool calling (e.g. Qwen or Gemma family), 4-bit GGUF | GPU via Ollama or llama.cpp | ~3 GB |
| Text-to-speech | VieNeu-TTS | GPU (or GGUF on CPU) | ~1–1.5 GB |

The memory figures are rough. Measure them yourself.

**Fitting it into 6 GB:** start with Zipformer on the CPU, the language model on the GPU, and text-to-speech on the GPU. Switch to PhoWhisper on the GPU only if Zipformer's accuracy isn't good enough in your tests.

Steps:

1. **Install Ollama** (the Windows installer handles the GPU automatically) or download the llama.cpp Windows CUDA build. Pull 2–3 candidate ~4B models.
2. **Test the language models with your real commands.** Write 20–30 typical Vietnamese commands, including multi-part ones ("tắt điều hòa rồi cho tôi biết thời tiết") and ambiguous ones. Check which model returns correct tool calls most often. This matters more than benchmark scores.
3. **Install sherpa-onnx** and the Zipformer-vi model, then test it on the recordings from your earlier speech test.
4. **Install faster-whisper with PhoWhisper** as the accuracy comparison. On Windows, faster-whisper's GPU support needs extra NVIDIA libraries; follow its README.
5. **Install VieNeu-TTS** and test typical replies ("Đã tắt điều hòa phòng ngủ", reading out a weather summary). Pick or clone a voice you like.
6. **Install Silero VAD** and check that it reliably detects the end of a sentence, including when you pause briefly mid-sentence.
7. Put all downloaded models under `data/models/` and record the chosen model names and versions in the config file.

**Done when:** every model works standalone, and you have a small table of time per request and GPU memory for each. Aim for the total from end of speech to start of the reply to be under about 2–3 seconds.

---

## 3. Server architecture

Build it as one Python application (FastAPI is a good fit) with these parts:

```
                ┌──────────────── Server ─────────────────┐
Android app ──► │ Voice gateway (WebSocket audio in/out)  │
                │   → VAD → STT → Intent → Execute → TTS  │
Admin web UI ─► │ REST API                                │
                │ Device registry (SQLite)                │
                │ Discovery service                       │
                │ Drivers: Broadlink | Zigbee (MQTT) |    │
                │          Tuya local | Imou (later)      │
                │ Scheduler (timers, reminders)           │
                └─────────────────────────────────────────┘
```

**Data model (keep it simple, Home Assistant uses a similar idea):**

- **Device:** one physical thing (a Broadlink RM4, a Zigbee sensor, a camera), with its brand, IP or ID, and connection details.
- **Entity:** one controllable or readable function of a device ("bedroom AC" as a climate entity, "bedroom temperature" as a sensor entity). One Broadlink can control several entities (AC + fan + TV).
- **Room:** groups entities and voice clients. This is what makes "turn on the AC" mean the AC in the room you're speaking from.
- **Voice client:** each Android phone, with its ID, assigned room, and access token.
- **User:** family members and their permissions (used later for limiting your child's actions).

---

## 4. Device discovery ("find everything on my network")

Discovery runs several scanners and puts results into a **"Discovered"** list. You then **adopt** a device in the admin UI, which runs that brand's setup.

| Method | Finds |
|---|---|
| Broadlink UDP discovery | Broadlink IR blasters |
| Tuya local UDP broadcasts | Mains-powered WiFi Tuya devices (plugs, switches) |
| ONVIF WS-Discovery | Imou cameras and most IP cameras |
| mDNS / Zeroconf | Chromecasts, printers, ESP32 boards, many smart devices |
| SSDP / UPnP | Smart TVs, media devices, routers |
| Zigbee2MQTT device list (via MQTT) | All paired Zigbee devices |
| Network scan + MAC vendor lookup | Everything else: shows "unknown device, made by X" |

**Adoption flow per brand:**

- **Broadlink:** authenticate, then name the entities it controls and learn their IR codes from the admin UI.
- **Zigbee:** pairing happens in Zigbee2MQTT. The server listens for new devices and creates entities from what Zigbee2MQTT reports (temperature, humidity, battery).
- **Tuya WiFi:** local control needs each device's *local key*, which you fetch once through a free Tuya IoT developer account (the tinytuya tool can do this). After that, control is fully local.
- **Imou:** enter the camera's safety code (its RTSP password). Store it now; it gets used when you add camera features.

**Be clear about the limit:** discovery can *find* almost anything, but *controlling* a device requires a driver for that brand. Home Assistant has thousands of drivers written by its community. You'll write drivers only for the brands you own. Unknown devices stay in the list as "found, not supported".

**Zigbee on Windows:** Zigbee2MQTT can run natively on Windows (Node.js) using the dongle's COM port, with Mosquitto installed as a Windows service. On Linux later, both move to Docker as in the Phase 1 guide.

**Done when:** a "Scan" button in the admin UI lists your Broadlink devices, cameras, and Zigbee sensors, and you can adopt them and assign them to rooms.

---

## 5. Text commands end to end (before any voice)

Get the logic right with typed text first. Debugging is much easier without audio in the way.

1. **Fast path:** a table of common commands (patterns like "tắt/bật + device + optional room") that maps straight to actions with no language model involved.
2. **Language model path:** for anything the fast path doesn't match, send the text to the language model along with:
   - a short description of the available tools (`set_climate`, `switch`, `get_sensor`, `get_weather`, `set_timer`)
   - the list of entities **in the speaker's room** plus the room names, kept short to keep it fast
   - the default room (the one the request came from)
3. **Execute** the tool calls through the drivers, then build a short Vietnamese reply.
4. **Weather:** call a free weather API directly. No model knowledge is needed.
5. **Timers:** store each timer in the database with its target time, originating room, and message. The scheduler checks it and announces through that room's voice client. Always repeat the parsed time back to the user ("Ok, 15:00 nhắc bạn…").
6. Add a text box in the admin UI that sends commands as if they came from a chosen room.

**Done when:** typing "tắt điều hòa" with room = bedroom turns off the bedroom AC, and typing a two-part sentence runs both actions.

---

## 6. Android app

**Tech:** Kotlin, minimum Android 8 (to support older phones). One screen for setup, plus a background service.

**Version 1: push-to-talk (start here):**

1. **Setup screen:** enter the server address (or auto-find it via Android's network service discovery if the server announces itself over mDNS), then pair with a one-time code shown in the admin UI. The server returns a token, and you assign the phone to a room in the admin UI.
2. **A big "hold to talk" button.** Record 16 kHz mono audio and stream it to the server over a WebSocket while the button is held.
3. **The server replies** with text plus audio (the text-to-speech output). The app plays it through the phone speaker or a connected Bluetooth speaker.
4. **Show the recognized text and the reply on screen.** This is very useful for spotting recognition errors.

**Version 2: hands-free:**

1. Move listening into a **foreground service with microphone type** (required on newer Android versions), with a persistent notification.
2. **Wake word on the phone:** openWakeWord or sherpa-onnx keyword spotting. Only after the wake word does the phone start streaming audio.
3. **End of speech:** run voice activity detection on the phone, or let the server detect silence and tell the phone to stop.
4. **Keep it alive:** ask the user to exclude the app from battery optimization. On Xiaomi/Oppo/Vivo, also enable autostart and lock the app in recent apps.
5. **Kiosk-style settings:** keep the screen off, auto-start after reboot, reconnect automatically when WiFi drops.
6. **Duplicate handling on the server:** if two phones send the same wake event within about half a second, keep the one with the louder signal.

**Phone hygiene:** don't leave phones charging at 100% 24/7; use a smart plug schedule or a charge-limit setting where available. Keep the app on the LAN only.

---

## 7. Security basics (even for a home test)

- Phones and the admin UI authenticate with tokens; no open endpoints.
- The server listens only on the home LAN. No port forwarding on the router. For remote access later, use a VPN such as Tailscale or WireGuard.
- Store device secrets (Tuya local keys, camera codes) in the data folder, not in Git.

---

## 8. Milestones and checks

| # | Milestone | Done when |
|---|---|---|
| M0 | Windows environment ready | GPU visible, firewall rules set, fixed IPs reserved |
| M1 | Models benchmarked | Table of latency + GPU memory per model; models chosen |
| M2 | Server skeleton + admin UI | Server starts, UI shows rooms and an empty device list |
| M3 | Discovery + adoption | Broadlink, Zigbee sensors, cameras found and assigned to rooms |
| M4 | Text commands | Typed commands control devices; timers fire |
| M5 | Android push-to-talk | Speak → device acts → spoken reply on the phone, under ~3 s |
| M6 | Hands-free + room context | Wake word works; "bật điều hòa" controls the phone's room |

After M6, the next steps are speaker identification (child limits), cameras with Frigate, and the AC state check with power monitoring plus temperature sensors. Then move everything to the Linux server.
