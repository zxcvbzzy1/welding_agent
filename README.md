
## 快速启动

### 环境要求

- Python：依赖项 `requirements.txt`
- Node.js：`IM_front/package.json` 要求 `^20.19.0 || >=22.12.0`
- MongoDB：默认连接 `mongodb://localhost:27017/`

### 启动 IM 后端

从仓库根目录执行：

```bash
IM_SSE_COOKIE_SECURE=false python -m uvicorn im_backend.api.index:app --host 0.0.0.0 --port 8010
```

健康检查：

```bash
curl http://127.0.0.1:8010/health
```

### 启动前端

```bash
cd IM_front
npm install
npm run dev
```

默认访问 Vite 输出的本地地址，通常是：

```text
http://127.0.0.1:5173
```

网页使用同源 `/api`，Vite 默认代理到 `http://127.0.0.1:8010`。后端地址不同时启动前端：

```bash
IM_API_PROXY_TARGET=http://127.0.0.1:8011 npm run dev
```

移除旧的跨源 `VITE_IM_API_BASE_URL` 配置。生产 HTTPS 反向代理示例见 [deploy/nginx.im.conf.example](deploy/nginx.im.conf.example)，后端设置 `IM_SSE_COOKIE_SECURE=true`。

### 点云内联预览

Agent 调用 `inline_artifact` 时，`point_cloud` 的 `file_id`、`file_path`、`url` 三选一：

```json
{"artifact_type":"point_cloud","point_cloud":{"title":"工件点云","file_id":"上传接口返回的文件 UUID"}}
```

```json
{"artifact_type":"point_cloud","point_cloud":{"title":"工件点云","file_path":"/absolute/path/to/cloud.ply"}}
```

`file_id` 复用上传文件的鉴权下载接口。`file_path` 必须是运行服务器上的 PLY 绝对路径，最大 100 MiB，默认只允许仓库的 `upload/`、`agent_flow/temp/`。额外目录在 `.env` 中配置 `POINT_CLOUD_ALLOWED_ROOTS`（多个目录用系统路径分隔符连接，macOS/Linux 为 `:`，Windows 为 `;`）。例如：

```dotenv
POINT_CLOUD_ALLOWED_ROOTS=/absolute/path/to/point_cloud_inputs
```

本地文件会复制到 `agent_flow/store/point_clouds/`，可用 `POINT_CLOUD_ARTIFACT_ROOT` 更改保存目录；工具进程与 IM 后端必须使用同一个持久化目录和配置。预览 URL 为 `/api/im/point-clouds/{id}`，后端按运行记录的用户身份校验访问权限；原始文件移动或删除不影响已生成的预览。运行上下文由工具自动获取。运行记录被删除后，该预览不再可下载。

前端预览和下载按钮都会向 IM 接口携带登录 Token，下载后校验 PLY 文件头。兼容已有相机服务的 HTTP/HTTPS `url`，外部服务不会收到 IM Token。不要将本机路径填入 `url`，也无需启动临时 HTTP 服务。旧的无效预览卡片需要重新调用工具生成。

健康检查：

```bash
curl http://127.0.0.1:8000/health
```
