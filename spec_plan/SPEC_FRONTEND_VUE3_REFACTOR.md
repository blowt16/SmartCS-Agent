# SPEC_FRONTEND_VUE3_REFACTOR：前端 Vue3 标准工程重构 + 移除旧链路

- 状态：待审核（草稿）
- 分支建议：`refactor/frontend-vue3`（从 `refactor/anaphora-resolution` 派生）
- 日期：2026-08-21
- 关联文档：[[SPEC_SEMANTIC_CACHE_RESOLVE]]、[[SPEC_RAG_RETRIEVAL_CONVERGENCE]]

## 1. 背景与目标

当前项目有两个前端入口：

| 入口 | 服务内容 | 问题 |
|---|---|---|
| `/` | `llm_backend/static/dist` 编译好的 Vue 旧产物（AssistGen） | 调用 `/api/chat`、`/api/reason`、`/api/search` 旧链路 + token 登录；**仓库无该产物源码**（无 .vue、无 package.json），无法维护 |
| `/chat.html` | 根目录手写单文件页面（1434 行，Vue3 global build + CDN） | 已完整调用 `/api/langgraph/query`（SSE 流式、语义缓存、会话、图片上传），但非标准工程 |

**目标**：
1. 把 chat.html 按 Vue3 标准重构为独立 Vite + Vue3 SFC 工程，**功能、风格、样式与原件完全一致**；
2. 访问入口收敛为 `http://127.0.0.1:8000`（`/` 直接打开新页面）；
3. `llm_backend/static` 重命名为 `frontend`，新工程放该目录；
4. 移除旧链路：后端删除 `/api/chat`、`/api/reason`、`/api/search` 三个端点及全部孤儿代码，前端不再调用；
5. **迁移 JWT 登录鉴权**：新前端接入登录/注册（后端 `/api/register`、`/api/token`、`/api/users/me` 已就绪、不在删除范围），登录态驱动 user.id 动态化（替代 chat.html 硬编码 id:1）；
6. 整改后全链路实测（构建 + 启动 + API + 浏览器交互）。

用户已确认的决策：
- 重构验证通过后**删除**根目录 `chat.html`（git 历史可找回）；
- **全部文档**（README、docs/、STUDY_NOTES.md、spec_plan/）同步更新旧链路引用；
- **仅前端登录**：会话 API 后端暂不校验 JWT（现状即如此），鉴权加固后续单独做；
- **包含注册页**：登录页带注册入口（email + 密码），后端 `/api/register` 已就绪。

## 2. 现状关键事实（已实测）

### 2.1 chat.html 结构（迁移唯一样板，`D:\SmartCS-Agent\chat.html`）
- CSS：**第 50–266 行**（约 217 行，无 `@media`、无 `@tailwind` 指令，纯自定义样式，全局性选择器为主）
- 模板：第 267–814 行；JS：第 823–1432 行（Vue3 单 `setup()`）
- CDN 依赖：Vue3 global build、marked、highlight.js 11.9.0、Tailwind CDN（含自定义 `tailwind.config`：`darkMode: 'class'` + `primary/sidebar/page/user/bot` 色板 + Inter 字体族）、Font Awesome 6.5.1、Google Fonts Inter（400/500/600/700）
- 能力清单（重构后必须全部保留）：SSE 流式聊天（手写 SSE 解析，支持 `data:` 字符串/数组两种 chunk + `interruption` 字段）、语义缓存（后端 `_stream_cached` 模拟流式，前端无感知）、会话 CRUD、流结束 save-messages、文档 XHR 上传带进度条、图片 FileReader 预览 + multipart 上传、Markdown 渲染（marked v4 `highlight` 选项 + breaks）、暗色模式、会话搜索、统计卡片（本地 computed）、自动滚动、输入框自适应、拖拽上传、标题编辑
- 调用的 API 全集：`/api/conversations`（CRUD 系列）、`/api/langgraph/query`（POST SSE）、`/api/conversations/save-messages`、`/api/upload`。**不调用** `/api/chat`、`/api/reason`、`/api/search`、`/api/token`、`/api/register`

