from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from uuid import uuid4

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.mediastreams import MediaStreamError
from av import Packet

from camera_serve.application.camera_service import CameraApplicationService
from camera_serve.domain.exceptions import RtcCapacityError
from camera_serve.infrastructure.gstreamer_encoder import GStreamerH264Encoder

logger = logging.getLogger(__name__)


class LiveFrameSource:
    """One capture and H.264 encoder pipeline shared by all WebRTC tracks."""

    def __init__(
        self,
        camera_service: CameraApplicationService,
        fps: float,
        width: int = 960,
        height: int = 720,
        bitrate_kbps: int = 2_000,
        encoder_name: str = "auto",
        encoder: GStreamerH264Encoder | None = None,
    ) -> None:
        self._camera_service = camera_service
        self.fps = fps
        self.width = width
        self.height = height
        self._encoder = encoder or GStreamerH264Encoder(
            width=width,
            height=height,
            fps=max(1, round(fps)),
            bitrate_kbps=bitrate_kbps,
            requested_encoder=encoder_name,
        )
        self._encoder_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gstreamer-h264"
        )
        self._consumers: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._condition = asyncio.Condition()
        self._encoded_frame: bytes | None = None
        self._frame_pts = 0
        self._sequence = 0
        self._first_frame_at: float | None = None
        self._last_frame_at: float | None = None
        self.capture_duration_ms = 0.0
        self.encode_duration_ms = 0.0
        self.actual_fps = 0.0
        self.last_error: str | None = None

    async def acquire(self, consumer_id: str) -> None:
        self._consumers.add(consumer_id)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._produce(), name="mecheye-live-frame-producer"
            )

    async def release(self, consumer_id: str) -> None:
        self._consumers.discard(consumer_id)
        if self._consumers or self._task is None:
            return
        task = self._task
        self._task = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._encoder.close()
        async with self._condition:
            self._encoded_frame = None
            self._sequence = 0
            self._frame_pts = 0
            self._first_frame_at = None
            self._last_frame_at = None
            self.actual_fps = 0.0
            self._condition.notify_all()

    async def stop(self) -> None:
        self._consumers.clear()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._encoder.close()
        async with self._condition:
            self._encoded_frame = None
            self._sequence = 0
            self._frame_pts = 0
            self._first_frame_at = None
            self._last_frame_at = None
            self.actual_fps = 0.0
            self._condition.notify_all()

    async def shutdown(self) -> None:
        await self.stop()
        self._encoder_executor.shutdown(wait=True, cancel_futures=True)

    async def next_frame(self, last_sequence: int) -> tuple[int, bytes, int]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._sequence > last_sequence or self._task is None
            )
            if self._encoded_frame is None:
                raise MediaStreamError
            return self._sequence, self._encoded_frame, self._frame_pts

    def diagnostics(self) -> dict[str, object]:
        return {
            "encoder": self._encoder.encoder_name,
            "hardware_accelerated": self._encoder.hardware_accelerated,
            "width": self.width,
            "height": self.height,
            "target_fps": self.fps,
            "actual_fps": round(self.actual_fps, 2),
            "capture_duration_ms": round(self.capture_duration_ms, 2),
            "encode_duration_ms": round(self.encode_duration_ms, 2),
        }

    async def _produce(self) -> None:
        interval = 1.0 / self.fps
        try:
            while self._consumers:
                started = time.monotonic()
                try:
                    frame = await self._camera_service.capture_live_frame()
                    captured_at = time.monotonic()
                    self.capture_duration_ms = (captured_at - started) * 1000
                    encoded = await self._encode(frame)
                    encoded_at = time.monotonic()
                    self.encode_duration_ms = (encoded_at - captured_at) * 1000
                    if self._first_frame_at is None:
                        self._first_frame_at = captured_at
                    if self._last_frame_at is not None:
                        elapsed = encoded_at - self._last_frame_at
                        if elapsed > 0:
                            self.actual_fps = 1.0 / elapsed
                    self._last_frame_at = encoded_at
                    pts = max(
                        self._frame_pts + 1,
                        round((captured_at - self._first_frame_at) * 90_000),
                    )
                    async with self._condition:
                        self._encoded_frame = encoded.data
                        self._frame_pts = pts
                        self._sequence += 1
                        self.last_error = None
                        self._condition.notify_all()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # Exposed through health diagnostics.
                    self.last_error = str(exc)
                delay = interval - (time.monotonic() - started)
                if delay > 0:
                    await asyncio.sleep(delay)
        finally:
            async with self._condition:
                self._condition.notify_all()

    async def _encode(self, frame):
        future = self._encoder_executor.submit(self._encoder.encode, frame)
        try:
            while not future.done():
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            while not future.done():
                await asyncio.sleep(0.001)
            raise
        return future.result()


class MechEyeVideoTrack(VideoStreamTrack):
    def __init__(self, source: LiveFrameSource) -> None:
        super().__init__()
        self._source = source
        self._last_sequence = 0

    async def recv(self) -> Packet:
        self._last_sequence, data, pts = await self._source.next_frame(
            self._last_sequence
        )
        packet = Packet(data)
        packet.pts = pts
        packet.time_base = Fraction(1, 90_000)
        return packet


