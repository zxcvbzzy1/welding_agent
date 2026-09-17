# Mech-Eye Camera Service

基于 FastAPI 的单相机服务，提供相机发现/连接、2D 图、深度图、点云、坐标转换、文件下载和 WebRTC 实时预览。实时链路将相机原图缩放为 960×720，使用单个共享 GStreamer H.264 编码器，避免多个 RTC 客户端重复编码。

## 启动

确认已安装 Mech-Eye SDK，并使用项目的 `camera` conda 环境：

Ubuntu 还需要 GStreamer 核心工具、WebRTC/H.264 插件：

```bash
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
```

```bash
conda run -n camera python -m pip install -r camera_serve/requirements.txt
conda run -n camera uvicorn camera_serve.main:app --host 0.0.0.0 --port 8000
```

接口文档位于 `http://<server-ip>:8000/docs`。服务不会在启动时自动连接相机，应先调用发现和连接接口：

首版未加入鉴权，并允许跨域请求，适合在受信任的设备局域网中部署，不应直接暴露到公网。

```bash
curl http://127.0.0.1:8000/api/v1/cameras
curl -X POST http://127.0.0.1:8000/api/v1/cameras/connect \
  -H 'Content-Type: application/json' \
  -d '{"serial_number":"CAMERA_SERIAL_NUMBER"}'
```

## 采集

```bash
# 2D 图
curl -X POST http://127.0.0.1:8000/api/v1/captures/2d

# 深度图
curl -X POST http://127.0.0.1:8000/api/v1/captures/depth

# 点云：临时应用采集参数、最多保存 250000 点，并额外生成坐标转换后的 PLY
curl -X POST http://127.0.0.1:8000/api/v1/captures/point-cloud \
  -H 'Content-Type: application/json' \
  -d '{
    "textured": true,
    "custom_reference_frame": false,
    "max_points": 250000,
    "parameters": {
      "depth_range": {"min_mm": 100, "max_mm": 1000},
      "roi": {"x": 0, "y": 0, "width": 500, "height": 500},
      "exposure_sequence_ms": [5, 10],
      "surface_smoothing": "Normal",
      "noise_removal": "Normal",
      "outlier_removal": "Normal",
      "edge_preservation": "Sharp"
    },
    "transformation": [
      [1, 0, 0, 100],
      [0, 1, 0, 0],
      [0, 0, 1, 0],
      [0, 0, 0, 1]
    ]
  }'

# 请求体可以省略，此时等价于 textured=true、custom_reference_frame=false
curl -X POST http://127.0.0.1:8000/api/v1/captures/point-cloud

# 同一次拍摄保存全部数据
curl -X POST http://127.0.0.1:8000/api/v1/captures/all \
  -H 'Content-Type: application/json' \
  -d '{"textured":true,"custom_reference_frame":false}'
```

`max_points=0` 表示保存全部有效点，正整数表示对 SDK 采集到的完整点云做确定性均匀抽样并最多保存该数量。抽样作用于无纹理和纹理 PLY；若传入 `transformation`，服务还会保留原始 PLY，并生成 `transformed_point_cloud` / `transformed_textured_point_cloud`。响应的 `point_cloud.original_points` 和 `point_cloud.saved_points` 分别给出采集有效点数与实际保存点数。

`parameters` 中未提供的字段保持相机当前值；提供的字段只在这次点云采集期间临时生效，采集完成（包括采集失败）后会恢复原值，不会调用 `save_all_parameters_to_device()`。`custom_reference_frame=true` 时，4×4 `transformation` 继续作用于 Viewer 自定义坐标系下的结果。

文件保存在 `camera_serve/captures/YYYYMMDD/<capture_id>/`。响应中的路径相对于 `camera_serve/`，可用 `/api/v1/files/{relative_path}` 下载。

坐标转换遵循列向量约定：

```text
p_target = T_target_from_source @ [x, y, z, 1]
```

`POST /api/v1/transforms/points` 请求示例：

