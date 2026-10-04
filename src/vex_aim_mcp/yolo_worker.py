"""Runs YOLO in its own process, so loading PyTorch (several seconds of heavy work) can
never stall the MCP server's connection to the robot. Started by vision.YoloDetector.

Protocol over stdin/stdout, each message prefixed by a 4-byte big-endian length:
  request: JPEG bytes
  reply:   JSON {"size": [w, h], "boxes": [[name, x0, y0, x1, y1, confidence], ...]}
The first reply, sent once the model is loaded and warmed up, is {"ready": true}.
"""

import json
import os
import struct
import sys
from io import BytesIO


def _send(out, obj) -> None:
    data = json.dumps(obj).encode()
    out.write(struct.pack(">I", len(data)) + data)
    out.flush()


def main() -> None:
    protocol_in = sys.stdin.buffer
    protocol_out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # anything else that prints (Ultralytics logs) goes to stderr, not the protocol
    sys.stdout = sys.stderr

    model_path, confidence = sys.argv[1], float(sys.argv[2])
    from PIL import Image
    from ultralytics import YOLO

    model = YOLO(model_path)
    model.predict(Image.new("RGB", (640, 480)), verbose=False)  # the first inference is slow; do it now
    _send(protocol_out, {"ready": True})

    while True:
        header = protocol_in.read(4)
        if len(header) < 4:
            return  # parent closed the pipe
        jpeg = protocol_in.read(struct.unpack(">I", header)[0])
        try:
            img = Image.open(BytesIO(jpeg)).convert("RGB")
            r = model.predict(img, conf=confidence, verbose=False)[0]
            boxes = [[r.names[int(c)], *xyxy, p] for xyxy, c, p in
                     zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist())]
            _send(protocol_out, {"size": list(img.size), "boxes": boxes})
        except Exception as e:  # a bad frame shouldn't kill the worker
            _send(protocol_out, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
