"""pytest 共享配置：把 voice/ 目录加入 sys.path."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))