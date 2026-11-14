"""园区能耗按历史归属分摊作业（纯标准库实现）。

模块划分
--------
- errors:     统一的校验/处理错误类型
- validation: JSON 语法与结构校验（类型、标识、权重、唯一性）
- graph:      时间分段、无环与权重和校验、精确有理数积分，得矩阵 A
- rounding:   自写最小费用最大流（含下界），约束取整得矩阵 B
- pipeline:   端到端编排与原子写入
- cli:        命令行入口
"""

from .errors import AllocationError
from .pipeline import process_text, process_files

__all__ = ["AllocationError", "process_text", "process_files"]
__version__ = "1.0.0"
