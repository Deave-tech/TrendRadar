# TrendRadar × WSJ 全文飞书云文档

## 数据流

1. `wsj-cn-rss-bridge.service` 每 15 分钟通过本机 BPC `POST /v1/list` 串行刷新 WSJ 中文网首页及 8 个栏目，只保留 `https://cn.wsj.com/articles/*`，并原子保存 last-known-good 快照。
2. TrendRadar 原有整点任务仍抓取并保存 `wsj-cn` RSS；该 feed 配置 `notify: false`，不会再推送原站链接。
3. `trendradar-wsj-delivery.timer` 每小时第 10 分钟运行独立 delivery。SQLite outbox 位于 `/var/lib/trendradar/wsj-delivery.db`。
4. delivery 调用本机 BPC `POST /v1/fetch`；只有正文通过 200/白名单/反爬/付费墙/480 字符/3 段质量门后，才创建飞书 Docx、按页面正文顺序写入文本与可选图片 Block、授予目标群只读权限并发送汇总卡片。

## 飞书应用前置条件

企业自建应用需启用机器人能力、加入目标群，并发布包含以下权限的新版本：

- `docx:document:create`
- `docx:document:write_only`
- `docs:document.media:upload`（启用正文图片时必需）
- `docs:permission.member:create`
- `space:document:delete`（自动清理旧 Docx 时必需）
- `im:message:send_as_bot`

目标配置必须使用群 `chat_id`（`oc_...`）。文档授权使用 `member_type=openchat`、`type=chat`、`perm=view`，授权成功后才发送链接。

官方接口：

- <https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/create>
- <https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/create>
- <https://open.feishu.cn/document/server-docs/docs/drive-v1/media/upload_all>
- <https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/patch>
- <https://open.feishu.cn/document/server-docs/docs/permission/permission-member/create>
- <https://open.feishu.cn/document/server-docs/docs/drive-v1/file/delete>
- <https://open.feishu.cn/document/server-docs/im-v1/message/create>

## 配置

敏感配置统一放在 `/etc/trendradar/wsj-delivery.env`，所有者 `root:root`、权限 `0600`：

```ini
BPC_API_TOKEN=<与 /etc/trendradar/bpc-server.env 的 API_TOKEN 相同>
FEISHU_APP_ID=<企业自建应用 App ID>
FEISHU_APP_SECRET=<企业自建应用 App Secret>
FEISHU_RECEIVE_ID=<目标群 chat_id，oc_ 开头>
FEISHU_RECEIVE_ID_TYPE=chat_id

BPC_BASE_URL=http://127.0.0.1:8080
WSJ_FEED_URL=http://127.0.0.1:4555/wsj-cn.xml
WSJ_DELIVERY_DB=/var/lib/trendradar/wsj-delivery.db
FEISHU_DOC_URL_PREFIX=https://feishu.cn/docx
WSJ_MAX_ITEMS_PER_RUN=20
WSJ_MAX_DRAIN_SECONDS=5400
WSJ_MAX_CLOUD_DOCUMENTS=300

# 图片功能默认关闭；权限和测试文档预检成功后才改为 true。
WSJ_INCLUDE_IMAGES=false
WSJ_IMAGE_ALLOWED_HOSTS=images.wsj.net
WSJ_IMAGE_MAX_BYTES=10485760
WSJ_IMAGE_MAX_COUNT=8
WSJ_IMAGE_MAX_REDIRECTS=3
WSJ_IMAGE_TIMEOUT=20
WSJ_IMAGE_MAX_PIXELS=40000000
```

若已知租户入口，可把 `FEISHU_DOC_URL_PREFIX` 换成 `https://<tenant>.feishu.cn/docx`。

BPC 自身敏感值放在 `/etc/trendradar/bpc-server.env`（`root:root`、`0600`）；扩展路径通过 `BPC_DIR` 指向外部 BPC checkout。生产单元强制 `NODE_ENV=production`、`REQUIRE_API_TOKEN=1`、`ENABLE_LEGACY_FETCH=0`；无 Token 会在 Chromium 启动前失败。

