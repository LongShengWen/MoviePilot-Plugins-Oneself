from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
import json
import re
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils
from app.helper.browser import PlaywrightHelper


class InvitesMonitor(_PluginBase):
    # 插件名称
    plugin_name = "药丸邀请监控"
    # 插件描述
    plugin_desc = "定时查看是否有新的发邀帖子"
    # 插件图标
    plugin_icon = "invites.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "longqiuyu"
    # 作者主页
    author_url = "https://github.com/LongShengWen"
    # 插件配置项ID前缀
    plugin_config_prefix = "invitesmonitor_"
    # 加载顺序
    plugin_order = 23
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None

    _onlyonce = False

    _notify = False

    _begin_id = None

    _cookie = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._begin_id = config.get("begin_id")
            self._cookie = config.get("cookie")

        if self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"药丸监控服务启动，立即运行一次")
            self._scheduler.add_job(func=self.__monitor,
                                    trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="药丸邀请监控")
            # 关闭一次性开关
            self._onlyonce = False
            self.update_config({
                "onlyonce": False,
                "cron": self._cron,
                "enabled": self._enabled,
                "notify": self._notify,
                "begin_id": self._begin_id,
                "cookie": self._cookie
            })

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __analyze(self, data: str) -> Tuple[bool, str, str]:
        """
        解析数据看是否有发邀
        """
        parsed_data = json.loads(data)
        # 提取标题
        title = parsed_data["data"]["attributes"]["title"]
        # 提取发布时间
        created_at = parsed_data["data"]["attributes"]["createdAt"]
        # 提取标签 ID
        tag_ids = [tag["id"] for tag in parsed_data["data"]["relationships"]["tags"]["data"]]

        # 根据标签 ID 判断是否有 "发邀" 这个标签
        has_fy_tag = any(included["attributes"]["name"] == "发邀"
            for included in parsed_data["included"]
            if included["type"] == "tags" and included["id"] in tag_ids
        )
        return [has_fy_tag, title, tag_ids]
        
    def __monitor(self):
        """
        药丸监控
        """
        try:
            if not self._begin_id:
                logger.error("最新的帖子ID未配置！")
            last_id = self.get_data(key="last_id")
            if not last_id:
                last_id = self._begin_id
            if not last_id:
                logger.error("未获取到最新的帖子ID！")
            logger.debug(f"最新ID: {last_id}")
            # 浏览器仿真
            html_content = PlaywrightHelper().get_page_source(
                url='https://invites.fun/t/FY?sort=newest',
                headless=True,
                cookies=self._cookie
            )
            soup = BeautifulSoup(html_content, 'html.parser')
            # 查找 <noscript id="flarum-content"> 标签
            noscript_content = soup.find(id="flarum-content")
            # 查找其中所有的 <a> 标签
            links = noscript_content.find_all('a')
            # 定义正则表达式来提取ID
            url_pattern = re.compile(r'/d/(\d+)')
            # 提取标题、地址和ID
            # 提取标题、地址和ID
            results = []
            for link in links:
                href = link.get('href', '')  # 提取链接地址
                title = link.get_text(strip=True)  # 提取标题
                # 使用正则表达式从 href 中提取 ID
                match = url_pattern.search(href)
                if href and match:  # 确保链接地址和ID都存在
                    id = int(match.group(1))  # 提取 ID 并转化为整数
                    if id > int(last_id):
                        results.append((title, href, id))

            # 按 ID 升序排序
            sorted_results = sorted(results, key=lambda x: x[2])  # 按第三个元素（ID）排序

            # 输出排序后的结果
            for title, href, id in sorted_results:
                logger.info(f"标题: {title}, 地址: {href}, ID: {id}")
                last_id = id
                # 发送通知
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【药丸有发邀新帖子】",
                        text=f"{title}",
                        link=href
                        )
            # 保持
            self.save_data(key="last_id", value=last_id)
            # 更新配置的最新ID
            c_config:dict = self.get_config()
            c_config["begin_id"] = last_id
            self.update_config(config=c_config)
            logger.info(f"监测完成！新增{len(results)}个帖子。")
        except Exception as e:
            logger.error(f"药丸帖子监测出错:{str(e)}")
        
    def get_state(self) -> bool:
        return True if self._enabled and self._cookie else False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "InvitesMonitor",
                "name": "药丸监控服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__monitor,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '监控周期'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'begin_id',
                                            'label': '最新的ID'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cookie',
                                            'label': '药丸的cookie'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "cron": "0 9 * * *",
            "begin_id": None,
            "cookie": None
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
