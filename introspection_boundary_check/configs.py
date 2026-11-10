"""三套可对照的验证/错误策略，以及内部、外部两个入口的正确与错误接法。

关键事实（graphql-core 3.3.2）：
- NoSchemaIntrospectionCustomRule 是 *额外* 规则，必须追加到 specified_rules，
  单独传入会把其它 spec 规则全部丢掉。
- hide_suggestions 只是错误消息层的开关，同时作用于 validation 与 execution
  （变量/enum 强转），与内省规则相互正交。
- 规则与 hide_suggestions 是“每次请求”的参数，不绑定在 schema 上。
"""
from __future__ import annotations

from dataclasses import dataclass

from graphql.validation import (
    NoSchemaIntrospectionCustomRule,
    specified_rules,
)


@dataclass(frozen=True)
class ValidationPreset:
    label: str
    rules: list[type] | None
    hide_suggestions: bool


# 1) 普通配置：仅 spec 规则。注意 specified_rules 已含 MaxIntrospectionDepthRule，
#    但它只限制内省深度，并不禁止 __schema。
PLAIN = ValidationPreset(
    label="plain (specified_rules, suggestions on)",
    rules=list(specified_rules),
    hide_suggestions=False,
)

# 2) 关闭内省：在完整 spec 规则之上追加自定义规则，建议词仍开启。
NO_INTROSPECTION = ValidationPreset(
    label="no-introspection (specified_rules + NoSchemaIntrospectionCustomRule)",
    rules=[*specified_rules, NoSchemaIntrospectionCustomRule],
    hide_suggestions=False,
)

# 3) 关闭内省 + 隐藏建议词：字段/参数/enum 的 “Did you mean ...” 被去掉。
NO_INTROSPECTION_HIDDEN = ValidationPreset(
    label="no-introspection + hide_suggestions",
    rules=[*specified_rules, NoSchemaIntrospectionCustomRule],
    hide_suggestions=True,
)

PRESETS = (PLAIN, NO_INTROSPECTION, NO_INTROSPECTION_HIDDEN)


# 入口配置：同一 schema，两套独立策略。
@dataclass(frozen=True)
class EntryConfig:
    name: str
    preset: ValidationPreset


INTERNAL_ENTRY = EntryConfig(name="internal", preset=PLAIN)
EXTERNAL_ENTRY = EntryConfig(name="external", preset=NO_INTROSPECTION_HIDDEN)
