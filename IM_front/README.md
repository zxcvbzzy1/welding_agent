# vue-project

This template should help get you started developing with Vue 3 in Vite.

## Recommended IDE Setup

[VS Code](https://code.visualstudio.com/) + [Vue (Official)](https://marketplace.visualstudio.com/items?itemName=Vue.volar) (and disable Vetur).

## Recommended Browser Setup

- Chromium-based browsers (Chrome, Edge, Brave, etc.):
  - [Vue.js devtools](https://chromewebstore.google.com/detail/vuejs-devtools/nhdogjmejiglipccpnnnanhbledajbpd)
  - [Turn on Custom Object Formatter in Chrome DevTools](http://bit.ly/object-formatters)
- Firefox:
  - [Vue.js devtools](https://addons.mozilla.org/en-US/firefox/addon/vue-js-devtools/)
  - [Turn on Custom Object Formatter in Firefox DevTools](https://fxdx.dev/firefox-devtools-custom-object-formatters/)

## Customize configuration

See [Vite Configuration Reference](https://vite.dev/config/).

## Project Setup

```sh
npm install
```

### Compile and Hot-Reload for Development

```sh
npm run dev
```

### 局域网访问

在提供前端服务的机器上运行：

```sh
npm run dev -- --host 0.0.0.0
```

其他机器打开 `http://<服务机器的局域网 IP>:5173`（以启动日志的实际端口为准）。预览构建时使用 `npm run preview -- --host 0.0.0.0`。

保持 `VITE_IM_API_BASE_URL` 为空，让浏览器通过同源 `/api` 访问后端；Vite 会代理到服务机器上的 `http://127.0.0.1:8010`。后端地址不同时，通过 `IM_API_PROXY_TARGET` 配置代理目标，避免浏览器请求访问者自己机器上的 `localhost`。

HTTP 环境启动后端时设置 `IM_SSE_COOKIE_SECURE=false`，让事件流认证 Cookie 能正常工作，启动命令见仓库根目录 README；HTTPS 部署保持默认值 `true`。

局域网 HTTP 页面通常不是安全上下文，`crypto.randomUUID()` 可能不可用。前端通过 `src/utils/uuid.js` 统一生成 ID，原生方法不可用时使用 `crypto.getRandomValues()` 生成 UUID v4，无需修改浏览器安全设置。

### Compile and Minify for Production

```sh
npm run build
```

## 机械臂离线预览

登录后打开 `/robot`，或点击顶部“机械臂”。页面通过 Three.js 和 `urdf-loader` 加载仓库 `urdf/fairino5_v6.urdf` 及 `urdf/fairino5_v6/` 中的 7 个 STL。Vite 将这些源文件作为资源导入，开发时本地提供、生产构建时输出到 `dist/assets`，运行时不依赖外部网格服务器。

支持旋转、缩放、平移、六关节角度调整、零位与视角复位、网格和坐标轴开关。关节轴和限位来自 URDF；场景单位为米、Z 向上，界面角度为度，加载器使用弧度。这是离线展示，不发送真实机械臂控制指令。

使用 `npm install` 安装依赖，`npm run dev` 启动，`npm run build` 构建。部署时需保留整个 `dist`，包含 URDF 和 STL 资源。

### 消息内联轨迹仿真

`RobotSimulator.vue` 是 `/robot` 手动页面和聊天 `robot_simulation` 产物共用的组件。传入 `sourceUrl`（认证后的仿真读取 URL）进入播放模式，也可用 `simulation` 传入已读取的快照；`compact` 控制卡片高度，`active=false` 暂停渲染。播放模式提供全部/单条焊缝选择、暂停/继续、重新开始、0.25×–2×倍率、循环和视角复位。倍率会受关节速度限幅；切换焊缝、不可达断点和循环回到起点时直接重置模型，不生成连接运动。离开可视区或浏览器后台时暂停，卸载时释放资源。

`npm run test:robot` 运行播放时序、分段边界、速度限幅和产物订阅的纯逻辑测试。
