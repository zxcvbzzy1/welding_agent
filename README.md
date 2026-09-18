# AgentHub

AgentHub 是一个以 IM 聊天为交互入口的多 Agent 协作平台。用户可以通过单聊、群聊、@ Agent、收藏上下文、消息回复/引用、运行轨迹和产物卡片，驱动 native Agent、Codex、Claude Code 等执行网页、文档、代码修改、部署等任务。

项目由三部分组成：

- `IM_front/`：Vue 3 + Vite 前端，负责聊天工作台、Agent/工具/技能管理、SSE 事件消费和 Artifact 渲染。
- `im_backend/`：FastAPI IM 产品后端，负责认证、会话、房间、消息、收藏、产物、部署、工具/技能入口和外部 coding agent 适配。
- `agent_flow/`：Agent 核心运行系统，负责 Agent、Context、Tool、Run、PlanOrchestrator、事件总线和工具执行。

## 演示

[视频演示地址](https://my.feishu.cn/file/A9mCbC95BoCs5HxkDyTc9U4Hn6c)

## 快速启动

### 环境要求

- Python：依赖项 `requirements.txt`
- Node.js：`IM_front/package.json` 要求 `^20.19.0 || >=22.12.0`
- MongoDB：默认连接 `mongodb://localhost:27017/`，不可用时后端存储会降级到内存，适合本地调试

常用环境变量：

| 变量 | 说明 | 默认值 |
| ---- | ---- | ------ |
| `VITE_IM_API_BASE_URL` | 前端连接的 IM 后端地址 | `http://127.0.0.1:8010` |
| `IM_MONGO_URL` | IM 后端 MongoDB 地址 | `mongodb://localhost:27017/` |
| `IM_MONGO_DB` | IM 后端数据库名 | `im_backend` |
| `IM_ARTIFACT_ROOT` | IM artifact 文件目录 | `im_backend/storage/artifacts` |
| `IM_AGENT_WORKDIR` | Agent 默认工作目录 | 仓库上级目录 |
| `AGENT_FLOW_ROOT` | im_backend 引用的 Agent Flow 根目录 | `agent_flow/` |
| `AGENT_FLOW_MONGO_URL` | Agent Flow API MongoDB 地址 | `mongodb://localhost:27017/` |
| `AGENT_FLOW_MONGO_DB` | Agent Flow API 数据库名 | `agent_flow` |
| `LLM_BASE_URL` | LLM 服务地址 | `https://api.deepseek.com` |
| `DEEPSEEK_API` / `GLM_API` / `MINIMAX_API` | LLM API Key | 无 |

`im_backend` 启动时会按顺序读取 `.env`、`im_backend/.env`、`agent_flow/.env`。

### 启动 IM 后端

从仓库根目录执行：

```bash
python -m uvicorn im_backend.api.index:app --host 0.0.0.0 --port 8010
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

如果后端地址不是 `8010`，在 `IM_front/.env` 中配置：

```text
VITE_IM_API_BASE_URL=http://127.0.0.1:8010
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 项目目录

```text
.
├── IM_front/              # Vue 3 前端应用
├── im_backend/            # IM 产品后端
├── agent_flow/            # Agent 核心运行系统
├── mid/                   # 中间层/实验性模块
├── tests/                 # 根目录测试与辅助用例
├── README.md              # 当前项目说明
└── .gitignore
```

### `IM_front/`

```text
IM_front/
├── src/api/               # HTTP client 与 IM API 封装
├── src/router/            # Vue Router 与鉴权跳转
├── src/stores/            # Pinia store，消息、房间、SSE、产物状态
├── src/views/             # ChatView、工具/技能/认证等页面
├── src/components/        # ArtifactCard 等通用组件
├── src/utils/             # runtime events 聚合、压缩、去重
├── package.json           # npm 脚本与依赖
└── vite.config.js         # Vite 配置
```

### `im_backend/`

```text
im_backend/
├── api/                   # FastAPI app、auth router、/api/im 路由
├── application/           # 应用服务：消息、房间、run、artifact、tool、skill
├── domain/                # IM 领域模型：conversation、message、room、agent
├── infra/                 # 存储、Agent Flow bridge、coding agent runner
├── storage/               # 本地 artifact 存储
└── tests/                 # IM 后端测试
```

### `agent_flow/`

```text
agent_flow/
├── api/                   # Agent Flow 原生 FastAPI 接口
├── application/           # Run、Agent、Context、Tool、Event 应用服务
├── domain/                # Agent、Tool、Event、Context、Memory 领域模型
├── infra/                 # EventBus、LLM、工具实现、部署、MongoDB
├── skills/                # Agent 技能文件
├── temp/                  # 部署、MCP 等运行时临时目录
└── README.md              # Agent Flow 子系统说明
```

## 技术架构

AgentHub 的整体形态是“IM 产品层 + Agent 运行核心”的组合。

```text
IM_front
  └─ HTTP / SSE
im_backend
  ├─ API Routes
  ├─ Application Services
  ├─ Domain Models
  ├─ Infra: storage / coding agents / agent_flow_bridge
  └─ Bridge
agent_flow
  ├─ Application Services
  ├─ Domain: Agent / Tool / Context / Event / Memory
  └─ Infra: EventBus / LLM / Tool Handlers / Deploy / DB
```

### 分层思想

`im_backend` 和 `agent_flow` 都采用接近 DDD 的分层方式，但没有引入复杂的聚合根建模：

- `api/`：HTTP/SSE 适配层，只处理请求、响应、鉴权和路由挂载。
- `application/`：应用用例层，编排消息发送、群聊 dispatch、run 创建、artifact 转换等流程。
- `domain/`：领域模型层，定义 Agent、消息、房间、事件、工具、上下文、记忆等核心对象。
- `infra/`：基础设施层，负责 MongoDB/内存存储、LLM 客户端、事件总线、工具实现、部署和外部 CLI。

### EDA 事件驱动底座

`agent_flow` 的工具调用和运行状态基于事件驱动：

1. Agent 通过 ReACT 决策生成 tool calls。
2. `ToolEventFactory` 根据工具注册表生成 `infra.{field}.{tool}.{called|succeeded|failed|retrying}` 事件。
3. `EventBus` 异步发布事件。
4. `On_bind` 将事件绑定到具体工具实现。
5. 工具成功或失败后发布回调事件。
6. Agent 收到工具结果，写入 state / memory，继续下一轮推理。

运行事件、工具事件、人工确认事件和产物事件会进入后端事件流，并通过 SSE 推送给前端。

### 多 Agent 群聊编排

群聊任务由 `im_backend` 接收用户消息和 @ mentions，再通过 bridge 创建 Agent Flow run：

- native Agent 直接进入 Agent Flow。
- Codex / Claude Code 等外部 coding agent 默认先生成确认卡片。
- `PlanOrchestrator` 负责 planner 计划拆解和 executor 调度。
- 计划以 DAG 形式执行，入度为 0 的 ready steps 可并发运行。
- 同一 executor 通过 lock 串行保护，减少状态污染。

### 前端运行时展示

前端以 `ChatView` 为主要工作台：

- `src/api/im.js` 封装 `/api/im` 接口。
- `src/stores/im.js` 管理会话、房间、消息、SSE、artifact 和运行事件。
- `src/utils/runtimeEvents.js` 负责流式事件压缩、trace 聚合、artifact 去重。
- `ArtifactCard.vue` 渲染 message、image、diff、document、web、deploy 等产物，并支持保存、回退、下载、部署停止/重启等操作。

## 主要接口

IM 后端入口：

- `GET /health`
- `/auth/*`：注册、登录、当前用户、退出
- `/api/im/agents`：Agent 列表、创建、删除、Builder
- `/api/im/conversations`：单聊会话、消息、回复、取消、重新生成、事件流
- `/api/im/rooms`：群聊房间、消息、dispatch、tasks、events、cancel
- `/api/im/favorites`：收藏上下文
- `/api/im/artifacts`：上传、下载、打包、diff apply、文档保存、版本历史
- `/api/im/deployments`：部署停止、重启、下载
- `/api/im/tools`：工具目录和配置
- `/api/im/skills`：技能文件 CRUD
- `/api/im/runs`：运行事件、确认请求与处理

Agent Flow 独立 API 入口：

- `/api/tools`
- `/api/contexts`
- `/api/agents`
- `/api/runs`
- `/api/conversations`

## 测试与构建

前端构建：

```bash
cd IM_front
npm run build
```

LLM 相关测试应使用 mock 或测试替身，避免在单元测试中依赖真实 API key 和网络。

## 安全与运行注意事项

- 外部 coding agent 默认走人工确认或只读/计划模式，避免未确认时直接修改文件。
- 工具上传和动态工具执行需要在生产环境增加源码审查、签名校验或沙箱隔离。
- 文件、diff、deploy artifact 需要限制路径、大小和下载行为。
- MongoDB 不可用时会降级到内存存储，适合开发调试，但重启后数据会丢失。
- SSE 高频事件已做前端合批和历史事件截断，长 run 场景仍建议增加分页、TTL 和容量限制。
