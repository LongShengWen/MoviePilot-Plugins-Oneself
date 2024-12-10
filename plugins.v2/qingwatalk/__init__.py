from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
from datetime import datetime
from datetime import datetime, timedelta
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils
from app.db.models.site import Site
from app.db.site_oper import SiteOper

class QingWaTalk(_PluginBase):
    # 插件名称
    plugin_name = "青蛙自动喊话"
    # 插件描述
    plugin_desc = "定时在青蛙喊话框中喊话"
    # 插件图标
    plugin_icon = "qingwa.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "longqiuyu"
    # 作者主页
    author_url = "https://github.com/LongShengWen"
    # 插件配置项ID前缀
    plugin_config_prefix = "qingwatalk_"
    # 加载顺序
    plugin_order = 23
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    # 立即执行一次
    _onlyonce = False
    # 是否开启通知
    _notify = False
    # 喊话上传
    _upload = False
    # 喊话下载
    _download = False

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):

        self.siteOper = SiteOper()

        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._download = config.get("download")
            self._upload = config.get("upload")

        if self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"立即运行一次")
            self._scheduler.add_job(func=self.__talk,
                                    trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="青蛙喊话")
            # 关闭一次性开关
            self._onlyonce = False
            self.update_config({
                "onlyonce": False,
                "cron": self._cron,
                "enabled": self._enabled,
                "notify": self._notify,
                "upload": self._upload,
                "download": self._download
            })

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __talk(self):
        """
        喊话
        """
        site: Site = self.siteOper.get_by_domain(domain="qingwapt.com")
        if not site or not site.cookie:
            logger.error("请检查青蛙站点是否配置")
            return
        if not site.is_active:
            logger.error("青蛙站点未启用")
            return
        message = ""
        if self._upload:
            params = {
                "shbox_text": "蛙总，求上传",
                "shout": "我喊",
                "sent": "yes",
                "type": "shoutbox"
            }
            result, info = self.__talk_request(site=site, params=params)
            message = "求上传成功！" if result else "求上传失败！"
        if self._download:
            params = {
                "shbox_text": "蛙总，求下载",
                "shout": "我喊",
                "sent": "yes",
                "type": "shoutbox"
            }
            result, info = self.__talk_request(site=site, params=params)
            message = message + "\n" + "求下载成功！" if result else "求下载失败！"

        if self._notify and message:
            message = self.__escape_markdown(text="\n".join(message))[:4096]
            self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【青蛙喊话】",
                    text=message
                )

    def __talk_request(self, site: Site, params: Dict) -> Tuple[bool, str]:
        try:
            url = f"https://{site.domain}/shoutbox.php"
            proxy_server = settings.PROXY if site.proxy else None
            res = RequestUtils(cookies=site.cookie, ua=site.ua, proxies=proxy_server).post(url=url, params=params)
            # 判断登录状态
            if res and res.status_code in [200, 500, 403]:
                logger.info("青蛙PT喊话成功!")
                return True, f"无法打开网站！"
            elif res is not None:
                logger.error(f"无法打开网站: {url}: {res.status_code}")
                return False, f"状态码：{res.status_code}！"
            else:
                logger.error(f"无法打开网站: {url}")
                return False, f"无法打开网站！"
        except Exception as e:
            logger.error(f"{e}")
            return False, f"喊话异常！"

    def __escape_markdown(self, text: str, version: int = 2) -> str:
        """
        Escapes Markdown special characters for Telegram.
        """
        if version == 1:
            escape_chars = r"_*"
        elif version == 2:
            escape_chars = r"_*"
        else:
            raise ValueError("Only Markdown versions 1 and 2 are supported.")
        return "".join(f"\\{char}" if char in escape_chars else char for char in text)
    
    def get_state(self) -> bool:
        return True if self._enabled and self._cron and (self._upload or self._download) else False

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
                "id": "QingWaTalk",
                "name": "青蛙喊话服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__talk,
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
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'upload',
                                            'label': '求上传'
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
                                            'model': 'download',
                                            'label': '求下载'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '喊话周期'
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
            "upload": False,
            "download": False
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