@dataclass(slots=True)
class PeerEntry:
    peer_id: str
    connection: RTCPeerConnection
    track: MechEyeVideoTrack
    created_at: float
    disconnected_at: float | None = None


class RtcPeerPool:
    def __init__(
        self,
        source: LiveFrameSource,
        max_peers: int = 8,
        disconnect_grace_seconds: float = 10.0,
        reap_interval_seconds: float = 5.0,
        ice_servers: tuple[dict[str, object], ...] = (),
    ) -> None:
        self._source = source
        self._max_peers = max_peers
        self._disconnect_grace_seconds = disconnect_grace_seconds
        self._reap_interval_seconds = reap_interval_seconds
        self._ice_servers = tuple(dict(server) for server in ice_servers)
        self._rtc_configuration = RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=server["urls"],
                    username=server.get("username"),
                    credential=server.get("credential"),
                )
                for server in self._ice_servers
            ]
        )
        self._peers: dict[str, PeerEntry] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(
                self._reap_disconnected(), name="rtc-peer-reaper"
            )

    async def create_answer(self, sdp: str, session_type: str) -> dict[str, str]:
        if session_type != "offer":
            raise ValueError("RTC session type must be 'offer'")

        peer_id = uuid4().hex
        connection = RTCPeerConnection(configuration=self._rtc_configuration)
        track = MechEyeVideoTrack(self._source)
        entry = PeerEntry(peer_id, connection, track, time.time())

        async with self._lock:
            if len(self._peers) >= self._max_peers:
                await connection.close()
                raise RtcCapacityError(
                    f"RTC peer pool reached its limit of {self._max_peers}"
                )
            self._peers[peer_id] = entry

        @connection.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            state = connection.connectionState
            logger.info("RTC peer %s connection state: %s", peer_id, state)
            if state in {"failed", "closed"}:
                await self.remove(peer_id)
            elif state == "disconnected":
                async with self._lock:
                    current = self._peers.get(peer_id)
                    if current is not None and current.disconnected_at is None:
                        current.disconnected_at = time.monotonic()
            else:
                async with self._lock:
                    current = self._peers.get(peer_id)
                    if current is not None:
                        current.disconnected_at = None

        @connection.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange() -> None:
            logger.info(
                "RTC peer %s ICE state: %s",
                peer_id,
                connection.iceConnectionState,
            )

        await self._source.acquire(peer_id)
        try:
            await connection.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=session_type)
            )
            sender = connection.addTrack(track)
            h264_codecs = [
                codec
                for codec in RTCRtpSender.getCapabilities("video").codecs
                if codec.mimeType.lower() == "video/h264"
            ]
            if not h264_codecs:
                raise RuntimeError("aiortc does not provide an H.264 RTP codec")
            transceiver = next(
                item
                for item in connection.getTransceivers()
                if item.sender is sender
            )
            transceiver.setCodecPreferences(h264_codecs)
            answer = await connection.createAnswer()
            await connection.setLocalDescription(answer)
            local = connection.localDescription
            if local is None:
                raise RuntimeError("WebRTC did not create a local description")
            return {"peer_id": peer_id, "sdp": local.sdp, "type": local.type}
        except Exception:
            await self.remove(peer_id)
            raise

    async def remove(self, peer_id: str) -> bool:
        async with self._lock:
            entry = self._peers.pop(peer_id, None)
        if entry is None:
            return False
        entry.track.stop()
        await entry.connection.close()
        await self._source.release(peer_id)
        return True

    async def close_all(self) -> None:
        reaper = self._reaper
        self._reaper = None
        if reaper is not None:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        await self.close_peers()
        await self._source.shutdown()

    async def close_peers(self) -> None:
        async with self._lock:
            peer_ids = list(self._peers)
        await asyncio.gather(
            *(self.remove(peer_id) for peer_id in peer_ids),
            return_exceptions=True,
        )
        await self._source.stop()

    async def snapshot(self) -> list[dict[str, object]]:
        async with self._lock:
            return [
                {
                    "peer_id": entry.peer_id,
                    "state": entry.connection.connectionState,
                    "ice_state": entry.connection.iceConnectionState,
                    "ice_gathering_state": entry.connection.iceGatheringState,
                    "created_at": entry.created_at,
                    "disconnected_at": entry.disconnected_at,
                }
                for entry in self._peers.values()
            ]

    async def count(self) -> int:
        async with self._lock:
            return len(self._peers)

    def browser_configuration(self) -> dict[str, list[dict[str, object]]]:
        return {"iceServers": [dict(server) for server in self._ice_servers]}

    async def _reap_disconnected(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._reap_interval_seconds)
                now = time.monotonic()
                async with self._lock:
                    expired = [
                        peer_id
                        for peer_id, entry in self._peers.items()
                        if entry.disconnected_at is not None
                        and now - entry.disconnected_at
                        >= self._disconnect_grace_seconds
                    ]
                for peer_id in expired:
                    await self.remove(peer_id)
        except asyncio.CancelledError:
            return
