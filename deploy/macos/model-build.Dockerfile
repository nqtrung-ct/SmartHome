# Build the YOLOv9 tiny 320px ONNX model recommended for Frigate's Apple Silicon detector.
# Source: https://docs.frigate.video/configuration/object_detectors/#apple-silicon-detector
FROM python:3.11 AS build
RUN apt-get update && apt-get install --no-install-recommends -y cmake libgl1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /bin/
WORKDIR /yolov9
COPY model-build-src/yolov9/ .
RUN uv pip install --system --index-url https://download.pytorch.org/whl/cpu 'torch==2.7.1+cpu'
RUN uv pip install --system --no-deps 'torchvision==0.22.1'
RUN uv pip install --system -r requirements.txt
RUN uv pip install --system onnx==1.18.0 onnxruntime onnxscript
COPY model-build-src/yolov9-t-converted.pt yolov9-t.pt
RUN sed -i "s/ckpt = torch.load(attempt_download(w), map_location='cpu')/ckpt = torch.load(attempt_download(w), map_location='cpu', weights_only=False)/g" models/experimental.py
RUN python3 export.py --weights ./yolov9-t.pt --imgsz 320 --include onnx

FROM scratch
COPY --from=build /yolov9/yolov9-t.onnx /yolo.onnx