### 2.2b 后端 auth 链路（独立于旧链路，**保留**并复用）
- `POST /api/register`（auth.py:16）：UserCreate → 创建用户（email 唯一校验，ValueError → 400）
- `POST /api/token`（auth.py:27）：UserLogin {email, password} → `{access_token, token_type: "bearer"}`，JWT 有效期 `settings.ACCESS_TOKEN_EXPIRE_MINUTES`
- `GET /api/users/me`（auth.py:44）：Bearer token → 当前用户（UserResponse）
- 依赖：`app/core/security.py` 的 `create_access_token` / `get_current_user`（jwt.decode + sub=email + 查库），`oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")`
- **会话 API（conversations 系列）现状不校验 token**，仅靠 `user_id` 参数——本次不加鉴权（用户已确认），前端仅携带 Authorization 头
- 旧前端 token 存储：`localStorage["token"]`，所有 fetch 带 `Authorization: Bearer <token>`（新前端沿用同 key）

### 2.2 后端（`llm_backend/main.py`）
- `/api/chat`（124–146 行，ChatMessage 模型）、`/api/reason`（148–168 行，ReasonRequest 模型）、`/api/search`（170–191 行，复用 ChatMessage）——本次删除
- 模型区 93–117 行：删除 `ReasonRequest`、`ChatMessage`；**保留** `RAGChatRequest`（/chat-rag 在用）
- `/api/upload`（193 行起）、`/api/upload/image`（489–532 行）：**保留**
- `/chat.html` FileResponse 路由（534–537 行）：删除
- 静态挂载（539–541 行）：`STATIC_DIR = static/dist` → 改 `frontend/dist`
- `/api/langgraph/query`（346–487 行）：multipart Form + SSE + 语义缓存 + X-Conversation-ID，**保持不动**
- `main.py:53` `_get_resolve_llm()`（指代消解）仍用 `LLMFactory.create_chat_service()` → **create_chat_service 必须保留**
- `main.py:335` `/api/conversations/save-messages` 仍用 `ConversationService.save_message` → **保留**
- `ollama_service.py` 中的 `/api/chat` 是**出站**到 Ollama 本地服务的 URL，**绝不能动**
- `/chat-rag`（251–267 行）引用未 import 的 `RAGChatService`（既有 bug），**本次不动**
- `LangGraphRequest` 模型（113–117 行）为既有冗余，**不动**（最小改动）

### 2.3 仓库与工具环境
- `llm_backend/static/` **未被 git 跟踪**（根 .gitignore 第 9 行 `dist/` 规则已忽略）→ 目录重命名用普通 `mv`，无需 git mv
- Node v22.20.0 + npm 11.11.1 可用；pnpm 不可用
- 后端运行：`cd llm_backend && .venv\Scripts\python.exe run.py`（uvicorn port 8000，Windows SelectorEventLoop 补丁，reload=True）
- 当前分支 `refactor/anaphora-resolution` 有未提交修改：`llm_backend/run.py`（第 36 行注释 `# http://127.0.0.1:8000/chat.html`，与本次改造同源，随分支带入）、`docs/项目问题.txt`（未跟踪文档）、`llm_backend/uploads/`（运行时产物）

## 3. 方案设计

### 3.1 Vue3 工程结构（新建 `frontend/`）

```
frontend/
├── package.json
├── vite.config.js                # base:'/'、outDir:'dist'、emptyOutDir、dev proxy /api → 8000
├── tailwind.config.js            # darkMode:'class' + 自定义色板原样迁移
├── postcss.config.js             # tailwindcss + autoprefixer
├── index.html                    # lang=zh-CN，title=SmartCS-Agent 智能客服
├── .gitignore                    # node_modules/、dist/
└── src/
    ├── main.js                   # createApp + 全局样式/字体/图标导入
    ├── App.vue                   # 布局编排 + 跨组件状态
    ├── style/global.css          # @tailwind 三指令 + chat.html 50–266 行 CSS 逐字复制
    ├── api/
    │   ├── conversations.js      # 会话 CRUD 6 个 fetch 封装
    │   ├── langgraph.js          # POST /api/langgraph/query（FormData，返回 Response）
    │   ├── upload.js             # XHR 带进度上传
    │   └── auth.js               # login / register / getMe + token 读写（localStorage["token"]）
    ├── composables/useChat.js    # SSE 流式聊天核心（原 sendMessage 逐行迁移）
    ├── utils/
    │   ├── markdown.js           # marked 配置 + renderMarkdown
    │   └── format.js             # 时间/文件大小/文档图标工具
    └── components/
        ├── LoginView.vue         # 登录/注册页（新写，无原件模板；样式沿用 Tailwind 色板）
        ├── Sidebar.vue           # 左侧栏（原件 279–385 行）
        ├── ChatArea.vue          # 中部（原件 387–683 行）
        └── DocsPanel.vue         # 右侧知识库面板（原件 685–803 行）
```

### 3.1a 项目命名一致性（硬约束）

