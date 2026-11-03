# Qwen3 8B: Configuration for the Smart Home Assistant

**Problems this fixes:**

1. The model outputs "thinking" text before answering.
2. Tool calls don't work.
3. The model runs slowly because it doesn't fully fit on the GTX 1660 Super (6 GB).

**Assumed setup:** Ollama (or llama.cpp) on the server, called from the smart home server's intent step.

---

## 1. Turn off thinking mode

Qwen3 is a hybrid model: it "thinks out loud" first unless told not to. For voice commands, keep it **off**; it makes replies much slower.

| Runtime | How to turn it off |
|---|---|
| **Ollama** | Set `think: false` in the chat API request, or start the CLI with `--think=false`. Needs a recent Ollama version; update if the option isn't recognized |
| **llama.cpp / vLLM / other OpenAI-compatible servers** | Pass `enable_thinking: false` in the chat template settings (`chat_template_kwargs`). With this set, the model produces no thinking content and no `<think>` block at all |
| **Fallback (any runtime)** | Add `/no_think` to the system prompt. An empty `<think></think>` pair may still appear, so strip it in the server |

**Check:** a response to "tắt đèn" contains no `<think>` text and arrives noticeably faster.

---

## 2. Sampling settings (non-thinking mode)

Qwen's recommended values:

| Setting | Value | Note |
|---|---|---|
| `temperature` | 0.7 | Can go to ~0.5 for more consistent command parsing. **Never 0**: Qwen warns greedy decoding causes degraded output and endless repetition |
| `top_p` | 0.8 | |
| `top_k` | 20 | |
| `min_p` | 0 | **llama.cpp: set explicitly**, its default is 0.1 |
| `presence_penalty` | 0–1.5 | Only if you see repetition; try 1.0 |

Always pass these with every request, rather than relying on runtime defaults.

---

## 3. Why tool calls fail: checklist

Work through these in order:

1. **Use the chat endpoint with the `tools` field.**
   - Ollama: `/api/chat` or the OpenAI-compatible `/v1/chat/completions`. `/api/generate` doesn't support tools.
   - Pass tools as structured definitions, not as text inside the prompt.
   - Read calls from the response's `tool_calls` field, not from the reply text.
2. **Increase the context size.** Ollama's default context window is small. If the system prompt plus tool definitions exceed it, the beginning of the prompt (including the tools) is **silently cut off**, and the model never sees the tools. Set `num_ctx` to at least 4096 (8192 if it fits, see section 4).
3. **Use a model with a proper chat template:** the official `qwen3:8b` from Ollama's library, or a well-known GGUF upload. A GGUF imported without its chat template has no tool-call format.
4. **llama.cpp: start the server with `--jinja`.** Without it, tool calling isn't enabled.
5. **Complete the tool loop:**
   1. send the user message + tools
   2. if the response contains `tool_calls`, run each one on the server
   3. send each result back as a message with role `tool`
   4. call the model again
   5. repeat until a response has no tool calls; that response is the final spoken reply

   Qwen3 can return **several tool calls at once** (e.g. "tắt điều hòa rồi cho tôi biết thời tiết"). Handle them all before calling the model again.

---

## 4. Fitting on the GTX 1660 Super (6 GB)

**The problem:** the 4-bit model is ~5 GB, and the context window needs extra GPU memory on top. At 8192 tokens, the total can exceed 6 GB, especially when Windows also uses the card for the display. Ollama then silently moves part of the model to the CPU, making it several times slower.

**How to check:** run `ollama ps`. The model should show **100% GPU**.

**If it doesn't, apply these one at a time until it fits:**

1. **Shrink the context's memory use:** set these Ollama environment variables, then restart Ollama:
   - `OLLAMA_FLASH_ATTENTION=1`
   - `OLLAMA_KV_CACHE_TYPE=q8_0`

   This roughly halves the memory the context uses.
2. **Use `num_ctx` 4096** if your system prompt + tools + conversation fit within it.
3. **Use a slightly smaller quantization:** Q4_K_S or IQ4_XS GGUF instead of Q4_K_M, which frees a few hundred MB.
4. **Move text-to-speech to the CPU:** use VieNeu's GGUF CPU version. With an 8B model there's no room left on a 6 GB card for TTS on the GPU.
5. **If it still doesn't fit with everything else running:** use **Qwen3-4B** for real-time commands, and keep 8B for background jobs (daily summaries, overnight camera event descriptions) where speed doesn't matter.

**Suggested GPU layout on the 1660 Super:**

| Model | Runs on |
|---|---|
| Zipformer speech-to-text | CPU |
| Silero VAD | CPU |
| Qwen3 8B (or 4B) | **GPU, 100%** |
| VieNeu-TTS | CPU (GGUF) |
| Frigate detection | Intel integrated GPU (OpenVINO), if the CPU has one |

---

## 5. Tool design for small models

1. **Keep it to about 5–8 tools**, with short, clear names and descriptions:
   - `set_climate` (AC: on/off, mode, temperature)
   - `switch_device` (fans, lights, plugs)
   - `get_state` (sensors, device states, visual states)
   - `get_weather`
   - `set_timer`
   - `where_is` (presence)
2. **Use fixed choices (enums) wherever possible:** room names, entity IDs, modes, on/off. The model then picks from a list instead of inventing names.
3. **Send only the current room's devices plus the list of room names** with each request. This keeps prompts short and fast.
4. **Write the system prompt in English,** and tell the model to **reply in short, natural Vietnamese.** Small models often follow tool instructions more reliably in English, while still understanding Vietnamese commands.
5. **Validate every tool call on the server:**
   - unknown entity, invalid value, or missing argument → send the error back as the tool result, and let the model correct itself **once**
   - still invalid → reply with a short "Mình chưa hiểu, bạn nói lại được không?"
6. **Keep the fast path:** common commands ("tắt đèn", "bật quạt") are handled by pattern matching without calling the model at all.

---

## 6. Testing

1. Prepare **20–30 real Vietnamese commands**, including:
   - simple commands ("tắt điều hòa")
   - commands with a room ("bật quạt phòng con")
   - two-part commands ("tắt điều hòa rồi cho tôi biết thời tiết")
   - questions ("phòng khách bao nhiêu độ?")
   - ambiguous commands ("lạnh quá")
   - things it should refuse or ask about
2. For each, record: correct tool(s)? correct arguments? correct reply? time taken?
3. Compare **Qwen3 8B vs Qwen3 4B** with the same list, and re-run after any change to the prompt, tools, or settings.
4. **Target:** correct tool calls for nearly all simple and room commands, and a reply in under ~1–1.5 s from the language model step.

---

## 7. Quick reference

| Setting | Value |
|---|---|
| Thinking | Off (`think: false` / `enable_thinking: false`) |
| temperature / top_p / top_k / min_p | 0.7 (or ~0.5) / 0.8 / 20 / 0 |
| presence_penalty | 0, or ~1.0 if repetition appears |
| Context (`num_ctx`) | 4096–8192, must still fit 100% on GPU |
| Flash attention | On |
| KV cache type | q8_0 |
| Quantization | Q4_K_M, or Q4_K_S / IQ4_XS if tight |
| Endpoint | `/api/chat` or `/v1/chat/completions` with `tools` |
| llama.cpp flag | `--jinja` |
