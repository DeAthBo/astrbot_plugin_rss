import aiohttp
import asyncio
import time
import re
import logging
import hashlib
import json
import os
import uuid
from lxml import etree
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult,MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp

from .data_handler import DataHandler
from .pic_handler import RssImageHandler
from .rss import RSSItem
from typing import List


@register(
    "astrbot_plugin_rss_deathbo",
    "Soulter",
    "RSS订阅插件",
    "1.3.1",
    "https://github.com/DeAthBo/astrbot_plugin_rss",
)
class RssPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        self.logger = logging.getLogger("astrbot")
        self.context = context
        self.config = config
        self.data_handler = DataHandler()

        # 提取scheme文件中的配置
        pic_config = config.get("pic_config") or {}
        self.title_max_length = config.get("title_max_length")
        self.description_max_length = config.get("description_max_length")
        self.max_items_per_poll = config.get("max_items_per_poll")
        if self.max_items_per_poll is None:
            self.max_items_per_poll = -1
        self.t2i = config.get("t2i")
        self.is_hide_url = config.get("is_hide_url")
        if self.is_hide_url is None:
            self.is_hide_url = True
        self.is_read_pic = pic_config.get("is_read_pic")
        if self.is_read_pic is None:
            self.is_read_pic = True
        self.is_adjust_pic = pic_config.get("is_adjust_pic")
        if self.is_adjust_pic is None:
            self.is_adjust_pic = False
        self.max_pic_item = pic_config.get("max_pic_item")
        if self.max_pic_item is None:
            self.max_pic_item = -1
        self.is_compose = config.get("compose")
        self.proxy_server = (config.get("proxy_server") or "").strip() or None

        weekly_report = config.get("weekly_report") or {}
        self.weekly_report_enabled = bool(weekly_report.get("enabled", False))
        self.weekly_report_cron_expr = str(weekly_report.get("cron_expr") or "0 9 * * 1").strip()
        self.weekly_report_max_items_per_feed = int(weekly_report.get("max_items_per_feed") or 500)

        self.pic_handler = RssImageHandler(
            is_adjust_pic=self.is_adjust_pic,
            proxy_server=self.proxy_server,
        )
        self.scheduler_lock_path = "data/astrbot_plugin_rss_scheduler.lock"
        self.scheduler_owner_token = None
        self.scheduler = AsyncIOScheduler()
        if self._claim_scheduler_owner():
            self.scheduler.start()
            self._fresh_asyncIOScheduler()
            self.logger.info("RSS 调度器已启动（当前实例持有调度锁）。")
        else:
            self.logger.warning(
                "检测到其他实例持有 RSS 调度锁，当前实例不启动调度任务。可用 /rss scheduler status 查看状态。"
            )

        # 可视化订阅：从插件配置中读取并同步到数据文件（通常需要重载插件生效）
        self._visual_subscriptions = config.get("subscriptions") or []
        try:
            asyncio.create_task(self._bootstrap_visual_subscriptions())
        except Exception as e:
            self.logger.warning(f"RSS 可视化订阅初始化失败：{e}")

    def parse_cron_expr(self, cron_expr: str):
        parsed = self._parse_cron_expr_safe(cron_expr)
        if parsed is None:
            raise ValueError(f"invalid cron expr: {cron_expr}")
        return parsed

    def _parse_cron_expr_safe(self, cron_expr: str) -> dict | None:
        try:
            fields = [x for x in cron_expr.split(" ") if x != ""]
            if len(fields) != 5:
                return None
            return {
                "minute": fields[0],
                "hour": fields[1],
                "day": fields[2],
                "month": fields[3],
                "day_of_week": fields[4],
            }
        except Exception:
            return None

    async def _count_items_published_since(
        self, url: str, since_timestamp: int, limit: int = 500
    ) -> tuple[int | None, bool]:
        """统计 RSS 中 pubDate >= since_timestamp 的条目数。

        Returns:
            (count_or_none, truncated)
            - count_or_none 为 None 表示该源缺少 pubDate，无法准确统计。
            - truncated=True 表示达到 limit 上限（显示时建议用 >=limit）。
        """
        try:
            text = await self.parse_channel_info(url)
            if text is None:
                return 0, False
            root = etree.fromstring(text)
            items = root.xpath("//item")
        except Exception:
            return 0, False

        count = 0
        truncated = False
        saw_pubdate = False

        for item in items:
            pub_nodes = item.xpath("pubDate")
            if not pub_nodes:
                continue
            saw_pubdate = True
            pub_date = pub_nodes[0].text or ""
            try:
                pub_date_parsed = time.strptime(
                    pub_date.replace("GMT", "+0000"),
                    "%a, %d %b %Y %H:%M:%S %z",
                )
                pub_date_timestamp = int(time.mktime(pub_date_parsed))
            except Exception:
                continue

            if pub_date_timestamp >= since_timestamp:
                count += 1
                if limit > 0 and count >= limit:
                    truncated = True
                    break
            else:
                # RSS 一般按新到旧，遇到更早的可以停止
                break

        if not saw_pubdate:
            return None, False
        return count, truncated

    def _get_channel_display_info(
        self, url: str, user: str | None = None, sub_key: str | None = None
    ) -> dict:
        info = self.data_handler.data.get(url, {}).get("info", {}) or {}

        title = ""
        description = ""

        # 优先：按订阅 id（主键）覆盖（仅对托管订阅生效）
        if user:
            user_map = (
                self.data_handler.data.get(url, {})
                .get("subscribers", {})
                .get(user, {})
            )
            if isinstance(user_map, dict) and sub_key:
                sub = user_map.get(sub_key) or {}
            else:
                sub = user_map or {}
            config_id = sub.get("config_id") if isinstance(sub, dict) else None
            if config_id:
                settings = self.data_handler.data.get("settings", {}) or {}
                config_index = settings.get("config_subscriptions", {}) or {}
                entry = config_index.get(config_id) if isinstance(config_index, dict) else None
                if isinstance(entry, dict):
                    title = (entry.get("title_override") or "").strip()
                    description = (entry.get("description_override") or "").strip()

        # 兼容旧逻辑：按 URL 覆盖（对所有订阅生效）
        if not title or not description:
            overrides = self.data_handler.data.get(url, {}).get("overrides", {}) or {}
            title = title or (overrides.get("title") or "").strip()
            description = description or (overrides.get("description") or "").strip()

        return {
            "title": title or (info.get("title") or "").strip(),
            "description": description or (info.get("description") or "").strip(),
        }

    def _validate_subscription_id(self, sub_id: str) -> str | None:
        sub_id = (sub_id or "").strip()
        if not sub_id:
            return None
        if len(sub_id) > 64:
            return None
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", sub_id):
            return None
        return sub_id

    def _normalize_all_subscribers(self) -> None:
        """将历史数据的 subscribers[user] = sub_info 迁移为 subscribers[user][sub_key] = sub_info。"""
        changed = False
        for url in list(self.data_handler.data.keys()):
            if url in ("rsshub_endpoints", "settings"):
                continue
            channel = self.data_handler.data.get(url)
            if not isinstance(channel, dict):
                continue
            subscribers = channel.get("subscribers")
            if not isinstance(subscribers, dict) or not subscribers:
                continue

            for user in list(subscribers.keys()):
                user_val = subscribers.get(user)
                if not isinstance(user_val, dict):
                    continue
                # 已是嵌套结构：{sub_key: sub_info}
                if any(
                    isinstance(v, dict) and "cron_expr" in v
                    for v in user_val.values()
                ) and "cron_expr" not in user_val:
                    continue

                # 旧结构：subscribers[user] 直接是 sub_info
                legacy_info = user_val
                legacy_key = legacy_info.get("config_id") if isinstance(legacy_info, dict) else None
                if not legacy_key:
                    legacy_key = "__legacy__"
                subscribers[user] = {legacy_key: legacy_info}
                changed = True

        if changed:
            self.data_handler.save_data()

    def _iter_user_subscription_entries(self, user: str) -> list[dict]:
        """扁平化返回某会话的订阅条目列表（每条包含 url + sub_key + sub_info）。"""
        self._normalize_all_subscribers()
        entries: list[dict] = []
        for url, channel in self.data_handler.data.items():
            if url in ("rsshub_endpoints", "settings"):
                continue
            if not isinstance(channel, dict):
                continue
            subscribers = channel.get("subscribers", {})
            if not isinstance(subscribers, dict):
                continue
            user_map = subscribers.get(user)
            if not isinstance(user_map, dict):
                continue
            for sub_key, sub_info in user_map.items():
                if not isinstance(sub_info, dict):
                    continue
                entries.append(
                    {"url": url, "sub_key": str(sub_key), "sub_info": sub_info}
                )
        entries.sort(key=lambda x: (x["url"], x["sub_key"]))
        return entries

    def _get_entry_display_id(self, entry: dict) -> str:
        sub_key = entry.get("sub_key") or ""
        sub_info = entry.get("sub_info") or {}
        if isinstance(sub_info, dict) and sub_info.get("managed_by_config"):
            return str(sub_info.get("config_id") or sub_key)
        if sub_key == "__manual__":
            return "manual"
        if sub_key == "__legacy__":
            return "legacy"
        return str(sub_key)

    def _remove_config_managed_by_id(self, sub_id: str) -> int:
        """删除所有由可视化配置托管且 config_id==sub_id 的订阅。返回删除的 subscriber 数。"""
        self._normalize_all_subscribers()
        removed = 0
        for url in list(self.data_handler.data.keys()):
            if url in ("rsshub_endpoints", "settings"):
                continue
            subscribers = self.data_handler.data.get(url, {}).get("subscribers", {})
            if not isinstance(subscribers, dict) or not subscribers:
                continue
            for user in list(subscribers.keys()):
                user_map = subscribers.get(user, {}) or {}
                if not isinstance(user_map, dict) or not user_map:
                    continue
                for sub_key in list(user_map.keys()):
                    info = user_map.get(sub_key, {}) or {}
                    if info.get("managed_by_config") and info.get("config_id") == sub_id:
                        user_map.pop(sub_key, None)
                        removed += 1
                if not user_map:
                    subscribers.pop(user, None)
            if not subscribers:
                self.data_handler.data.pop(url, None)
        return removed

    async def _ensure_channel_initialized(self, url: str) -> RSSItem | None:
        """确保 data[url] 已存在并包含 info/subscribers，返回最新一条 RSSItem（用于初始化 last_update/latest_link）。"""
        try:
            normalized_url = self.parse_rss_url(url)
            if normalized_url not in self.data_handler.data:
                text = await self.parse_channel_info(normalized_url)
                title, desc = self.data_handler.parse_channel_text_info(text)
                self.data_handler.data[normalized_url] = {
                    "subscribers": {},
                    "info": {"title": title, "description": desc},
                }
            latest_items = await self.poll_rss(normalized_url, num=1)
            if not latest_items:
                return None
            return latest_items[0]
        except Exception as e:
            self.logger.warning(f"RSS 初始化频道失败 {url}: {e}")
            return None

    async def _bootstrap_visual_subscriptions(self) -> None:
        """将可视化配置 subscriptions 同步到 data 文件与调度任务。"""
        self._normalize_all_subscribers()
        subs = self._visual_subscriptions or []
        if not isinstance(subs, list) or not subs:
            return

        cache_latest: dict[str, RSSItem | None] = {}
        changed = False

        settings = self.data_handler.data.setdefault("settings", {})
        config_index = settings.setdefault("config_subscriptions", {})
        if not isinstance(config_index, dict):
            config_index = {}
            settings["config_subscriptions"] = config_index

        current_ids: set[str] = set()

        for entry in subs:
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled", True) is False:
                continue

            sub_id = self._validate_subscription_id(entry.get("id"))
            if not sub_id:
                self.logger.warning("RSS 可视化订阅跳过：订阅 id 为空或非法（需要在配置中填写唯一 id）")
                continue
            current_ids.add(sub_id)

            raw_url = (entry.get("url") or "").strip()
            if not raw_url:
                continue
            url = self.parse_rss_url(raw_url)

            cron_expr = (entry.get("cron_expr") or "0 * * * *").strip()
            if self._parse_cron_expr_safe(cron_expr) is None:
                self.logger.warning(f"RSS 可视化订阅跳过：Cron 非法 {cron_expr}（url={url}）")
                continue

            targets_text = entry.get("targets") or ""
            targets = [x.strip() for x in str(targets_text).splitlines() if x.strip()]
            if not targets:
                continue

            title_override = (entry.get("title_override") or "").strip()
            description_override = (entry.get("description_override") or "").strip()
            weekly_stats_enabled = bool(entry.get("weekly_stats_enabled", True))

            # 如果该 id 之前绑定过旧 url，先清理旧位置的托管订阅（支持 URL 变更自动迁移）
            prev = config_index.get(sub_id) if isinstance(config_index, dict) else None
            prev_url = (prev or {}).get("url") if isinstance(prev, dict) else None
            if prev_url and prev_url != url:
                removed = self._remove_config_managed_by_id(sub_id)
                if removed:
                    changed = True

            if url not in cache_latest:
                cache_latest[url] = await self._ensure_channel_initialized(url)
                if url in self.data_handler.data:
                    changed = True
            latest_item = cache_latest.get(url)
            if latest_item is None:
                continue

            self.data_handler.data.setdefault(url, {})
            self.data_handler.data[url].setdefault("subscribers", {})
            self.data_handler.data[url].setdefault("info", {})

            for user in targets:
                self.data_handler.data[url]["subscribers"].setdefault(user, {})
                sub = self.data_handler.data[url]["subscribers"][user].get(sub_id, {})
                # 仅更新 cron；last_update/latest_link 如果已有则保留，避免重置导致漏推
                sub["cron_expr"] = cron_expr
                sub.setdefault("last_update", latest_item.pubDate_timestamp)
                sub.setdefault("latest_link", latest_item.link)
                sub["managed_by_config"] = True
                sub["config_id"] = sub_id
                self.data_handler.data[url]["subscribers"][user][sub_id] = sub
                changed = True

            # targets 缩减时：清理该 id 在当前 url 下不再需要的托管订阅
            subscribers = self.data_handler.data.get(url, {}).get("subscribers", {}) or {}
            for existing_user in list(subscribers.keys()):
                existing = subscribers.get(existing_user, {}) or {}
                if (
                    isinstance(existing, dict)
                    and sub_id in existing
                    and (existing.get(sub_id) or {}).get("managed_by_config")
                    and existing_user not in targets
                ):
                    existing.pop(sub_id, None)
                    if not existing:
                        subscribers.pop(existing_user, None)
                    changed = True
            if not subscribers:
                self.data_handler.data.pop(url, None)

            # 写入索引（用于下次识别 url 变更/删除）
            config_index[sub_id] = {
                "url": url,
                "targets": targets,
                "cron_expr": cron_expr,
                "title_override": title_override,
                "description_override": description_override,
                "weekly_stats_enabled": weekly_stats_enabled,
            }

        # 清理：配置中已删除的 id，对应托管订阅也移除
        for sub_id in list(config_index.keys()):
            if sub_id not in current_ids:
                removed = self._remove_config_managed_by_id(sub_id)
                config_index.pop(sub_id, None)
                if removed:
                    changed = True

        if changed:
            self.data_handler.save_data()
            self._fresh_asyncIOScheduler()

    def _build_job_id(self, url: str, user: str, sub_key: str) -> str:
        """构造稳定且长度可控的任务 ID，避免重复创建同一订阅任务。"""
        digest = hashlib.md5(f"{url}|{user}|{sub_key}".encode("utf-8")).hexdigest()
        return f"rss_{digest}"

    def _parse_unified_msg_origin(self, umo: str) -> tuple[str, str, str]:
        """解析 unified_msg_origin，限制 split 次数避免 session_id 含冒号时误切分。"""
        parts = umo.split(":", 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return parts[0], parts[1], ""
        if len(parts) == 1:
            return parts[0], "", ""
        return "", "", ""

    def _get_platform_type_by_id(self, platform_id: str) -> str | None:
        """根据平台 ID 获取平台类型（如 aiocqhttp, telegram 等）。"""
        try:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                if meta.id == platform_id:
                    return meta.name
        except Exception:
            return None
        return None

    def _should_compose_for_session(self, umo: str) -> bool:
        """判断该会话是否应使用合并转发。"""
        if not self.is_compose:
            return False
        platform_id, _, _ = self._parse_unified_msg_origin(umo)
        platform_type = self._get_platform_type_by_id(platform_id)
        # 兼容极端场景：无法取到平台实例时，回退判断 ID 本身。
        return platform_type == "aiocqhttp" or platform_id == "aiocqhttp"

    def _read_scheduler_lock(self) -> dict | None:
        try:
            if not os.path.exists(self.scheduler_lock_path):
                return None
            with open(self.scheduler_lock_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _pid_exists(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _claim_scheduler_owner(self, force_same_pid: bool = False) -> bool:
        os.makedirs(os.path.dirname(self.scheduler_lock_path), exist_ok=True)
        current_pid = os.getpid()
        lock_info = self._read_scheduler_lock()

        if lock_info:
            lock_pid = int(lock_info.get("pid", 0))
            same_pid = lock_pid == current_pid
            stale_lock = not self._pid_exists(lock_pid)
            if not (stale_lock or same_pid):
                self.scheduler_owner_token = None
                return False
            if same_pid and not force_same_pid:
                # 热重载后同进程重建插件实例，默认允许接管。
                pass
            try:
                os.remove(self.scheduler_lock_path)
            except FileNotFoundError:
                pass
            except OSError:
                self.scheduler_owner_token = None
                return False

        token = str(uuid.uuid4())
        payload = {"pid": current_pid, "token": token, "ts": int(time.time())}
        try:
            fd = os.open(
                self.scheduler_lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            self.scheduler_owner_token = token
            return True
        except FileExistsError:
            self.scheduler_owner_token = None
            return False

    def _is_active_scheduler_owner(self) -> bool:
        if not self.scheduler_owner_token:
            return False
        lock_info = self._read_scheduler_lock()
        if not lock_info:
            return False
        return (
            str(lock_info.get("token")) == self.scheduler_owner_token
            and int(lock_info.get("pid", -1)) == os.getpid()
        )

    def _release_scheduler_owner(self):
        if not self.scheduler_owner_token:
            return
        lock_info = self._read_scheduler_lock()
        if (
            lock_info
            and str(lock_info.get("token")) == self.scheduler_owner_token
            and int(lock_info.get("pid", -1)) == os.getpid()
        ):
            try:
                os.remove(self.scheduler_lock_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self.scheduler_owner_token = None

    async def parse_channel_info(self, url):
        headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        try:
            async with aiohttp.ClientSession(trust_env=True,
                                        connector=connector,
                                        timeout=timeout,
                                        headers=headers
                                        ) as session:
                async with session.get(url, proxy=self.proxy_server) as resp:
                    if resp.status != 200:
                        self.logger.error(f"rss: 无法正常打开站点 {url}")
                        return None
                    text = await resp.read()
                    return text
        except asyncio.TimeoutError:
            self.logger.error(f"rss: 请求站点 {url} 超时")
            return None
        except aiohttp.ClientError as e:
            self.logger.error(f"rss: 请求站点 {url} 网络错误: {str(e)}")
            return None
        except Exception as e:
            self.logger.error(f"rss: 请求站点 {url} 发生未知错误: {str(e)}")
            return None

    async def cron_task_callback(self, url: str, user: str, sub_key: str):
        """定时任务回调"""
        if not self._is_active_scheduler_owner():
            # 非持锁实例（或已失去持锁）直接跳过，避免重复推送。
            return

        if url not in self.data_handler.data:
            return
        self._normalize_all_subscribers()
        user_map = self.data_handler.data.get(url, {}).get("subscribers", {}).get(user)
        if not isinstance(user_map, dict) or sub_key not in user_map:
            return

        sub_info = user_map.get(sub_key) or {}
        self.logger.info(f"RSS 定时任务触发: {url} - {user} - {sub_key}")
        last_update = sub_info.get("last_update", 0)
        latest_link = sub_info.get("latest_link", "")
        max_items_per_poll = self.max_items_per_poll
        # 拉取 RSS
        rss_items = await self.poll_rss(
            url,
            num=max_items_per_poll,
            after_timestamp=last_update,
            after_link=latest_link,
            user=user,
            sub_key=sub_key,
        )
        max_ts = last_update

        # 分解MessageSesion
        platform_id, message_type, session_id = self._parse_unified_msg_origin(user)

        # 分平台处理消息
        if self._should_compose_for_session(user):
            nodes = []
            for item in rss_items:
                comps = await self._get_chain_components(item)
                node = Comp.Node(
                            uin=0,
                            name="Astrbot",
                            content=comps
                        )
                nodes.append(node)
                sub_info["last_update"] = int(time.time())
                max_ts = max(max_ts, item.pubDate_timestamp)

            # 合并消息发送
            if len(nodes) > 0:
                msc = MessageChain(
                    chain=nodes,
                    use_t2i_= self.t2i
                )
                await self.context.send_message(user, msc)
        else:
            # 每个消息单独发送
            for item in rss_items:
                comps = await self._get_chain_components(item)
                msc = MessageChain(
                chain=comps,
                use_t2i_= self.t2i
            )
                await self.context.send_message(user, msc)
                sub_info["last_update"] = int(time.time())
                max_ts = max(max_ts, item.pubDate_timestamp)

        # 更新最后更新时间
        if rss_items:
            sub_info["last_update"] = max_ts
            sub_info["latest_link"] = rss_items[0].link
            self.data_handler.save_data()
            self.logger.info(f"RSS 定时任务 {url} 推送成功 - {user}")
        else:
            self.logger.info(f"RSS 定时任务 {url} 无消息更新 - {user}")


    async def poll_rss(
        self,
        url: str,
        num: int = -1,
        after_timestamp: int = 0,
        after_link: str = "",
        user: str | None = None,
        sub_key: str | None = None,
    ) -> List[RSSItem]:
        """从站点拉取RSS信息"""
        text = await self.parse_channel_info(url)
        if text is None:
            self.logger.error(f"rss: 无法解析站点 {url} 的RSS信息")
            return []
        root = etree.fromstring(text)
        items = root.xpath("//item")

        cnt = 0
        rss_items = []

        for item in items:
            try:
                display_info = self._get_channel_display_info(url, user=user, sub_key=sub_key)
                chan_title = display_info.get("title") or "未知频道"

                title = item.xpath("title")[0].text
                if len(title) > self.title_max_length:
                    title = title[: self.title_max_length] + "..."

                link = item.xpath("link")[0].text
                if not re.match(r"^https?://", link):
                    link = self.data_handler.get_root_url(url) + link

                description = item.xpath("description")[0].text

                pic_url_list = self.data_handler.strip_html_pic(description)
                description = self.data_handler.strip_html(description)

                if len(description) > self.description_max_length:
                    description = (
                        description[: self.description_max_length] + "..."
                    )

                if item.xpath("pubDate"):
                    # 根据 pubDate 判断是否为新内容
                    pub_date = item.xpath("pubDate")[0].text
                    pub_date_parsed = time.strptime(
                        pub_date.replace("GMT", "+0000"),
                        "%a, %d %b %Y %H:%M:%S %z",
                    )
                    pub_date_timestamp = int(time.mktime(pub_date_parsed))
                    # 已处理过的最新链接，直接停止，避免同内容重复推送。
                    if link == after_link:
                        break

                    if pub_date_timestamp > after_timestamp or (
                        pub_date_timestamp == after_timestamp and link != after_link
                    ):
                        rss_items.append(
                            RSSItem(
                                chan_title,
                                title,
                                link,
                                description,
                                pub_date,
                                pub_date_timestamp,
                                pic_url_list
                            )
                        )
                        cnt += 1
                        if num != -1 and cnt >= num:
                            break
                    else:
                        break
                else:
                    # 根据 link 判断是否为新内容
                    if link != after_link:
                        rss_items.append(
                            RSSItem(chan_title, title, link, description, "", 0, pic_url_list)
                        )
                        cnt += 1
                        if num != -1 and cnt >= num:
                            break
                    else:
                        break

            except Exception as e:
                self.logger.error(f"rss: 解析Rss条目 {url} 失败: {str(e)}")
                break

        return rss_items

    def parse_rss_url(self, url: str) -> str:
        """解析RSS URL，确保以http或https开头"""
        if not re.match(r"^https?://", url):
            url = "https://" + url.lstrip("/")
        return url

    def _fresh_asyncIOScheduler(self):
        """刷新定时任务"""
        if not self._is_active_scheduler_owner():
            self.logger.warning("当前实例不是调度器持有者，跳过任务刷新。")
            return
        # 删除所有定时任务
        self.logger.info("刷新定时任务")
        self.scheduler.remove_all_jobs()

        # 为每个订阅添加定时任务
        self._normalize_all_subscribers()
        for url, info in self.data_handler.data.items():
            if url == "rsshub_endpoints" or url == "settings":
                continue
            subscribers = info.get("subscribers", {})
            if not isinstance(subscribers, dict) or not subscribers:
                continue
            for user, user_map in subscribers.items():
                if not isinstance(user_map, dict):
                    continue
                for sub_key, sub_info in user_map.items():
                    if not isinstance(sub_info, dict):
                        continue
                    try:
                        cron_fields = self.parse_cron_expr(sub_info["cron_expr"])
                    except Exception as e:
                        self.logger.warning(
                            f"RSS 跳过非法 cron_expr：{sub_info.get('cron_expr')}（url={url}, user={user}, sub={sub_key}）: {e}"
                        )
                        continue
                    self.scheduler.add_job(
                        self.cron_task_callback,
                        "cron",
                        id=self._build_job_id(url, user, str(sub_key)),
                        replace_existing=True,
                        **cron_fields,
                        args=[url, user, str(sub_key)],
                    )

        # 每周统计推送（按会话聚合）
        if self.weekly_report_enabled:
            cron_fields = self._parse_cron_expr_safe(self.weekly_report_cron_expr)
            if cron_fields is None:
                self.logger.warning(
                    f"RSS weekly_report cron_expr 非法：{self.weekly_report_cron_expr}，已跳过周报定时推送"
                )
            else:
                users: set[str] = set()
                for url, info in self.data_handler.data.items():
                    if url in ("rsshub_endpoints", "settings"):
                        continue
                    subs = (info or {}).get("subscribers", {})
                    if isinstance(subs, dict):
                        users.update([u for u in subs.keys() if isinstance(u, str) and u])

                for user in users:
                    digest = hashlib.md5(f"weekly|{user}".encode("utf-8")).hexdigest()
                    job_id = f"rss_weekly_{digest}"
                    self.scheduler.add_job(
                        self.weekly_report_task_callback,
                        "cron",
                        id=job_id,
                        replace_existing=True,
                        **cron_fields,
                        args=[user],
                    )

    async def weekly_report_task_callback(self, user: str):
        """每周统计推送（聚合到单个会话）。"""
        if not self._is_active_scheduler_owner():
            return
        await self._send_weekly_report(user)

    async def _send_weekly_report(self, user: str) -> None:
        msg = await self._build_weekly_report_text(user)
        if not msg:
            return
        try:
            await self.context.send_message(user, MessageChain(chain=[Comp.Plain(msg)], use_t2i_=self.t2i))
        except Exception as e:
            self.logger.warning(f"RSS 周报推送失败 {user}: {e}")

    async def _build_weekly_report_text(self, user: str) -> str | None:
        now_ts = int(time.time())
        since_ts = now_ts - 7 * 24 * 60 * 60
        entries = self._iter_user_subscription_entries(user)
        if not entries:
            return None

        settings = self.data_handler.data.get("settings", {}) or {}
        config_index = settings.get("config_subscriptions", {}) or {}

        lines = ["RSS 周报（最近 7 天）", ""]
        for entry in entries:
            url = entry["url"]
            sub_key = entry["sub_key"]
            sub_info = entry["sub_info"] or {}

            # 若为托管订阅且关闭 weekly_stats_enabled，则跳过
            if sub_info.get("managed_by_config"):
                cid = sub_info.get("config_id")
                conf = config_index.get(cid) if isinstance(config_index, dict) else None
                if isinstance(conf, dict) and conf.get("weekly_stats_enabled") is False:
                    continue

            display = self._get_channel_display_info(url, user=user, sub_key=sub_key)
            count, truncated = await self._count_items_published_since(
                url, since_timestamp=since_ts, limit=self.weekly_report_max_items_per_feed
            )
            if count is None:
                count_text = "N/A(pubDate缺失)"
            else:
                count_text = (
                    f">={self.weekly_report_max_items_per_feed}" if truncated else str(count)
                )

            display_id = self._get_entry_display_id(entry)
            lines.append(f"- {display.get('title') or '未知频道'} (id={display_id}): {count_text}")

        if len(lines) <= 2:
            return None
        return "\n".join(lines)

    async def terminate(self):
        """插件终止时关闭调度器，避免重载后旧任务残留导致重复推送。"""
        try:
            if hasattr(self, "scheduler") and self.scheduler:
                self.scheduler.remove_all_jobs()
                # wait=False 避免在关闭阶段阻塞事件循环
                self.scheduler.shutdown(wait=False)
            self._release_scheduler_owner()
            self.logger.info("RSS 插件已终止，调度任务已清理。")
        except Exception as e:
            self.logger.warning(f"RSS 插件终止时清理调度器失败: {e}")

    async def _add_url(self, url: str, cron_expr: str, message: AstrMessageEvent):
        """内部方法：添加URL订阅的共用逻辑"""
        user = message.unified_msg_origin
        self._normalize_all_subscribers()
        manual_key = "__manual__"
        if url in self.data_handler.data:
            latest_item = await self.poll_rss(url, user=user)
            self.data_handler.data[url].setdefault("subscribers", {})
            self.data_handler.data[url]["subscribers"].setdefault(user, {})
            self.data_handler.data[url]["subscribers"][user][manual_key] = {
                "cron_expr": cron_expr,
                "last_update": latest_item[0].pubDate_timestamp,
                "latest_link": latest_item[0].link,
            }
        else:
            try:
                text = await self.parse_channel_info(url)
                title, desc = self.data_handler.parse_channel_text_info(text)
                latest_item = await self.poll_rss(url, user=user)
            except Exception as e:
                return message.plain_result(f"解析频道信息失败: {str(e)}")

            self.data_handler.data[url] = {
                "subscribers": {
                    user: {
                        manual_key: {
                        "cron_expr": cron_expr,
                        "last_update": latest_item[0].pubDate_timestamp,
                        "latest_link": latest_item[0].link,
                        }
                    }
                },
                "info": {
                    "title": title,
                    "description": desc,
                },
            }
        self.data_handler.save_data()
        return self.data_handler.data[url]["info"]

    def _remove_user_subscription(self, url: str, user: str) -> None:
        """移除用户在指定 URL 下的订阅；当该 URL 已无订阅者时一并删除该 URL 数据。"""
        if url not in self.data_handler.data:
            return
        subscribers = self.data_handler.data.get(url, {}).get("subscribers", {})
        subscribers.pop(user, None)
        if not subscribers:
            self.data_handler.data.pop(url, None)

    async def _edit_subscription_url(
        self, message: AstrMessageEvent, old_url: str, new_url: str
    ) -> MessageEventResult:
        """修改当前会话已添加订阅的 Feed URL，保留原 cron，并重置 last_update/latest_link。"""
        user = message.unified_msg_origin
        if old_url not in self.data_handler.data:
            return message.plain_result("修改失败：找不到原订阅数据")
        old_sub = self.data_handler.data.get(old_url, {}).get("subscribers", {}).get(user)
        if not old_sub:
            return message.plain_result("修改失败：当前会话未订阅该源")

        normalized_new_url = self.parse_rss_url(new_url)
        if normalized_new_url == old_url:
            return message.plain_result("修改失败：新 URL 与原 URL 相同")

        cron_expr = old_sub.get("cron_expr", "* * * * *")
        try:
            text = await self.parse_channel_info(normalized_new_url)
            title, desc = self.data_handler.parse_channel_text_info(text)
            latest_item = await self.poll_rss(normalized_new_url, num=1)
            if not latest_item:
                return message.plain_result("修改失败：新 URL 无法获取到订阅内容")
        except Exception as e:
            return message.plain_result(f"修改失败：新 URL 解析失败: {str(e)}")

        if normalized_new_url not in self.data_handler.data:
            self.data_handler.data[normalized_new_url] = {
                "subscribers": {},
                "info": {"title": title, "description": desc},
            }
        else:
            # 更新频道信息（尽量保持最新）
            try:
                self.data_handler.data[normalized_new_url].setdefault("info", {})
                self.data_handler.data[normalized_new_url]["info"]["title"] = title
                self.data_handler.data[normalized_new_url]["info"]["description"] = desc
            except Exception:
                pass

        self.data_handler.data[normalized_new_url].setdefault("subscribers", {})
        self.data_handler.data[normalized_new_url]["subscribers"][user] = {
            "cron_expr": cron_expr,
            "last_update": latest_item[0].pubDate_timestamp,
            "latest_link": latest_item[0].link,
        }

        self._remove_user_subscription(old_url, user)
        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        return message.plain_result(
            "修改成功。\n"
            f"- 新订阅源: {normalized_new_url}\n"
            f"- Cron: {cron_expr}"
        )

    async def _get_chain_components(self, item: RSSItem):
        """组装消息链"""
        comps = []
        comps.append(Comp.Plain(f"频道 {item.chan_title} 最新 Feed\n---\n标题: {item.title}\n---\n"))
        if not self.is_hide_url:
            comps.append(Comp.Plain(f"链接: {item.link}\n---\n"))
        comps.append(Comp.Plain(item.description+"\n---\n"))
        if self.is_read_pic and item.pic_urls:
            # 如果max_pic_item为-1则不限制图片数量
            temp_max_pic_item = len(item.pic_urls) if self.max_pic_item == -1 else self.max_pic_item
            for pic_url in item.pic_urls[:temp_max_pic_item]:
                base64str = await self.pic_handler.modify_corner_pixel_to_base64(pic_url)
                if base64str is None:
                    comps.append(Comp.Plain("图片链接读取失败\n"))
                    continue
                else:
                    comps.append(Comp.Image.fromBase64(base64str))
        return comps


    def _is_url_or_ip(self,text: str) -> bool:
        """
        判断一个字符串是否为网址（http/https 开头）或 IP 地址。
        """
        url_pattern = r"^(?:http|https)://.+$"
        ip_pattern = r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
        return bool(re.match(url_pattern, text) or re.match(ip_pattern, text))

    @filter.command_group("rss", alias={"RSS"})
    def rss(self):
        """RSS订阅插件

        可以订阅和管理多个RSS源，支持cron表达式设置更新频率

        cron 表达式格式：
        * * * * *，分别表示分钟 小时 日 月 星期，* 表示任意值，支持范围和逗号分隔。例：
        1. 0 0 * * * 表示每天 0 点触发。
        2. 0/5 * * * * 表示每 5 分钟触发。
        3. 0 9-18 * * * 表示每天 9 点到 18 点触发。
        4. 0 0 1,15 * * 表示每月 1 号和 15 号 0 点触发。
        星期的取值范围是 0-6，0 表示星期天。
        """
        pass

    @rss.group("scheduler")
    def scheduler_group(self, event: AstrMessageEvent):
        """RSS 调度器管理命令"""
        pass

    @scheduler_group.command("status")
    async def scheduler_status(self, event: AstrMessageEvent):
        """查看调度器状态"""
        lock_info = self._read_scheduler_lock() or {}
        lock_pid = int(lock_info.get("pid", 0)) if lock_info else 0
        lock_token = str(lock_info.get("token", "")) if lock_info else ""
        lock_token_short = lock_token[:8] + "..." if lock_token else "N/A"
        jobs_count = len(self.scheduler.get_jobs()) if self.scheduler.running else 0
        is_owner = self._is_active_scheduler_owner()
        current_pid = os.getpid()
        ret = (
            "RSS 调度器状态：\n"
            f"- 当前进程 PID: {current_pid}\n"
            f"- 本实例持锁: {is_owner}\n"
            f"- 调度器运行中: {self.scheduler.running}\n"
            f"- 当前实例任务数: {jobs_count}\n"
            f"- 锁文件 PID: {lock_pid or 'N/A'}\n"
            f"- 锁文件 Token: {lock_token_short}\n"
        )
        if lock_pid and lock_pid != current_pid and self._pid_exists(lock_pid):
            ret += "- 检测到锁被其他存活进程持有，当前实例不会执行定时任务。"
        yield event.plain_result(ret)

    @scheduler_group.command("repair")
    async def scheduler_repair(self, event: AstrMessageEvent):
        """尝试修复调度器并重建任务"""
        lock_info = self._read_scheduler_lock() or {}
        lock_pid = int(lock_info.get("pid", 0)) if lock_info else 0
        current_pid = os.getpid()

        if lock_pid and lock_pid != current_pid and self._pid_exists(lock_pid):
            yield event.plain_result(
                f"修复失败：调度锁当前由其他进程持有 (pid={lock_pid})。请先停止其他 AstrBot 实例后重试。"
            )
            return

        claimed = self._claim_scheduler_owner(force_same_pid=True)
        if not claimed and not self._is_active_scheduler_owner():
            yield event.plain_result("修复失败：无法获取调度锁。")
            return

        if not self.scheduler.running:
            self.scheduler.start()
        self._fresh_asyncIOScheduler()
        jobs_count = len(self.scheduler.get_jobs()) if self.scheduler.running else 0
        yield event.plain_result(
            f"修复完成：当前实例已持锁并重建任务，任务数 {jobs_count}。"
        )

    @rss.group("rsshub")
    def rsshub(self, event: AstrMessageEvent):
        """RSSHub相关操作

        可以添加、查看、删除RSSHub的端点
        """
        pass

    @rsshub.command("add")
    async def rsshub_add(self, event: AstrMessageEvent, url: str):
        """添加一个RSSHub端点

        Args:
            url: RSSHub服务器地址，例如：https://rsshub.app
        """
        if url.endswith("/"):
            url = url[:-1]
        # 检查是否为url或ip
        if not self._is_url_or_ip(url):
            yield event.plain_result("请输入正确的URL")
            return
        # 检查该网址是否已存在
        elif url in self.data_handler.data["rsshub_endpoints"]:
            yield event.plain_result("该RSSHub端点已存在")
            return
        else:
            self.data_handler.data["rsshub_endpoints"].append(url)
            self.data_handler.save_data()
            yield event.plain_result("添加成功")

    @rsshub.command("list")
    async def rsshub_list(self, event: AstrMessageEvent):
        """列出所有已添加的RSSHub端点"""
        ret = "当前Bot添加的rsshub endpoint：\n"
        yield event.plain_result(
            ret
            + "\n".join(
                [
                    f"{i}: {x}"
                    for i, x in enumerate(self.data_handler.data["rsshub_endpoints"])
                ]
            )
        )

    @rsshub.command("remove")
    async def rsshub_remove(self, event: AstrMessageEvent, idx: int):
        """删除一个RSSHub端点

        Args:
            idx: 要删除的端点索引，可通过list命令查看
        """
        if idx < 0 or idx >= len(self.data_handler.data["rsshub_endpoints"]):
            yield event.plain_result("索引越界")
            return
        else:
            # TODO:删除对应的定时任务
            self.scheduler.remove_job()
            self.data_handler.data["rsshub_endpoints"].pop(idx)
            self.data_handler.save_data()
            yield event.plain_result("删除成功")

    @rss.command("add")
    async def add_command(
        self,
        event: AstrMessageEvent,
        idx: int,
        route: str,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """通过RSSHub路由添加订阅

        Args:
            idx: RSSHub端点索引，可通过/rss rsshub list查看
            route: RSSHub路由，需以/开头
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        if idx < 0 or idx >= len(self.data_handler.data["rsshub_endpoints"]):
            yield event.plain_result(
                "索引越界, 请使用 /rss rsshub list 查看已经添加的 rsshub endpoint"
            )
            return
        if not route.startswith("/"):
            yield event.plain_result("路由必须以 / 开头")
            return

        url = self.data_handler.data["rsshub_endpoints"][idx] + route
        cron_expr = f"{minute} {hour} {day} {month} {day_of_week}"

        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        else:
            chan_title = ret["title"]
            chan_desc = ret["description"]

        # 刷新定时任务
        self._fresh_asyncIOScheduler()

        yield event.plain_result(
            f"添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}"
        )

    @rss.command("add-url")
    async def add_url_command(
        self,
        event: AstrMessageEvent,
        url: str,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """直接通过Feed URL添加订阅

        Args:
            url: RSS Feed的完整URL
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        cron_expr = f"{minute} {hour} {day} {month} {day_of_week}"
        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        else:
            chan_title = ret["title"]
            chan_desc = ret["description"]

        # 刷新定时任务
        self._fresh_asyncIOScheduler()

        yield event.plain_result(
            f"添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}"
        )

    @rss.command("list")
    async def list_command(self, event: AstrMessageEvent):
        """列出当前所有订阅的RSS频道"""
        user = event.unified_msg_origin
        entries = self._iter_user_subscription_entries(user)
        if not entries:
            yield event.plain_result("当前没有订阅。")
            return

        ret = "当前订阅：\n"
        for idx, entry in enumerate(entries):
            url = entry["url"]
            sub_key = entry["sub_key"]
            sub_info = entry["sub_info"]
            info = self._get_channel_display_info(url, user=user, sub_key=sub_key)
            display_id = self._get_entry_display_id(entry)
            cron_expr = sub_info.get("cron_expr", "")
            ret += f"{idx}. {info['title']} - {info['description']} (id={display_id}, cron={cron_expr})\n"
        yield event.plain_result(ret)

    @rss.command("sync-config")
    async def sync_config_command(self, event: AstrMessageEvent):
        """将可视化配置 subscriptions 同步到运行时数据（尽量无需重载插件）。"""
        self._visual_subscriptions = self.config.get("subscriptions") or []
        await self._bootstrap_visual_subscriptions()
        yield event.plain_result("已同步可视化订阅配置到运行中数据。若仍未生效，请尝试重载插件。")

    @rss.command("weekly")
    async def weekly_command(self, event: AstrMessageEvent):
        """立即查看当前会话的“最近 7 天更新条目数”统计。"""
        msg = await self._build_weekly_report_text(event.unified_msg_origin)
        if not msg:
            yield event.plain_result("当前没有可统计的订阅（或订阅源缺少 pubDate）。")
            return
        yield event.plain_result(msg)

    @rss.command("edit-url")
    async def edit_url_command(self, event: AstrMessageEvent, idx: int, url: str):
        """修改已添加订阅的订阅源（Feed URL）"""
        user = event.unified_msg_origin
        entries = self._iter_user_subscription_entries(user)
        if idx < 0 or idx >= len(entries):
            yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
            return
        entry = entries[idx]
        old_url = entry["url"]
        sub_key = entry["sub_key"]
        sub_info = entry["sub_info"]
        if sub_info.get("managed_by_config"):
            yield event.plain_result("该订阅由可视化配置托管，请在插件配置界面修改后执行 /rss sync-config。")
            return

        new_url = self.parse_rss_url(url)
        if new_url == old_url:
            yield event.plain_result("修改失败：新 URL 与原 URL 相同")
            return

        try:
            latest_item = await self._ensure_channel_initialized(new_url)
            if latest_item is None:
                yield event.plain_result("修改失败：新 URL 无法获取到订阅内容")
                return
        except Exception as e:
            yield event.plain_result(f"修改失败：新 URL 解析失败: {e}")
            return

        # 从旧 URL 移除该条目
        self._normalize_all_subscribers()
        old_user_map = (
            self.data_handler.data.get(old_url, {}).get("subscribers", {}).get(user, {})
        )
        if isinstance(old_user_map, dict):
            old_user_map.pop(sub_key, None)
            if not old_user_map:
                self.data_handler.data.get(old_url, {}).get("subscribers", {}).pop(user, None)
        # 若旧 URL 已无订阅者，清理 URL 节点
        old_subscribers = self.data_handler.data.get(old_url, {}).get("subscribers", {})
        if not old_subscribers and old_url in self.data_handler.data:
            self.data_handler.data.pop(old_url, None)

        # 添加到新 URL（保持 sub_key 不变）
        self.data_handler.data.setdefault(new_url, {}).setdefault("subscribers", {})
        self.data_handler.data[new_url]["subscribers"].setdefault(user, {})
        self.data_handler.data[new_url]["subscribers"][user][sub_key] = {
            **sub_info,
            "last_update": latest_item.pubDate_timestamp,
            "latest_link": latest_item.link,
        }

        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        yield event.plain_result(f"修改成功：已将订阅源切换为 {new_url}")

    @rss.command("edit-cron")
    async def edit_cron_command(
        self,
        event: AstrMessageEvent,
        idx: int,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """修改已添加订阅的 Cron 表达式"""
        user = event.unified_msg_origin
        entries = self._iter_user_subscription_entries(user)
        if idx < 0 or idx >= len(entries):
            yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
            return
        entry = entries[idx]
        url = entry["url"]
        sub_key = entry["sub_key"]
        sub_info = entry["sub_info"]
        if sub_info.get("managed_by_config"):
            yield event.plain_result("该订阅由可视化配置托管，请在插件配置界面修改后执行 /rss sync-config。")
            return

        cron_expr = f"{minute} {hour} {day} {month} {day_of_week}"
        self._normalize_all_subscribers()
        self.data_handler.data[url]["subscribers"].setdefault(user, {})
        self.data_handler.data[url]["subscribers"][user].setdefault(sub_key, {})
        self.data_handler.data[url]["subscribers"][user][sub_key]["cron_expr"] = cron_expr
        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        yield event.plain_result(f"修改成功。新 Cron: {cron_expr}")

    @rss.command("remove")
    async def remove_command(self, event: AstrMessageEvent, idx: int):
        """删除一个RSS订阅

        Args:
            idx: 要删除的订阅索引，可通过/rss list查看
        """
        user = event.unified_msg_origin
        entries = self._iter_user_subscription_entries(user)
        if idx < 0 or idx >= len(entries):
            yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
            return
        entry = entries[idx]
        url = entry["url"]
        sub_key = entry["sub_key"]
        sub_info = entry["sub_info"]
        if sub_info.get("managed_by_config"):
            yield event.plain_result("该订阅由可视化配置托管，请在插件配置界面删除后执行 /rss sync-config。")
            return

        self._normalize_all_subscribers()
        user_map = self.data_handler.data.get(url, {}).get("subscribers", {}).get(user, {})
        if isinstance(user_map, dict):
            user_map.pop(sub_key, None)
            if not user_map:
                self.data_handler.data.get(url, {}).get("subscribers", {}).pop(user, None)

        subscribers = self.data_handler.data.get(url, {}).get("subscribers", {})
        if not subscribers and url in self.data_handler.data:
            self.data_handler.data.pop(url, None)

        self.data_handler.save_data()

        # 刷新定时任务
        self._fresh_asyncIOScheduler()
        yield event.plain_result("删除成功")

    @rss.command("get")
    async def get_command(self, event: AstrMessageEvent, idx: int):
        """获取指定订阅的最新内容

        Args:
            idx: 要查看的订阅索引，可通过/rss list查看
        """
        user = event.unified_msg_origin
        entries = self._iter_user_subscription_entries(user)
        if idx < 0 or idx >= len(entries):
            yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
            return
        entry = entries[idx]
        url = entry["url"]
        sub_key = entry["sub_key"]
        rss_items = await self.poll_rss(url, user=user, sub_key=sub_key)
        if not rss_items:
            yield event.plain_result("没有新的订阅内容")
            return
        item = rss_items[0]
        # 分解MessageSesion
        platform_id, message_type, session_id = self._parse_unified_msg_origin(
            event.unified_msg_origin
        )
        # 构造返回消息链
        comps = await self._get_chain_components(item)
        # 区分平台
        if self._should_compose_for_session(event.unified_msg_origin):
            node = Comp.Node(
                    uin=0,
                    name="Astrbot",
                    content=comps
                )
            yield event.chain_result([node]).use_t2i(self.t2i)
        else:
            yield event.chain_result(comps).use_t2i(self.t2i)
