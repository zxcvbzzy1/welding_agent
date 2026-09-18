# IM Backend

独立的 IM Agent Platform 后端。它保留 IM 房间、富消息、artifact、Claude Code / Codex 适配等产品逻辑，并通过 `infra/agent_flow_bridge/` 集中复用 `agent_flow` 的 Agent、Run、PlanOrchestrator、SSE 和 MongoDB 存储能力。

## 启动

```bash
PYTHONPATH=/Users/zxcvbzzy1/Desktop/项目/ByteDance_AgentHub \
/Users/zxcvbzzy1/miniconda3/envs/MY_env/bin/python -m uvicorn im_backend.api.index:app --host 127.0.0.1 --port 8010
```

前端默认连接 `http://127.0.0.1:8010`，也可以通过 `IM_front/.env` 设置：

```text
VITE_IM_API_BASE_URL=http://127.0.0.1:8010
```

## API

- `GET /health`
- `GET /api/im/agents`
- `POST /api/im/rooms`
- `GET /api/im/rooms`
- `GET /api/im/rooms/{room_id}`
- `GET /api/im/rooms/{room_id}/messages`
- `POST /api/im/rooms/{room_id}/messages`
- `POST /api/im/rooms/{room_id}/dispatch`
- `GET /api/im/rooms/{room_id}/stream`
- `POST /api/im/messages/{message_id}/actions`
- `POST /api/im/artifacts/upload`
- `GET /api/im/artifacts/{artifact_id}`

## 安全默认值

Claude Code / Codex agent 第一版默认需要人工确认；未确认前只生成确认卡片，不直接启动外部 CLI。Runner 的命令构造使用只读/计划模式，不使用危险跳权参数。

## 聊天文件上传

聊天附件由 `application/services/file/` 管理，实体默认保存在仓库根目录
`upload/<file_id>/<安全文件名>`，`im_files.storage_path` 保存绝对路径。
后端与 Native、Claude Code、Codex 需共享本机文件系统；移动仓库后需迁移数据库内的绝对路径。

- `POST /api/im/files/upload`：Bearer 登录，multipart 字段 `file`；单文件最大 20 MiB。
- `GET /api/im/files?limit=50&before=<file_id>`：分页查询自己的已发送文件，返回
  `items`、`has_more`、`next_cursor`。
- `GET /api/im/files/{file_id}/download`：已发送文件允许所有登录用户下载，暂存文件仅上传者可下载。
- `DELETE /api/im/files/{file_id}`：仅上传者可删除实体，元数据保留为 `delete`，重复删除幂等。
- 单聊／群聊原消息接口接收 `content_parts: [{"type":"file","file_id":"..."}]`，
  支持与文本混合及纯附件消息，每条最多 10 个附件。服务端校验上传者并补齐可信属性。
  前端在既有 `metadata` 中携带 UUID `client_message_id`，网络重试沿用此值，避免重复入库。

`im_files` 保存文件身份、上传者、名称、类型、实际大小、绝对路径、状态、过期时间及创建／更新时间。
`im_conversation_files` 保存 `relation_id`、`file_id`、`message_id`、`conversation_id`、`room_id` 和时间戳。
索引在后端启动时幂等创建，其中 `(file_id, message_id)` 唯一。

上传为 `pending`；成功发送后为 `attached`，永久保留到上传者主动删除。
删除消息／会话／群聊只清除引用。删除实体先置 `deleting`，成功后置 `delete`；失败保留状态等待重试。
未发送文件 24 小时后过期，启动时和每小时由应用清理实体并保留删除记录，恢复中断的关联操作。
只有匹配消息／会话关联且状态有效的文件路径进入当前消息、历史或引用上下文；不自动读取内容、OCR、RAG 或发送视觉附件。

数据库内存兜底已全局移除。启动前必须运行 MongoDB（默认 `127.0.0.1:27017`），
可用 `IM_MONGO_URL` / `IM_MONGO_DB` 配置 IM 数据库。运行期间数据库故障返回 503。
单元测试可显式注入测试替身，集成测试使用独立临时数据库。

文件服务验证（会自动创建并删除 `im_file_test_<uuid>` 测试库，不操作业务库）：

```bash
/Users/zxcvbzzy1/miniconda3/envs/MY_env/bin/python -m pytest im_backend/checks/file_upload_test.py im_backend/tests/test_prompting.py -v
npm --prefix IM_front run build
```


<!-- 重复原因不是 artifacts.py 重复发布，而是前端同时渲染了：
- 实时 artifacts.* 事件卡片。
- 最终消息 content_parts 中持久化的同一张卡片。
现在 [runtimeEvents.js](/home/ByteDance_AgentHub/IM_front/src/utils/runtimeEvents.js) 会通过 artifact_source.event_id，并以同一 run 内的产物特征作为兜底去重：
- 运行过程中仍实时显示卡片。
- 最终消息落库后，只保留消息内的正式卡片。
- 同时移除重复的 run 产物摘要和打包下载项。 -->