全项目正式名称为 **SmartCS-Agent**（README、pyproject.toml `name = "smartcs-agent"`、FastAPI title、chat.html 侧栏品牌与欢迎语均已统一）。**旧前端产物（AssistGen 的 title 与欢迎语"给 AssistGen 发送消息"）是唯一不一致处**。新前端工程不得引入 AssistGen 字样，所有品牌位置统一 SmartCS-Agent：

| 位置 | 值 |
|---|---|
| package.json `name` | `smartcs-agent-frontend`（与 pyproject 的 `smartcs-agent` 同源、加 -frontend 区分前后端子工程） |
| index.html `<title>` | `SmartCS-Agent 智能客服`（与 chat.html 一致） |
| 主界面侧栏品牌 / 欢迎语 | 随 chat.html 模板原样迁移（已是 SmartCS-Agent） |
| LoginView 标题 | `SmartCS-Agent`（不出现 AssistGen） |

### 3.1b JWT 登录鉴权迁移设计（仅前端，后端 auth 链路复用）

| 项 | 设计 |
|---|---|
| 登录态入口 | App.vue 顶层 `isAuthenticated`（`!!localStorage.getItem("token")`）：未登录渲染 `<LoginView/>`，已登录渲染原主界面；刷新后从 localStorage 恢复（与旧前端同 key `token`） |
| 登录/注册流程 | LoginView 内 tab 切换登录/注册：登录调 `POST /api/token`（email+password）→ 存 token → `GET /api/users/me` 拿 user（{id, email, name}）→ 进入主界面；注册调 `POST /api/register` 成功后自动切到登录 tab |
| **页面风格** | **与旧前端登录页一致**（仅取样式，品牌名不沿用）：`.login-container` 全屏 flex 居中、背景 `#1e1e1e`；`.login-box` 400px 卡片、背景 `#2d2d2d`、padding 40px、圆角 12px、阴影 `0 4px 12px rgba(0,0,0,.1)`；`.login-title` 白色 24px/500 居中（文案用 `SmartCS-Agent`，不用 AssistGen）；表单校验与文案沿用旧前端：邮箱格式校验、密码 ≥8 位含大小写字母和数字、注册需确认密码、"账号登录/立即注册/返回登录/注册成功/邮箱或密码错误" |
| user.id 动态化 | 原件硬编码 `user = {id: 1, name: '访客用户'}` → 改为登录用户；会话列表、消息加载、save-messages、文档上传、langgraph/query 的 `user_id` 全部取登录用户 id（Sidebar 底部用户区显示真实邮箱/昵称） |
| 请求头 | api/ 层每个 fetch/XHR 统一附 `Authorization: Bearer <token>`（与旧前端行为一致；后端会话 API 不校验，仅携带） |
| 401 处理 | api/ 层统一拦截：响应 401 → 清 localStorage token → 回到登录视图 |
| 登出 | Sidebar 用户区登出按钮：清 token → 回登录视图（原件 logout 仅为占位，本次落地） |

### 3.2 依赖与版本决策

| 依赖 | 版本 | 理由 |
|---|---|---|
| vue | ^3.5 | 与原件 Vue3 一致 |
| marked | **^4.3（必须 4.x）** | 原件用 `marked.setOptions({highlight, breaks})`，v5 移除了 highlight 选项；锁 4.x 渲染逻辑逐字一致 |
| highlight.js | ^11.9 | 与原 CDN 同版本；github-dark 主题从包内导入 |
| tailwindcss | **^3.4（必须 v3）** | v4 无 tailwind.config.js，色板无法原样迁移；配套 postcss + autoprefixer |
| @fortawesome/fontawesome-free | ^6.5.1 | 与 CDN 同源同内容，Vite 自动拷贝字体 |
| @fontsource/inter | ^5 | 本地化 Inter 400/500/600/700（字形与 Google Fonts 同源），摆脱外网依赖 |
| vite / @vitejs/plugin-vue | ^6 / ^5 | 标准 Vite 6 工具链 |

### 3.3 样式迁移（像素级一致的关键）

- `global.css` 顶部放 `@tailwind base; @tailwind components; @tailwind utilities;` 三指令，随后**逐字复制** chat.html 第 50–266 行 `<style>` 内容。原页面 = Tailwind CDN preflight + 自定义 CSS 叠加，构建版保持相同叠加顺序，结果一致。
- **CSS 不做组件 scoped 拆分**：选择器多为全局性（`.dark body`、`::-webkit-scrollbar`、`.markdown-content` 等），且 `.markdown-content` 作用于 `v-html` 注入的 DOM（scoped 无法命中）。
- `tailwind.config.js` 的 `content` **必须含 `./src/**/*.{vue,js}`**：`getDocIcon`/`getDocIconBg`/`stats` 的颜色类（`text-red-500`、`bg-green-100` 等）只存在于 js 字符串中，Tailwind v3 扫描原始文本检出。

