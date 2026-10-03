# MediaResume

MediaResume 用于在 Plex 和 Emby 之间双向同步播放记录。

在任意一端播放、暂停、标记已看或标记未看后，MediaResume 会找到另一端对应的电影或剧集，并同步观看状态和播放进度。项目提供中文 Web 管理页面，不需要手写配置文件。

还可以选择接入 Trakt：实时推送播放进度，并在需要时手动与 Trakt 双向全量同步已看记录和进度。

## 功能

- 双向同步电影和剧集的播放进度
- 双向同步“已看 / 未看”状态
- 检测并同步从“继续观看”中移除的条目
- 手动预览并同步两端的“继续观看”列表
- 可选：实时推送播放进度到 Trakt，手动与 Trakt 全量同步已看记录和进度
- 支持多个 Plex 用户与 Emby 用户映射
- Plex 播放事件通过 WebSocket 接收，其他变化通过增量轮询发现
- 定时全量对账，修复服务离线期间遗漏的变化
- Dry-run 模式：只记录计划执行的操作，不修改 Plex 或 Emby
- Web 页面展示运行状态、用户映射、对账结果和实时日志
- Docker 镜像支持 `linux/amd64` 和 `linux/arm64`

## 使用前准备

你需要：

1. 一台可运行 Docker 的设备，例如 NAS、家用服务器或安装了 Docker Desktop 的电脑。
2. 可从该设备访问的 Plex 和 Emby 服务。
3. Plex 服务器所有者的 `X-Plex-Token`。
4. Emby 管理后台创建的 API Key。
5. 可选：Trakt 账户，以及在 Trakt 上自建的 App（Client ID 和 Client Secret）。免费账户即可。

获取凭据：

