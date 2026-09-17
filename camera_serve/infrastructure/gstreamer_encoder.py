from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass

import cv2
import numpy as np
from aiortc.codecs.h264 import H264PayloadDescriptor


@dataclass(frozen=True, slots=True)
class EncodedFrame:
    data: bytes
    encoder: str


class GStreamerH264Encoder:
    """Persistent low-latency GStreamer encoder with RTP frame boundaries."""

    HARDWARE_ENCODERS = ("nvv4l2h264enc",)

    def __init__(
        self,
        width: int = 960,
        height: int = 720,
        fps: int = 10,
        bitrate_kbps: int = 2_000,
        requested_encoder: str = "auto",
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps
        self._auto_encoder = requested_encoder.strip().lower() == "auto"
        self.encoder_name = self._select_encoder(requested_encoder)
        self._socket: socket.socket | None = None
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def hardware_accelerated(self) -> bool:
        return self.encoder_name in self.HARDWARE_ENCODERS

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.close()

        output_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        output_socket.bind(("127.0.0.1", 0))
        output_socket.settimeout(5.0)
        output_port = output_socket.getsockname()[1]
        process = subprocess.Popen(
            self._pipeline_command(output_port),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._socket = output_socket
        self._process = process

    def encode(self, image: np.ndarray) -> EncodedFrame:
        try:
            return self._encode_current(image)
        except (OSError, RuntimeError) as exc:
            if not self._auto_encoder or not self.hardware_accelerated:
                raise RuntimeError(
                    f"GStreamer {self.encoder_name} encoding failed: {exc}"
                ) from exc
            self.close()
            if not self._gst_element_available("x264enc"):
                raise RuntimeError(
                    f"GStreamer hardware encoding failed and x264enc is unavailable: {exc}"
                ) from exc
            self.encoder_name = "x264enc"
            return self._encode_current(image)

    def _encode_current(self, image: np.ndarray) -> EncodedFrame:
        self.start()
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._socket is not None

        if self._process.poll() is not None:
            raise RuntimeError(
                f"GStreamer encoder exited with code {self._process.returncode}"
            )
        resized = cv2.resize(
            image,
            (self.width, self.height),
            interpolation=cv2.INTER_AREA,
        )
        if resized.ndim == 2:
            resized = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
        resized = np.ascontiguousarray(resized, dtype=np.uint8)
        self._process.stdin.write(resized.tobytes())

        annex_b = bytearray()
        while True:
            datagram, _ = self._socket.recvfrom(65_535)
            payload, marker = self._rtp_payload(datagram)
            _, nal_data = H264PayloadDescriptor.parse(payload)
            annex_b.extend(nal_data)
            if marker:
                break
        return EncodedFrame(bytes(annex_b), self.encoder_name)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _pipeline_command(self, output_port: int) -> list[str]:
        frame_bytes = self.width * self.height * 3
        command = [
            "gst-launch-1.0",
            "-q",
            "fdsrc",
            "fd=0",
            f"blocksize={frame_bytes}",
            "!",
            "rawvideoparse",
            f"width={self.width}",
            f"height={self.height}",
            "format=bgr",
            f"framerate={self.fps}/1",
            "!",
            "videoconvert",
            "!",
        ]
        if self.encoder_name == "nvv4l2h264enc":
            command.extend(
                [
                    "video/x-raw,format=I420",
                    "!",
                    "nvv4l2h264enc",
                    f"bitrate={self.bitrate_kbps * 1000}",
                    f"iframeinterval={self.fps}",
                    "insert-sps-pps=true",
                    "insert-aud=true",
                    "maxperf-enable=true",
                ]
            )
        else:
            command.extend(
                [
                    "video/x-raw,format=I420",
                    "!",
                    "x264enc",
                    "tune=zerolatency",
                    "speed-preset=ultrafast",
                    f"key-int-max={self.fps}",
                    f"bitrate={self.bitrate_kbps}",
                    "byte-stream=true",
                    "aud=true",
                    "bframes=0",
                ]
            )
        command.extend(
            [
                "!",
                "h264parse",
                "config-interval=-1",
                "!",
                "video/x-h264,profile=constrained-baseline,stream-format=byte-stream,alignment=au",
                "!",
                "rtph264pay",
                "pt=96",
                "mtu=1200",
                "config-interval=-1",
                "aggregate-mode=zero-latency",
                "!",
                "udpsink",
                "host=127.0.0.1",
                f"port={output_port}",
                "sync=false",
                "async=false",
            ]
        )
        return command

    @classmethod
    def _select_encoder(cls, requested: str) -> str:
        requested = requested.strip().lower()
        supported = (*cls.HARDWARE_ENCODERS, "x264enc")
        if requested == "auto":
            for encoder in cls.HARDWARE_ENCODERS:
                if cls._gst_element_available(encoder):
                    return encoder
            if cls._gst_element_available("x264enc"):
                return "x264enc"
            raise RuntimeError("No supported GStreamer H.264 encoder is available")
        if requested not in supported:
            raise ValueError(
                f"Unsupported CAMERA_STREAM_ENCODER '{requested}'; "
                f"expected auto, {', '.join(supported)}"
            )
        if not cls._gst_element_available(requested):
            raise RuntimeError(f"GStreamer encoder is unavailable: {requested}")
        return requested

    @staticmethod
    def _gst_element_available(element: str) -> bool:
        try:
            result = subprocess.run(
                ["gst-inspect-1.0", element],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    @staticmethod
    def _rtp_payload(datagram: bytes) -> tuple[bytes, bool]:
        if len(datagram) < 12 or datagram[0] >> 6 != 2:
            raise RuntimeError("GStreamer produced an invalid RTP packet")
        marker = bool(datagram[1] & 0x80)
        offset = 12 + (datagram[0] & 0x0F) * 4
        if datagram[0] & 0x10:
            if len(datagram) < offset + 4:
                raise RuntimeError("Truncated RTP extension header")
            extension_words = int.from_bytes(datagram[offset + 2 : offset + 4], "big")
            offset += 4 + extension_words * 4
        end = len(datagram)
        if datagram[0] & 0x20:
            end -= datagram[-1]
        if offset >= end:
            raise RuntimeError("GStreamer produced an empty RTP payload")
        return datagram[offset:end], marker
