#!/usr/bin/env python3
"""文档检查：英文文档不许有中文、链接和锚点都要通、中英两套文档一一对应。

    python3 scripts/check-docs.py

1. README.md 和 docs/en/** 里不许出现中日韩字符和全角符号（代码块也算），范围是
   U+3000–U+303F、U+3400–U+4DBF、U+4E00–U+9FFF、U+F900–U+FAFF、U+FF00–U+FFEF。
2. README.md、README.zh-CN.md、docs/**/*.md、deploy/README.md 里的相对链接：文件要存在，带 #锚点 的要对得上
   目标文件的标题（GitHub 的规则：小写，去掉字母、数字、空格、连字符、下划线以外的字符，空格换成 -，同名标题
   依次加 -1、-2）。代码块里的标题不算标题，代码块和行内代码里的链接不算链接。
3. docs/zh-CN 和 docs/en 的文件名一一对应，每一对的标题层级序列相同。

有问题时逐条列出、退出码 1。
"""
import os
import re
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CJK = re.compile("[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LINK = re.compile(r"\]\(([^)\s]+)\)")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")


def md_files():
    out = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "README.zh-CN.md"),
           os.path.join(ROOT, "deploy", "README.md")]
    for base, _dirs, files in os.walk(os.path.join(ROOT, "docs")):
        out += [os.path.join(base, f) for f in sorted(files) if f.endswith(".md")]
    return [p for p in out if os.path.exists(p)]


def slug_of(text):
    text = CODE_SPAN.sub(lambda m: m.group(2), text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    kept = [ch for ch in text.strip().lower()
            if ch in " -" or unicodedata.category(ch)[0] in "LMN" or unicodedata.category(ch) == "Pc"]
    return "".join(kept).replace(" ", "-")


def scan(path):
    """返回 (锚点集合, 标题层级序列, 链接 [(行号, 目标)])。"""
    anchors, seen, levels, links = set(), {}, [], []
    fence = None
    with open(path, encoding="utf-8") as f:
        for no, line in enumerate(f, 1):
            s = line.strip()
            m = re.match(r"^(`{3,}|~{3,})", s)
            if m:
                if fence is None:
                    fence = m.group(1)[0] * 3
                elif s.startswith(fence):
                    fence = None
                continue
            if fence:
                continue
            h = HEADING.match(line.rstrip("\n"))
            if h:
                levels.append(len(h.group(1)))
                base = slug_of(h.group(2))
                slug = base
                while slug in seen:
                    seen[base] += 1
                    slug = "%s-%d" % (base, seen[base])
                seen[slug] = 0
                anchors.add(slug)
            for target in LINK.findall(CODE_SPAN.sub("", line)):
                links.append((no, target))
    return anchors, levels, links


def main():
    problems = []
    rel = lambda p: os.path.relpath(p, ROOT)

    # 1. 英文文档里的中文
    english = [os.path.join(ROOT, "README.md")]
    for base, _dirs, files in os.walk(os.path.join(ROOT, "docs", "en")):
        english += [os.path.join(base, f) for f in sorted(files) if f.endswith(".md")]
    cjk_hits = 0
    for path in english:
        with open(path, encoding="utf-8") as f:
            for no, line in enumerate(f, 1):
                for m in CJK.finditer(line):
                    cjk_hits += 1
                    problems.append("%s:%d:%d 英文文档里有中文字符 %r" % (rel(path), no, m.start() + 1, m.group(0)))

    # 2. 链接和锚点
    cache = {}

    def info(p):
        if p not in cache:
            cache[p] = scan(p)
        return cache[p]

    link_count = 0
    files = md_files()
    for path in files:
        anchors, _levels, links = info(path)
        for no, target in links:
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):     # http:、mailto: 之类的外链不查
                continue
            link_count += 1
            file_part, _, frag = target.partition("#")
            dest = os.path.normpath(os.path.join(os.path.dirname(path), file_part)) if file_part else path
            if not os.path.exists(dest):
                problems.append("%s:%d 链接的文件不存在：%s" % (rel(path), no, target))
            elif frag and dest.endswith(".md") and frag not in info(dest)[0]:
                problems.append("%s:%d 锚点对不上标题：%s" % (rel(path), no, target))

    # 3. 中英两套文档一一对应
    zh_dir, en_dir = os.path.join(ROOT, "docs", "zh-CN"), os.path.join(ROOT, "docs", "en")
    zh = sorted(f for f in os.listdir(zh_dir) if f.endswith(".md")) if os.path.isdir(zh_dir) else []
    en = sorted(f for f in os.listdir(en_dir) if f.endswith(".md")) if os.path.isdir(en_dir) else []
    for name in sorted(set(zh) ^ set(en)):
        problems.append("docs/zh-CN 和 docs/en 的文件对不上：%s 只在一边" % name)
    pairs = sorted(set(zh) & set(en)) + [None]
    for name in pairs:
        if name is None:
            a, b = os.path.join(ROOT, "README.zh-CN.md"), os.path.join(ROOT, "README.md")
        else:
            a, b = os.path.join(zh_dir, name), os.path.join(en_dir, name)
        if info(a)[1] != info(b)[1]:
            problems.append("标题层级对不上：%s %s ↔ %s %s" % (rel(a), info(a)[1], rel(b), info(b)[1]))

    for p in problems:
        print(p)
    print("英文文档 %d 个，中文字符 %d 处；Markdown 文件 %d 个，相对链接 %d 条；中英文档 %d 对；问题 %d 条"
          % (len(english), cjk_hits, len(files), link_count, len(pairs), len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
