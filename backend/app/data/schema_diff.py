"""表结构的前后差异（docs/design/next-round/03-数据接入与目录维护.md B1）：纯函数，不连库、不读目录。

**为什么要有它。** 以前重新探查是整份替换 schema_cache，不比前后：删了一列，那一列上人工确认过的目录项、写死了这一列的
已发布模板都收不到提醒，正式运行要到查询那一步才报错。现在每张表有结构版本（data/source_objects：指纹变了才加 1），
后台探查读到新结构时拿上一次的结构和这一次的比（diff_table），变化写进 source_changes（data/source_changes.py），再由
影响面（data/catalog_impact.structure_impact）说清哪些目录项、哪些写死的 SQL 会受影响。

**表级**的四种由调用方判（它知道对象索引和范围）：table_added（对象索引里新出现的）、table_removed（对象索引里也没了，
真删除）、table_left_scope（离开了探查范围）、table_reflect_failed（在范围里、这次读不到结构）。后两种**不算删除**
（is_deletion）：范围外、读不到结构时，目录和模板里说到它的照旧有效，只是这次没看到。

**列级**按列名比：column_added、column_removed、column_type_changed（比的是按方言编译的完整写法，VARCHAR2(20 CHAR)、
ENUM('a','b')，见 introspect.type_text）、column_nullable_changed、comment_changed（列注释，表注释的 column 为 None）。
**键**：主键、唯一约束的增删（key_changed），外键的增删（fk_changed）。反射时读不到唯一约束、外键的表不写那两个键：
两边有一边没有就不比（不知道 ≠ 删了）。

**疑似改名**：被删的列和新加的列类型相同、在列清单里的位置相同（夹在同样的两列之间），而且这样的配对只有一对时，两条变化都带 rename，
说法只写「可能是改名（未确认）」——库里只看得到删了一列、加了一列，不下结论。

**基线**（is_baseline）：升级后第一次按对象索引读到这张表（结构版本还是 0：升级前同步探查的老缓存接进来的那份、或者
从没读过）只建基线、不出变化。列类型的写法第 1 波从通用改成了方言完整写法（目录-D10），拿老缓存的 VARCHAR2 和新读的
VARCHAR2(20 CHAR) 比，所有 Oracle 列都会被说成「类型变了」。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 表级的四种（调用方判）
TABLE_ADDED = "table_added"
TABLE_REMOVED = "table_removed"
TABLE_LEFT_SCOPE = "table_left_scope"
TABLE_REFLECT_FAILED = "table_reflect_failed"
#: 列级、键
COLUMN_ADDED = "column_added"
COLUMN_REMOVED = "column_removed"
COLUMN_TYPE = "column_type_changed"
COLUMN_NULLABLE = "column_nullable_changed"
COMMENT = "comment_changed"
KEY = "key_changed"
FK = "fk_changed"
#: 探查范围的规则改了（源级，table 为空）
SCOPE = "scope_changed"

STRUCT_KINDS = (TABLE_ADDED, TABLE_REMOVED, TABLE_LEFT_SCOPE, TABLE_REFLECT_FAILED, COLUMN_ADDED, COLUMN_REMOVED,
                COLUMN_TYPE, COLUMN_NULLABLE, KEY, FK, COMMENT, SCOPE)

#: 没有影响面时的严重程度（影响面会升级：已确认的项、写死的 SQL 指向被删的列时是 action）
DEFAULT_SEVERITY = {
    TABLE_ADDED: "info", TABLE_REMOVED: "warning", TABLE_LEFT_SCOPE: "info", TABLE_REFLECT_FAILED: "warning",
    COLUMN_ADDED: "info", COLUMN_REMOVED: "warning", COLUMN_TYPE: "warning", COLUMN_NULLABLE: "info",
    KEY: "warning", FK: "warning", COMMENT: "info", SCOPE: "info",
}

#: 会让说到它的目录项、写死的 SQL 失效的：表真删了、列删了
DESTRUCTIVE = frozenset({TABLE_REMOVED, COLUMN_REMOVED})


def is_deletion(kind: str) -> bool:
    """这种变化是不是删除。离开范围、读不到结构都不是：范围外的表还在库里，读不到结构的上一次的结构照旧留着。"""
    return kind in DESTRUCTIVE


def is_baseline(old_version: int | None, old_meta: Mapping[str, Any] | None) -> bool:
    """这一次读到的结构只建基线、不和以前比：结构版本还是 0（升级前的老缓存接进来的、从没按对象索引读过），或者上一次的
    结构不在手上（离开过范围，结构清掉了）。"""
    return not old_version or not isinstance(old_meta, Mapping) or not old_meta.get("columns")


@dataclass(frozen=True)
class Diff:
    """一条变化。column 是列级变化的那一列（表注释、键、外键为 None）；before / after 是前后各是什么（列的类型、可空、注释，
    键的列组，外键的两端）；rename 是疑似改名的另一半 {from, to}（只说可能）；key 是键的种类（primary / unique）。"""

    kind: str
    column: str | None = None
    before: Any = None
    after: Any = None
    rename: dict[str, str] | None = None
    key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return DEFAULT_SEVERITY.get(self.kind, "info")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "column": self.column, "before": self.before, "after": self.after}
        if self.rename:
            out["rename"] = dict(self.rename)
        if self.key:
            out["key"] = self.key
        if self.extra:
            out.update(self.extra)
        return out


def _columns(meta: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [c for c in (meta or {}).get("columns") or [] if isinstance(c, Mapping) and c.get("name") is not None]


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _groups(raw: Any) -> set[tuple[str, ...]]:
    return {tuple(str(c) for c in g) for g in raw or [] if isinstance(g, (list, tuple)) and g}


def _fks(raw: Any) -> dict[tuple[Any, ...], dict[str, Any]]:
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for fk in raw or []:
        if not isinstance(fk, Mapping):
            continue
        cols = tuple(str(c) for c in fk.get("columns") or [])
        key = (cols, str(fk.get("to_table") or ""), tuple(str(c) for c in fk.get("to_columns") or []))
        out[key] = {"columns": list(key[0]), "to_table": key[1], "to_columns": list(key[2])}
    return out


def _slots_of(names: list[str], common: set[str]) -> dict[str, tuple[str | None, str | None]]:
    """每一列在列清单里的「位置」：它前后最近的、两次都在的那两列。按下标比会被别处的删列、加列带偏（删了前面一列，后面
    的下标全都前移一位），按前后的邻居比才说得出「还在原来那个位置」。"""
    out: dict[str, tuple[str | None, str | None]] = {}
    for i, name in enumerate(names):
        before = next((n for n in reversed(names[:i]) if n in common), None)
        after = next((n for n in names[i + 1:] if n in common), None)
        out[name] = (before, after)
    return out


def diff_table(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[Diff]:
    """同一张表前后两次的结构 → 变化（列级、键）。顺序：删掉的列、加上的列（各按列清单的顺序），再是各列的类型、可空、
    注释，表注释，主键、唯一约束，外键。两边一样返回空列表。是不是基线（该不该比）由调用方先用 is_baseline 判。"""
    old_cols = _columns(before)
    new_cols = _columns(after)
    old_by = {str(c["name"]): c for c in old_cols}
    new_by = {str(c["name"]): c for c in new_cols}
    removed = [n for n in old_by if n not in new_by]
    added = [n for n in new_by if n not in old_by]
    common = set(old_by) & set(new_by)
    old_pos = _slots_of([str(c["name"]) for c in old_cols], common)
    new_pos = _slots_of([str(c["name"]) for c in new_cols], common)

    # 疑似改名：被删的和新加的类型相同、位置相同（夹在同样的两列之间）；一个被删的只配得上一个新加的、反过来也是，才算
    pairs: dict[str, str] = {}
    for r in removed:
        hits = [a for a in added if old_pos[r] == new_pos[a] and old_by[r].get("type") == new_by[a].get("type")]
        if len(hits) == 1:
            back = [x for x in removed if old_pos[x] == new_pos[hits[0]]
                    and old_by[x].get("type") == new_by[hits[0]].get("type")]
            if back == [r]:
                pairs[r] = hits[0]
    reverse = {a: r for r, a in pairs.items()}

    out: list[Diff] = []
    for name in removed:
        col = old_by[name]
        out.append(Diff(COLUMN_REMOVED, name, before={"type": col.get("type"), "nullable": col.get("nullable", True)},
                        rename={"from": name, "to": pairs[name]} if name in pairs else None))
    for name in added:
        col = new_by[name]
        out.append(Diff(COLUMN_ADDED, name, after={"type": col.get("type"), "nullable": col.get("nullable", True)},
                        rename={"from": reverse[name], "to": name} if name in reverse else None))
    for name, col in new_by.items():
        old = old_by.get(name)
        if old is None:
            continue
        if (old.get("type") or "") != (col.get("type") or ""):
            out.append(Diff(COLUMN_TYPE, name, before=old.get("type"), after=col.get("type")))
        if bool(old.get("nullable", True)) != bool(col.get("nullable", True)):
            out.append(Diff(COLUMN_NULLABLE, name, before=bool(old.get("nullable", True)),
                            after=bool(col.get("nullable", True))))
        if _text(old.get("comment")) != _text(col.get("comment")):
            out.append(Diff(COMMENT, name, before=_text(old.get("comment")), after=_text(col.get("comment"))))
    if _text((before or {}).get("comment")) != _text((after or {}).get("comment")):
        out.append(Diff(COMMENT, None, before=_text((before or {}).get("comment")),
                        after=_text((after or {}).get("comment"))))

    old_pk = [str(c) for c in (before or {}).get("primary_key") or []]
    new_pk = [str(c) for c in (after or {}).get("primary_key") or []]
    if old_pk != new_pk:
        out.append(Diff(KEY, None, before=old_pk or None, after=new_pk or None, key="primary"))
    # 唯一约束、外键：反射读不到时不写这个键，两边都有才比
    if "unique" in (before or {}) and "unique" in (after or {}):
        old_u, new_u = _groups(before.get("unique")), _groups(after.get("unique"))  # type: ignore[union-attr]
        for g in sorted(old_u - new_u):
            out.append(Diff(KEY, g[0] if len(g) == 1 else None, before=list(g), after=None, key="unique"))
        for g in sorted(new_u - old_u):
            out.append(Diff(KEY, g[0] if len(g) == 1 else None, before=None, after=list(g), key="unique"))
    if "foreign_keys" in (before or {}) and "foreign_keys" in (after or {}):
        old_f, new_f = _fks(before.get("foreign_keys")), _fks(after.get("foreign_keys"))  # type: ignore[union-attr]
        for k in sorted(set(old_f) - set(new_f)):
            cols = old_f[k]["columns"]
            out.append(Diff(FK, cols[0] if len(cols) == 1 else None, before=old_f[k], after=None))
        for k in sorted(set(new_f) - set(old_f)):
            cols = new_f[k]["columns"]
            out.append(Diff(FK, cols[0] if len(cols) == 1 else None, before=None, after=new_f[k]))
    return out


def touched_columns(diffs: list[Diff]) -> dict[str, set[str]]:
    """各条变化说到的列，按种类分：{"removed": {…}, "changed": {…}}。removed 是被删的列（含疑似改名的旧名），changed 是
    类型、可空变了的列。影响面按它找目录里说到这些列的项。"""
    removed = {d.column for d in diffs if d.kind == COLUMN_REMOVED and d.column}
    changed = {d.column for d in diffs if d.kind in (COLUMN_TYPE, COLUMN_NULLABLE) and d.column}
    return {"removed": removed, "changed": changed}


__all__ = [
    "COLUMN_ADDED", "COLUMN_NULLABLE", "COLUMN_REMOVED", "COLUMN_TYPE", "COMMENT", "DEFAULT_SEVERITY", "DESTRUCTIVE",
    "Diff", "FK", "KEY", "SCOPE", "STRUCT_KINDS", "TABLE_ADDED", "TABLE_LEFT_SCOPE", "TABLE_REFLECT_FAILED",
    "TABLE_REMOVED", "diff_table", "is_baseline", "is_deletion", "touched_columns",
]
