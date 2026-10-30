# Smart Home: Camera AI Features Plan

**Goal:** use the Imou cameras for two things:

1. Answer "where is [person] now?" using face recognition.
2. Detect visual states: TV on/off, light on/off, door open/closed.

**Main tools:** Frigate 0.17+ (face recognition, state classification), MQTT, the smart home server's device registry, and optionally a vision language model.

---

## 1. Feasibility summary

| Feature | Possible? | Reliability | Main limit |
|---|---|---|---|
| "Where is my wife now?" | Yes | Medium | Only 2 cameras, so the answer is "last seen at X", and the face must be visible |
| TV on/off | Yes | High | Region must stay fixed (PTZ) |
| Light on/off | Yes | Medium–High | Harder in daylight |
| Door open/closed | Yes, if a camera sees the door | High | Camera placement |
| Door locked/unlocked | Not with camera | Low | Lock is too small in the image; use a sensor instead |
| Ad-hoc questions ("is there a bag on the sofa?") | Yes, via vision model | Medium | Slower, less reliable than trained models |

---

## 2. Prerequisites

1. **Upgrade Frigate to 0.17 or later.** State classification (custom classification models) was introduced in 0.17.
2. **Detect resolution:** face recognition runs on the stream assigned to the `detect` role. If that's a low-resolution substream, faces may be too small. Test with the main stream or a higher detect resolution, and watch CPU/GPU load.
3. **"Home" preset per camera:** using the Dahua protocol (port 37777) PTZ driver, save one preset per camera as its fixed "home" view. All training and classification uses this view.
4. **MQTT:** make sure Frigate publishes to the same Mosquitto broker the smart home server uses.

---

## 3. Feature A: Face recognition and "where is [person]?"

### 3.1 Set up face recognition in Frigate

1. Enable `face_recognition` in the Frigate config (start with model size `small`, which runs on CPU; switch to `large` on the GPU if accuracy is too low).
2. In the Frigate UI's **Face Library**, add each family member, uploading clear photos:
   - front-facing and slightly angled
   - daytime and **night IR mode**
   - with and without glasses, if relevant
3. Run it for about a week. In the Face Library, review the recognition attempts: confirm correct ones and reassign wrong ones to improve accuracy.
4. Tune `recognition_threshold` and `min_area` based on what you see: fewer false matches vs more recognitions.

### 3.2 Presence service in the smart home server

1. Subscribe to Frigate's event and tracked-object messages over MQTT.
2. When a `person` event carries a recognized name (Frigate's sub-label), update a **last-seen table**:
   - person
   - camera and its room
   - timestamp
   - confidence score
3. Optionally combine other signals for better answers:
   - **phone on home WiFi:** home/away
   - **voice:** which room device last heard this person (once speaker identification exists)
   - **camera:** last face sighting
4. Expose a tool to the language model: `where_is(person)`.

### 3.3 How answers should sound

- Recent sighting: "Seen in the living room 3 minutes ago."
- Old sighting: "Last seen in the living room at 14:32, about 40 minutes ago."
- No sighting, but phone at home: "At home, but not seen on camera recently."
- Phone not home: "Probably not at home."

Never claim a precise location the cameras can't see.

### 3.4 Limits

- Only two cameras, so most of the house isn't covered.
- People facing away, looking down, or far away are recorded as "person", but without a name.
- Night IR lowers accuracy.
- Similar-looking family members can be confused, so review the Face Library regularly at first.

---

## 4. Feature B: Visual states (TV, lights, door)

### 4.1 Where cameras work and where sensors are better

| Check | Use camera? | Better non-camera option |
|---|---|---|
| TV on/off | Yes, reliable | Smart TV local API, or smart plug with power metering |
| Light on/off | Yes, mostly | Smart switch or room light sensor |
| Door open/closed | Yes, if visible | Zigbee door contact sensor |
| Door locked/unlocked | **No** | Smart lock with local API, or a sensor on the lock |

Where a sensor exists, treat the sensor as the main source and the camera as a backup. For the door lock, never rely on the camera, because a wrong answer is a security problem.

### 4.2 Train state classification models in Frigate

For each check (e.g. `living_tv`, `living_light`, `front_door`):

1. In the Frigate UI, open **Classification** and create a new model of type **State**.
2. Define the classes, e.g. `on` / `off`, or `open` / `closed`.
3. Draw the crop region around the object, with the camera at its **home preset**.
4. Collect balanced training examples:
   - morning, afternoon, evening, night (IR)
   - lights on and off in the room (they change how the TV and door look)
   - people partly blocking the view
5. Train. Training needs a one-time internet connection to download the base model; after that it runs fully offline.
6. Configure when it runs: on a schedule and/or when motion is detected in the region.
7. Results are published on MQTT at `frigate/<camera_name>/classification/<model_name>`.

### 4.3 Connect to the smart home server

1. Subscribe to the classification topics.
2. Create **entities** in the device registry, e.g. `living_tv` (state: on/off/unknown), linked to the room.
3. The language model's existing `get_sensor` / `get_state` tool now answers "is the TV on?" like any other sensor.
4. Debounce: only change state after a few consistent readings, to avoid flicker.

---

## 5. PTZ rule: the camera must be at home for visual states

State classification looks at a fixed area of the image. After a pan/tilt, the TV or door is somewhere else in the frame, and the model reads the wrong area.

1. The server tracks each camera's position: `home` or `moved`.
2. When the camera is `moved`, mark all its visual-state entities as **unknown** and ignore classification results.
3. After a manual move, automatically return to the home preset after a set time (e.g. 3–5 minutes of no PTZ commands).
4. When asked about a state while the camera is away from home, answer honestly: "The camera is pointed elsewhere right now; want me to turn it back and check?"

---

## 6. Feature C (optional, last): vision model for ad-hoc questions

For questions without a trained model:

1. Grab a fresh snapshot from Frigate (camera at home preset, or wherever the question needs).
2. Send the snapshot plus the question to a vision-capable language model:
   - **local:** a small vision model on the GTX 1660 (test speed and memory alongside the other models)
   - **cloud:** better accuracy, but needs internet and sends images outside the house
3. Expose it as a tool, e.g. `look(camera, question)`.
4. Make the assistant say when an answer comes from this kind of visual guess: "It looks like…"

Use this as a fallback only, not as the source for TV/light/door states.

---

## 7. Privacy and data retention

- Tell family members their faces are recognized at home.
- Keep the face library, snapshots, and the last-seen history **local**.
- Set retention: e.g. keep presence history for 7–30 days, then delete.
- Don't add guests' faces. Unknown people stay "person" without a name.
- If you use a cloud vision model, decide which cameras may send images (e.g. never bedrooms).

---

## 8. Milestones

| # | Milestone | Done when |
|---|---|---|
| C1 | Frigate 0.17+ ready | Upgraded, detect resolution tested, home presets saved |
| C2 | Face recognition | Family members recognized correctly most of the time over a week (day and night) |
| C3 | Presence service | "Where is [person]?" answers with room and time from the last-seen table |
| C4 | TV/light state models | Trained, states appear as entities, assistant answers "is the TV on?" |
| C5 | PTZ safety | States go "unknown" when moved; camera returns home automatically |
| C6 | Door sensor/lock | Door open/closed and lock state come from sensors, camera only as backup |
| C7 | Vision fallback (optional) | `look(camera, question)` works locally or via cloud |
