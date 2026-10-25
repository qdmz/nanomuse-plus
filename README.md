# nanoMuse Plus（qdmz 增强版）

基于 [nano-muse/nanoMuse](https://github.com/nano-muse/nanoMuse) `v1.0.6`（上游提交 `50df84e`）的二次开发版本，版本 **1.1.0**。
在上游个人 Agent（网页端 + 终端 + 多设备同步、Sentinel 审批、技能/MCP、浏览器工具）之上，补齐了**国内聊天渠道**与**图片/视频生成**，并已在生产环境实机验收。

## 本版新增（相对上游）

### 1. 三个国内聊天渠道（`nanomuse/channels/`）
| 渠道 | 文件 | 接入方式 | 能力 |
|---|---|---|---|
| 元宝派（腾讯元宝） | `yuanbao.py` | 机器人 `app_key` / `app_secret`（设置页填写） | 私聊 + 派群、文字、图片/文件/视频（COS 上传） |
| QQ 机器人 | `qq.py` | **扫码绑定**（官方 lite 流程，自动回传 AppID/Secret）或手填 | 私聊 + 群 @、文字、富媒体图片/文件（官方 `/files` 两步上传）；**群管**（可选）：开通全量消息后识别广告/刷屏，第一次撤回+警告、第二次尝试移出 |
| 微信（ClawBot/iLink） | `wechat.py` | **扫码登录**（iLink 二维码，自动回传 bot_token） | 私聊、文字、图片/文件/视频（AES-ECB 加密 CDN 上传） |

- 渠道框架改动（`channels/manager.py`）：把飞书原生的扫码登录流程**通用化**——任何渠道只要在模块里实现 `login_begin` / `login_poll`，设置页即出现扫码绑定入口（QQ、微信走此机制，飞书保持其原生流程）。
- 配对机制与上游一致：新联系人首次发消息得到配对码，在「设置 → 聊天入口」批准即可。

### 2. 图片 / 视频生成工具（`nanomuse/tools/media.py`）
- `generate_image` / `generate_video`，走 Agnes AI（OpenAI 兼容接口），产物存 `workspace/images|videos`，并作为**真实附件**发回当前对话（网页端与全部聊天渠道均可）。
- 配置见 `config/config.example.toml` 的 `[image]` / `[video]` 段，密钥用 `${AGNES_API_KEY}` 从 `.env` 注入。

### 3. 部署默认值
- `docker-compose.yml`：构建参数 `WITH_BROWSER=1`（镜像自带 Chromium，浏览器工具开箱可用）。
- `docker-compose.override.yml`：应用端口映射为 `127.0.0.1:18787:8787`（适合挂隧道/反向代理）；想用上游默认的公网 `8787:8787`，删掉这个文件即可。

## 一键部署

```bash
git clone https://github.com/qdmz/nanomuse-plus.git
cd nanomuse-plus

cp .env.example .env
# 编辑 .env：至少填模型 API key（如 DEEPSEEK_API_KEY 或 NANOMUSE_LLM_API_KEY）
#           与 NANOMUSE_SERVER_TOKEN（网页登录令牌，自己设一个强随机值）
# 可选：AGNES_API_KEY（图片/视频生成）

cp config/config.example.toml config/config.toml
# 按需改 [llm] 预设（provider/model/base_url/api_key 用 ${VAR} 引用 .env）

docker compose up -d --build app
# 打开 http://<主机>:18787/#token=<你的 NANOMUSE_SERVER_TOKEN>
# （删除了 override 文件时端口为 8787）
```

数据落在 Docker 卷 `nanomuse-data`（记忆/目标/保险库/会话），工作区在 `./workspace`。

## 渠道开通速记

- **Telegram**：BotFather 建机器人 → 设置页填 token。⚠️ 群聊需把机器人设为**群管理员**，或在 BotFather 关闭 Group Privacy 后**把机器人移出群再重新拉入**，否则机器人收不到群消息。
- **QQ**：设置页 → QQ 渠道 → 扫码绑定（手机 QQ 打开链接、选机器人、点连接；任务有效期短，请尽快确认）。也可在 [QQ 开放平台](https://q.qq.com) 建机器人后手填 AppID/Secret。群里使用需 @ 机器人。
- **微信**：设置页 → 微信渠道 → 扫码登录（微信打开链接确认），令牌自动保存。
- **元宝派**：在元宝开放平台创建机器人，设置页填 `app_key` / `app_secret`。
- **飞书**：上游原生扫码创建应用，设置页点登录扫码即可。
- 每个渠道连上后，让对方先发一条消息拿到**配对码**，在设置页批准。

## 与上游的差异文件

```
nanomuse/channels/qq.py        新增（QQ 适配器 + 扫码绑定）
nanomuse/channels/wechat.py    新增（微信 iLink 适配器 + 扫码登录）
nanomuse/channels/yuanbao.py   新增（元宝适配器，含 COS 媒体上传）
nanomuse/channels/manager.py   修改（注册三渠道 + 扫码登录通用化）
nanomuse/tools/media.py        新增（图片/视频生成工具）
nanomuse/tools/__init__.py     修改（导出新工具）
nanomuse/app.py                修改（注册新工具）
docker-compose.yml             修改（WITH_BROWSER=1）
docker-compose.override.yml    新增（本地端口映射）
config/config.example.toml     追加 [image]/[video]/MCP 示例段
.env.example                   追加 AGNES_API_KEY 说明
pyproject.toml / nanomuse/__init__.py  版本 1.1.0
```

## 已知限制

- 为便于分发，本仓库未包含上游 `server/static` 下的少量二进制素材（头像动画 webp/mp4、PWA 图标、woff2 字体），不影响功能，界面字体会回退到系统字体。
- QQ 官方对机器人**主动消息**有月度条数限制；被动回复（私聊约 55 分钟 / 群约 5 分钟窗口内）不受此限，本实现已按窗口规则发送。
- QQ 群管的移出/禁言接口属平台**内邀能力**：普通机器人调用返回 11253「应用无接口访问权限」，需向平台申请白名单；未获批时第二次违规改为群内通知群主/管理员手动移出（撤回不受此限，但需在群设置里给机器人开「撤回成员消息」权限，且对群主/管理员的消息无效）。
- Agnes 免费视频模型（`agnes-video-2.5-flash`）队列繁忙时会返回 503，稍后重试即可；付费版 `agnes-video-2.5` 按秒计费。
- 上游的 Android 客户端、cloud 服务端、文档站不在本仓库范围内，需要时见上游仓库。

## 许可证

GPL-3.0-or-later（与上游一致）。原项目版权归 nano-muse 团队所有，本增强版的改动同样以 GPL-3.0 发布。