### 3.4 组件拆分映射与状态归属

| 状态/逻辑 | 归属 | 说明 |
|---|---|---|
| isDark / sidebarOpen / docsPanelOpen / previewImage / user（硬编码 id:1）/ isMobile / stats | App.vue | 跨组件共享；stats 依赖 conversations+messages+documents |
| conversations / currentConversation / create/select/delete/rename | App.vue | 全页核心数据 |
| messages / isTyping / langgraphConversationId / sendMessage / loadMessages | useChat.js | SSE 顶层业务流，贯穿会话创建与保存 |
| documents / uploadProgress / 上传执行 | App.vue + api/upload.js | stats 需要 documents；XHR 细节下沉 api 层 |
| searchQuery / filteredConversations / formatTime | Sidebar.vue | 唯一消费者 |
| inputMessage / selectedImages / 标题编辑态 / autoResize / 键盘事件 | ChatArea.vue | 输入区局部 UI，发送时事件上抛 |
| dragOver / docInput | DocsPanel.vue | 面板局部状态 |
| renderMarkdown / 时间与文件格式 | utils/ | 纯函数复用 |

**唯一有意的行为适配**：原件 `scrollToBottom()` 在 6 处显式调用 → 改为 ChatArea 深度 watch `messages` 内容变化后 `nextTick` 滚底。视觉效果等价（流式期间钉底、历史加载后钉底），消除跨组件 DOM 引用，代码注释说明。其余逻辑 1:1 迁移。

### 3.5 后端改造清单

**main.py**：

| 位置 | 改动 |
|---|---|
| 第 3 行 | 删 `FileResponse`（/chat.html 路由删除后成孤儿 import） |
| 第 7 行 | 删 `from app.services.search_service import SearchService` |
| 第 93–100 行 | 删 `ReasonRequest`、`ChatMessage` 模型 |
| 第 124–191 行 | 删 `/api/chat`、`/api/reason`、`/api/search` 端点 |
| 第 534–537 行 | 删 `/chat.html` 路由 |
| 第 540 行 | `STATIC_DIR`：`"static" / "dist"` → `"frontend" / "dist"` |

**app/services/llm_factory.py**：删 `create_reasoner_service`（17–25 行）、`create_search_service`（27–31 行）及第 5 行 SearchService import；**保留 create_chat_service**。

**run.py**：第 36 行注释 `# http://127.0.0.1:8000/chat.html` → `# http://127.0.0.1:8000`。

**目录与文件**：`mv llm_backend/static frontend`；验证通过后删除根目录 `chat.html`。

**孤儿清理（先 grep 验证再删）**：`app/services/search_service.py`、`app/tools/search.py`、`app/tools/definitions.py`、`app/prompts/search_prompts.py`（互引成环、无外部引用，整链删除）。

### 3.6 文档同步（全部文档）

| 文件 | 改动 |
|---|---|
| README.md | 架构图/API 表删旧三端点；前端描述改 Vue3 SFC（frontend → dist）；目录树 static/dist → frontend/；注明访问入口 http://127.0.0.1:8000 |
| llm_backend/README.md | 删旧链路端点条目与 search_service 目录树行 |
| docs/PROJECT_ANALYSIS.md | 删旧端点引用、STATIC_DIR 路径、chat.html 描述改"已重构为 frontend 工程" |
| STUDY_NOTES.md | 同上 |
| docs/SHOP_SAGE_ANALYSIS.md | chat.html 引用与免鉴权端点描述更新 |
| spec_plan/PLAN_GraphRAG_TO_StandardRAG.md、SPEC_ENTITY_PARALLEL_RAG.md | 语义缓存"仅服务 /api/chat"描述更新 |
| docs/superpowers/specs/2026-08-16-postgres-pgvector-design.md | chat.html 注释同步 |

## 4. 验证方案

1. **构建**：`cd frontend && npm install && npm run build` → 生成 dist/index.html + assets/（带 hash）
2. **后端启动**：`cd llm_backend && D:\SmartCS-Agent\.venv\Scripts\python.exe run.py`（前置 `docker compose up -d` 起 postgres/redis）
3. **HTTP 验证**（Git Bash curl）：
   - `GET /` → 200；`GET /chat.html` → 404；`POST /api/chat|reason|search` → 404/405
   - `GET /health` → `{"status":"ok"}`；dist asset 资源 → 200
