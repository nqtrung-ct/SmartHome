## How the system fits together

Each room device listens locally for the wake word, then streams your speech to the PC. The PC turns speech into text, identifies who's speaking, and uses a language model to break a sentence like "turn off AC and tell me the weather" into actions. It runs those actions (IR command, weather lookup), creates a spoken reply, and sends the audio back to the device in that room. A typical command takes about 2–3 seconds. For common commands ("tắt điều hòa"), you can add a simple rule-based shortcut that skips the language model and responds almost instantly.

For the software pieces, here's what I'd pick today:

- **Speech-to-text:** PhoWhisper-large set the state-of-the-art error rate on every Vietnamese benchmark VinAI tested. The medium version is nearly as accurate and lighter. A very small, fast alternative is the Zipformer model: about 75 MB, around 7.7% error on clean Vietnamese, and roughly 61 ms for a 3.7-second clip. One caveat: English loanwords, numbers, and names may come out as phonetic Vietnamese, which matters if you say things like "bật Netflix".
- **Text-to-speech:** VieNeu-TTS is open source. Version 2 handles English words inside Vietnamese sentences well and can clone a voice from 3–5 seconds of audio, so you can give your assistant whatever voice you like.
- **Language model:** a 7–14B model with good Vietnamese and tool-calling, run locally. If you want smarter answers to general questions, you can route those to a cloud API and keep device control local.

On Home Assistant: its weak Vietnamese recognition mostly comes from the default speech engine, and you can plug PhoWhisper into it. Building your own is still reasonable if HA feels too heavy, and you can reuse parts of its ecosystem (like Frigate, below) without running HA itself.

## PC spec

The GPU's memory (VRAM) matters most, because speech-to-text, the language model, and text-to-speech all need to stay loaded at once.

| Tier | Spec | What it runs well |
|---|---|---|
| Minimum | Mini PC, recent Ryzen 7 or Core i5, 32 GB RAM, no GPU | Zipformer + TTS on CPU, language model in the cloud |
| Recommended | Ryzen 5 / Core i5 desktop, 32 GB RAM, used **RTX 3060 12 GB** | PhoWhisper-medium + 7–8B local model + TTS + camera detection |
| Comfortable | Same, with RTX 4060 Ti 16 GB or used RTX 3090 24 GB | PhoWhisper-large + 14B model, more headroom |

For storage, use a 512 GB NVMe drive for the system and a separate 2–4 TB HDD for camera clips. Add a small UPS, and run Ubuntu with Docker. The recommended tier draws around 60–120 W most of the time, so factor in 24/7 electricity.

One honest point: going from an RTX 3060 to a 3090 improves recognition less than going from a phone across the room to a proper mic array.

## Feature by feature

**1. Microphones.**
I'd suggest is ESP32-S3 voice boards with a dual-mic array, such as ESP32-S3-BOX-3 or ReSpeaker Lite. They are cheap, designed for far-field listening, run the wake word on the board, include a speaker, and don't have the battery or background-app problems.

**2. Speaker per room, and knowing the room.** Easy. Register each device with a room ID, and the server uses that room as the default target for "bật điều hòa". Two details to handle:

- If two devices hear the same command, keep only the one with the strongest signal within a few hundred milliseconds.
- Many Bluetooth speakers switch themselves off after 10–30 minutes idle. A wired or USB-powered speaker avoids this.

**3. Cameras, recording only suspicious time ranges.** Possible. Imou is Dahua's consumer brand, and its cameras support ONVIF and RTSP on port 554; you may need to enable ONVIF in the Imou app first. Run Frigate, an open-source video recorder that works without Home Assistant. It detects people and saves only event clips (with a few seconds before and after) for as long as you choose to keep them. Your assistant can then query Frigate's event list to answer "what happened in the living room yesterday afternoon?", and a vision model can describe the snapshots. The limits:

- "Suspicious" has to be defined as rules, such as a person detected in a zone during certain hours, or an unknown face. General "something looks wrong" detection isn't reliable.
- WiFi camera streams drop sometimes, and night IR footage is less accurate for detection.

**4. Knowing who is speaking, to limit your child.** Partly possible. You record each family member's voice once, and the system compares new commands against those recordings. The limits:

- Short commands of 1–2 seconds give less reliable matches.
- Children's voices change over time, so you'll need to re-record them.
- A recording or imitation of a parent's voice could fool it, so treat this as a convenience filter, not security.

A sensible design: when the system isn't confident who spoke, apply the child's (most limited) permissions, and require a spoken PIN for sensitive actions like disabling cameras.

**5. Timers and reminders.** Easy. The language model reads the time out of the sentence, and a scheduler on the server fires at that time. The open questions are design choices:

- **Where to announce:** at 15:00 the system doesn't know which room you're in. Options are the room where you asked, every room, or a push notification to your own phone.
- **Confirming the time:** "15 phút" (15 minutes) and "15 giờ" (15:00) are easy to mishear, so have it repeat the time back to you.

## What isn't realistic

- Voice ID that your child truly can't get around.
- Knowing an AC's actual state through IR.
- Truly understanding "suspicious" video beyond detection plus rules.

## Suggested order

1. Server, one room device, and Broadlink control with a fixed set of commands.
2. Add the language model for natural sentences, text-to-speech, and timers.
3. Expand to multiple rooms with room context.
4. Add Frigate for the cameras and the ability to ask about events.
5. Add speaker identification and child permissions last, once everything else is stable.