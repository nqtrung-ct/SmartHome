package com.smarthome.voice

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** WebSocket link to the server's voice gateway. */
class VoiceClient(
    private val serverUrl: String,
    private val token: String,
    private val events: Events,
) {
    interface Events {
        fun onOpen()
        fun onClosed()
        fun onFailure(reason: String)
        fun onMessage(json: JSONObject)
        fun onAudio(data: ByteArray)
    }

    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(5, TimeUnit.SECONDS)
        .build()
    private var ws: WebSocket? = null
    @Volatile var open = false
        private set

    fun connect() {
        if (open) return
        val url = serverUrl.trimEnd('/') + "/ws/voice?token=" + token
        val request = Request.Builder().url(url).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                open = true
                events.onOpen()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    events.onMessage(JSONObject(text))
                } catch (_: Exception) {
                }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                events.onAudio(bytes.toByteArray())
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                open = false
                events.onClosed()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                open = false
                events.onFailure(t.message ?: "lỗi kết nối")
            }
        })
    }

    fun sendStart(): Boolean = ws?.send("""{"type":"start"}""") ?: false

    fun sendStop(): Boolean = ws?.send("""{"type":"stop"}""") ?: false

    fun sendHandsfree(silenceMs: Int): Boolean =
        ws?.send("""{"type":"handsfree","silence_ms":$silenceMs}""") ?: false

    fun sendHandsfreeOff(): Boolean = ws?.send("""{"type":"handsfree_off"}""") ?: false

    fun sendAudio(pcm: ByteArray): Boolean =
        open && ws?.send(pcm.toByteString()) == true

    fun close() {
        open = false
        ws?.close(1000, "bye")
        ws = null
    }

    companion object {
        /** POST /api/pair - one-time code -> permanent token. Runs blocking. */
        fun pair(serverUrl: String, code: String, name: String): PairingResult {
            val url = serverUrl.trimEnd('/') + "/api/pair"
            val body = """{"code":"$code","client_name":"$name"}"""
                .toRequestBody("application/json; charset=utf-8".toMediaType())
            val request = Request.Builder().url(url).post(body).build()
            OkHttpClient().newCall(request).execute().use { resp ->
                val text = resp.body?.string() ?: ""
                return if (resp.isSuccessful) {
                    val json = JSONObject(text)
                    PairingResult(ok = true, token = json.getString("token"),
                        clientId = json.getInt("client_id"))
                } else {
                    val detail = try { JSONObject(text).getString("detail") } catch (_: Exception) { text }
                    PairingResult(ok = false, error = detail)
                }
            }
        }

        /** GET /api/me - shows which room this phone belongs to. */
        fun fetchRoom(serverUrl: String, token: String): String? {
            val url = serverUrl.trimEnd('/') + "/api/me?token=" + token
            val request = Request.Builder().url(url).build()
            OkHttpClient().newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val json = JSONObject(resp.body?.string() ?: "{}")
                return if (json.isNull("room")) null else json.getString("room")
            }
        }
    }

    data class PairingResult(val ok: Boolean, val token: String? = null,
                             val clientId: Int = -1, val error: String? = null)
}
