package com.smarthome.voice

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Bundle
import android.view.MotionEvent
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import kotlin.concurrent.thread

class MainActivity : Activity() {

    private lateinit var prefs: android.content.SharedPreferences

    private lateinit var setupCard: LinearLayout
    private lateinit var mainCard: LinearLayout
    private lateinit var serverInput: EditText
    private lateinit var nameInput: EditText
    private lateinit var codeInput: EditText
    private lateinit var setupStatus: TextView
    private lateinit var statusText: TextView
    private lateinit var roomText: TextView
    private lateinit var talkBtn: Button
    private lateinit var transcriptText: TextView
    private lateinit var replyText: TextView

    private var client: VoiceClient? = null
    private var recorder: AudioRecorder? = null
    private val player = AudioPlayer()

    private var serverUrl: String = ""
    private var token: String = ""
    private var discoveryListener: NsdManager.DiscoveryListener? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = getSharedPreferences("smarthome", Context.MODE_PRIVATE)

        setupCard = findViewById(R.id.setupCard)
        mainCard = findViewById(R.id.mainCard)
        serverInput = findViewById(R.id.serverInput)
        nameInput = findViewById(R.id.nameInput)
        codeInput = findViewById(R.id.codeInput)
        setupStatus = findViewById(R.id.setupStatus)
        statusText = findViewById(R.id.statusText)
        roomText = findViewById(R.id.roomText)
        talkBtn = findViewById(R.id.talkBtn)
        transcriptText = findViewById(R.id.transcriptText)
        replyText = findViewById(R.id.replyText)

        findViewById<Button>(R.id.discoverBtn).setOnClickListener { discoverServer() }
        findViewById<Button>(R.id.pairBtn).setOnClickListener { pair() }
        talkBtn.isEnabled = false
        talkBtn.setOnTouchListener { _, event -> handleTouch(event) }

        volumeControlStream = AudioManager.STREAM_MUSIC

        token = prefs.getString("token", "") ?: ""
        serverUrl = prefs.getString("server_url", "") ?: ""
        if (token.isNotEmpty() && serverUrl.isNotEmpty()) {
            enterMain()
        } else {
            nameInput.setText(android.os.Build.MODEL)
        }
    }

    // ---------------------------------------------------------------- setup

    private fun discoverServer() {
        setupStatus.text = "Đang tìm server trên mạng..."
        try {
            val nsd = getSystemService(Context.NSD_SERVICE) as NsdManager
            val listener = object : NsdManager.DiscoveryListener {
                override fun onDiscoveryStarted(serviceType: String) {}
                override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                    runOnUiThread { setupStatus.text = "Không tìm thấy (bấm lại sau)." }
                }

                override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                    nsd.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                        override fun onServiceResolved(info: NsdServiceInfo) {
                            runOnUiThread {
                                serverInput.setText("http://${info.host.hostAddress}:${info.port}")
                                setupStatus.text = "Đã tìm thấy server!"
                                stopDiscovery()
                            }
                        }

                        override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {}
                    })
                }

                override fun onServiceLost(serviceInfo: NsdServiceInfo) {}
                override fun onDiscoveryStopped(serviceType: String) {}
                override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            }
            discoveryListener = listener
            nsd.discoverServices("_smarthome._tcp.", NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            setupStatus.text = "Tự tìm thất bại - nhập tay địa chỉ server."
        }
    }

    private fun stopDiscovery() {
        val nsd = getSystemService(Context.NSD_SERVICE) as? NsdManager ?: return
        discoveryListener?.let { listener ->
            try { nsd.stopServiceDiscovery(listener) } catch (_: Exception) {}
        }
        discoveryListener = null
    }

    private fun pair() {
        val url = serverInput.text.toString().trim()
        val code = codeInput.text.toString().trim()
        val name = nameInput.text.toString().trim().ifEmpty { android.os.Build.MODEL }
        if (!url.startsWith("http") || code.length < 4) {
            setupStatus.text = "Nhập địa chỉ server và mã ghép nối (6 số)."
            return
        }
        setupStatus.text = "Đang ghép nối..."
        thread {
            val result = VoiceClient.pair(url, code, name)
            runOnUiThread {
                if (result.ok && result.token != null) {
                    prefs.edit()
                        .putString("token", result.token)
                        .putString("server_url", url)
                        .putString("name", name)
                        .apply()
                    serverUrl = url
                    token = result.token!!
                    enterMain()
                } else {
                    setupStatus.text = "Lỗi: ${result.error}"
                }
            }
        }
    }

    // ---------------------------------------------------------------- main

    private fun enterMain() {
        setupCard.visibility = android.view.View.GONE
        mainCard.visibility = android.view.View.VISIBLE
        statusText.text = "Đang kết nối..."
        connectClient()
        refreshRoom()
    }

    private fun refreshRoom() {
        thread {
            val room = VoiceClient.fetchRoom(serverUrl, token)
            runOnUiThread {
                roomText.text = if (room != null) "Phòng: $room" else "Chưa gán phòng (xem admin UI)"
            }
        }
    }

    private fun connectClient() {
        client?.close()
        client = VoiceClient(serverUrl, token, object : VoiceClient.Events {
            override fun onOpen() {
                runOnUiThread {
                    statusText.text = "● Đã kết nối"
                    talkBtn.isEnabled = true
                }
            }

            override fun onClosed() {
                runOnUiThread {
                    statusText.text = "○ Mất kết nối - nhấn nút nói để thử lại"
                    talkBtn.isEnabled = false
                }
            }

            override fun onFailure(reason: String) {
                runOnUiThread {
                    statusText.text = "○ Mất kết nối ($reason)"
                    talkBtn.isEnabled = false
                }
            }

            override fun onMessage(json: JSONObject) {
                runOnUiThread {
                    when (json.optString("type")) {
                        "transcript" -> transcriptText.text = json.optString("text", "")
                        "reply" -> replyText.text = json.optString("text", "")
                        "announce" -> replyText.text = "🔔 " + json.optString("text", "")
                        "audio" -> player.start(json.optInt("sample_rate", 16000))
                        "end" -> player.finish()
                    }
                }
            }

            override fun onAudio(data: ByteArray) {
                player.write(data)
            }
        })
        client?.connect()
    }

    // ---------------------------------------------------------------- talk

    private fun handleTouch(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED
                ) {
                    requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
                    return false
                }
                startTalking()
                return true
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                stopTalking()
                return true
            }
        }
        return false
    }

    private fun startTalking() {
        val c = client
        if (c == null || !c.open) {
            connectClient()
            Toast.makeText(this, "Đang kết nối lại, thử lại sau giây lát", Toast.LENGTH_SHORT).show()
            return
        }
        transcriptText.text = "…"
        replyText.text = ""
        if (!c.sendStart()) return
        recorder = AudioRecorder(16000) { chunk -> c.sendAudio(chunk) }
        recorder?.start()
        talkBtn.text = "🎙 ĐANG NGHE... (thả để gửi)"
        statusText.text = "● Đang nghe"
    }

    private fun stopTalking() {
        recorder?.stop()
        recorder = null
        client?.sendStop()
        talkBtn.text = getString(R.string.talk_button)
        statusText.text = "● Đang xử lý..."
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1 && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            startTalking()
        }
    }

    override fun onDestroy() {
        recorder?.stop()
        client?.close()
        player.stop()
        super.onDestroy()
    }
}
