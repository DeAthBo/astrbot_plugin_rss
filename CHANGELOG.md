# Changelog

All notable changes to this project will be documented in this file.

## v1.1.8 - 2026-03-06

### Fixed
- 修复 `compose` 配置在自定义平台 ID 场景下不生效的问题。
- 由“按会话中的 platform_id 是否等于 `aiocqhttp`”改为“通过平台实例元数据判断平台类型是否为 `aiocqhttp`”，确保合并转发判断准确。
- 统一会话解析为 `split(':', 2)`，避免 session_id 中包含冒号时切分异常。

### Changed
- 同步版本号到 `v1.1.8`（`metadata.yaml` 与 `main.py`）。

## v1.1.7 - 2026-03-06

### Fixed
- 针对重复触发问题增加调度锁机制：同一时间仅允许一个实例执行 RSS 定时任务。
- 定时回调与任务重建增加持锁校验，非持锁实例会跳过执行，避免“多实例并发推送”。

### Added
- 新增 `/rss scheduler status` 命令：查看锁归属、当前任务数和实例状态。
- 新增 `/rss scheduler repair` 命令：在可接管时重建调度任务并恢复执行。

### Changed
- 同步版本号到 `v1.1.7`（`metadata.yaml` 与 `main.py`）。

## v1.1.6 - 2026-03-06

### Changed
- 插件唯一识别名由 `astrbot_plugin_rss` 调整为 `astrbot_plugin_rss_deathbo`，避免与上游同名插件冲突。
- 同步版本号到 `v1.1.6`（`metadata.yaml` 与 `main.py`）。

### Fixed
- 在 `pubDate` 分支下增加基于 `latest_link` 的去重保护：当条目链接与已记录最新链接相同则停止处理，减少同内容重复推送风险。
- 支持 `pubDate == last_update` 但链接不同的场景，避免同时间戳下的新条目被漏发。

## v1.1.5 - 2026-03-06

### Fixed
- 修复 `/rss remove` 删除订阅后，调度任务未完全清理的问题。
- 删除某 URL 下最后一个订阅者时，会同步删除该 URL 键，防止残留无效订阅数据。
- 刷新调度任务时增加空订阅保护，避免因历史残留数据导致异常任务重建。
- 为调度任务增加稳定 `job_id` 并启用 `replace_existing=True`，防止同一订阅重复注册。
- 新增插件 `terminate()`，在插件重载/禁用时主动关闭并清理 `AsyncIOScheduler`，避免旧任务实例残留导致重复推送。

### Docs
- 更新 README Q&A：该重复推送问题已在 `v1.1.5` 修复。

## v1.1.4 - 2026-03-06

### Changed
- 将插件元数据中的仓库地址更新为 `https://github.com/DeAthBo/astrbot_plugin_rss`。
- 将插件元数据中的帮助链接独立为 README 文档地址：`https://github.com/DeAthBo/astrbot_plugin_rss/blob/master/README.md`。
- 同步插件版本号到 `v1.1.4`（`metadata.yaml` 与 `main.py`）。

### Docs
- 在 `README.md` 新增“链接”章节，区分仓库地址、帮助文档与发布记录地址。

## v1.1.3 - 2026-03-06

### Added
- 新增配置项 `proxy_server`，可为 RSS 拉取与图片抓取统一设置 HTTP/HTTPS 代理。

### Changed
- `is_hide_url` 默认值调整为 `true`。
- `pic_config.is_read_pic` 默认值调整为 `true`。
- `max_items_per_poll` 默认值调整为 `-1`（不限制）。
- `pic_config.max_pic_item` 默认值调整为 `-1`（不限制）。
- 统一并同步插件版本号到 `v1.1.3`（`metadata.yaml` 与 `main.py`）。

### Docs
- 更新 `README.md` 配置说明，新增代理配置并同步默认值变化。
