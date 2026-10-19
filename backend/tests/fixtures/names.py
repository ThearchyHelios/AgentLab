"""测试里随机起的名字不带数字。

名字会拼进导出的文件、通知、任务说明这类文本，而不少断言写的是「文本里不许出现某个数」（不许出现 330、300、48……）：
随机的十六进制里带着那几个数字的概率不大但不是零（8 位里出现「330」约千分之一点五），一撞上就无缘无故地失败。
十六进制里的数字换成字母（同 test_run_data_versions 的写法），随机性来自 uuid4（不受 pytest-randomly 每个测试重设
random 种子的影响，不会在不同测试里撞名）。
"""

from __future__ import annotations

import uuid

#: 十六进制里的数字换成字母：名字里不带数字，数字的断言才只看得见数据
NO_DIGITS = str.maketrans("0123456789", "ghijklmnop")


def letters(n: int = 8) -> str:
    """n 个随机小写字母（a–p），n 最多 32。"""
    return uuid.uuid4().hex[:n].translate(NO_DIGITS)
