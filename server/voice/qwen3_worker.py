"""Isolated Qwen worker; communicates with the main server using JSON lines."""
import base64
import contextlib
import json
import sys


def main():
    # Windows pipes otherwise use a legacy code page and fail on Vietnamese.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    # ML libraries may print progress to stdout: reserve it for the protocol.
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import torch
        from qwen_asr import Qwen3ASRModel
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA không sẵn sàng cho Qwen3-ASR")
        model = Qwen3ASRModel.from_pretrained(
            sys.argv[1], dtype=torch.bfloat16, device_map="cuda:0",
            max_inference_batch_size=1, max_new_tokens=512,
        )
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            pcm = base64.b64decode(request["audio"], validate=True)
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            with contextlib.redirect_stdout(sys.stderr):
                result = model.transcribe(audio=(samples, 16000), language="Vietnamese",
                                          context=request.get("context") or "")[0]
            response = {"text": result.text.strip()}
        except Exception as exc:
            response = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
