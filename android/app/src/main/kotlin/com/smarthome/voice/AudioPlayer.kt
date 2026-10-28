package com.smarthome.voice

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack

/** Streams TTS PCM/WAV audio chunks to the speaker.

The server sends {"type":"audio","sample_rate":N} then raw chunks (a WAV
header rides along in the first chunk and is skipped here).
*/
class AudioPlayer {
    private var track: AudioTrack? = null
    private var skipHeader = 0

    @Synchronized
    fun start(sampleRate: Int) {
        stop()
        skipHeader = 44
        track = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(sampleRate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build()
            )
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(sampleRate * 2) // 1s buffer
            .build()
        track?.play()
    }

    @Synchronized
    fun write(data: ByteArray) {
        var chunk = data
        if (skipHeader > 0 && chunk.size > skipHeader) {
            chunk = chunk.copyOfRange(skipHeader, chunk.size)
            skipHeader = 0
        } else if (skipHeader > 0) {
            skipHeader -= chunk.size
            return
        }
        track?.write(chunk, 0, chunk.size)
    }

    @Synchronized
    fun finish() {
        // writes block only until enqueued; give the tail a moment before stopping
        track?.let { t ->
            Thread {
                try {
                    Thread.sleep(250)
                    t.stop()
                    t.release()
                } catch (_: Exception) {
                }
            }.start()
        }
    }

    @Synchronized
    fun stop() {
        try {
            track?.stop()
            track?.release()
        } catch (_: Exception) {
        }
        track = null
    }
}
