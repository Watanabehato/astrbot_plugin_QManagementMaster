import html
import json
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, At
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


@register("QManagementMaster", "Watanabehato", "QQ多群联动违规管理插件", "1.2.4")
class GroupManagerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # 使用 AstrBot 配置系统
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "QManagementMaster"
        self.db_path = self.data_dir / "records.db"
        self._groups_lock = asyncio.Lock()
        self._blacklist_lock = asyncio.Lock()

    async def initialize(self):
        """插件初始化：创建目录和数据库"""
        # 创建数据目录
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 初始化数据库
        self.init_database()

        # 迁移旧的 KV groups 数据到插件配置
        old_groups = await self.get_kv_data("groups", {})
        if old_groups and isinstance(old_groups, dict) and len(old_groups) > 0:
            new_groups = {}
            for name, cfg in old_groups.items():
                if not isinstance(cfg, dict):
                    continue
                new_groups[str(name)] = {
                    "log_group": cfg.get("播报群", ""),
                    "exec_groups": cfg.get("执行群列表", [])
                }
            if new_groups:
                self.config["groups"] = self._groups_config_text_from_mapping(new_groups)
                self.config.save_config()
                await self.put_kv_data("groups", {})
                logger.info(f"已迁移 {len(new_groups)} 个联动组到插件配置")

        # 将 v1.2.2 的 list 配置或手写 dict 配置转换为后台 JSON 文本编辑器格式
        if self._normalize_groups_config():
            self.config.save_config()

        # 迁移旧的 KV blacklist 数据到插件配置
        old_blacklist = await self.get_kv_data("blacklist", [])
        if old_blacklist and isinstance(old_blacklist, list) and len(old_blacklist) > 0:
            migrated = []
            for entry in old_blacklist:
                if isinstance(entry, dict):
                    migrated.append({
                        "qq": str(entry.get("qq", "")),
                        "reason": entry.get("reason", ""),
                        "time": entry.get("time", ""),
                        "operator": entry.get("operator", "")
                    })
            if migrated:
                self.config["blacklist"] = migrated
                self.config.save_config()
                await self.put_kv_data("blacklist", [])
                logger.info(f"已迁移 {len(migrated)} 条黑名单到插件配置")

        logger.info("GroupManager 插件初始化完成")

    def _get_groups(self) -> list:
        raw_groups = self.config.get("groups", "{}")
        return self._groups_from_config(raw_groups)

    def _save_groups(self, groups: list):
        self.config["groups"] = self._groups_config_text_from_list(groups)
        self.config.save_config()

    def _normalize_groups_config(self) -> bool:
        raw_groups = self.config.get("groups", "{}")
        if isinstance(raw_groups, str):
            return False
        if isinstance(raw_groups, list):
            self.config["groups"] = self._groups_config_text_from_list(raw_groups)
            return True
        if isinstance(raw_groups, dict):
            self.config["groups"] = self._groups_config_text_from_mapping(raw_groups)
            return True
        return False

    def _groups_from_config(self, raw_groups: Any) -> list:
        if isinstance(raw_groups, list):
            return self._groups_from_list(raw_groups)
        if isinstance(raw_groups, dict):
            return self._groups_from_mapping(raw_groups)
        if isinstance(raw_groups, str):
            text = raw_groups.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.error(f"groups 不是合法 JSON: {exc}")
                return []
            if isinstance(parsed, dict):
                return self._groups_from_mapping(parsed)
            if isinstance(parsed, list):
                return self._groups_from_list(parsed)
            logger.error("groups JSON 必须是对象，例如 {\"主群网络\": {\"log_group\": \"123\", \"exec_groups\": [\"123\"]}}")
            return []
        return []

    def _groups_from_mapping(self, groups: dict) -> list:
        normalized = []
        for name, cfg in groups.items():
            if not isinstance(cfg, dict):
                logger.warning(f"忽略非法联动组配置: {name}")
                continue
            normalized.append(self._normalize_group_config(str(name), cfg))
        return normalized

    def _groups_from_list(self, groups: list) -> list:
        normalized = []
        for item in groups:
            if not isinstance(item, dict):
                logger.warning(f"忽略非法联动组配置: {item}")
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                logger.warning(f"忽略缺少 name 的联动组配置: {item}")
                continue
            normalized.append(self._normalize_group_config(name, item))
        return normalized

    def _normalize_group_config(self, name: str, cfg: dict) -> dict:
        exec_groups = cfg.get("exec_groups", cfg.get("执行群列表", []))
        if isinstance(exec_groups, str):
            exec_groups = [line.strip() for line in exec_groups.splitlines() if line.strip()]
        if not isinstance(exec_groups, list):
            exec_groups = []
        return {
            "name": name,
            "log_group": str(cfg.get("log_group", cfg.get("播报群", ""))).strip(),
            "exec_groups": [str(group).strip() for group in exec_groups if str(group).strip()],
        }

    def _groups_config_text_from_list(self, groups: list) -> str:
        mapping = {}
        for group in self._groups_from_list(groups):
            name = group.get("name", "")
            if not name:
                continue
            mapping[name] = {
                "log_group": group.get("log_group", ""),
                "exec_groups": group.get("exec_groups", []),
            }
        return self._groups_config_text_from_mapping(mapping)

    def _groups_config_text_from_mapping(self, groups: dict) -> str:
        mapping = {}
        for group in self._groups_from_mapping(groups):
            name = group.get("name", "")
            if not name:
                continue
            mapping[name] = {
                "log_group": group.get("log_group", ""),
                "exec_groups": group.get("exec_groups", []),
            }
        return json.dumps(mapping, ensure_ascii=False, indent=2)

    def _get_blacklist(self) -> list:
        blacklist = self.config.get("blacklist", [])
        if not isinstance(blacklist, list):
            return []
        return blacklist

    def _save_blacklist(self, blacklist: list):
        self.config["blacklist"] = blacklist
        self.config.save_config()

    @staticmethod
    def _pure_gid(group_id: str) -> str:
        return group_id.split(':')[-1] if ':' in group_id else group_id

    @staticmethod
    def _parse_duration(raw: str) -> int:
        """解析时长字符串，返回分钟数。支持 1m/1h/1d 或纯数字"""
        raw = raw.strip().lower()
        if raw.endswith('m'):
            return int(raw[:-1])
        if raw.endswith('h'):
            return int(raw[:-1]) * 60
        if raw.endswith('d'):
            return int(raw[:-1]) * 1440
        return int(raw)

    def init_database(self):
        """初始化 SQLite 数据库"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                target_qq TEXT NOT NULL,
                operator_qq TEXT NOT NULL,
                reason TEXT,
                duration INTEGER,
                timestamp TEXT NOT NULL,
                status TEXT DEFAULT 'active'
            )
        ''')
        conn.commit()
        conn.close()

    def is_super_admin(self, qq: str) -> bool:
        """判断是否为超管"""
        # 归一化为 str，避免配置被手改成数字型时静默失效
        super_admins = [str(x) for x in self.config.get("super_admins", [])]
        return str(qq) in super_admins

    def is_group_admin(self, event: AstrMessageEvent) -> bool:
        """判断是否为群管理员"""
        try:
            role = event.message_obj.sender.role
            return role in ['admin', 'owner']
        except Exception as exc:
            logger.debug(f"判定群管理员失败: {exc}")
            return False

    def check_permission(self, event: AstrMessageEvent, require_super: bool = False) -> bool:
        """检查权限"""
        sender_qq = str(event.message_obj.sender.user_id)

        if self.is_super_admin(sender_qq):
            return True

        if not require_super and self.is_group_admin(event):
            return True

        return False

    def _parse_target_and_args(self, event: AstrMessageEvent) -> tuple:
        """解析处罚指令的目标 QQ 与剩余参数，兼容 @提及 与纯 QQ 号两种写法。

        AstrBot 的 event.message_str 只含纯文本、@提及会被剥离，因此两种写法下
        剩余参数落在不同的 token 位置：
        - @提及：message_str 已不含目标 token，命令名之后的全部 token 都是剩余参数。
        - 纯 QQ 号：目标占据命令名之后的第一个 token，剩余参数从其后开始。

        统一在此归一，返回 (target_qq 或 None, remaining_args: List[str])，
        remaining_args 不含命令名与目标 token。
        """
        tokens = event.message_str.strip().split()
        body = tokens[1:] if tokens else []  # tokens[0] 为命令名（如 /mute）

        # 优先从消息链的 At 组件取目标：
        # - 跳过机器人自身的 @（@提及唤醒场景下 @bot 会先于目标出现，否则会误处罚机器人）
        # - 仅接受纯数字 QQ，排除 @全体成员（At.qq == "all"）等非数字目标，避免后续 int() 崩溃
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = ""
        for msg in event.get_messages():
            if isinstance(msg, At):
                at_qq = str(msg.qq)
                if self_id and at_qq == self_id:
                    continue
                if at_qq.isdecimal():
                    return at_qq, body

        # 无有效 @提及：把命令名后的第一个 token 当作纯 QQ 号
        # 用 isdecimal 而非 isdigit，确保后续 int(target_qq) 不会崩溃
        if body and body[0].isdecimal():
            return body[0], body[1:]

        return None, body

    async def get_group_network_async(self, group_id: str) -> Optional[tuple]:
        """获取群所属的网络组，返回 (组名, 组配置dict)
        支持完整格式和纯群号匹配"""
        groups = self._get_groups()
        pure_gid = self._pure_gid(group_id)
        for g in groups:
            if not isinstance(g, dict):
                continue
            for exec_gid in g.get("exec_groups", []):
                if self._pure_gid(str(exec_gid)) == pure_gid:
                    return g.get("name", ""), g
        return None

    def _exec_group_ids(self, net_config: dict) -> List[str]:
        """返回联动组内去重后的纯群号列表。"""
        raw_groups = net_config.get("exec_groups", [])
        if not isinstance(raw_groups, list):
            return []

        result: List[str] = []
        seen = set()
        for item in raw_groups:
            pure_gid = self._pure_gid(str(item).strip())
            if not pure_gid or not pure_gid.isdigit():
                logger.warning(f"忽略非法执行群配置: {item}")
                continue
            if pure_gid in seen:
                continue
            result.append(pure_gid)
            seen.add(pure_gid)
        return result

    async def _call_onebot_action(
        self,
        event: Optional[AstrMessageEvent],
        action: str,
        **payload: Any,
    ) -> Any:
        """按 OneBot/SnowLuma action 名调用 API，优先使用当前事件的 bot.api。"""
        try:
            bot = getattr(event, "bot", None) if event is not None else None
            api = getattr(bot, "api", None) if bot is not None else None
            if api is not None and hasattr(api, "call_action"):
                logger.info(f"调用 OneBot API: {action} {payload}")
                return await api.call_action(action, **payload)

            return await self._call_onebot_direct(action, **payload)
        except Exception as exc:
            if self._should_ignore_send_timeout(action, exc):
                logger.warning(f"忽略 OneBot 发送超时: action={action}, payload={payload}, error={exc}")
                return {"status": "ok", "retcode": 0, "message": "ignored send timeout"}
            raise

    async def _call_onebot_direct(self, action: str, **payload: Any) -> Any:
        """没有事件对象时，通过平台适配器客户端调用 OneBot action。"""
        last_error = ""
        for platform in self.context.platform_manager.get_insts():
            if not hasattr(platform, "get_client"):
                continue

            client = platform.get_client()
            if client is None:
                continue

            method = getattr(client, action, None)
            if callable(method):
                try:
                    logger.info(f"通过客户端方法调用 OneBot API: {action} {payload}")
                    return await method(**payload)
                except Exception as exc:
                    last_error = str(exc)
                    logger.debug(f"客户端方法调用失败: {action}, error={exc}")
                    continue

            call_api = getattr(client, "call_api", None)
            if callable(call_api):
                try:
                    logger.info(f"通过 call_api 调用 OneBot API: {action} {payload}")
                    return await call_api(action, **payload)
                except Exception as exc:
                    last_error = str(exc)
                    logger.debug(f"call_api 调用失败: {action}, error={exc}")
                    continue

            api = getattr(client, "api", None)
            call_action = getattr(api, "call_action", None) if api is not None else None
            if callable(call_action):
                try:
                    logger.info(f"通过 client.api 调用 OneBot API: {action} {payload}")
                    return await call_action(action, **payload)
                except Exception as exc:
                    last_error = str(exc)
                    logger.debug(f"client.api 调用失败: {action}, error={exc}")
                    continue

        raise RuntimeError(last_error or f"未找到可用 OneBot API 客户端: {action}")

    async def _call_group_action(
        self,
        event: Optional[AstrMessageEvent],
        action: str,
        group_id: str,
        **payload: Any,
    ) -> Any:
        pure_gid = self._pure_gid(str(group_id))
        if not pure_gid.isdigit():
            raise ValueError(f"群号格式不正确: {group_id}")
        return await self._call_onebot_action(event, action, group_id=int(pure_gid), **payload)

    async def _run_group_action(
        self,
        event: Optional[AstrMessageEvent],
        action: str,
        group_ids: List[str],
        **payload: Any,
    ) -> tuple:
        success: List[str] = []
        failed: Dict[str, str] = {}
        for group_id in group_ids:
            try:
                await self._call_group_action(event, action, group_id, **payload)
                success.append(group_id)
            except Exception as exc:
                failed[group_id] = str(exc)
                logger.warning(f"群 {group_id} 执行 {action} 失败: {exc}")
        return success, failed

    def _format_group_result(self, success: List[str], failed: Dict[str, str]) -> str:
        lines = []
        if success:
            lines.append(f"成功群: {', '.join(success)}")
        if failed:
            # 只回显失败群号，不带原始异常文本，避免泄露后端错误与兄弟群细节
            lines.append("失败群: " + ", ".join(failed.keys()))
        return "\n".join(lines)

    def _should_ignore_send_timeout(self, action: str, exc: Exception) -> bool:
        if not self.config.get("ignore_send_timeout_1200", True):
            return False
        if action not in {"send_group_msg", "send_msg", "_send_group_notice"}:
            return False
        if getattr(exc, "retcode", None) != 1200:
            return False
        message = str(exc)
        return "Timeout" in message and "sendMsg" in message

    def add_record(self, action_type: str, target_qq: str, operator_qq: str,
                   reason: str, duration: Optional[int] = None) -> int:
        """添加处罚记录到数据库，返回记录ID"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute('''
            INSERT INTO records (action_type, target_qq, operator_qq, reason, duration, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (action_type, target_qq, operator_qq, reason, duration, timestamp))

        record_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return record_id

    async def broadcast_to_log_group(self, net_name: str, message: str):
        """向播报群发送消息"""
        groups = self._get_groups()
        net_config = None
        for g in groups:
            if g.get("name") == net_name:
                net_config = g
                break
        if not net_config:
            return

        log_group = net_config.get("log_group", "")
        if not log_group:
            return

        try:
            await self._call_group_action(None, "send_group_msg", log_group, message=message)
            logger.info(f"播报消息发送成功到群 {log_group}")
        except Exception as e:
            logger.error(f"播报消息失败: {e}", exc_info=True)

    @filter.command("mute")
    async def cmd_mute(self, event: AstrMessageEvent):
        """禁言指令: /mute <目标> <时长> <原因>"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        # 解析参数（兼容 @提及 与纯 QQ 号）
        target_qq, rest = self._parse_target_and_args(event)
        if not target_qq:
            yield event.plain_result("❌ 无法识别目标用户，请@用户或输入QQ号")
            return

        if len(rest) < 2:
            yield event.plain_result("❌ 参数不足\n用法: /mute <目标> <时长> <原因>\n时长格式: 30m(分钟) / 2h(小时) / 1d(天) / 纯数字(分钟)")
            return

        try:
            duration = self._parse_duration(rest[0])
        except ValueError:
            yield event.plain_result("❌ 时长格式错误，支持: 30m(分钟) / 2h(小时) / 1d(天) / 纯数字(分钟)")
            return

        reason = " ".join(rest[1:])
        operator_qq = str(event.message_obj.sender.user_id)
        group_id = str(event.unified_msg_origin)

        # 获取当前平台信息
        current_platform = event.get_platform_name()
        logger.info(f"当前平台: {current_platform}, 群ID: {group_id}")

        # 获取群网络
        network_info = await self.get_group_network_async(group_id)
        if not network_info:
            yield event.plain_result("❌ 当前群未加入任何联动组")
            return

        net_name, net_config = network_info
        exec_groups = self._exec_group_ids(net_config)
        if not exec_groups:
            yield event.plain_result("❌ 当前联动组未配置执行群")
            return

        success_groups, failed_groups = await self._run_group_action(
            event,
            "set_group_ban",
            exec_groups,
            user_id=int(target_qq),
            duration=duration * 60,
        )
        if not success_groups:
            yield event.plain_result(f"❌ 禁言失败\n{self._format_group_result(success_groups, failed_groups)}")
            return

        # 记录到数据库
        record_id = self.add_record("mute", target_qq, operator_qq, reason, duration)

        # 播报
        if duration >= 1440 and duration % 1440 == 0:
            time_str = f"{duration // 1440}天"
        elif duration >= 60 and duration % 60 == 0:
            time_str = f"{duration // 60}小时"
        else:
            time_str = f"{duration}分钟"
        broadcast_msg = (
            f"【禁言通知】\n记录ID: {record_id}\n目标: {target_qq}\n时长: {time_str}\n"
            f"原因: {reason}\n操作者: {operator_qq}\n执行群: {', '.join(success_groups)}"
        )
        if failed_groups:
            broadcast_msg += f"\n失败群: {', '.join(failed_groups.keys())}"
        await self.broadcast_to_log_group(net_name, broadcast_msg)

        yield event.plain_result(f"✅ 禁言完成\n记录ID: {record_id}\n{self._format_group_result(success_groups, failed_groups)}")

    @filter.command("kick")
    async def cmd_kick(self, event: AstrMessageEvent):
        """踢人指令: /kick <目标> <原因> [-b]"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        target_qq, rest = self._parse_target_and_args(event)
        if not target_qq:
            yield event.plain_result("❌ 无法识别目标用户，请@用户或输入QQ号")
            return

        # 剔除 -b 标志后剩余的即为原因，避免 -b 被并入原因文本
        add_blacklist = "-b" in rest
        reason_tokens = [tok for tok in rest if tok != "-b"]
        if not reason_tokens:
            yield event.plain_result("❌ 参数不足\n用法: /kick <目标> <原因> [-b]")
            return

        reason = " ".join(reason_tokens)
        operator_qq = str(event.message_obj.sender.user_id)
        group_id = str(event.unified_msg_origin)

        network_info = await self.get_group_network_async(group_id)
        if not network_info:
            yield event.plain_result("❌ 当前群未加入任何联动组")
            return

        net_name, net_config = network_info
        exec_groups = self._exec_group_ids(net_config)
        if not exec_groups:
            yield event.plain_result("❌ 当前联动组未配置执行群")
            return

        success_groups, failed_groups = await self._run_group_action(
            event,
            "set_group_kick",
            exec_groups,
            user_id=int(target_qq),
            reject_add_request=add_blacklist,
        )
        if not success_groups:
            yield event.plain_result(f"❌ 踢出失败\n{self._format_group_result(success_groups, failed_groups)}")
            return

        # 添加黑名单
        if add_blacklist:
            async with self._blacklist_lock:
                blacklist = self._get_blacklist()
                existing = next((e for e in blacklist if str(e.get("qq")) == target_qq), None)
                if existing:
                    existing["reason"] = reason
                    existing["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    existing["operator"] = operator_qq
                else:
                    blacklist.append({
                        "qq": target_qq,
                        "reason": reason,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "operator": operator_qq
                    })
                self._save_blacklist(blacklist)

        # 记录到数据库
        record_id = self.add_record("kick", target_qq, operator_qq, reason)

        # 播报
        blacklist_text = "✅ 已加入黑名单" if add_blacklist else ""
        broadcast_msg = (
            f"【踢出通知】\n记录ID: {record_id}\n目标: {target_qq}\n原因: {reason}\n"
            f"操作者: {operator_qq}\n执行群: {', '.join(success_groups)}\n{blacklist_text}"
        )
        if failed_groups:
            broadcast_msg += f"\n失败群: {', '.join(failed_groups.keys())}"
        await self.broadcast_to_log_group(net_name, broadcast_msg)

        result_text = f"✅ 踢出完成\n记录ID: {record_id}\n{self._format_group_result(success_groups, failed_groups)}"
        if add_blacklist:
            result_text += "\n✅ 已加入黑名单"
        yield event.plain_result(result_text)

    @filter.command("warn")
    async def cmd_warn(self, event: AstrMessageEvent):
        """警告指令: /warn <目标> <原因>"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        target_qq, rest = self._parse_target_and_args(event)
        if not target_qq:
            yield event.plain_result("❌ 无法识别目标用户，请@用户或输入QQ号")
            return

        if not rest:
            yield event.plain_result("❌ 参数不足\n用法: /warn <目标> <原因>")
            return

        reason = " ".join(rest)
        operator_qq = str(event.message_obj.sender.user_id)
        group_id = str(event.unified_msg_origin)

        network_info = await self.get_group_network_async(group_id)
        if not network_info:
            yield event.plain_result("❌ 当前群未加入任何联动组")
            return

        net_name, net_config = network_info
        exec_groups = self._exec_group_ids(net_config)
        if not exec_groups:
            yield event.plain_result("❌ 当前联动组未配置执行群")
            return

        # 在联动组执行群发送警告
        warn_msg = f"⚠️ 警告\n用户: {target_qq}\n原因: {reason}\n操作者: {operator_qq}"

        success_groups, failed_groups = await self._run_group_action(
            event,
            "send_group_msg",
            exec_groups,
            message=warn_msg,
        )
        if not success_groups:
            yield event.plain_result(f"❌ 发送警告失败\n{self._format_group_result(success_groups, failed_groups)}")
            return

        # 记录到数据库
        record_id = self.add_record("warn", target_qq, operator_qq, reason)

        # 播报
        broadcast_msg = (
            f"【警告通知】\n记录ID: {record_id}\n目标: {target_qq}\n原因: {reason}\n"
            f"操作者: {operator_qq}\n执行群: {', '.join(success_groups)}"
        )
        if failed_groups:
            broadcast_msg += f"\n失败群: {', '.join(failed_groups.keys())}"
        await self.broadcast_to_log_group(net_name, broadcast_msg)

        yield event.plain_result(f"✅ 警告已发送\n记录ID: {record_id}\n{self._format_group_result(success_groups, failed_groups)}")

    @filter.command("record")
    async def cmd_record(self, event: AstrMessageEvent):
        """查询违规历史: /record <目标>"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        target_qq, _ = self._parse_target_and_args(event)
        if not target_qq:
            yield event.plain_result("❌ 无法识别目标用户，请@用户或输入QQ号")
            return

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        # 统计汇总
        cursor.execute('''
            SELECT action_type, COUNT(*), SUM(duration)
            FROM records
            WHERE target_qq = ? AND status = 'active'
            GROUP BY action_type
        ''', (target_qq,))
        summary = cursor.fetchall()

        # 最近3条记录
        cursor.execute('''
            SELECT id, action_type, reason, duration, timestamp, operator_qq
            FROM records
            WHERE target_qq = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT 3
        ''', (target_qq,))
        recent = cursor.fetchall()

        conn.close()

        # 构建回复
        result = f"📊 违规历史 - {target_qq}\n\n"

        if summary:
            result += "【汇总统计】\n"
            for action_type, count, total_duration in summary:
                if action_type == "mute":
                    result += f"• 禁言: {count}次，累计{total_duration or 0}分钟\n"
                elif action_type == "kick":
                    result += f"• 踢出: {count}次\n"
                elif action_type == "warn":
                    result += f"• 警告: {count}次\n"
        else:
            result += "【汇总统计】\n暂无记录\n"

        if recent:
            result += "\n【最近3条记录】\n"
            for record_id, action_type, reason, duration, timestamp, operator_qq in recent:
                action_name = {"mute": "禁言", "kick": "踢出", "warn": "警告"}.get(action_type, action_type)
                duration_text = f"{duration}分钟" if duration else ""
                result += f"• ID{record_id} [{timestamp}]\n  {action_name} {duration_text} - {reason}\n  操作者: {operator_qq}\n"

        yield event.plain_result(result)

    @filter.command("undo")
    async def cmd_undo(self, event: AstrMessageEvent):
        """撤销处罚: /undo <记录ID> <原因>"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("❌ 参数不足\n用法: /undo <记录ID> <原因>")
            return

        try:
            record_id = int(parts[1])
        except ValueError:
            yield event.plain_result("❌ 记录ID必须为整数")
            return

        undo_reason = parts[2]
        operator_qq = str(event.message_obj.sender.user_id)
        group_id = str(event.unified_msg_origin)

        network_info = await self.get_group_network_async(group_id)
        if not network_info:
            yield event.plain_result("❌ 当前群未加入任何联动组")
            return

        net_name, net_config = network_info
        exec_groups = self._exec_group_ids(net_config)
        if not exec_groups:
            yield event.plain_result("❌ 当前联动组未配置执行群")
            return

        # 查询记录
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute('''
            SELECT action_type, target_qq, reason, status
            FROM records WHERE id = ?
        ''', (record_id,))
        record = cursor.fetchone()

        if not record:
            conn.close()
            yield event.plain_result(f"❌ 未找到记录ID: {record_id}")
            return

        action_type, target_qq, original_reason, status = record

        if status == 'revoked':
            conn.close()
            yield event.plain_result(f"❌ 记录ID {record_id} 已被撤销")
            return

        conn.close()

        # 执行撤销操作
        success_groups: List[str] = []
        failed_groups: Dict[str, str] = {}
        if action_type == "mute":
            success_groups, failed_groups = await self._run_group_action(
                event,
                "set_group_ban",
                exec_groups,
                user_id=int(target_qq),
                duration=0,
            )
            if not success_groups:
                yield event.plain_result(f"❌ 撤销失败：解除禁言操作未成功\n{self._format_group_result(success_groups, failed_groups)}")
                return

        elif action_type == "kick":
            async with self._blacklist_lock:
                blacklist = self._get_blacklist()
                blacklist = [
                    entry for entry in blacklist
                    if str(entry.get("qq")) != str(target_qq)
                ]
                self._save_blacklist(blacklist)

        # 更新数据库状态
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute('UPDATE records SET status = ? WHERE id = ?', ('revoked', record_id))
        conn.commit()
        conn.close()

        # 播报
        broadcast_msg = (
            f"【撤销通知】\n记录ID: {record_id}\n类型: {action_type}\n目标: {target_qq}\n"
            f"原处罚原因: {original_reason}\n撤销原因: {undo_reason}\n操作者: {operator_qq}"
        )
        if action_type == "mute":
            broadcast_msg += f"\n解除禁言群: {', '.join(success_groups)}"
            if failed_groups:
                broadcast_msg += f"\n失败群: {', '.join(failed_groups.keys())}"
        await self.broadcast_to_log_group(net_name, broadcast_msg)

        result_text = f"✅ 撤销完成\n记录ID: {record_id}\n类型: {action_type}\n目标: {target_qq}"
        if action_type == "mute":
            result_text += f"\n{self._format_group_result(success_groups, failed_groups)}"
        yield event.plain_result(result_text)

    @filter.command("g_join")
    async def cmd_g_join(self, event: AstrMessageEvent):
        """加入联动组: /g_join <组名>"""
        sender_qq = str(event.message_obj.sender.user_id)
        if not self.is_super_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅超管可用")
            return

        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("❌ 参数不足\n用法: /g_join <组名>")
            return

        net_name = parts[1]
        group_id = str(event.unified_msg_origin)
        pure_gid = self._pure_gid(group_id)
        added = False
        already = False

        async with self._groups_lock:
            groups = self._get_groups()

            target = None
            for g in groups:
                if g.get("name") == net_name:
                    target = g
                    break
            if target is None:
                target = {"name": net_name, "log_group": "", "exec_groups": []}
                groups.append(target)

            exec_list = target.get("exec_groups", [])
            if any(self._pure_gid(str(g)) == pure_gid for g in exec_list):
                already = True
            else:
                exec_list.append(group_id)
                target["exec_groups"] = exec_list
                self._save_groups(groups)
                added = True

        if added:
            yield event.plain_result(f"✅ 当前群已加入联动组: {net_name}")
        elif already:
            yield event.plain_result(f"ℹ️ 当前群已在联动组: {net_name}")

    @filter.command("g_leave")
    async def cmd_g_leave(self, event: AstrMessageEvent):
        """离开联动组: /g_leave"""
        sender_qq = str(event.message_obj.sender.user_id)
        if not self.is_super_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅超管可用")
            return

        group_id = str(event.unified_msg_origin)
        pure_gid = self._pure_gid(group_id)
        found_name = None

        async with self._groups_lock:
            groups = self._get_groups()
            for g in groups:
                exec_list = g.get("exec_groups", [])
                for existing in list(exec_list):
                    if self._pure_gid(str(existing)) == pure_gid:
                        exec_list.remove(existing)
                        g["exec_groups"] = exec_list
                        self._save_groups(groups)
                        found_name = g.get("name", "")
                        break
                if found_name is not None:
                    break

        if found_name is not None:
            yield event.plain_result(f"✅ 当前群已离开联动组: {found_name}")
        else:
            yield event.plain_result("ℹ️ 当前群未加入任何联动组")

    @filter.command("g_log")
    async def cmd_g_log(self, event: AstrMessageEvent):
        """设置播报群: /g_log <组名>"""
        sender_qq = str(event.message_obj.sender.user_id)
        if not self.is_super_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅超管可用")
            return

        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("❌ 参数不足\n用法: /g_log <组名>")
            return

        net_name = parts[1]
        group_id = str(event.unified_msg_origin)

        async with self._groups_lock:
            groups = self._get_groups()
            target = None
            for g in groups:
                if g.get("name") == net_name:
                    target = g
                    break
            if target is None:
                target = {"name": net_name, "log_group": "", "exec_groups": []}
                groups.append(target)
            target["log_group"] = group_id
            self._save_groups(groups)

        yield event.plain_result(f"✅ 当前群已设为联动组 [{net_name}] 的播报群")

    @filter.command("g_list")
    async def cmd_g_list(self, event: AstrMessageEvent):
        """查看联动组配置: /g_list [组名]"""
        sender_qq = str(event.message_obj.sender.user_id)
        if not self.is_super_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅超管可用")
            return

        parts = event.message_str.strip().split(maxsplit=1)
        filter_name = parts[1] if len(parts) >= 2 else None

        async with self._groups_lock:
            groups = self._get_groups()

        if not groups:
            yield event.plain_result("📋 暂无联动组配置")
            return

        result = "📋 联动组配置\n\n"
        for g in groups:
            name = g.get("name", "")
            if filter_name and name != filter_name:
                continue
            log_group = g.get("log_group", "")
            log_pure = self._pure_gid(log_group) if log_group else ""
            exec_list = g.get("exec_groups", [])
            result += f"【{name}】\n"
            result += f"  播报群: {log_pure or '未设置'}\n"
            result += f"  执行群 ({len(exec_list)}个):\n"
            for gid in exec_list:
                result += f"    - {self._pure_gid(str(gid))}\n"
            result += "\n"

        yield event.plain_result(result.strip())

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_event(self, event: AstrMessageEvent):
        """
        处理所有事件（消息和通知）

        参考 github_star_verify 插件的实现方式
        """
        try:
            # 只处理 aiocqhttp 平台（OneBot v11）
            if event.get_platform_name() != "aiocqhttp":
                return

            # 获取原始事件数据
            raw = event.message_obj.raw_message
            post_type = raw.get("post_type")

            # 只处理通知类型事件
            if post_type != "notice":
                return

            notice_type = raw.get("notice_type")

            # 只处理群成员增加事件
            if notice_type != "group_increase":
                return

            # 提取信息
            user_id = raw.get("user_id")
            group_id = raw.get("group_id")

            if not user_id or not group_id:
                logger.warning("[黑名单拦截] 群成员增加事件缺少必要字段")
                return

            logger.info(f"[黑名单拦截] 检测到群成员增加: user_id={user_id}, group_id={group_id}")

            # 检查黑名单
            await self.check_and_kick_blacklist(str(user_id), str(group_id), event)

        except Exception as e:
            logger.error(f"[黑名单拦截] 处理事件失败: {e}", exc_info=True)

    async def check_and_kick_blacklist(
        self,
        new_member_qq: str,
        group_id: str,
        event: Optional[AstrMessageEvent] = None,
    ):
        """
        检查新成员是否在黑名单中，如果在则踢出并拉黑

        参数：
            new_member_qq: 新加入成员的QQ号
            group_id: 群号
        """
        try:
            # 检查黑名单
            blacklist = self._get_blacklist()

            blacklist_entry = None
            for entry in blacklist:
                if str(entry.get("qq")) == new_member_qq:
                    blacklist_entry = entry
                    break

            if not blacklist_entry:
                return

            logger.warning(f"发现黑名单用户 {new_member_qq} 加入群 {group_id}")

            # 踢出并拉黑（无论群是否加入联动组，命中黑名单一律踢出）
            pure_gid = group_id.split(':')[-1] if ':' in group_id else group_id
            kicked = False

            try:
                await self._call_group_action(
                    event,
                    "set_group_kick",
                    pure_gid,
                    user_id=int(new_member_qq),
                    reject_add_request=True,  # 拒绝再次加群（拉黑）
                )
                kicked = True
                logger.info(f"成功踢出并拉黑黑名单用户 {new_member_qq} (群 {group_id})")
            except Exception as e:
                logger.warning(f"平台踢出黑名单用户失败: {e}")

            # 获取群网络信息（用于播报），找不到则跳过播报但不影响踢人
            network_info = await self.get_group_network_async(group_id)
            if not network_info:
                logger.info(f"群 {group_id} 未加入任何联动组，已执行踢出但跳过播报")
                return

            net_name, net_config = network_info

            if kicked:
                # 播报拦截信息
                broadcast_msg = (
                    f"🚫 【黑名单自动拦截】\n"
                    f"目标QQ: {new_member_qq}\n"
                    f"拦截群: {pure_gid}\n"
                    f"原处罚原因: {blacklist_entry.get('reason', '未知')}\n"
                    f"加入黑名单时间: {blacklist_entry.get('time', '未知')}\n"
                    f"原操作者: {blacklist_entry.get('operator', '未知')}\n"
                    f"已执行: 踢出 + 拉黑（禁止再次加群）"
                )
                await self.broadcast_to_log_group(net_name, broadcast_msg)
                logger.info(f"黑名单拦截成功: {new_member_qq} 已从群 {group_id} 移除并拉黑")
            else:
                logger.error(f"黑名单拦截失败: 无法踢出用户 {new_member_qq} (群 {group_id})")
                # 播报失败信息
                broadcast_msg = (
                    f"⚠️ 【黑名单拦截失败】\n"
                    f"目标QQ: {new_member_qq}\n"
                    f"群: {pure_gid}\n"
                    f"原因: {blacklist_entry.get('reason', '未知')}\n"
                    f"状态: 检测到黑名单用户加群，但踢出失败（可能权限不足）"
                )
                await self.broadcast_to_log_group(net_name, broadcast_msg)

        except Exception as e:
            logger.error(f"检查黑名单并踢出失败: {e}", exc_info=True)

    @filter.command("blacklist")
    async def cmd_blacklist(self, event: AstrMessageEvent):
        """查看黑名单: /blacklist [页码]"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        parts = event.message_str.strip().split()
        page = 1
        if len(parts) >= 2 and parts[1].isdigit():
            page = int(parts[1])

        blacklist = self._get_blacklist()

        if not blacklist:
            yield event.plain_result("📋 黑名单为空")
            return

        # 分页显示
        page_size = 10
        total_pages = (len(blacklist) + page_size - 1) // page_size
        page = max(1, min(page, total_pages))

        start = (page - 1) * page_size
        end = start + page_size
        page_data = blacklist[start:end]

        result = f"📋 黑名单 (第 {page}/{total_pages} 页，共 {len(blacklist)} 人)\n\n"
        for idx, entry in enumerate(page_data, start=start+1):
            result += (
                f"{idx}. QQ: {entry.get('qq')}\n"
                f"   原因: {entry.get('reason', '未知')}\n"
                f"   时间: {entry.get('time', '未知')}\n"
                f"   操作者: {entry.get('operator', '未知')}\n"
            )

        if total_pages > 1:
            result += f"\n使用 /blacklist {page+1} 查看下一页"

        yield event.plain_result(result)

    @filter.command("unblacklist")
    async def cmd_unblacklist(self, event: AstrMessageEvent):
        """从黑名单移除: /unblacklist <QQ号>"""
        if not self.check_permission(event, require_super=True):
            yield event.plain_result("❌ 权限不足，仅超管可用")
            return

        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("❌ 参数不足\n用法: /unblacklist <QQ号>")
            return

        target_qq = parts[1]
        if not target_qq.isdigit():
            yield event.plain_result("❌ QQ号必须为数字")
            return

        async with self._blacklist_lock:
            blacklist = self._get_blacklist()
            original_count = len(blacklist)
            blacklist = [entry for entry in blacklist if str(entry.get("qq")) != str(target_qq)]
            item_removed = len(blacklist) < original_count
            if item_removed:
                self._save_blacklist(blacklist)

        if not item_removed:
            yield event.plain_result(f"❌ QQ {target_qq} 不在黑名单中")
            return

        operator_qq = str(event.message_obj.sender.user_id)
        group_id = str(event.unified_msg_origin)

        # 播报
        network_info = await self.get_group_network_async(group_id)
        if network_info:
            net_name, _ = network_info
            broadcast_msg = (
                f"✅ 【黑名单移除】\n"
                f"目标QQ: {target_qq}\n"
                f"操作者: {operator_qq}\n"
                f"备注: 该用户已从黑名单移除，可正常加群"
            )
            await self.broadcast_to_log_group(net_name, broadcast_msg)

        yield event.plain_result(f"✅ 已将 QQ {target_qq} 从黑名单移除")

    @filter.command("gminfo")
    async def cmd_gminfo(self, event: AstrMessageEvent):
        """全局违规记录汇总: /gminfo [页码]"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        parts = event.message_str.strip().split()
        page = 1
        # 用 isdecimal 而非 isdigit，避免上标数字等 isdigit=True 但 int() 无法解析的字符导致崩溃
        if len(parts) >= 2 and parts[1].isdecimal():
            page = int(parts[1])

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM records")
        total_count = cursor.fetchone()[0]

        if total_count == 0:
            conn.close()
            yield event.plain_result("📋 暂无违规记录")
            return

        cursor.execute("SELECT COUNT(*) FROM records WHERE status = 'active'")
        active_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM records WHERE status = 'revoked'")
        revoked_count = cursor.fetchone()[0]

        cursor.execute('''
            SELECT action_type, COUNT(*), SUM(duration)
            FROM records
            GROUP BY action_type
        ''')
        type_summary = cursor.fetchall()

        # 明细行分页，避免全表渲染导致图片过大 / 超时 / OOM
        page_size = 100
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * page_size

        cursor.execute('''
            SELECT id, action_type, target_qq, operator_qq, reason, duration, timestamp, status
            FROM records
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        ''', (page_size, offset))
        all_records = cursor.fetchall()
        conn.close()

        action_map = {"mute": "禁言", "kick": "踢出", "warn": "警告"}
        status_map = {"active": "生效中", "revoked": "已撤销"}

        type_rows = ""
        for action_type, count, total_duration in type_summary:
            label = html.escape(action_map.get(action_type, action_type or ""))
            dur_text = f"，累计{total_duration or 0}分钟" if action_type == "mute" else ""
            type_rows += f'<tr><td>{label}</td><td>{count} 次{dur_text}</td></tr>'

        record_rows = ""
        for record_id, action_type, target_qq, operator_qq, reason, duration, timestamp, status in all_records:
            status_style = 'color:#e74c3c;font-weight:bold' if status == 'revoked' else 'color:#27ae60'
            # 所有来自 DB 的用户可控字段都需转义，防止存储型 HTML 注入
            label = html.escape(action_map.get(action_type, action_type or ""))
            dur_text = f" {duration}分钟" if duration else ""
            reason_text = html.escape(reason) if reason else "-"
            status_text = html.escape(status_map.get(status, status or ""))
            record_rows += (
                f'<tr>'
                f'<td>{record_id}</td>'
                f'<td>{label}{dur_text}</td>'
                f'<td>{html.escape(str(target_qq))}</td>'
                f'<td>{html.escape(str(operator_qq))}</td>'
                f'<td>{reason_text}</td>'
                f'<td>{html.escape(str(timestamp))}</td>'
                f'<td style="{status_style}">{status_text}</td>'
                f'</tr>'
            )

        html_template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#f0f2f5;padding:30px;color:#333}
.container{max-width:1100px;margin:0 auto}
.header{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:30px 40px;border-radius:12px 12px 0 0}
.header h1{font-size:26px;margin-bottom:8px}
.header p{font-size:14px;opacity:.85}
.stats{display:flex;gap:16px;padding:24px 40px;background:#fff;border-bottom:1px solid #e8e8e8}
.stat-card{flex:1;text-align:center;padding:16px;background:#f7f8fc;border-radius:8px}
.stat-card .num{font-size:28px;font-weight:700;color:#667eea}
.stat-card .label{font-size:13px;color:#888;margin-top:4px}
.stat-card.active .num{color:#27ae60}
.stat-card.revoked .num{color:#e74c3c}
.type-table-wrap{padding:0 40px 24px;background:#fff}
.type-table-wrap h3{font-size:16px;padding:16px 0 8px;color:#555}
.type-table{width:100%;border-collapse:collapse}
.type-table td{padding:8px 12px;border-bottom:1px solid #f0f0f0;font-size:14px}
.type-table td:first-child{font-weight:600;width:80px}
.record-table-wrap{padding:0 40px 30px;background:#fff;border-radius:0 0 12px 12px}
.record-table-wrap h3{font-size:16px;padding:0 0 12px;color:#555}
.record-table{width:100%;border-collapse:collapse;font-size:13px}
.record-table th{background:#f7f8fc;padding:10px 8px;text-align:left;border-bottom:2px solid #e8e8e8;font-weight:600;white-space:nowrap}
.record-table td{padding:8px;border-bottom:1px solid #f0f0f0;word-break:break-all}
.record-table tr:hover{background:#fafbff}
.footer{text-align:center;padding:20px;color:#aaa;font-size:12px}
</style>
</head>
<body>
<div class="container">
<div class="header"><h1>📋 违规管理记录汇总</h1><p>生成时间：{{ gen_time }}</p></div>
<div class="stats">
<div class="stat-card"><div class="num">{{ total }}</div><div class="label">总记录数</div></div>
<div class="stat-card active"><div class="num">{{ active }}</div><div class="label">生效中</div></div>
<div class="stat-card revoked"><div class="num">{{ revoked }}</div><div class="label">已撤销</div></div>
</div>
<div class="type-table-wrap"><h3>分类统计</h3>
<table class="type-table">{{ type_rows }}</table></div>
<div class="record-table-wrap"><h3>{{ record_title }}</h3>
<table class="record-table">
<thead><tr><th>ID</th><th>类型</th><th>目标QQ</th><th>操作者</th><th>原因</th><th>时间</th><th>状态</th></tr></thead>
<tbody>{{ record_rows }}</tbody></table></div>
<div class="footer">QManagementMaster - 多群联动违规管理系统</div>
</div>
</body>
</html>"""

        record_title = f"全部记录（第 {page}/{total_pages} 页，每页 {page_size} 条）"

        url = await self.html_render(html_template, {
            "gen_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": str(total_count),
            "active": str(active_count),
            "revoked": str(revoked_count),
            "type_rows": type_rows,
            "record_rows": record_rows,
            "record_title": record_title,
        }, return_url=True)

        yield event.image_result(url)
        if total_pages > 1 and page < total_pages:
            yield event.plain_result(f"共 {total_pages} 页，使用 /gminfo {page + 1} 查看下一页")

    async def terminate(self):
        """插件卸载时的清理"""
        logger.info("GroupManager 插件已卸载")




