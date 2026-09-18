
## 快速启动

### 环境要求

- Python：依赖项 `requirements.txt`
- Node.js：`IM_front/package.json` 要求 `^20.19.0 || >=22.12.0`
- MongoDB：默认连接 `mongodb://localhost:27017/`

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
