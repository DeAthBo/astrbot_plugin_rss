# Changelog

All notable changes to this project will be documented in this file.

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
