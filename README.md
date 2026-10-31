# 我会和你一起听

这是一个让 AI 陪你听网易云的 AstrBot 插件。它可以和你进入同一个听歌房间，按你的口味和当下心情准备下一首，在排队成功后说一句自然的推荐理由，并用听歌手帐慢慢了解你的偏好。插件只在真实确认的状态上更新界面；自动功能在首次安装时关闭。

![市场封面](assets/market-cover.png)

## 能做什么

- 在 AstrBot 页面查看当前歌曲、队列、歌词、陪听状态和操作回执。
- 搜索歌曲、歌手、作品原声和不完整歌名；搜索批次与自动推荐批次隔离。
- 根据本地播放记录、手帐评价、明确偏好、本场需求和少量探索位准备下一首。
- 推荐排队确认后发送一段简短介绍；依据会在播放器中留下记录。
- 只有可靠暂停持续 300 秒才自动收尾；快照失败、短暂离房、断线和第三方加入不会直接退房。
- 本地 SQLite 保存播放手帐、推荐依据、介绍回执和诊断轨迹，不上传跨用户画像。

## 安装前提

- AstrBot 4.27.0 或更高版本。
- `aiocqhttp` 平台，以及一个可用的主聊天模型。
- 需要自动陪听时，再安装随包提供的 Cove 服务。Linux x64/ARM64 正式支持；Docker/Linux 容器可按文档部署，Windows/macOS 原生服务暂列社区支持。
- 网易云账号需要在插件页面通过二维码登录；插件不接受命令行明文密码。

## 安装插件

在 AstrBot 插件市场安装，或从 GitHub 克隆后把仓库目录放入 AstrBot 的插件目录。插件包根目录必须保留 `metadata.yaml`。首次加载默认：

- 总开关关闭；
- 自动推荐关闭；
- 主动邀听关闭；
- 不创建房间、不发送消息、不自动排歌。

打开插件页面的“运行与账号”，按向导完成 AstrBot 版本、平台、Provider、Cove ready、令牌文件、网易云账号、主人会话和人格检查。所有检查通过后，手动确认才打开自动功能。

## 安装 Cove 服务（Linux）

```bash
bash scripts/install-cove.sh
bash scripts/check-cove.sh
```

安装器检查 Node.js 22、npm 和权限，复制 `service/`、执行 `npm ci` 与构建，并创建用户级 systemd 服务。令牌文件默认位于 AstrBot 插件数据目录的 `cove-bridge-token`；已有令牌不会被覆盖。更新和卸载：

```bash
bash scripts/update-cove.sh
bash scripts/uninstall-cove.sh       # 保留数据
bash scripts/uninstall-cove.sh --purge  # 明确确认后才删除数据
```

也可以复制 `service/systemd/cove-companion.service` 自行管理。Cove ready 地址默认是 `http://127.0.0.1:8787/astrbot/v1/health/ready`。令牌、Cookie、网易云 UID、主人完整 UMO 和数据库都只放在本机数据目录，不提交到 Git。

## 推荐与额度

推荐会过滤不可播放、排除项、当前队列和两小时内播放记录；用户手动点歌优先，自动推荐只排队。口味权重默认 70%，本场需求 15%，多样性 10%，人格音乐兴趣 5%；探索默认 15%，每轮最多一首陌生歌。一次跳过不会被当作不喜欢，手帐中的明确评价才会形成强信号。

每日插件相关额度默认 1,000,000 token，按 Asia/Shanghai 零点重置。能拿到 Provider usage 时按真实输入输出计量，否则明确标记估算或暂停自动行为。达到上限后停止自动推歌和新介绍，手动搜索、播放控制和普通聊天仍可用。

## 隐私、限制与数据删除

本项目是独立开源实现，与网易云音乐没有官方隶属、授权或背书；不代理音频，也不保证网易云非官方接口长期稳定。Cove 使用上游 `yanceydaisy/cove-resonance` 的 MIT 代码和本项目的适配补丁，详见 `NOTICE.md`。

播放记录和数据库默认在 AstrBot plugin_data 目录。要删除数据，请先停用插件，再显式删除该目录中的 SQLite 文件；卸载脚本默认不会删除用户数据。诊断导出会脱敏，不应把运行目录、Cookie、令牌或真实聊天提交到 Issue。

## 常见问题

- **Cove 不可用：** 页面会显示配置错误；原有 AstrBot 聊天不因插件失败而崩溃，搜索和控制会等待服务恢复。
- **二维码失效：** 在账号页重新扫码。不要把二维码、Cookie 或令牌发到公开渠道。
- **没有自动推荐：** 先确认房间正在可靠播放、自动推荐已打开、额度可计量且没有待播歌曲；可在页面运行 dry-run 查看阻塞原因。
- **升级失败：** 保留 SQLite 备份和令牌文件，重新运行 `update-cove.sh`；必要时回退到上一个 Git 标签。

## 开发与测试

```bash
python scripts/verify-public-release.py
cd frontend && npm ci && npm test && npm run build
cd ../service && npm ci && npm test && npm run build
```

本项目采用 MIT License。欢迎通过 GitHub Issues 报告安装、连接、认证、搜索、推荐和隐私问题。