4. **SSE 回归**：`curl -N -X POST http://127.0.0.1:8000/api/langgraph/query -F "query=..." -F "user_id=1"` → 200 + X-Conversation-ID + 多行 `data:` SSE 块
5. **会话 API 冒烟**：create / list / messages / save-messages / rename / delete 全流程
6. **导入检查**：`llm_backend` 下 `.venv python -c "import main"` 无 ImportError
7. **浏览器交互**（http://127.0.0.1:8000）：与重构前 `/chat.html` 渲染逐项对比（侧边栏深绿、统计卡、气泡圆角，像素级一致是硬指标）；暗色模式；新建对话→SSE 流式→"来源: LangGraph"徽章→自动滚底；刷新回显；重命名/删除会话；图片预览+上传；文档拖拽上传+进度条；输入框自适应；Markdown 代码高亮；<1024px 抽屉侧栏；DevTools Network 确认仅 `/api/conversations*`、`/api/langgraph/query`、`/api/upload`、`/api/token`、`/api/register`、`/api/users/me`（不得出现 /api/chat、/api/reason、/api/search）
8. **登录鉴权实测**：未登录访问 `/` 显示登录页 → 注册新账号 → 自动切登录 → 登录后进入主界面且侧栏显示登录用户 → 提问后会话列表按该用户隔离 → 刷新保持登录态 → 登出回登录页 → 重复登录 → Network 确认请求带 `Authorization: Bearer` 头；`/api/users/me` 带假 token 返回 401 触发回登录

## 5. 执行步骤（按依赖排序）

| # | 步骤 | 验证点 |
|---|---|---|
| 0 | 基线：`docker compose up -d`；记录 run.py 未提交修改 | 服务就绪、基线明确 |
| 1 | `mv llm_backend/static frontend` | 目录存在 |
| 2 | 创建 frontend 工程：骨架（package.json 等 6 个根文件）→ global.css 样式迁移 → App/Sidebar/ChatArea/DocsPanel 模板迁移 → useChat/api/utils 逻辑迁移 | 逐块与 chat.html 对应行区间比对 |
| 3 | `npm install && npm run build` | 构建成功、产物生成、marked 确认 4.x |
| 4 | 后端改造：main.py / llm_factory.py / run.py | grep 归零 + `import main` 通过 |
| 5 | 删搜索链路 4 个孤儿模块 | grep 归零 |
| 6 | 启动后端 → HTTP + SSE + 会话冒烟 | 全部通过 |
| 7 | 浏览器交互实测 | 像素级一致 + 交互全通过 + Network 无旧端点 |
| 8 | 文档同步（全部文档） | grep 文档旧端点仅剩历史性描述 |
| 9 | 删除根目录 chat.html；git 审查 → 提交推送 | 变更集干净 |

## 6. 风险与注意

- **marked 必须锁 4.x**：升 5.x 需引入 marked-highlight 改写渲染逻辑，违反"逻辑一致"。
- **Tailwind 必须 v3**：v4 配置方式完全不同。
- **create_chat_service / ConversationService.save_message 保留**：指代消解与 save-messages 端点仍在用，误删会导致运行时 ImportError。
- **ollama_service.py 的 /api/chat 是出站 URL**：与本次删除的 FastAPI 端点同名但完全无关。
- **/chat-rag + RAGChatService 既有 bug**：本次不动，避免扩大变更面。
- **run.py 当前有未提交修改**：仅第 36 行注释，与本次改造同源（主入口路径），随分支带入新分支一并处理。
- **登录 token 存储沿用 localStorage["token"]**：与旧前端同 key；后端会话 API 不加鉴权（用户已确认），token 仅前端携带，安全加固列为后续迭代。
- **项目命名一致性**：新前端任何品牌/标题位置（index.html title、package.json name、登录页标题、侧栏/欢迎语）统一 `SmartCS-Agent`，**严禁出现旧前端 AssistGen 字样**（详见 3.1a）。
- **LoginView 为全新 UI（无原件模板）**：风格参照**旧前端登录页**（深色：`#1e1e1e` 背景 + `#2d2d2d` 卡片 + 白色标题，类名/数值见 3.1b 表），不参与主界面"像素级一致"对比；类名可原样照抄旧前端 CSS（`.login-container`/`.login-box`/`.login-title`/`.login`）保证视觉一致。
