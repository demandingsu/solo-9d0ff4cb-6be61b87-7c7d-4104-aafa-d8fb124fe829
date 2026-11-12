"""基于标准库 html.parser 的轻量 DOM 解析。

仅支持本项目报价站所需的查询能力（按标签/属性查找、取文本、取属性），
不引入第三方依赖，保证在最小镜像与离线环境下可运行。
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional

# 这些元素没有闭合标签，解析时不应等待结束标记。
_VOID_TAGS = {"meta", "link", "br", "hr", "img", "input", "area", "base", "col", "embed", "source", "track", "wbr"}


class Element:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Optional[dict] = None, parent: Optional["Element"] = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []  # list[Element | str]
        self.parent = parent

    def find_all(self, tag: Optional[str] = None, attr: Optional[str] = None,
                 attr_value: Optional[str] = None) -> list["Element"]:
        result: list[Element] = []
        stack = [self]
        while stack:
            node = stack.pop()
            if not isinstance(node, Element):
                continue
            if tag is None or node.tag == tag:
                if attr is None or (attr in node.attrs and
                                    (attr_value is None or node.attrs[attr] == attr_value)):
                    result.append(node)
            stack.extend(reversed(node.children))
        return result

    def find(self, tag: Optional[str] = None, attr: Optional[str] = None,
             attr_value: Optional[str] = None) -> Optional["Element"]:
        found = self.find_all(tag=tag, attr=attr, attr_value=attr_value)
        return found[0] if found else None

    @property
    def text(self) -> str:
        parts: list[str] = []
        stack: list = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                parts.append(node)
            else:
                stack.extend(reversed(node.children))
        return "".join(parts).strip()

    def get(self, name: str, default=None):
        return self.attrs.get(name, default)


class _DOMBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("[document]")
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        element = Element(tag, dict(attrs), parent=self._stack[-1])
        self._stack[-1].children.append(element)
        if tag not in _VOID_TAGS:
            self._stack.append(element)

    def handle_startendtag(self, tag, attrs):
        element = Element(tag, dict(attrs), parent=self._stack[-1])
        self._stack[-1].children.append(element)

    def handle_endtag(self, tag):
        # 容错：向上找到最近的同名未闭合标签再弹栈。
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if data:
            self._stack[-1].children.append(data)


def parse_html(markup: str) -> Element:
    builder = _DOMBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root