Bridge 不再直连 WSJ，也不持有 DataDome Cookie 或浏览器 User-Agent。把本机 BPC 地址和 Bearer Token 放入 `/etc/trendradar/wsj-bridge.env`（`root:root`、`0600`）：

```ini
BPC_BASE_URL=http://127.0.0.1:8080
BPC_API_TOKEN=<与 /etc/trendradar/bpc-server.env 的 API_TOKEN 相同>
```

仓库示例位于 `deploy/systemd/wsj-bridge.env.example`。Bridge 只允许 loopback `BPC_BASE_URL`，禁用代理和重定向后逐 source 调用严格列表 API；生产单元使用单 worker、单请求 120 秒超时，以覆盖正文任务排队、列表导航及传输余量。列表和正文共用 BPC 的单并发 WSJ 队列与一个持久 Chromium context，DataDome 的读写和轮换因此只发生在该浏览器会话中。缺少 `BPC_API_TOKEN` 或 endpoint 非 loopback 时 Bridge 拒绝启动；刷新期故障仍保留 last-known-good 快照。

## systemd 安装和启动

仓库模板：

- `deploy/systemd/bpc-server.service`
- `deploy/systemd/wsj-cn-rss-bridge.service`
- `deploy/systemd/trendradar-wsj-delivery.service`
- `deploy/systemd/trendradar-wsj-delivery.timer`
- `deploy/systemd/trendradar-wsj-backfill.service`

模板按 Tokyo 主机的 `ubuntu` 用户和 `/home/ubuntu/TrendRadar` checkout 编写；
部署到其他主机时先调整各 unit 的 `User`、`HOME`、`WorkingDirectory` 和
`ExecStart`。

安装后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bpc-server.service wsj-cn-rss-bridge.service

curl -fsS --max-time 5 http://127.0.0.1:8080/healthz
curl -fsS --max-time 5 http://127.0.0.1:4555/health

# 首次只运行一次；当前列表全部入 outbox，最长 90 分钟。
sudo systemctl start trendradar-wsj-backfill.service
sudo systemctl show trendradar-wsj-backfill.service \
  -p ActiveState -p SubState -p Result -p ExecMainStatus --no-pager