```json
{
  "points": [{"x": 1, "y": 2, "z": 3}],
  "transformation": [
    [1, 0, 0, 100],
    [0, 1, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1]
  ]
}
```

## WebRTC

浏览器必须先创建一个 `recvonly` 视频 transceiver，并等待本地 ICE candidate 收集完成，再把完整 SDP 发到 `/api/v1/rtc/offer`：

```javascript
const cameraApiBase = "http://10.42.0.1:7899";
const rtcConfig = await fetch(`${cameraApiBase}/api/v1/rtc/config`)
  .then((response) => response.json());
const pc = new RTCPeerConnection(rtcConfig);
pc.addTransceiver("video", { direction: "recvonly" });
pc.ontrack = ({ streams: [stream] }) => {
  document.querySelector("video").srcObject = stream;
};

await pc.setLocalDescription(await pc.createOffer());
if (pc.iceGatheringState !== "complete") {
  await new Promise((resolve) => {
    const listener = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", listener);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", listener);
  });
}

const response = await fetch(`${cameraApiBase}/api/v1/rtc/offer`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(pc.localDescription),
});
const answer = await response.json();
await pc.setRemoteDescription(answer);
// answer.peer_id 可用于 DELETE /api/v1/rtc/peers/{peer_id}
```

所有连接共享一个 2D 采集生产者；最后一个连接关闭后生产者自动停止。浏览器和服务端必须使用同一组 STUN/TURN 配置，服务通过 `/api/v1/rtc/config` 把配置提供给前端。

实时编码默认使用 `CAMERA_STREAM_ENCODER=auto`：优先选择 Jetson/NVIDIA 的 `nvv4l2h264enc`，不可用时回退到 `x264enc` 的 `ultrafast + zerolatency`。可通过 `/health` 的 `stream.encoder` 与 `stream.hardware_accelerated` 确认实际编码器。硬件编码要求 NVIDIA 驱动正常，并存在对应 `/dev/nv*` 设备节点。

容器、跨子网或 NAT 环境应配置 `CAMERA_RTC_ICE_SERVERS`。它是 JSON 数组；只有 STUN 时可发现公网地址，但 STUN 不能绕过严格防火墙或对称 NAT：

```bash
export CAMERA_RTC_ICE_SERVERS='[{"urls":"stun:your-stun.example.com:3478"}]'
```

无法确保端到端 UDP 直连时，应部署可被浏览器和相机服务同时访问的 TURN（例如 coturn）：

```bash
export CAMERA_RTC_ICE_SERVERS='[
  {
    "urls":[
      "turn:turn.example.com:3478?transport=udp",
      "turn:turn.example.com:3478?transport=tcp"
    ],
    "username":"camera",
    "credential":"replace-with-secret"
  }
]'
```

仅开放 FastAPI 的 `7899/TCP` 不足以承载 WebRTC。若不使用 TURN，需要允许 aiortc 和浏览器之间的 UDP candidate 流量；有防火墙或复杂 NAT 时优先使用 TURN。

可选环境变量：

- `CAMERA_STREAM_FPS`，默认 `10`
- `CAMERA_STREAM_WIDTH`，默认 `960`
- `CAMERA_STREAM_HEIGHT`，默认 `720`
- `CAMERA_STREAM_BITRATE_KBPS`，默认 `2000`
- `CAMERA_STREAM_ENCODER`，默认 `auto`，可设为 `nvv4l2h264enc` 或 `x264enc`
- `CAMERA_RTC_MAX_PEERS`，默认 `8`
- `CAMERA_RTC_DISCONNECT_GRACE_SECONDS`，默认 `10`
- `CAMERA_RTC_REAP_INTERVAL_SECONDS`，默认 `5`
- `CAMERA_RTC_ICE_SERVERS`，默认使用 `stun:stun.l.google.com:19302`，格式见上方 STUN/TURN 示例；生产环境建议替换为可控的 TURN/STUN 服务

## 测试

核心测试不需要连接真实相机：

```bash
conda run -n camera python -m unittest discover -s camera_serve/tests -v
```