- [Plex 官方：查找 X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- [Emby 官方：创建 API Key](https://dev.emby.media/doc/restapi/API-Key-Authentication.html)
- [Trakt：新建 App](https://trakt.tv/oauth/applications/new)，详见 [Trakt 同步](#trakt-同步可选)

> Plex Token、Emby API Key 和 Trakt Client Secret 都具有账户访问权限。不要把它们提交到 Git、发到聊天记录或写入公开的 Compose 文件。

## 快速开始

### 方式一：Docker Run

创建用于保存配置和同步状态的目录：

```bash
mkdir -p mediaresume-data
```

启动容器：

```bash
docker run -d \
  --name mediaresume \
  --restart unless-stopped \
  -p 8095:8095 \
  -e MEDIARESUME_PASSWORD='请替换为一个强密码' \
  -v "$PWD/mediaresume-data:/app/config" \
  redfofo/mediaresume:latest
```

然后在浏览器打开：

```text
http://你的服务器IP:8095
```

浏览器弹出登录框时，用户名可以任意填写，密码是 `MEDIARESUME_PASSWORD` 设置的值。

如果只在可信内网中使用，可以删除 `-e MEDIARESUME_PASSWORD=...`。此时管理页面及其 API 没有登录保护，不要直接暴露到公网。

Windows PowerShell 请把 `$PWD/mediaresume-data` 换成一个绝对路径，例如：

```powershell
-v "D:\docker\mediaresume:/app/config"
```

### 方式二：Docker Compose 使用发布镜像

新建一个目录，并在其中创建 `compose.yaml`：

```yaml
services:
  mediaresume:
    image: redfofo/mediaresume:latest
    container_name: mediaresume
    restart: unless-stopped
    ports:
      - "8095:8095"
    environment:
      MEDIARESUME_PASSWORD: ${MEDIARESUME_PASSWORD:-}
    volumes:
      - ./config:/app/config
```

在同一目录创建 `.env`：

```dotenv
MEDIARESUME_PASSWORD=请替换为一个强密码
```

启动：

```bash
mkdir -p config
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
```

停止服务：

```bash
docker compose down
```

配置文件和同步数据库保存在 `./config`，执行 `docker compose down` 不会删除它们。

### 方式三：Docker Compose 从源码构建

仓库内置的 `docker-compose.yml` 使用当前源码构建镜像。下载或克隆本仓库后，在项目根目录执行：

```bash
mkdir -p config
docker compose build --pull
docker compose up -d
```

也可以合并为一条命令：

```bash
docker compose up -d --build
```

如需为管理页面设置密码，在项目根目录创建 `.env`：

```dotenv
MEDIARESUME_PASSWORD=请替换为一个强密码
```

源码构建会先编译 Vue 前端，再创建 Python 运行镜像。

## 首次配置

首次打开页面时，MediaResume 会自动进入“配置”页。

1. 填写 Plex 地址，例如 `http://192.168.1.10:32400`。
2. 填写 Plex 服务器所有者的 `X-Plex-Token`，点击“测试连接”。
3. 填写 Emby 地址，例如 `http://192.168.1.11:8096`。
4. 填写 Emby API Key，点击“测试连接”。
5. 添加用户映射：
   - Plex 用户留空表示 Plex 服务器所有者，使用上方的所有者 Token。
   - 共享 Plex 用户需要选择用户名，并填写该用户自己的 Token。
   - Emby 用户可以从连接测试返回的用户列表中选择。
6. 首次运行建议开启 **Dry-run 模式**。
7. 点击“保存并重启同步”。
8. 在“运行状态”页观察日志和首次对账结果。
9. 确认匹配和同步方向正确后，关闭 Dry-run，再次保存。
10. 可选：按 [Trakt 同步](#trakt-同步可选) 的步骤接入 Trakt。

保存后会在挂载目录中创建：

```text
config.yaml
data/
├── state.db
└── trakt_tokens.json   # 授权 Trakt 后才会出现
```

实际目录结构为：

```text
/app/config/config.yaml
/app/config/data/state.db
```

备份宿主机上的整个挂载目录即可保留配置和同步状态。

## 媒体如何匹配

MediaResume 使用 TMDB ID 匹配两端媒体：

- 电影：TMDB 电影 ID
- 剧集：TMDB 剧集 ID + 季号 + 集号
- 如果媒体路径中含有 `{tmdb-12345}`，优先使用路径中的 ID
- 没有路径标记时，使用 Plex 和 Emby 媒体元数据中的 TMDB ID

因此，两端媒体需要具有一致且正确的 TMDB 元数据。出现“另一端没有对应条目”时，请先检查两边的媒体识别结果，而不是只比较标题。

## 同步规则

### 日常增量同步

MediaResume 会同步：

- 播放进度
- 已看状态
- 未看状态
- 从“继续观看”中手动移除的电影或剧集

为防止一次写入在两端来回触发，写入后会在短时间内忽略同一条目的回传事件。

### 首次对账和冲突

首次发现同一媒体时：

1. 一端已看、另一端未看时，优先保留“已看”。
2. 两端都未看时，优先保留播放进度更大的状态。
3. 建立同步基线后，如果两端都发生过变化，优先采用最后播放时间较新的一端。

不确定结果是否符合预期时，请先保持 Dry-run 开启并检查日志。

### 继续观看列表

“运行状态”页中的“同步继续观看”可以：

1. 选择 `Plex → Emby` 或 `Emby → Plex`。
2. 先预览将添加和移除的条目。
3. 确认后执行一次性同步。

限制：Plex 没有可用于手动加入“下一集推荐”的对应接口。遇到这种条目时，预览页面会将其标记为不支持，而不会伪造同步结果。

## Trakt 同步（可选）

接入 Trakt 后，MediaResume 会自动把实时播放进度推送到 Trakt；已看记录和历史进度的同步由你在页面上手动触发，两个方向都支持。不需要 Trakt VIP。

容器需要能访问 `https://api.trakt.tv`。

### 配置步骤

1. 登录 Trakt，打开 [新建 App](https://trakt.tv/oauth/applications/new)：
   - Name 任意填写，例如 `MediaResume`；
   - Redirect URI 填写 `urn:ietf:wg:oauth:2.0:oob`；
   - 其余选项保持默认，保存后得到 Client ID 和 Client Secret。
2. 在 MediaResume 的“配置”页填写 Trakt Client ID 和 Client Secret。
3. 点击“授权 Trakt 账户”，按提示打开 Trakt 网页并输入授权码。多个家庭成员可以分别授权各自的 Trakt 账户。
4. 在“用户映射”中为需要同步的映射选择 Trakt 账户。
5. 点击“保存并重启同步”，在“运行状态”页的 Trakt 卡片和日志中查看结果。
6. 首次对账完成后，按需在 Trakt 卡片中执行一次全量同步，补齐历史记录。

### 自动：实时推送播放进度

默认开启，可在配置页关闭。只处理实时发生的播放，不会自动推送或拉取历史记录。

- Plex 播放时，Trakt 显示“正在观看”；暂停或停止时保存进度。
- 本次运行中推送过进度的条目播放完成后，Trakt 记录一次观看。
- 在 Plex / Emby 中直接标记已看，不会自动同步到 Trakt，需要执行全量同步。
- Emby 没有可用的实时播放事件，Emby 上的播放只推送暂停进度，不显示“正在观看”。

### 手动：全量同步

在“运行状态”页的 Trakt 卡片中执行。两个方向都先预览，确认后在后台执行，进度和结果在日志中查看。全量同步基于最近一次对账的结果。

| 按钮 | 已看记录 | 播放进度 |
| --- | --- | --- |
| 全量同步到 Trakt | 本地已看而 Trakt 上没有的，添加观看记录，时间取最后播放时间 | 本地看到一半的写入 Trakt；本地已看完的清除 Trakt 上残留的进度 |
| 从 Trakt 全量同步 | Trakt 已看而本地未看的，在 Plex / Emby 中标记为已看 | Trakt 的进度比本地新且相差明显时，写入 Plex / Emby |

两个方向都只做补充：不会删除 Trakt 上的观看记录，也不会把 Plex / Emby 的条目改为未看。

### 限制

- 只向 Trakt 推送 1%～79% 的进度；Trakt 会把 80% 以上的停止记为看完，而本地此时还没看完。
- Trakt 的进度只有百分比，写入 Plex / Emby 的位置有一定误差；本地进度与 Trakt 相差不到 2% 片长时视为一致。
- 一个文件包含多集时（如 `S01E01-E02`），无法换算单集进度，不同步进度。
- Trakt 只保留最近 6 个月的播放进度。
- 重看不会追加 Trakt 播放次数（实时播放看完除外）。
- Dry-run 模式下只在日志中列出将要执行的操作。
- Trakt 的 access token 有效期为 24 小时，MediaResume 会自动续期。授权失效时，状态页会提示重新授权。
- 授权信息保存在挂载目录的 `data/trakt_tokens.json` 中，请和 `config.yaml` 一样妥善保管。

如果同时使用 Emby 的 Trakt 插件或 PlexTraktSync，它们会各自写入 Trakt。建议只保留一种方式，避免同一次观看被记录多次。

## 常用设置

| 设置 | 默认值 | 说明 |
| --- | ---: | --- |
| 对账间隔 | 900 秒 | 定期扫描两端媒体并修复遗漏状态 |
| 增量轮询间隔 | 30 秒 | 检查 Emby 变化和 Plex 已看变化 |
| Plex 未看检测间隔 | 120 秒 | 检测 Plex 中被重新标记为未看的条目 |
| 播放进度推送间隔 | 60 秒 | 播放过程中向另一端推送进度的最小间隔 |
| 防回环窗口 | 10 秒 | 写入后忽略同一条目回传事件的时间 |
| Dry-run | 新页面默认开启 | 记录操作但不实际修改媒体服务器 |
| 日志级别 | INFO | 可选 DEBUG、INFO、WARNING、ERROR |
| 实时推送播放进度到 Trakt | 开启 | 仅对绑定了 Trakt 账户的用户映射生效 |

通常不需要修改这些默认值。媒体库较大或服务器性能较低时，可以适当增加轮询和对账间隔。

## 更新

### Docker Run

先拉取新镜像：

```bash
docker pull redfofo/mediaresume:latest
```

删除旧容器，然后使用原启动命令重新创建：

```bash
docker rm -f mediaresume
```

只要继续挂载原来的 `mediaresume-data` 目录，配置和同步状态不会丢失。

### Docker Compose

```bash
docker compose pull
docker compose up -d
```

### 源码构建

拉取新代码后执行：

```bash
docker compose up -d --build
```

## 排查问题

### 页面打不开

检查容器是否运行以及端口是否正确映射：

```bash
docker ps --filter name=mediaresume
docker logs mediaresume
```

确认服务器防火墙允许访问 TCP `8095`。

### Plex 或 Emby 测试连接失败

容器中的 `127.0.0.1` 指向容器自身，不是 Docker 宿主机。Plex 或 Emby 运行在宿主机上时，优先填写宿主机的局域网 IP，例如：

```text
http://192.168.1.10:32400
http://192.168.1.10:8096
```

同时检查 Token、API Key、端口以及 Plex/Emby 的远程访问设置。

### 找不到对应媒体

检查两端是否具有相同的 TMDB ID、季号和集号。仅标题相同不足以完成匹配。

### 一直显示 Dry-run

进入“配置”，关闭 Dry-run，点击“保存并重启同步”。

### Trakt 授权失败

- 提示“获取设备码失败（HTTP 401）”：Client ID 填写错误。
- 输入授权码后一直等待：确认登录的是要同步的 Trakt 账户，并在授权码过期前完成授权；过期后点击“重新获取授权码”。
- 状态页提示“授权已失效”或“授权属于其他 App”：更换过 Trakt App 或在 Trakt 上撤销了授权。在配置页重新授权该账户并保存。

### Trakt 没有收到播放进度

- 确认该用户映射已选择 Trakt 账户，且配置页的“实时推送播放进度”已开启。
- 只推送 1%～79% 的进度；刚开始播放（不足 1%）暂停时不会推送。
- Dry-run 模式下只记录日志，不会实际推送。
- 在 Plex / Emby 中直接标记已看不会实时推送，需要执行“全量同步到 Trakt”。
- 日志中出现“无法识别以下 TMDB ID”时，表示 Trakt 上没有对应条目，请检查媒体的 TMDB 元数据。

### 查看详细日志

先在 Web 配置中把日志级别改为 `DEBUG`，再查看：

```bash
docker logs -f mediaresume
```

## 安全建议

- 建议始终设置 `MEDIARESUME_PASSWORD`。
- 不要把 8095 端口直接暴露到公网；需要远程使用时，通过带 HTTPS 和身份认证的反向代理访问。
- `config.yaml` 以明文保存 Plex Token、Emby API Key 和 Trakt Client Secret，`data/trakt_tokens.json` 保存 Trakt 授权信息，请限制挂载目录的访问权限并妥善备份。
- 不要把 `config/`、`.env` 或数据库文件提交到 Git。仓库提供的 `.gitignore` 已默认排除这些文件。

## 开源许可证

本项目采用宽松的 [MIT License](LICENSE)。你可以使用、复制、修改、发布和分发本项目，也可以用于商业用途；分发时需要保留原版权声明和许可证文本。
