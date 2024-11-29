import threading
import datetime
from pathlib import Path
import platform
import traceback
import pytz

from typing import Any, List, Dict, Tuple, Optional
from app.chain.tmdb import TmdbChain
from app.core.metainfo import MetaInfoPath
from app.schemas import MediaInfo, TransferInfo

from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings
from app.utils.system import SystemUtils
from app.schemas.types import NotificationType
from watchdog.events import FileSystemEventHandler, FileSystemMovedEvent, FileSystemEvent
import re
from app.db.downloadhistory_oper import DownloadHistoryOper
from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler, FileSystemMovedEvent, FileSystemEvent
from watchdog.observers.polling import PollingObserver
from app.schemas import TransferInfo, Notification, FileItem, TransferDirectoryConf
from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import FileItem, TransferInfo, Notification
from app.schemas.types import MediaType, NotificationType
from app.utils.string import StringUtils

class MonitorChain(ChainBase):
    pass

class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, mon_path: Path, callback: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = mon_path
        self.callback = callback

    def on_created(self, event: FileSystemEvent):
        self.callback.event_handler(event=event, text="创建",
                                    mon_path=self._watch_path, event_path=Path(event.src_path))

    def on_moved(self, event: FileSystemMovedEvent):
        self.callback.event_handler(event=event, text="移动",
                                    mon_path=self._watch_path, event_path=Path(event.dest_path))


