"""表名、列名、维度值的规范化和比较。

导入（data/tabular.py、data/table_versions.py）和证据层（engine/evidence.py）共用这一份。
两边各写一套的话，「导入时认为没撞名、核对时认为是同一个名字」这种口径分歧迟早会出现。

三个用途，三个函数，不能混用：
- canon：比较和存储维度值、主键用的规范写法（全角、各类连字符、格式字符、空白都抹平）；
- name_key：按名字找表、找列。只对 ASCII 不区分大小写、不做 NFKC，和 SQLite 的标识符比较完全一致
  （导入的名字都满足 name_problem、等于自身的 NFKC 形式，对它们做不做 NFKC 没有区别）；
- collide_key：判断两个名字算不算撞名。用 casefold，比 SQLite 严：「Дата」和「дата」
  在 SQLite 里是两张表，但并存只会让人和模型都分不清，所以也不许。
"""
from __future__ import annotations

import re
import unicodedata

#: NFKC 之后仍然不是 ASCII 连字符的各种横线：连字符、不换行连字符、数字线、en dash、
#: em dash、水平线、减号。全角「－」NFKC 会归一，不在这里
_DASHES = frozenset("‐‑‒–—―−")
#: 看起来是空白、却不算 Zs 也不算 Cf 的填充字符（韩文填充符一类），混进名字里肉眼看不出来。
#: 证据层核对 [[t:]] / [[c:]] 的写法时也用它（engine/evidence.py）
FILLERS = frozenset("ᅟᅠㅤﾠ")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
#: 名字上限 48 字：工具描述、表结构里要列出来，太长的名字模型和人都读不全
NAME_MAX = 48
_NAME = re.compile(rf"^[^\W\d]\w{{0,{NAME_MAX - 1}}}$")
#: 作名字会让不加引号的 SQL 读错的关键字。不求全，收常见的、表头里可能出现的
RESERVED = frozenset({
    "abort", "add", "all", "alter", "and", "as", "asc", "between", "by", "case", "check",
    "collate", "column", "commit", "constraint", "create", "cross", "default", "delete",
    "desc", "distinct", "drop", "else", "end", "escape", "except", "exists", "foreign",
    "from", "full", "group", "having", "in", "index", "inner", "insert", "intersect",
    "into", "is", "join", "key", "left", "like", "limit", "natural", "not", "null",
    "offset", "on", "or", "order", "outer", "primary", "references", "right", "rowid",
    "select", "set", "table", "then", "to", "transaction", "trigger", "union", "unique",
    "update", "using", "values", "view", "when", "where", "with",
})


def canon(text: str) -> str:
    """规范写法：NFKC，各类横线统一成「-」，去掉格式字符（Cf），空白折叠成一个空格、去首尾。

    九月的表把「8-9」写成「8–9」（en dash）或「８－９」（全角），按原文存的话，
    `WHERE 时段 = '8-9'` 会静默漏掉那个月——所以维度值和主键都按这个存，原文另记。
    """
    s = unicodedata.normalize("NFKC", str(text))
    s = "".join("-" if ch in _DASHES else ch for ch in s if unicodedata.category(ch) != "Cf")
    return re.sub(r"\s+", " ", s).strip()


def name_problem(name: str) -> str | None:
    """表名或列名能不能用。能用返回 None，不能用返回一句给人看的原因。

    规则：等于自身的 NFKC 形式（挡住全角 ASCII「ＡＢＣ」、带圈数字「①」）；不含控制字符、
    格式字符、空格类和组合附加符号，也不含填充字符；字母或汉字开头，只含字母、数字、汉字和
    下划线，最长 NAME_MAX 字；不以 sqlite_ 开头（SQLite 保留）；不是 SQL 关键字。
    """
    if not name:
        return "名称不能为空"
    if name != unicodedata.normalize("NFKC", name):
        return f"名称「{name}」含全角或兼容字符，请改用常规写法"
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Mn") or ch in FILLERS for ch in name):
        return f"名称「{name}」含不可见字符或空格"
    if not _NAME.match(name):
        return (f"名称「{name}」需以字母或汉字开头，只能包含字母、数字、汉字和下划线，"
                f"最长 {NAME_MAX} 字")
    if name.lower().startswith("sqlite_"):
        return f"名称「{name}」以 sqlite_ 开头，这是 SQLite 的保留前缀"
    if name.lower() in RESERVED:
        return f"名称「{name}」是 SQL 关键字"
    return None


def name_key(name: str) -> str:
    """按名字查找时用的键：只把 ASCII 字母转小写，其余逐字比，不做 NFKC。

    SQLite 比较标识符时只对 ASCII 不区分大小写（`ABC` 和 `abc` 是同一张表，`Дата` 和
    `дата` 不是），也不做任何归一（全角的 `ＡＢＣ` 和 `abc` 是两张表，库里可以并存）。证据层用这个键
    找表找列、给 SQL 里的表名去重，和真正执行 SQL 的数据库口径一致。

    不能先做 NFKC：手工接入的库里全角「ＯＲＤＥＲＳ」和「orders」并存时，[[t:orders]] 会被静默绑到
    全角那张，SQL 台账也会把真正 JOIN 的两张表当成一张。导入产生的名字都满足 name_problem（等于自身的
    NFKC 形式），对它们这两种写法得出的键一样；证据层的 [[t:]] / [[c:]] 引用也要求 NFKC 稳定。
    """
    return str(name).translate(_ASCII_LOWER)


def collide_key(name: str) -> str:
    """撞名检查用的键：NFKC 加 casefold，比 SQLite 严。"""
    return unicodedata.normalize("NFKC", str(name)).casefold()


def to_sql_name(header: object, *, fallback: str) -> str:
    """原表头 → 一个满足 name_problem 的名字。原表头另存作显示名，这里只管能用。

    canon 之后把不是字母、数字、汉字、下划线的字符（括号、斜杠、空格、换行、方括号、竖线）
    换成「_」并合并，去掉首尾的「_」；数字开头加「c_」，关键字后面加「_」，超长截断。
    什么都不剩时用 fallback（一般是 col_<位置>）。「金额(元)」→「金额_元」。
    """
    s = canon("" if header is None else str(header))
    s = re.sub(r"[^\w]+", "_", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Mn", "Cf") and ch not in FILLERS)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return fallback
    if s[0].isdigit():
        s = f"c_{s}"
    if s.lower().startswith("sqlite_"):
        s = f"t_{s}"
    s = s[:NAME_MAX].rstrip("_") or fallback
    if s.lower() in RESERVED:
        s = f"{s}_"
    return s if name_problem(s) is None else fallback
