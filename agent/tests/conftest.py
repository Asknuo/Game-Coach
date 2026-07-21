"""pytest 共享配置：把 agent/ 根目录加入 sys.path，使测试可以 import 各模块."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
