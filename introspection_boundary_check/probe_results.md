# 分阶段探针结果（由 probes.py 生成，勿手改）

## 预设：plain (specified_rules, suggestions on)

| 探针 | 解析 | 验证 | 执行 | 各阶段错误消息（截断 220 字符） |
|---|---|---|---|---|
| P1 | OK | OK | OK | （无错误） |
| P2 | OK | OK | OK | （无错误） |
| P3 | OK | ERROR×1 | —（未到达） | Cannot query field 'nam' on type 'User'. Did you mean 'name'? |
| P4 | OK | ERROR×1 | —（未到达） | Unknown argument 'uid' on field 'Query.user'. Did you mean 'id'? |
| P5 | OK | ERROR×1 | —（未到达） | Value 'ADMIM' does not exist in 'Role' enum. Did you mean the enum value 'ADMIN'? |
| P6 | OK | OK | ERROR×1 | Variable '$r' has invalid value: Value 'ADMIM' does not exist in 'Role' enum. Did you mean the enum value 'ADMIN'? |
| P7 | OK | ERROR×1 | —（未到达） | Variable '$count' of type 'String' used in position expecting type 'Int'. |
| P8 | OK | OK | ERROR×1 | Variable '$id' has invalid value: Int cannot represent non-integer value: 'not-an-int' |
| P9 | ERROR | —（未到达） | —（未到达） | Syntax Error: Expected Name, found <EOF>. |
| P10 | OK | OK | OK | （无错误） |

## 预设：no-introspection (specified_rules + NoSchemaIntrospectionCustomRule)

| 探针 | 解析 | 验证 | 执行 | 各阶段错误消息（截断 220 字符） |
|---|---|---|---|---|
| P1 | OK | ERROR×3 | —（未到达） | GraphQL introspection has been disabled, but the requested query contained the field '__schema'. <br> GraphQL introspection has been disabled, but the requested query contained the field 'queryType'. <br> GraphQL intr... |
| P2 | OK | OK | OK | （无错误） |
| P3 | OK | ERROR×1 | —（未到达） | Cannot query field 'nam' on type 'User'. Did you mean 'name'? |
| P4 | OK | ERROR×1 | —（未到达） | Unknown argument 'uid' on field 'Query.user'. Did you mean 'id'? |
| P5 | OK | ERROR×1 | —（未到达） | Value 'ADMIM' does not exist in 'Role' enum. Did you mean the enum value 'ADMIN'? |
| P6 | OK | OK | ERROR×1 | Variable '$r' has invalid value: Value 'ADMIM' does not exist in 'Role' enum. Did you mean the enum value 'ADMIN'? |
| P7 | OK | ERROR×1 | —（未到达） | Variable '$count' of type 'String' used in position expecting type 'Int'. |
| P8 | OK | OK | ERROR×1 | Variable '$id' has invalid value: Int cannot represent non-integer value: 'not-an-int' |
| P9 | ERROR | —（未到达） | —（未到达） | Syntax Error: Expected Name, found <EOF>. |
| P10 | OK | OK | OK | （无错误） |

## 预设：no-introspection + hide_suggestions

| 探针 | 解析 | 验证 | 执行 | 各阶段错误消息（截断 220 字符） |
|---|---|---|---|---|
| P1 | OK | ERROR×3 | —（未到达） | GraphQL introspection has been disabled, but the requested query contained the field '__schema'. <br> GraphQL introspection has been disabled, but the requested query contained the field 'queryType'. <br> GraphQL intr... |
| P2 | OK | OK | OK | （无错误） |
| P3 | OK | ERROR×1 | —（未到达） | Cannot query field 'nam' on type 'User'. |
| P4 | OK | ERROR×1 | —（未到达） | Unknown argument 'uid' on field 'Query.user'. |
| P5 | OK | ERROR×1 | —（未到达） | Value 'ADMIM' does not exist in 'Role' enum. |
| P6 | OK | OK | ERROR×1 | Variable '$r' has invalid value: Value 'ADMIM' does not exist in 'Role' enum. |
| P7 | OK | ERROR×1 | —（未到达） | Variable '$count' of type 'String' used in position expecting type 'Int'. |
| P8 | OK | OK | ERROR×1 | Variable '$id' has invalid value: Int cannot represent non-integer value: 'not-an-int' |
| P9 | ERROR | —（未到达） | —（未到达） | Syntax Error: Expected Name, found <EOF>. |
| P10 | OK | OK | OK | （无错误） |
