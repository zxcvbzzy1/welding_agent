from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

DEFAULT_RTC_ICE_SERVERS = '[{"urls":"stun:stun.l.google.com:19302"}]'


@dataclass(frozen=True, slots=True)
class Settings:
    stream_fps: float = 10.0
    stream_width: int = 960
    stream_height: int = 720
    stream_bitrate_kbps: int = 2_000
    stream_encoder: str = "auto"
    rtc_max_peers: int = 8
    rtc_disconnect_grace_seconds: float = 10.0
    rtc_reap_interval_seconds: float = 5.0
    rtc_ice_servers: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            stream_fps=float(os.getenv("CAMERA_STREAM_FPS", "10")),
            stream_width=int(os.getenv("CAMERA_STREAM_WIDTH", "960")),
            stream_height=int(os.getenv("CAMERA_STREAM_HEIGHT", "720")),
            stream_bitrate_kbps=int(
                os.getenv("CAMERA_STREAM_BITRATE_KBPS", "2000")
            ),
            stream_encoder=os.getenv("CAMERA_STREAM_ENCODER", "auto"),
            rtc_max_peers=int(os.getenv("CAMERA_RTC_MAX_PEERS", "8")),
            rtc_disconnect_grace_seconds=float(
                os.getenv("CAMERA_RTC_DISCONNECT_GRACE_SECONDS", "10")
            ),
            rtc_reap_interval_seconds=float(
                os.getenv("CAMERA_RTC_REAP_INTERVAL_SECONDS", "5")
            ),
            rtc_ice_servers=cls._parse_ice_servers(
                os.getenv("CAMERA_RTC_ICE_SERVERS", DEFAULT_RTC_ICE_SERVERS)
            ),
        )

    @staticmethod
    def _parse_ice_servers(raw_value: str) -> tuple[dict[str, Any], ...]:
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError("CAMERA_RTC_ICE_SERVERS must be valid JSON") from exc
        if not isinstance(value, list):
            raise ValueError("CAMERA_RTC_ICE_SERVERS must be a JSON array")

        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or not item.get("urls"):
                raise ValueError("Every ICE server must contain a non-empty 'urls'")
            urls = item["urls"]
            if not isinstance(urls, (str, list)):
                raise ValueError("ICE server 'urls' must be a string or string array")
            if isinstance(urls, list) and (
                not urls or any(not isinstance(url, str) for url in urls)
            ):
                raise ValueError("ICE server 'urls' must contain strings")
            normalized.append(
                {
                    key: item[key]
                    for key in ("urls", "username", "credential")
                    if key in item and item[key] is not None
                }
            )
        return tuple(normalized)
