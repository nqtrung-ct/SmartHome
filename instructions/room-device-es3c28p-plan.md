# Smart Home: Room Voice Device (ES3C28P) Implementation Plan

**Device:** ES3C28P, a 2.8" IPS touch display board with ESP32-S3 (8 MB PSRAM, 16 MB flash), ES8311 audio codec, a single bottom-port MEMS mic, an FM8002E amplifier with a speaker connector, and FT6336G capacitive touch.

**Role in the system:** one device per room. It detects the wake word, streams your voice to the server, plays the server's spoken reply, and shows a small touch UI. All intelligence (speech-to-text, language model, text-to-speech, device control) stays on the server.

**Approach:** fork the open-source **Xiaozhi** firmware (xiaozhi-esp32, MIT license) rather than writing firmware from scratch. It already handles wake word detection, audio capture, Opus compression, WebSocket streaming, playback, display, WiFi setup, and OTA updates. Your work is a board definition, a custom UI, and pointing it at your own server.

---

## 1. Hardware preparation

1. **Speaker:** 8 Ω 1.5 W or 4 Ω 2 W, plugged into the board's speaker connector. A slightly larger speaker in a closed box sounds noticeably better than a bare one.
2. **Power:** a good 5 V / 1 A or better USB-C adapter. **No LiPo battery**: the board has a charging circuit, but a device running 24/7 is safer on USB only.
3. **Case:** 3D-printed or bought. Two requirements:
   - a **sound hole aligned with the bottom-port microphone**, with nothing covering it
   - the speaker facing out, ideally away from the mic, to reduce echo
4. **Placement:** about head height, not right next to the AC unit or a fan, and not in a corner where sound reflects.
5. **Network:** reserve a fixed IP in the router for each device (useful for debugging, even though the device connects to the server and not the other way around).

---

## 2. Development environment

