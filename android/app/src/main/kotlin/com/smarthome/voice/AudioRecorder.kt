package com.smarthome.voice

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.concurrent.thread

/** Records 16 kHz mono PCM16 while started, streaming chunks to a callback. */
class AudioRecorder(
    private val sampleRate: Int = 16000,
    private val onChunk: (ByteArray) -> Unit,
) {
    @Volatile private var running = false
    private var audioRecord: AudioRecord? = null

    @SuppressLint("MissingPermission") // caller checks RECORD_AUDIO first
    fun start() {
        if (running) return
        running = true
        thread(isDaemon = true, name = "mic") {
            val minBuf = AudioRecord.getMinBufferSize(
                sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION, sampleRate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, 8192),
            )
            audioRecord?.startRecording()
            val buf = ShortArray(sampleRate / 10) // 100 ms chunks
            while (running) {
                val read = audioRecord?.read(buf, 0, buf.size) ?: break
                if (read > 0) {
                    val bytes = ByteBuffer.allocate(read * 2)
                        .order(ByteOrder.LITTLE_ENDIAN)
                        .asShortBuffer()
                        .put(buf, 0, read)
                        .array()
                    onChunk(bytes)
                }
            }
            try {
                audioRecord?.stop()
                audioRecord?.release()
            } catch (_: Exception) {
            }
            audioRecord = null
        }
    }

    fun stop() {
        running = false
    }
}
