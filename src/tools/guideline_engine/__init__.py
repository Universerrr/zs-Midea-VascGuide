"""工具A · 指南引擎的公开接口 —— 只有 re-export，零逻辑。

为什么需要这个文件：
    ``src/preop_agent/execution_layer/registry.py`` 的消费方式是
    ``from ...tools.guideline_engine import SPEC, run``。包级再导出把这个形式
    固定成**契约**——本包内部拆成多少个文件，调用方不需要知道。若改写成
    ``...guideline_engine.engine import SPEC``，编排层就绑定了内部布局，
    日后拆分或改名会波及 ``src/preop_agent/``。

为什么 ``SPEC`` 不写在本文件：
    ``engine.run()`` 需要引用 ``SPEC.label``。若 ``SPEC`` 定义在包初始化文件里，
    ``__init__`` → ``engine`` → ``__init__`` 会构成循环导入。放进 ``spec.py`` 后，
    依赖方向是单向的 ``__init__ → engine → spec``。

本包的多文件结构只对工具A 成立：其余 4 个工具是 61–68 行的单文件。工具A 的复杂度
来自 ``VascGuide_V3_6.xlsx`` 的 21 个病种 sheet / 172 条规则本身，拆包是这部分的
实际需要，不是风格偏好。
"""

from .engine import run
from .spec import SPEC

__all__ = ["SPEC", "run"]