1. Install **ESP-IDF** (the version required by the Xiaozhi firmware; the board's own BSP targets ESP-IDF 5.4+) via the VS Code ESP-IDF extension.
2. Fork **xiaozhi-esp32** on GitHub and clone it inside `SmartHome/firmware/`.
3. Download the board's documentation pack from LCDWIKI (2.8inch ESP32-S3 Display): schematic, pin allocation table, and the ESP-IDF LVGL demo. Also look at the community BSP for this board (search "esp32_s3_es3c28p" on GitHub) for a ready-made pin map.
4. **First flash test:** build and flash the LCDWIKI ESP-IDF LVGL demo unchanged, to confirm the display, touch, and USB flashing work before touching Xiaozhi.
5. **Note:** keep project paths free of spaces and non-English characters; the board vendor warns that ESP-IDF builds fail otherwise.

---

## 3. Board definition in the Xiaozhi firmware

Xiaozhi supports many boards through a per-board folder (see its "Custom Board Guide").

1. **Check first** whether the seller's firmware or the Xiaozhi repo already includes this board (search for ES3C28P / "2.8inch ESP32-S3" / LCDWIKI). If yes, copy that board definition as your starting point.
2. If not, create a new board folder based on a similar board (ESP32-S3 + ES8311 + ILI9341 SPI display), and fill in from the pin table:
   - **display:** ILI9341V over SPI, 240×320, backlight pin, rotation
   - **touch:** FT6336G over I2C (shares the I2C bus with the ES8311)
   - **audio codec:** ES8311, configured over I2C, audio over I2S
   - **amplifier enable pin**
   - **any onboard button or RGB LED**
3. Build, flash, and confirm with Xiaozhi's default UI that the screen, touch, mic, and speaker all work.

**Done when:** the stock Xiaozhi UI runs on the board and its built-in audio test can record and play back.

---

## 4. Point the device at your own server

1. **Remove the default cloud server** from the firmware configuration. The device must only ever talk to your server.
2. **Set the server address.** Older firmware versions set the WebSocket URL directly in the firmware config (e.g. `ws://<server-ip>:<port>/xiaozhi/v1/`). Newer versions first ask an **OTA/config endpoint** at boot, which replies with the WebSocket address and a token. Check which one your firmware version uses; if it's the endpoint, your server must provide it (section 7).
3. **WiFi setup:** use Xiaozhi's built-in provisioning (the device opens a temporary WiFi hotspot with a setup page) on first boot.
4. **Device identity:** each device has a unique ID (based on its MAC address). The server uses this ID to know which room the device is in.

---

## 5. Voice capture (mic → server)

How the audio path works in the firmware:

1. **Mic input:** the MEMS mic → ES8311 ADC → I2S → ESP32-S3, as 16 kHz mono audio.
2. **Audio front end (ESP-SR AFE):** runs on the device:
   - **noise suppression**
   - **voice activity detection** (knows when you're speaking)
   - **echo cancellation** (removes the device's own speaker sound from the mic). With a single mic this is weaker than on dual-mic boards, so test it in section 10.
3. **Wake word (ESP-SR WakeNet):** runs fully on the device. Built-in wake words are mostly English or Chinese. Pick one that's easy to say for everyone in the family. Commands after the wake word can be in Vietnamese, because they're recognized on the server.
4. **Push-to-talk fallback:** a big on-screen button that starts listening without the wake word. Useful for testing and for noisy moments.
5. **Streaming:** after the wake word (or button press), audio is compressed with **Opus** and streamed in small frames over the WebSocket.
6. **End of speech:** detected by the device's voice activity detection, or the server tells the device to stop.

**Settings to tune during testing:** wake word sensitivity (false triggers vs missed triggers), mic gain on the ES8311, and the silence length that counts as "end of speech".

---

## 6. Playback (server → speaker)

1. The server sends the **text-to-speech reply as Opus audio frames** over the same WebSocket, plus the reply text.
2. The device decodes Opus → I2S → ES8311 DAC → FM8002E amplifier → speaker.
3. **Features to keep or add:**
   - **interrupting:** saying the wake word while it's talking stops playback and starts listening again (depends on echo cancellation quality)
   - **volume control:** from the touch UI, and by voice through the server ("nói nhỏ lại")
   - **local sounds:** a short tone when the wake word is detected and when listening ends, stored on the device, so feedback is instant
   - **server-initiated announcements:** timers, reminders, power outage alerts, camera events. The server pushes audio to a device without anyone speaking first, so the device must stay connected and accept unrequested audio.
4. **Night mode:** lower volume and dim the screen during set hours (configured from the server).

---

## 7. Server side: implement the Xiaozhi protocol

Add a **Xiaozhi gateway** to your smart home server. It connects the devices to your existing voice pipeline. Use the official protocol documents (WebSocket protocol, MQTT+UDP protocol, MCP interaction flow) in the xiaozhi-esp32 repo, and the open-source servers (e.g. xinnan-tech/xiaozhi-esp32-server) as references.

The gateway's responsibilities:

1. **OTA/config endpoint** (if your firmware version uses it): returns the WebSocket address, a device token, and, later, firmware update info.
2. **WebSocket endpoint:**
   - handshake ("hello") with audio parameters (Opus, sample rate, frame length)
   - receive the device's listening state and audio frames
   - send back recognized text, reply text, TTS start/stop signals, and audio frames
3. **Map the device to a room:** look up the device ID in your device registry. Unknown devices show up in the admin UI as "pending"; you assign a room and approve them.
4. **Connect to your pipeline:** Opus audio → decode → VAD/STT → intent (fast path, then language model) → execute → TTS → Opus encode → back to the same device.
5. **Device-side tools (MCP):** the firmware can expose tools to the server, such as volume, screen brightness, and showing a message. Your language model can call these, e.g. "turn the screen off".
6. **Push messages:** an API inside your server so other parts (scheduler, Frigate events, solar alerts) can send text, audio, or screen notifications to one room or all rooms.
7. **OTA updates:** host firmware files so all devices can update over WiFi, with no USB cable needed after the first flash.

---

## 8. Custom screen UI (LVGL)

The screen is small (240×320), so keep each screen simple with large touch targets.

### 8.1 Screens

| Screen | Content |
|---|---|
| **Idle** | Clock, date, room temperature/humidity (from Zigbee), solar battery %, small weather icon |
| **Listening** | Clear animation so people know it's recording; live recognized text if the server streams it |
| **Reply** | What it heard (small) and the reply (large), auto-returns to idle |
| **Room controls** | Big buttons for this room's devices: AC on/off and temperature, fan, light. States come from the server |
| **Notification** | Timers, reminders, alerts (e.g. "Có người ở cổng"), with a dismiss button |
| **Settings** | Volume, brightness, device ID/room, connection status, firmware version |

### 8.2 How UI and server talk

- **The server is the source of truth.** The device only displays the states the server sends (AC on, 26°C, battery 80%).
- **Button presses** are sent to the server as commands, either as a text command in the protocol or as an MCP tool call. The device never controls a Broadlink or Zigbee device directly.
- **Updates arrive by push:** the server sends state changes as they happen, so the device doesn't poll.

### 8.3 Vietnamese text

- LVGL's default fonts **don't include Vietnamese diacritics**. Generate custom fonts that include the Vietnamese character range, e.g. with LVGL's font converter or Xiaozhi's online assets generator.
- Fonts take flash and memory. Use one or two sizes only (e.g. 16 px for body text, 28–32 px for the clock and headings).
- Test long words and all tone marks (ặ, ổ, ữ…) on the real screen.

### 8.4 Design tips

- **High contrast**, readable from 2 m away.
- **Screen off or dimmed** after inactivity; wake on touch, on the wake word, or for notifications.
- **A visible "mic active" indicator.** People should always know when it's listening.

---

## 9. Reliability and security

- **Auto-reconnect:** reconnect to WiFi and the server after drops, with growing wait times between attempts. Show a "disconnected" icon on screen.
- **Watchdog:** the device restarts itself if the firmware hangs.
- **LAN only:** no cloud server, no port forwarding. Each device uses its own token, revocable from the admin UI.
- **Privacy:** audio is sent only after the wake word or button press, never continuously. Make this visible on screen.
- **Updates:** test new firmware on one device before pushing OTA to all.

---

## 10. Test plan (before buying more units)

| Test | How | Pass if |
|---|---|---|
| Mic distance | Same 5 Vietnamese commands at 1, 2, 4 m, with AC/fan off and on | Correct transcripts at the distance you need in that room |
| Wake word | 50 tries at different distances; plus a day of normal TV/talking | Few misses; false triggers rare (e.g. under 1–2 per day) |
| Interrupting | Say the wake word while it's replying | It stops and listens most of the time |
| Speaker | Replies at normal volume in the room | Clear and loud enough; no distortion at high volume |
| Latency | End of speech → start of reply | Under about 2–3 s |
| Stability | Run 7 days | No freezes; reconnects after router restart |
| Heat | Feel/measure after a day, inside the case | Warm at most |

**Decision:** if it passes at the distance a room needs, buy more for similar rooms. Use dual-mic boards (Waveshare ESP32-S3-AUDIO-Board, ESP32-S3-BOX-3) for large or noisy rooms. They run the same Xiaozhi protocol, so the server treats all devices the same way.

---

## 11. Milestones

| # | Milestone | Done when |
|---|---|---|
| D1 | Hardware + toolchain | LCDWIKI demo flashes and runs; speaker and case ready |
| D2 | Board definition | Stock Xiaozhi UI runs; mic/speaker loopback test works |
| D3 | Own server connection | Device connects only to your server; gateway logs hello + audio frames |
| D4 | Voice round trip | Wake word → speech to server → reply audio plays on the device |
| D5 | Room mapping | Device approved in admin UI; "bật điều hòa" controls that room's AC |
| D6 | Custom UI | Idle, listening, reply, and room control screens with Vietnamese fonts |
| D7 | Push + OTA | Timers/alerts appear and speak on devices; firmware updates over WiFi |
| D8 | Test plan passed | Section 10 results recorded; decide on buying more units |
| D9 | Follow-up conversations | Confirmations and clarifications work without repeating the wake word (section 12) |
| D10 | Room noise level | Each device reports a noise level the presence service uses (section 13.2) |
| D11 | Sound alerts | Smoke alarm / glass break detected on the device and announced in all rooms (section 13.3) |
| D12 | Offline commands + custom wake word (optional) | Basic Vietnamese commands work with the server down; family wake word trained (sections 13.4–13.5) |

---

## 12. Follow-up conversations (no repeated wake word)

**Goal:** after the device asks something ("Đồng ý không?", "Phòng nào?"), you can just answer without saying the wake word again.

### 12.1 Example flow

1. "Hey Sophia" (wake word, detected on the device)
2. "Tắt hết điều hòa trong nhà" (streamed to the server)
3. Server: "Tắt 3 điều hòa: phòng khách, phòng ngủ, phòng con. Đồng ý không?"
4. The device **keeps listening** for a short window (e.g. 6–8 s), with the listening indicator on screen
5. "Đồng ý" → the server runs the pending action → "Đã tắt."
6. Silence, or the window ends → the conversation closes; the device goes back to waiting for the wake word

### 12.2 Firmware

- Use the Xiaozhi protocol's listening mode where the device automatically starts listening again after playback, as long as the server keeps the conversation open. Check the exact mode names in the protocol document for your firmware version.
- Start the listening window only **after playback ends plus a short pause**, so the device doesn't pick up the end of its own reply (single-mic echo cancellation is weak).
- Show a clear "waiting for your answer" state on screen, with a visible countdown or animation, and a cancel button.

### 12.3 Server

- **The server decides when to keep listening.** Keep the conversation open only when an answer is expected: a confirmation, a clarification, or an open chat. Close immediately after simple commands ("tắt đèn" → "Đã tắt" → closed).
- **Pending action store:** when a confirmation is needed, store the action, the device/room, and an expiry time (e.g. 10 s). It's discarded on expiry, cancellation, or a new unrelated request.
- **Accepted answers:**
  - confirm: "đồng ý", "được", "ok", "làm đi", "ừ"
  - cancel: "không", "thôi", "hủy"
  - anything else: treat it as a new request and drop the pending action
- **Only the same device** can confirm a pending action. A confirmation heard in another room is ignored.

### 12.4 When to ask for confirmation

- **Ask** for bulk actions (all rooms), risky actions (cameras off, door/lock related), unusual requests (AC at 16°C), and anything the language model is unsure about.
- **Don't ask** for simple everyday commands. Constant confirmations get annoying fast.
- **Later, with speaker identification:** require that a confirmation for a risky action comes from an adult's voice, so a child or the TV can't confirm it.

---

## 13. On-device TinyML (small models on the ESP32-S3)

**What fits:** small audio models (sound classification, keyword spotting). **What doesn't:** Vietnamese speech-to-text, language models, text-to-speech. Those stay on the server.

### 13.1 Tools and processing budget

- **Run models with:** Espressif ESP-DL or TensorFlow Lite Micro (esp-tflite-micro).
- **Collect data and train with:** Edge Impulse, which exports a ready-to-use ESP-IDF library.
- **Budget:** the device already runs wake word detection, the audio front end (noise suppression, echo cancellation, VAD), Opus, and the LVGL screen. Add **one model at a time**, and after each one re-run the wake word, latency, and stability tests from section 10.
- Run the extra models on the lower-priority core, and pause them while the device is in a conversation if CPU gets tight.

### 13.2 Room noise level (presence signal), no ML needed

1. Measure the microphone's sound level continuously (e.g. average level per few seconds).
2. Report it to the server every 30–60 s, or when it changes significantly, **as a number only, never audio**.
3. The server uses it as a presence signal: "someone is active in this room". This adds to the camera and WiFi signals in the "where is [person]" feature.
4. Calibrate each room's normal background level (AC, fan, street noise), so the signal means "more than usual".

### 13.3 Sound alerts (first TinyML project)

**Sounds to detect, in priority order:**

1. smoke/fire alarm beeping
2. glass breaking
3. a baby crying (if relevant)
4. a doorbell or knocking
5. a dog barking

**Steps:**

1. Collect training sounds: public sound datasets plus recordings made in your own house (your actual smoke alarm, your doorbell), and background noise from each room (TV, AC, talking, cooking).
2. Train a small audio classifier in Edge Impulse; export it for ESP-IDF.
3. Run it on the device. When a sound is detected with high confidence for a few consecutive windows, send an **event** to the server (type, confidence, room). Send no audio.
4. The server decides what to do:
   - **smoke alarm:** announce in every room, show an alert on every screen, notify phones
   - **glass break when nobody's home:** alert phones and save camera clips around that time
   - **doorbell:** announce in the rooms where people are
5. Tune thresholds to avoid false alarms from TV sounds. For example, require a pattern (the smoke alarm's repeated beeps), not a single sound.

### 13.4 Offline fallback commands (optional, harder)

**Goal:** basic control still works when the server or WiFi is down.

1. ESP-SR's built-in command recognition supports English and Chinese, **not Vietnamese**, so this needs a custom keyword-spotting model.
2. Choose 10–20 short commands, e.g. "tắt đèn", "bật đèn", "tắt quạt", "bật quạt", "tắt điều hòa".
3. Record each command many times from **every family member**, at different distances and noise levels, plus lots of "not a command" audio.
4. Train and run it on the device. Use it **only when the server is unreachable**, so it never conflicts with the normal pipeline.
5. **Limit to keep in mind:** without the server, the device can't reach Broadlink or Zigbee directly unless you also give it a minimal local control path (e.g. sending Broadlink IR commands from the device itself). Decide whether the extra work is worth it.

### 13.5 Custom Vietnamese wake word (optional, harder)

1. Pick a 3–4 syllable phrase that's distinctive and rarely said in normal conversation or on TV.
2. Training data: your family's recordings, plus synthetic samples generated with VieNeu-TTS in many voices, plus lots of negative audio (TV, talking, music).
3. Train a small wake word model (the microWakeWord approach works for custom phrases).
4. **Port it into the Xiaozhi firmware** in place of, or alongside, ESP-SR WakeNet. This is the main effort.
5. Test it exactly like the built-in wake word (section 10): misses at distance, and false triggers over a full day.

### 13.6 Order

1. Follow-up conversations (section 12)
2. Room noise level (13.2)
3. Sound alerts, starting with the smoke alarm (13.3)
4. Offline commands and a custom wake word only once everything else is stable (13.4, 13.5)
