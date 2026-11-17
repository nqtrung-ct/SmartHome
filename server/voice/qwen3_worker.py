"""Isolated MLX worker; communicates with the main server using JSON lines."""
import base64
import contextlib
import json
import sys


def main():
    # ML libraries may print progress to stdout: reserve it for the protocol.
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        from mlx_audio.stt import load
        model = load(sys.argv[1])
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            pcm = base64.b64decode(request["audio"], validate=True)
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            with contextlib.redirect_stdout(sys.stderr):
                result = model.generate(samples, language="Vietnamese", max_tokens=512,
                                        system_prompt=request.get("context") or None)
            response = {"text": result.text.strip()}
        except Exception as exc:
            response = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