# 确认首次回补结果后再启用小时 timer。
sudo systemctl enable --now trendradar-wsj-delivery.timer
```

普通 delivery 未初始化时会失败关闭（退出码 2），不会把当时文章误当成“已见”；只有显式 `--backfill-current` 才初始化。进程使用 `${WSJ_DELIVERY_DB}.lock` 的非阻塞文件锁，冲突退出 75，禁止重叠执行。

## outbox 与故障处理

自动状态：

```text
discovered -> fetch_pending -> fetched -> doc_created -> shared -> notified
```

- 每个远程阶段完成后立即提交 SQLite 事务。
- `doc_created` 后的授权或推送失败只从当前阶段继续，不会重复建文档。
- 创建结果不确定进入 `unknown`；确定的单篇不可重试质量或内容参数错误进入 `manual`，两者都不会自动重复创建。
- 若飞书返回明确的应用鉴权、权限点或目标 `openchat` 配置错误，本轮立即停止继续建文档；当前阶段保留并退避，修复配置后再从该阶段恢复。普通单篇内容/Block 参数 4xx 不触发全局熔断。
- Block 每批最多 50 个，批间至少 400ms；429/5xx 指数退避。
- 图片采用飞书官方三步流程：创建空 Image Block，向该 Block 上传
  `docx_image` 素材（同时以 `extra.drive_route_token` 指明文档），再用
  `replace_image` 绑定 `file_token` 并写入原始宽高，避免飞书按 100×100 默认值缩小。渲染计划会在建文档前冻结，运行中切换
  `WSJ_INCLUDE_IMAGES` 不会造成 cursor 漂移。
- 图片只接受精确主机 `images.wsj.net` 的 HTTPS/443 地址；每次重定向重新校验
  主机与公网 DNS，禁止凭据、Cookie、授权头、私网地址、SVG/HTML 和超过限制的
  内容。默认单图上限 10MB、4,000 万像素（严于飞书上传素材接口的 20MB 上限），每篇最多 8 张
  （配置校验允许 0～20；默认值按素材接口 10,000 次/日额度留出余量）。
- 下载成功的图片先作为 0600 SQLite outbox 中的临时 BLOB 持久化，再创建空图片
  Block。策略/格式/大小错误在建空 Block 前跳过（图片说明也跳过），瞬时下载失败
  会退避且不分享缺图文档；绑定成功即删除临时 BLOB。素材上传有 write-ahead 标记，
  结果不确定进入 `unknown`，不会盲目重复上传；图片绑定使用确定性 client token。
- BPC Cookie、DataDome、CAPTCHA 或付费墙等系统性故障会熔断批量抓取。同类告警最多每 6 小时一次，恢复后通知一次。
- 卡片使用确定性 UUID；同一轮文章尽量合并一张，序列化超过 30KB 才拆分。
- 每轮投递和通知完成后统计本 WSJ outbox 中尚未确认删除的 Docx；默认最多
  `300` 个（`WSJ_MAX_CLOUD_DOCUMENTS` 可配置）。超限时只按文档创建时间从老到新
  删除 `status=notified` 的文档；`doc_created`、`shared`、`manual`、`unknown` 等文档
  仍计入总量但绝不作为自动删除候选。创建结果不确定且尚无文档 ID 时也保守预留
  一个名额。程序不会扫描或删除 outbox 之外的云文档。
- DELETE 前先持久化独立的 `delete_pending` 意图，文章状态仍保持 `notified`。删除
  请求可在崩溃或响应丢失后安全重放，并把飞书返回的“不存在/已删除”视为收敛成功；
  429、5xx 和临时错误按指数退避。只有远端确认删除后才写入 `deleted`，删除失败时
  不会越过最老文档继续删除较新的文档。飞书删除接口会把文档移入回收站；本上限
  指活跃的、由本流水线管理的 WSJ 云文档。
- 若上轮仍超限，下一轮会先恢复最老文档的清理，并暂停创建新的 Docx（已创建文档
  的写入、授权和通知仍可恢复）。清理无法收敛时命令返回非零并在摘要中报告
  `retention_occupied`、`retention_excess` 和 `retention_capacity_blocked`，避免静默增长。

只读检查（不会输出正文或密钥）：

```bash
sqlite3 -readonly /var/lib/trendradar/wsj-delivery.db \
  "select status,count(*) from articles group by status order by status;"

sqlite3 -readonly /var/lib/trendradar/wsj-delivery.db \
  "select article_key,status,last_error_code,document_url <> '' as has_document_url from articles where status in ('manual','unknown');"
```

`unknown` 必须先在飞书中按标题核对是否已经生成文档，再人工决定如何修复数据库；不要删除行后盲目重跑。

## Cookie 更新与验收

Cookie 失效时通过 BPC 的鉴权 `/cookies` 接口更新 `wsj.com` 记录。人工注入只发生一次；此后列表与正文响应产生的 DataDome 轮换会在共享队列释放前原子写回 Cookie store。若写回失败，BPC 会在当前任务释放前关闭 WSJ 队列，`/healthz` 及后续列表、正文请求持续返回 `SESSION_PERSIST_FAILED`；Delivery 将其视为系统性故障并停止本轮批量抓取。修复存储后必须重启 BPC，不会在进程内重新打开队列。不要把 Cookie、App Secret 或 Token 写入 Git、日志或命令输出。

验收至少检查：

1. `/healthz` 的 browser connected、站点数、DNR 规则数和 WSJ queue 正常；bridge `/health` 非 stale。
2. 目标群能打开 Docx；正文只来自页面 `article section`，尾部正文图集完整，且无导航、行情、推荐。
3. 相同文章换跟踪参数、跨日或重启后不增加文档。
4. 模拟 BPC 403、挑战页、短正文、超时、队列满和浏览器崩溃时不创建文档。
5. `doc_created` 后模拟授权/发送失败，恢复后文档 ID 不变。
