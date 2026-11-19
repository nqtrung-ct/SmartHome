import httpx

response = httpx.post('http://127.0.0.1:8125/tts', json={
    'text': 'Xin chào, hệ thống đã sẵn sàng.', 'model': 'vieneu', 'voice': 'Mai Anh'
}, timeout=300)
response.raise_for_status()
print('TTS model:', response.headers.get('X-TTS-Model'), 'audio bytes:', len(response.content))
if response.headers.get('X-TTS-Model') != 'vieneu' or len(response.content) < 1000:
    raise SystemExit('TTS did not produce the requested audio')
