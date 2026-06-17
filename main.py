import json
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, At
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


@register("QManagementMaster", "YourName", "QQ多群联动违规管理插件", "1.0.0")
class GroupManagerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # 使用 AstrBot 配置系统
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "QManagementMaster"
        self.db_path = self.data_dir / "records.db"

    async def initialize(self):
        """插件初始化：创建目录和数据库"""
        # 创建数据目录
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 初始化数据库
        self.init_database()

        # 初始化 groups 配置（使用 KV 存储）
        groups = await self.get_kv_data("groups", {})
        if not groups:
            await self.put_kv_data("groups", {})
            logger.info("已初始化空的群网络配置")

        logger.info("GroupManager 插件初始化完成")

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
        return qq in self.config.get("super_admins", [])

    def is_group_admin(self, event: AstrMessageEvent) -> bool:
        """判断是否为群管理员"""
        try:
            role = event.message_obj.sender.role
            return role in ['admin', 'owner']
        except:
            return False

    def check_permission(self, event: AstrMessageEvent, require_super: bool = False) -> bool:
        """检查权限"""
        sender_qq = str(event.message_obj.sender.user_id)

        if self.is_super_admin(sender_qq):
            return True

        if not require_super and self.is_group_admin(event):
            return True

        return False

    def extract_target_qq(self, event: AstrMessageEvent) -> Optional[str]:
        """从消息链中提取目标QQ号"""
        message_chain = event.get_messages()

        for msg in message_chain:
            if isinstance(msg, At):
                return str(msg.qq)

        # 如果没有@，尝试从文本中提取QQ号
        parts = event.message_str.strip().split()
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]

        return None

    def get_group_network(self, group_id: str) -> Optional[tuple]:
        """获取群所属的网络组，返回 (组名, 配置)"""
        # 同步获取 KV 数据需要在异步上下文中调用
        # 这里我们使用一个辅助方法
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果在异步上下文中，创建一个 task
                return None  # 临时返回，需要改为异步方法
            else:
                groups = loop.run_until_complete(self.get_kv_data("groups", {}))
        except:
            return None

        if not isinstance(groups, dict):
            logger.error(f"groups 配置格式错误: {type(groups)}")
            return None

        for net_name, net_config in groups.items():
            if not isinstance(net_config, dict):
                logger.error(f"网络配置 {net_name} 格式错误: {type(net_config)}")
                continue

            exec_list = net_config.get("执行群列表", [])
            if not isinstance(exec_list, list):
                logger.error(f"执行群列表格式错误: {type(exec_list)}")
                continue

            if group_id in exec_list:
                return net_name, net_config
        return None

    async def get_group_network_async(self, group_id: str) -> Optional[tuple]:
        """异步获取群所属的网络组，返回 (组名, 配置)"""
        groups = await self.get_kv_data("groups", {})

        if not isinstance(groups, dict):
            logger.error(f"groups 配置格式错误: {type(groups)}")
            return None

        for net_name, net_config in groups.items():
            if not isinstance(net_config, dict):
                logger.error(f"网络配置 {net_name} 格式错误: {type(net_config)}")
                continue

            exec_list = net_config.get("执行群列表", [])
            if not isinstance(exec_list, list):
                logger.error(f"执行群列表格式错误: {type(exec_list)}")
                continue

            if group_id in exec_list:
                return net_name, net_config
        return None

    def add_record(self, action_type: str, target_qq: str, operator_qq: str,
                   reason: str, duration: int = None) -> int:
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
        groups = await self.get_kv_data("groups", {})
        net_config = groups.get(net_name)
        if not net_config or not isinstance(net_config, dict):
            return

        log_group = net_config.get("播报群")
        if not log_group:
            return

        try:
            # 使用平台适配器直接发送消息
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, 'send_group_msg'):
                        try:
                            # 提取纯群号
                            pure_gid = log_group.split(':')[-1] if ':' in log_group else log_group
                            await client.send_group_msg(group_id=int(pure_gid), message=message)
                            logger.info(f"播报消息发送成功到群 {log_group}")
                            break
                        except Exception as e:
                            logger.debug(f"平台发送播报失败: {e}")
                            continue
        except Exception as e:
            logger.error(f"播报消息失败: {e}", exc_info=True)

    @filter.command("mute")
    async def cmd_mute(self, event: AstrMessageEvent):
        """禁言指令: /mute <目标> <分钟数> <原因>"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        # 解析参数
        parts = event.message_str.strip().split(maxsplit=3)
        if len(parts) < 4:
            yield event.plain_result("❌ 参数不足\n用法: /mute <目标> <分钟数> <原因>")
            return

        target_qq = self.extract_target_qq(event)
        if not target_qq:
            yield event.plain_result("❌ 无法识别目标用户，请@用户或输入QQ号")
            return

        try:
            duration = int(parts[2])
        except ValueError:
            yield event.plain_result("❌ 时长必须为整数")
            return

        reason = parts[3]
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
        exec_groups = net_config.get("执行群列表", [])

        # 记录到数据库
        record_id = self.add_record("mute", target_qq, operator_qq, reason, duration)

        # 执行禁言 - 使用 AstrBot 的统一 API
        success_count = 0
        for gid in exec_groups:
            try:
                logger.info(f"尝试在群 {gid} 禁言用户 {target_qq}")

                # 通过 context 获取所有平台实例
                platforms = self.context.platform_manager.get_insts()
                logger.info(f"可用平台数量: {len(platforms)}")

                # 寻找能处理这个群的平台
                for idx, platform in enumerate(platforms):
                    logger.info(f"平台 {idx}: {type(platform).__name__}")

                    try:
                        # 提取纯群号（去除前缀）
                        pure_gid = gid.split(':')[-1] if ':' in gid else gid
                        logger.info(f"  - 尝试操作群 {pure_gid}")

                        # 尝试方式1：通过 get_client() 获取客户端
                        if hasattr(platform, 'get_client'):
                            client = platform.get_client()
                            logger.info(f"  - 获取到 client: {type(client).__name__}")

                            if hasattr(client, 'set_group_ban'):
                                await client.set_group_ban(
                                    group_id=int(pure_gid),
                                    user_id=int(target_qq),
                                    duration=duration * 60
                                )
                                success_count += 1
                                logger.info(f"群 {gid} 禁言成功（方式1：client.set_group_ban）")
                                break
                            elif hasattr(client, 'call_api'):
                                await client.call_api(
                                    'set_group_ban',
                                    group_id=int(pure_gid),
                                    user_id=int(target_qq),
                                    duration=duration * 60
                                )
                                success_count += 1
                                logger.info(f"群 {gid} 禁言成功（方式2：client.call_api）")
                                break

                    except Exception as e:
                        logger.warning(f"  - 平台 {idx} 处理失败: {e}", exc_info=True)
                        continue

                if success_count == 0:
                    logger.warning(f"未找到能处理群 {gid} 的平台")

            except Exception as e:
                logger.error(f"禁言失败 (群{gid}): {e}", exc_info=True)

        # 播报
        broadcast_msg = f"【禁言通知】\n记录ID: {record_id}\n目标: {target_qq}\n时长: {duration}分钟\n原因: {reason}\n操作者: {operator_qq}\n执行群数: {success_count}/{len(exec_groups)}"
        await self.broadcast_to_log_group(net_name, broadcast_msg)

        yield event.plain_result(f"✅ 禁言完成\n记录ID: {record_id}\n成功执行: {success_count}/{len(exec_groups)}个群")

    @filter.command("kick")
    async def cmd_kick(self, event: AstrMessageEvent):
        """踢人指令: /kick <目标> <原因> [-b]"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        parts = event.message_str.strip().split(maxsplit=3)
        if len(parts) < 3:
            yield event.plain_result("❌ 参数不足\n用法: /kick <目标> <原因> [-b]")
            return

        target_qq = self.extract_target_qq(event)
        if not target_qq:
            yield event.plain_result("❌ 无法识别目标用户，请@用户或输入QQ号")
            return

        reason = parts[2] if len(parts) >= 3 else "违规"
        add_blacklist = "-b" in event.message_str
        operator_qq = str(event.message_obj.sender.user_id)
        group_id = str(event.unified_msg_origin)

        network_info = await self.get_group_network_async(group_id)
        if not network_info:
            yield event.plain_result("❌ 当前群未加入任何联动组")
            return

        net_name, net_config = network_info
        exec_groups = net_config.get("执行群列表", [])

        # 添加黑名单
        if add_blacklist:
            blacklist_entry = {
                "qq": target_qq,
                "reason": reason,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "operator": operator_qq
            }
            blacklist = await self.get_kv_data("blacklist", [])
            blacklist.append(blacklist_entry)
            await self.put_kv_data("blacklist", blacklist)

        # 记录到数据库
        record_id = self.add_record("kick", target_qq, operator_qq, reason)

        # 执行踢出
        success_count = 0
        platforms = self.context.platform_manager.get_insts()
        for gid in exec_groups:
            try:
                pure_gid = gid.split(':')[-1] if ':' in gid else gid
                logger.info(f"尝试踢出用户 {target_qq} (群 {pure_gid})")

                for platform in platforms:
                    if hasattr(platform, 'get_client'):
                        client = platform.get_client()
                        if client and hasattr(client, 'set_group_kick'):
                            try:
                                await client.set_group_kick(
                                    group_id=int(pure_gid),
                                    user_id=int(target_qq),
                                    reject_add_request=add_blacklist
                                )
                                success_count += 1
                                logger.info(f"群 {gid} 踢人成功")
                                break
                            except Exception as e:
                                logger.warning(f"平台处理群 {gid} 踢人失败: {e}")
                                continue
            except Exception as e:
                logger.error(f"踢人失败 (群{gid}): {e}", exc_info=True)

        # 播报
        blacklist_text = "✅ 已加入黑名单" if add_blacklist else ""
        broadcast_msg = f"【踢出通知】\n记录ID: {record_id}\n目标: {target_qq}\n原因: {reason}\n操作者: {operator_qq}\n执行群数: {success_count}/{len(exec_groups)}\n{blacklist_text}"
        await self.broadcast_to_log_group(net_name, broadcast_msg)

        result_text = f"✅ 踢出完成\n记录ID: {record_id}\n成功执行: {success_count}/{len(exec_groups)}个群"
        if add_blacklist:
            result_text += "\n✅ 已加入黑名单"
        yield event.plain_result(result_text)

    @filter.command("warn")
    async def cmd_warn(self, event: AstrMessageEvent):
        """警告指令: /warn <目标> <原因>"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("❌ 参数不足\n用法: /warn <目标> <原因>")
            return

        target_qq = self.extract_target_qq(event)
        if not target_qq:
            yield event.plain_result("❌ 无法识别目标用户，请@用户或输入QQ号")
            return

        reason = parts[2]
        operator_qq = str(event.message_obj.sender.user_id)
        group_id = str(event.unified_msg_origin)

        network_info = await self.get_group_network_async(group_id)
        if not network_info:
            yield event.plain_result("❌ 当前群未加入任何联动组")
            return

        net_name, net_config = network_info
        exec_groups = net_config.get("执行群列表", [])

        # 记录到数据库
        record_id = self.add_record("warn", target_qq, operator_qq, reason)

        # 在执行群发送警告
        warn_msg = f"⚠️ 警告\n用户: {target_qq}\n原因: {reason}\n操作者: {operator_qq}"

        success_count = 0
        platforms = self.context.platform_manager.get_insts()
        for gid in exec_groups:
            try:
                pure_gid = gid.split(':')[-1] if ':' in gid else gid
                for platform in platforms:
                    if hasattr(platform, 'get_client'):
                        client = platform.get_client()
                        if client and hasattr(client, 'send_group_msg'):
                            try:
                                await client.send_group_msg(group_id=int(pure_gid), message=warn_msg)
                                success_count += 1
                                break
                            except Exception as e:
                                logger.debug(f"平台发送警告失败: {e}")
                                continue
            except Exception as e:
                logger.error(f"发送警告失败 (群{gid}): {e}")

        # 播报
        broadcast_msg = f"【警告通知】\n记录ID: {record_id}\n目标: {target_qq}\n原因: {reason}\n操作者: {operator_qq}\n执行群数: {success_count}/{len(exec_groups)}"
        await self.broadcast_to_log_group(net_name, broadcast_msg)

        yield event.plain_result(f"✅ 警告已发送\n记录ID: {record_id}\n成功执行: {success_count}/{len(exec_groups)}个群")

    @filter.command("record")
    async def cmd_record(self, event: AstrMessageEvent):
        """查询违规历史: /record <目标>"""
        if not self.check_permission(event):
            yield event.plain_result("❌ 权限不足，操作取消")
            return

        target_qq = self.extract_target_qq(event)
        if not target_qq:
            parts = event.message_str.strip().split()
            if len(parts) >= 2 and parts[1].isdigit():
                target_qq = parts[1]
            else:
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
            ORDER BY timestamp DESC
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
        exec_groups = net_config.get("执行群列表", [])

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

        # 更新状态
        cursor.execute('UPDATE records SET status = ? WHERE id = ?', ('revoked', record_id))
        conn.commit()
        conn.close()

        # 执行撤销操作
        success_count = 0
        platforms = self.context.platform_manager.get_insts()
        if action_type == "mute":
            # 解除禁言
            for gid in exec_groups:
                try:
                    pure_gid = gid.split(':')[-1] if ':' in gid else gid
                    for platform in platforms:
                        if hasattr(platform, 'get_client'):
                            client = platform.get_client()
                            if client and hasattr(client, 'set_group_ban'):
                                try:
                                    await client.set_group_ban(
                                        group_id=int(pure_gid),
                                        user_id=int(target_qq),
                                        duration=0
                                    )
                                    success_count += 1
                                    break
                                except Exception as e:
                                    logger.debug(f"平台处理群 {gid} 解除禁言失败: {e}")
                                    continue
                except Exception as e:
                    logger.error(f"解除禁言失败 (群{gid}): {e}")

        elif action_type == "kick":
            # 从黑名单移除
            blacklist = await self.get_kv_data("blacklist", [])
            blacklist = [
                entry for entry in blacklist
                if entry.get("qq") != target_qq
            ]
            await self.put_kv_data("blacklist", blacklist)
            success_count = 1

        # 播报
        broadcast_msg = f"【撤销通知】\n记录ID: {record_id}\n类型: {action_type}\n目标: {target_qq}\n原处罚原因: {original_reason}\n撤销原因: {undo_reason}\n操作者: {operator_qq}"
        await self.broadcast_to_log_group(net_name, broadcast_msg)

        yield event.plain_result(f"✅ 撤销完成\n记录ID: {record_id}\n类型: {action_type}\n目标: {target_qq}")

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

        groups = await self.get_kv_data("groups", {})

        if net_name not in groups:
            groups[net_name] = {
                "播报群": "",
                "执行群列表": []
            }

        if group_id not in groups[net_name]["执行群列表"]:
            groups[net_name]["执行群列表"].append(group_id)
            await self.put_kv_data("groups", groups)
            yield event.plain_result(f"✅ 当前群已加入联动组: {net_name}")
        else:
            yield event.plain_result(f"ℹ️ 当前群已在联动组: {net_name}")

    @filter.command("g_leave")
    async def cmd_g_leave(self, event: AstrMessageEvent):
        """离开联动组: /g_leave"""
        sender_qq = str(event.message_obj.sender.user_id)
        if not self.is_super_admin(sender_qq):
            yield event.plain_result("❌ 权限不足，仅超管可用")
            return

        group_id = str(event.unified_msg_origin)
        groups = await self.get_kv_data("groups", {})
        found = False

        for net_name, net_config in groups.items():
            if group_id in net_config.get("执行群列表", []):
                net_config["执行群列表"].remove(group_id)
                await self.put_kv_data("groups", groups)
                found = True
                yield event.plain_result(f"✅ 当前群已离开联动组: {net_name}")
                break

        if not found:
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

        groups = await self.get_kv_data("groups", {})

        if net_name not in groups:
            groups[net_name] = {
                "播报群": "",
                "执行群列表": []
            }

        groups[net_name]["播报群"] = group_id
        await self.put_kv_data("groups", groups)
        yield event.plain_result(f"✅ 当前群已设为联动组 [{net_name}] 的播报群")

    async def on_event(self, event: AstrMessageEvent):
        """
        监听所有事件，包括群成员增加事件

        AstrBot 会自动调用此方法处理各类事件
        """
        try:
            if not hasattr(event, 'message_obj'):
                return

            message_obj = event.message_obj

            # 检查事件类型
            # SnowLuma 协议
            kind = getattr(message_obj, 'kind', None)
            # OneBot v11 标准
            post_type = getattr(message_obj, 'post_type', None)
            notice_type = getattr(message_obj, 'notice_type', None)

            # 判断是否为群成员增加事件
            is_member_join = (
                kind == "group_member_join" or
                (post_type == "notice" and notice_type == "group_increase")
            )

            if not is_member_join:
                return

            # 获取新成员信息
            new_member_qq = None
            group_id = None

            # SnowLuma 协议
            if hasattr(message_obj, 'userUin'):
                new_member_qq = str(message_obj.userUin)
            # OneBot v11 标准
            elif hasattr(message_obj, 'user_id'):
                new_member_qq = str(message_obj.user_id)

            # 群号
            if hasattr(message_obj, 'groupId'):
                group_id = str(message_obj.groupId)
            elif hasattr(message_obj, 'group_id'):
                group_id = str(message_obj.group_id)
            elif hasattr(event, 'unified_msg_origin'):
                group_id = str(event.unified_msg_origin)

            if not new_member_qq or not group_id:
                logger.warning(f"群成员增加事件缺少必要字段: user={new_member_qq}, group={group_id}")
                return

            logger.info(f"检测到群成员增加: user_id={new_member_qq}, group_id={group_id}")

            # 检查黑名单
            await self.check_and_kick_blacklist(new_member_qq, group_id)

        except Exception as e:
            logger.error(f"处理事件失败: {e}", exc_info=True)

    async def check_and_kick_blacklist(self, new_member_qq: str, group_id: str):
        """
        检查新成员是否在黑名单中，如果在则踢出并拉黑

        参数：
            new_member_qq: 新加入成员的QQ号
            group_id: 群号
        """
        try:
            # 检查黑名单
            blacklist = await self.get_kv_data("blacklist", [])
            blacklist_entry = None

            for entry in blacklist:
                if entry.get("qq") == new_member_qq:
                    blacklist_entry = entry
                    break

            if not blacklist_entry:
                logger.debug(f"用户 {new_member_qq} 不在黑名单中")
                return

            logger.warning(f"发现黑名单用户 {new_member_qq} 加入群 {group_id}")

            # 获取群网络信息
            network_info = await self.get_group_network_async(group_id)
            if not network_info:
                logger.info(f"群 {group_id} 未加入任何联动组，跳过黑名单拦截")
                return

            net_name, net_config = network_info

            # 踢出并拉黑
            pure_gid = group_id.split(':')[-1] if ':' in group_id else group_id
            platforms = self.context.platform_manager.get_insts()
            kicked = False

            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, 'set_group_kick'):
                        try:
                            await client.set_group_kick(
                                group_id=int(pure_gid),
                                user_id=int(new_member_qq),
                                reject_add_request=True  # 拒绝再次加群（拉黑）
                            )
                            kicked = True
                            logger.info(f"成功踢出并拉黑黑名单用户 {new_member_qq} (群 {group_id})")
                            break
                        except Exception as e:
                            logger.debug(f"平台踢出黑名单用户失败: {e}")
                            continue

            if kicked:
                # 播报拦截信息
                broadcast_msg = (
                    f"🚫 【黑名单自动拦截】\n"
                    f"目标QQ: {new_member_qq}\n"
                    f"拦截群: {group_id}\n"
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
                    f"群: {group_id}\n"
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

        blacklist = await self.get_kv_data("blacklist", [])

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

        blacklist = await self.get_kv_data("blacklist", [])
        original_count = len(blacklist)

        blacklist = [entry for entry in blacklist if entry.get("qq") != target_qq]

        if len(blacklist) == original_count:
            yield event.plain_result(f"❌ QQ {target_qq} 不在黑名单中")
            return

        await self.put_kv_data("blacklist", blacklist)

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

    async def terminate(self):
        """插件卸载时的清理"""
        logger.info("GroupManager 插件已卸载")




