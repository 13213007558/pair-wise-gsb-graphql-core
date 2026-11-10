"""关闭内省之后，外部仍然能拿到的 schema / 数据信息（可重放）。

全部基于 EXTERNAL_ENTRY（NoSchemaIntrospection + hide_suggestions）。
"""
from __future__ import annotations

from graphql import execute, parse, validate

from configs import EXTERNAL_ENTRY
from schema import schema


def run(query, variables=None, show_suggestion=False):
    preset = EXTERNAL_ENTRY.preset
    # show_suggestion 时临时打开建议词，模拟只关内省、未开 hide_suggestions 的常见配置
    hide = preset.hide_suggestions and not show_suggestion
    document = parse(query)
    errors = validate(schema, document, preset.rules, hide_suggestions=hide)
    if errors:
        return {"stage": "validate",
                "messages": [" ".join(e.message.split()) for e in errors]}
    result = execute(schema, document, variable_values=variables,
                     hide_suggestions=hide)
    return {"stage": "execute", "data": result.data,
            "messages": [" ".join(e.message.split()) for e in (result.errors or [])]}


CASES = [
    ("R1", "__type 内省入口（返回 __Type，应被规则拦截）",
     '{ __type(name: "User") { name fields { name } } }', None, False),
    ("R2", "未授权业务字段 internalAuditLog 仍可直接查询（规则不做字段授权）",
     "{ internalAuditLog }", None, False),
    ("R3", "__typename 判别具体对象类型",
     "{ viewer { __typename name } users { __typename } }", None, False),
    ("R4", "错误消息回显父类型名（无需内省即可确认 Query/User 存在）",
     "{ viewer { nope } }", None, False),
    ("R5", "仅关内省、未 hide_suggestions 时，错误消息直接给出候选字段名",
     "{ viewer { nam } }", None, True),
    ("R6", "通过别名一次性探测多个字段是否存在（错误按字段返回）",
     "{ viewer { a: id b: name c: nonexistent } }", None, False),
]


def main():
    for key, title, query, variables, sug in CASES:
        print(f"\n[{key}] {title}")
        print("  query:", query)
        print("  result:", run(query, variables, sug))


if __name__ == "__main__":
    main()