class FixedTransfer(_PluginBase):
    # 插件名称
    plugin_name = "定向整理"
    # 插件描述
    plugin_desc = "将指定目录的文件整理至指定媒体库"
    # 插件图标
    plugin_icon = "torrenttransfer.jpg"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "longqiuyu"
    # 作者主页
    author_url = "https://github.com/LongShengWen"
    # 插件配置项ID前缀
    plugin_config_prefix = "fixedtransfer_"
    # 加载顺序
    plugin_order = 23
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 监控目录
    _monitor_confs = None
    # 立即执行一次
    _onlyonce = False
    # 排除关键字
    _exclude_keywords = ""
    # 整理方式
    _transfer_type = "link"
    # 是否刮削
    _scraping = False
    # 延时
    _interval = 10
    # 是否发送通知
    _notify = False

    # 目录配置
    _dirconf = {}
    # 消息汇总
    _msg_medias = {}
    # 退出事件
    _event = threading.Event()
    # 监控服务
    _observers = []
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._renameconf = {}
        self._coverconf = {}

        self.chain = MonitorChain()
        self.transferhis = TransferHistoryOper()
        self.transferchain = TransferChain()
        self.downloadhis = DownloadHistoryOper()
        self.mediaChain = MediaChain()
        self.tmdbchain = TmdbChain()
        self.storagechain = StorageChain()
        self.directoryhelper = DirectoryHelper()
        self.systemmessage = MessageHelper()
        self.systemconfig = SystemConfigOper()

        self.all_exts = settings.RMT_MEDIAEXT

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._interval = config.get("interval")
            self._notify = config.get("notify")
            self._monitor_confs = config.get("monitor_confs")
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._transfer_type = config.get("transfer_type") or "link"
            self._scraping = config.get("scraping") or False

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.__send_msg, trigger='interval', seconds=15)

            # 读取目录配置
            monitor_confs = self._monitor_confs.split("\n")
            if not monitor_confs:
                return
            for monitor_conf in monitor_confs:
                # 格式 监控方式#监控目录#目的目录#是否重命名#封面比例
                if not monitor_conf:
                    continue
                if str(monitor_conf).count("#") != 2:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue
                mode = str(monitor_conf).split("#")[0]
                # 监控目录
                source_dir = str(monitor_conf).split("#")[1]
                # 目标目录
                target_dir = str(monitor_conf).split("#")[2]

                # 存储目录监控配置
                self._dirconf[source_dir] = target_dir

                # 启用目录监控
                if self._enabled:
                    # 检查媒体库目录是不是下载目录的子目录
                    try:
                        if target_dir and Path(target_dir).is_relative_to(Path(source_dir)):
                            logger.warn(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                            self.systemmessage.put(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                            continue
                    except Exception as e:
                        logger.debug(str(e))
                        pass

                    try:
                        if mode == "fast":
                            observer = self.__choose_observer()
                        else:
                            observer = PollingObserver()
                        self._observers.append(observer)
                        observer.schedule(FileMonitorHandler(source_dir, self), path=source_dir, recursive=True)
                        observer.daemon = True
                        observer.start()
                        logger.info(f"已启动 {source_dir} 的目录监控服务, 监控模式：{mode}")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                + """
                                echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                sudo sysctl -p
                                """)
                        else:
                            logger.error(f"{source_dir} 启动目录监控失败：{err_msg}")
                        self.systemmessage.put(f"{source_dir} 启动目录监控失败：{err_msg}", title="目录监控")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("目录监控服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync_all, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                        name="目录监控全量执行")
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()
    
    @staticmethod
    def __choose_observer() -> Any:
        """
        选择最优的监控模式
        """
        system = platform.system()

        try:
            if system == 'Linux':
                from watchdog.observers.inotify import InotifyObserver
                return InotifyObserver()
            elif system == 'Darwin':
                from watchdog.observers.fsevents import FSEventsObserver
                return FSEventsObserver()
            elif system == 'Windows':
                from watchdog.observers.read_directory_changes import WindowsApiObserver
                return WindowsApiObserver()
        except Exception as error:
            logger.warn(f"导入模块错误：{error}，将使用 PollingObserver 监控目录")
        return PollingObserver()

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步短剧监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            logger.debug(mon_path)
            # 遍历目录下所有文件
            for file_path in SystemUtils.list_files(Path(mon_path), settings.RMT_MEDIAEXT):
                self.__handle_file(is_directory=Path(file_path).is_dir(),event_path=Path(file_path),source_dir=mon_path, storage='local')
        logger.info("全量同步短剧监控目录完成！")

    
    def event_handler(self, event, mon_path: Path, text: str, event_path: Path):
        """
        处理文件变化
        :param event: 事件
        :param source_dir: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        # 回收站及隐藏的文件不处理
        if (event_path.find("/@Recycle") != -1
                or event_path.find("/#recycle") != -1
                or event_path.find("/.") != -1
                or event_path.find("/@eaDir") != -1):
            logger.info(f"{event_path} 是回收站或隐藏的文件，跳过处理")
            return

        # 不是媒体文件不处理
        if event_path.suffix.lower() not in self.all_exts:
            logger.debug(f"{event_path} 不是媒体文件")
            return
        
        # 命中过滤关键字不处理
        if self._exclude_keywords:
            for keyword in self._exclude_keywords.split("\n"):
                if keyword and re.findall(keyword, event_path):
                    logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                    return
        storage = "local"
        # 查询历史记录，已转移的不处理
        if self.transferhis.get_by_src(str(event_path), storage=storage):
            logger.info(f"{event_path} 已经整理过了")
            return
        
        # 文件发生变化
        logger.debug(f"变动类型 {event.event_type} 变动路径 {event_path}")
        self.__handle_file(is_directory=event.is_directory, event_path=event_path, source_dir=mon_path, storage=storage)

    def __handle_file(self, is_directory: bool, event_path: Path, source_dir: str, storage: str):
        """
        同步一个文件
        :event.is_directory
        :param event_path: 事件文件路径
        :param source_dir: 监控目录
        :params storage: 存储
        """
        try:
            # 转移目标路径
            dest_dir = self._dirconf.get(source_dir)
            # 元数据
            file_meta = MetaInfoPath(Path(event_path))
            if not file_meta.name:
                logger.error(f"{Path(event_path).name} 无法识别有效信息")
                return
            # 根据父路径获取下载历史
            download_history = None
            # 按文件全路径查询
            download_file = self.downloadhis.get_file_by_fullpath(str(event_path))
            if download_file:
                download_history = self.downloadhis.get_by_hash(download_file.download_hash)
            # 获取下载Hash
            download_hash = None
            if download_history:
                download_hash = download_history.download_hash
            # 识别媒体信息
            if download_history and (download_history.tmdbid or download_history.doubanid):
                # 下载记录中已存在识别信息
                mediainfo: MediaInfo = self.mediaChain.recognize_media(mtype=MediaType(download_history.type),
                                                                        tmdbid=download_history.tmdbid,
                                                                        doubanid=download_history.doubanid, cache=True)
            else:
                mediainfo: MediaInfo = self.mediaChain.recognize_by_meta(file_meta)

            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{file_meta.name}')
                # 新增转移失败历史记录
                his = self.transferhis.add_fail(
                    fileitem=FileItem(
                        storage=storage,
                        type="file",
                        path=str(event_path),
                        name=event_path.name,
                        basename=event_path.stem,
                        extension=event_path.suffix[1:],
                    ),
                    mode='',
                    meta=file_meta,
                    download_hash=download_hash
                )
                if self._notify:
                    self.chain.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{event_path.name} 未识别到媒体信息，无法入库！",
                        text=f"回复：```\n/redo {his.id} [tmdbid]|[类型]\n``` 手动识别转移。",
                        link=settings.MP_DOMAIN('#/history')
                    ))
                return
            
            # 查询转移目的目录
            dir_info = TransferDirectoryConf()
            dir_info.storage = storage
            dir_info.download_path = event_path
            dir_info.media_type = ""
            dir_info.renaming = True
            dir_info.scraping = self._scraping
            dir_info.library_path = dest_dir
            dir_info.library_storage = storage
            dir_info.transfer_type = self._transfer_type
            dir_info.overwrite_mode = "always"
            dir_info.name = "定向整理"

            # 查找这个文件项
            file_item = self.storagechain.get_file_item(storage=storage, path=Path(event_path))
            if not file_item:
                logger.warn(f"{event_path.name} 未找到对应的文件")
                return
            # 更新媒体图片
            self.chain.obtain_images(mediainfo=mediainfo)
            # 获取集数据
            if mediainfo.type == MediaType.TV:
                episodes_info = self.tmdbchain.tmdb_episodes(tmdbid=mediainfo.tmdb_id,season=file_meta.begin_season or 1)
            else:
                episodes_info = None
            # 转移
            transferinfo: TransferInfo = self.chain.transfer(fileitem=file_item,
                                                                meta=file_meta,
                                                                mediainfo=mediainfo,
                                                                target_directory=dir_info,
                                                                episodes_info=episodes_info)

            if not transferinfo:
                logger.error("文件转移模块运行失败")
                return
            
            if not transferinfo.success:
                # 转移失败
                logger.warn(f"{event_path.name} 入库失败：{transferinfo.message}")
                # 新增转移失败历史记录
                self.transferhis.add_fail(
                    fileitem=file_item,
                    mode=transferinfo.transfer_type if transferinfo else '',
                    download_hash=download_hash,
                    meta=file_meta,
                    mediainfo=mediainfo,
                    transferinfo=transferinfo
                )
                # 发送失败消息
                if self._notify:
                    self.chain.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{mediainfo.title_year} {file_meta.season_episode} 入库失败！",
                        text=f"原因：{transferinfo.message or '未知'}",
                        image=mediainfo.get_message_image(),
                        link=settings.MP_DOMAIN('#/history')
                    ))
                return
            # 转移成功
            logger.info(f"{event_path.name} 入库成功：{transferinfo.target_diritem.path}")
            # 新增转移成功历史记录
            self.transferhis.add_success(
                fileitem=file_item,
                mode=transferinfo.transfer_type if transferinfo else '',
                download_hash=download_hash,
                meta=file_meta,
                mediainfo=mediainfo,
                transferinfo=transferinfo
            )
            # 汇总刮削
            if transferinfo.need_scrape:
                self.mediaChain.scrape_metadata(fileitem=transferinfo.target_diritem,meta=file_meta,mediainfo=mediainfo)
            # 发送消息汇总
            if transferinfo.need_notify:
                self.__collect_msg_medias(mediainfo=mediainfo, file_meta=file_meta, transferinfo=transferinfo)
            # 移动模式删除空目录
            if transferinfo.transfer_type in ["move"]:
                self.storagechain.delete_media_file(file_item, delete_self=False)

        except Exception as e:
            logger.error(f"event_handler_created error: {e}")
            print(str(e))
            traceback.print_exc()

    def __collect_msg_medias(self, mediainfo: MediaInfo, file_meta: MetaInfoPath, transferinfo: TransferInfo):
        """
        收集媒体处理完的消息
        """
        media_list = self._msg_medias.get(mediainfo.title_year + " " + file_meta.season) or {}
        if media_list:
            media_files = media_list.get("files") or []
            if media_files:
                file_exists = False
                for file in media_files:
                    if str(transferinfo.fileitem.path) == file.get("path"):
                        file_exists = True
                        break
                if not file_exists:
                    media_files.append({
                        "path": str(transferinfo.fileitem.path),
                        "mediainfo": mediainfo,
                        "file_meta": file_meta,
                        "transferinfo": transferinfo
                    })
            else:
                media_files = [
                    {
                        "path": str(transferinfo.fileitem.path),
                        "mediainfo": mediainfo,
                        "file_meta": file_meta,
                        "transferinfo": transferinfo
                    }
                ]
            media_list = {
                "files": media_files,
                "time": datetime.datetime.now()
            }
        else:
            media_list = {
                "files": [
                    {
                        "path": str(transferinfo.fileitem.path),
                        "mediainfo": mediainfo,
                        "file_meta": file_meta,
                        "transferinfo": transferinfo
                    }
                ],
                "time": datetime.datetime.now()
            }
        self._msg_medias[mediainfo.title_year + " " + file_meta.season] = media_list

    def __send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._msg_medias or not self._msg_medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._msg_medias.keys()):
            media_list = self._msg_medias.get(medis_title_year_season)
            logger.info(f"开始处理媒体 {medis_title_year_season} 消息")

            if not media_list:
                continue

            # 获取最后更新时间
            last_update_time = media_list.get("time")
            media_files = media_list.get("files")
            if not last_update_time or not media_files:
                continue

            transferinfo = media_files[0].get("transferinfo")
            file_meta = media_files[0].get("file_meta")
            mediainfo = media_files[0].get("mediainfo")
            # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
            if (datetime.datetime.now() - last_update_time).total_seconds() > int(self._interval) or mediainfo.type == MediaType.MOVIE:
                # 汇总处理文件总大小
                total_size = 0
                file_count = 0

                # 剧集汇总
                episodes = []
                for file in media_files:
                    transferinfo = file.get("transferinfo")
                    total_size += transferinfo.total_size
                    file_count += 1

                    file_meta = file.get("file_meta")
                    if file_meta and file_meta.begin_episode:
                        episodes.append(file_meta.begin_episode)

                transferinfo.total_size = total_size
                # 汇总处理文件数量
                transferinfo.file_count = file_count

                # 剧集季集信息 S01 E01-E04 || S01 E01、E02、E04
                season_episode = None
                # 处理文件多，说明是剧集，显示季入库消息
                if mediainfo.type == MediaType.TV:
                    # 季集文本
                    season_episode = f"{file_meta.season} {StringUtils.format_ep(episodes)}"
                # 发送消息
                self.transferchain.send_transfer_message(meta=file_meta,mediainfo=mediainfo,transferinfo=transferinfo,season_episode=season_episode)
                # 发送完消息，移出key
                del self._msg_medias[medis_title_year_season]
                continue

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "exclude_keywords": self._exclude_keywords,
            "transfer_type": self._transfer_type,
            "onlyonce": self._onlyonce,
            "interval": self._interval,
            "notify": self._notify,
            "monitor_confs": self._monitor_confs,
            "scraping": self._scraping,
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'scraping',
                                            'label': '是否刮削',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'transfer_type',
                                            'label': '转移方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '硬链接', 'value': 'link'},
                                                {'title': '软链接', 'value': 'filesoftlink'},
                                                {'title': 'Rclone复制', 'value': 'rclone_copy'},
                                                {'title': 'Rclone移动', 'value': 'rclone_move'}
                                            ]
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
                                            'model': 'interval',
                                            'label': '入库消息延迟',
                                            'placeholder': '10'
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_confs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '监控方式#监控目录#目的目录'
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
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
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
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '默认从tmdb刮削，刮削失败则从pt站刮削。当重命名方式为smart时，如站点管理已配置AGSV、ilolicon，则优先从站点获取短剧封面。'
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
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '开启封面裁剪后，会把封面裁剪成配置的比例。'
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
            "interval": 10,
            "monitor_confs": "",
            "exclude_keywords": "",
            "transfer_type": "link",
            "scraping": False
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        self._event.set()
        if self._observers:
            for observer in self._observers:
                try:
                    logger.info(f"正在停止目录监控服务：{observer}...")
                    observer.stop()
                    observer.join()
                    logger.info(f"{observer} 目录监控已停止")
                except Exception as e:
                    logger.error(f"停止目录监控服务出现了错误：{e}")
            self._observers = []
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                try:
                    self._scheduler.shutdown()
                except Exception as e:
                    logger.error(f"停止定时服务出现了错误：{e}")
            self._scheduler = None
        self._event.clear()