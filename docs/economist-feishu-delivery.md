# TrendRadar × Economist 全文飞书云文档

Economist 与 WSJ 使用同一套飞书应用、同一个群 `chat_id`、同一个 SQLite
outbox 和同一个 **300 篇云文档总上限**，但发现、抓取、熔断、告警和通知状态按
publisher 隔离。

## 数据流

1. `economist-rss-bridge.service` 每 15 分钟读取 Economist 官方
   `https://www.economist.com/latest/rss.xml`，只保留直接的日期文章 URL，排除
   podcast、audio、video 和首版不支持的 interactive 模板。快照以原子方式写入
   `/var/lib/trendradar/economist-rss-bridge.json`；空结果不会覆盖 last-known-good。
2. TrendRadar 正常抓取 `economist` feed 并保存归档，但 `notify: false` 阻止原站
   链接与云文档通知重复。
3. `trendradar-economist-delivery.timer` 每小时第 40 分钟运行，与 WSJ 的第 10
   分钟错开。两者通过共享数据库的非阻塞文件锁禁止重叠。
4. BPC 的 `POST /v1/economist/fetch` 只允许
   `https://www.economist.com/<section>/YYYY/MM/DD/<slug>`。正文仅取
   `p[data-component="paragraph"]`；图片只取正文前的 hero 与正文 section 内的
   figure，并按段落顺序插入，拒绝链接到其他文章、末段后的推荐图以及非
   `content-assets/images` 资源。
5. 文档标题为 `Economist｜标题`，同轮文章合并为一张 `Economist 新文章` 卡片，
   推送到与 WSJ 相同的群。

## 配置

Economist service 故意复用权限为 `0600` 的
`/etc/trendradar/wsj-delivery.env`，避免第二份飞书密钥和 `chat_id` 漂移。新增：

```dotenv
NEWS_DELIVERY_DB=/var/lib/trendradar/wsj-delivery.db
NEWS_MAX_CLOUD_DOCUMENTS=300

ECONOMIST_FEED_URL=http://127.0.0.1:4556/economist.xml
ECONOMIST_MAX_ITEMS_PER_RUN=20
ECONOMIST_MAX_DRAIN_SECONDS=5400
ECONOMIST_INCLUDE_IMAGES=true
ECONOMIST_IMAGE_ALLOWED_HOSTS=www.economist.com
ECONOMIST_IMAGE_MAX_COUNT=20
ECONOMIST_IMAGE_MAX_BYTES=10485760
ECONOMIST_IMAGE_MAX_REDIRECTS=3
ECONOMIST_IMAGE_TIMEOUT=20
ECONOMIST_IMAGE_MAX_PIXELS=40000000
```

`NEWS_DELIVERY_DB` 和 `NEWS_MAX_CLOUD_DOCUMENTS` 同时供 WSJ 与 Economist
worker 使用。旧库会自动迁移：现有行归属 `wsj`，两站初始化、工作队列、通知、
告警和熔断彼此隔离；文档占用统计与“删除最老文档”跨两站全局执行。

BPC 环境可选设置：

```dotenv
ECONOMIST_MAX_QUEUE=20
```

Economist 不需要复制 WSJ 的 DataDome Cookie。所有 Token、飞书密钥、Cookie 与
群 ID 均不得进入 Git、命令输出或日志。

## 安装与首次运行

```bash
sudo install -m 0644 deploy/systemd/economist-rss-bridge.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/trendradar-economist-delivery.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/trendradar-economist-delivery.timer /etc/systemd/system/
sudo install -m 0644 deploy/systemd/trendradar-economist-backfill.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/trendradar-economist-initialize.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now economist-rss-bridge.service
sudo systemctl start trendradar-economist-backfill.service
sudo systemctl enable --now trendradar-economist-delivery.timer
```

首次回补最多 90 分钟，失败项保留到后续小时任务。若不希望回补当前 80 篇，不要
启动 backfill unit；先执行下列命令把当前列表建为终态基线（不会调用 BPC 或飞书，
也不会创建文档），成功后再启用 timer：

```bash
sudo systemctl start trendradar-economist-initialize.service
sudo systemctl enable --now trendradar-economist-delivery.timer
```

## 验收

```bash
curl --max-time 3 http://127.0.0.1:4556/health
curl --max-time 3 http://127.0.0.1:8080/healthz
systemctl show economist-rss-bridge.service \
  trendradar-economist-delivery.timer -p ActiveState -p SubState --no-pager
```

验收文档需同时满足：正文不少于 3 段/500 字；群成员可打开；图片保持原始比例；
图注紧随图片；不含页尾推荐、侧栏、导航或广告。BPC 返回 Cloudflare、付费墙、
短正文、队列满或浏览器异常时绝不创建文档。
