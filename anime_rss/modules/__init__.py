"""内置模块清单。

刻意写成静态元组而不是扫目录：PyInstaller 打包后目录结构不可靠，
静态清单能保证内置模块一定被收集进去（第三方插件走 entry_points，
那是 P3 的事）。

清单里放的是**类**不是实例：内核每次启动自己实例化，这样一个进程里
起两个应用（嵌入场景）不会共用同一份状态。

书写顺序不代表加载顺序 —— 加载顺序由各模块的 ``depends`` 拓扑排序决定。
"""
from .feeds import FeedsModule
from .reader import ReaderModule
from .schedule import ScheduleModule
from .settings import SettingsModule
from .userdata import UserDataModule

BUILTIN_MODULES = (
    FeedsModule,
    ReaderModule,
    SettingsModule,
    ScheduleModule,
    UserDataModule,
)

__all__ = [
    "BUILTIN_MODULES",
    "FeedsModule",
    "ReaderModule",
    "ScheduleModule",
    "SettingsModule",
    "UserDataModule",
]
