# 关闭 GraphQL 内省 ≠ 隐藏 Schema：一次可重放的边界检查

基于 `graphql-core 3.3.2`（本仓库 `src/graphql`），用同一个业务 schema 对三套配置做
**解析（parse）→ 验证（validate）→ 执行（execute）** 三阶段探针，并演示内部/外部入口的规则隔离。
所有输出均由脚本生成，可用 `./run_all.sh` 一键重放。

## 1. 结论先行：那句话的实际边界

> “对外接口关闭了内省，所以外部无法了解 schema。”

这句话**不成立**。`NoSchemaIntrospectionCustomRule` 的实现（`src/graphql/validation/rules/custom/no_schema_introspection.py`）
只做一件事：当字段的返回类型是内省类型（`__Schema` / `__Type`）时报错。因此它的实际边界是：

- **只拦截 `__schema` 和 `__type` 这两个内省元字段**（以及其下返回 `__Type` 的子选择），不触及任何业务字段。
- 它是一条**验证阶段**规则，不做字段级授权、不改变解析、不改变执行期的数据可见性。
- schema 结构仍可通过**普通业务查询 + 校验错误消息 + `__typename`** 被逐字段推断出来。
- `hide_suggestions` 只是**错误消息层**开关，去掉 “Did you mean ...” 候选词；它不改变规则判定，也不隐藏错误里回显的类型名/字段名。

## 2. 三套配置（见 `configs.py`）

```python
from graphql.validation import NoSchemaIntrospectionCustomRule, specified_rules

# 1) 普通配置：仅 spec 规则（其中已含 MaxIntrospectionDepthRule，但不禁止 __schema）
PLAIN = dict(rules=list(specified_rules), hide_suggestions=False)

# 2) 关闭内省：必须“追加”到完整 spec 规则，而不是替换
NO_INTROSPECTION = dict(
    rules=[*specified_rules, NoSchemaIntrospectionCustomRule],
    hide_suggestions=False,
)

# 3) 关闭内省 + 隐藏建议词（两个开关相互正交）
NO_INTROSPECTION_HIDDEN = dict(
    rules=[*specified_rules, NoSchemaIntrospectionCustomRule],
    hide_suggestions=True,
)
```

两个易踩的坑：

- `validate(schema, doc, [NoSchemaIntrospectionCustomRule])` 只传单条规则会让
  `specified_rules` 全部失效——未知字段、错误参数等将不再被校验。必须用列表相加追加。
- 规则与 `hide_suggestions` 是**每次请求**的参数（`validate(..., hide_suggestions=...)`、
  `execute(..., hide_suggestions=...)`），并不绑定在 `GraphQLSchema` 对象上。

## 3. 错误输出表（分阶段，来自 `probe_results.md`）

阶段归属规则：语法错在 parse；字段/参数/enum 字面量/变量静态类型在 validate；
变量运行期强转（enum 值、标量 coercion）在 execute。校验失败的文档不会进入执行。

| 探针 | plain（关内省前） | no-introspection | + hide_suggestions | 阶段 |
|---|---|---|---|---|
| P1 `__schema{...}` | **成功返回完整 schema** | 验证拦截 ×3 | 验证拦截 ×3（消息不变） | validate |
| P2 `__typename`（根/对象） | 成功 | **成功，照常返回类型名** | 成功 | execute |
| P3 未知字段 `viewer{ nam }` | `Cannot query field 'nam' on type 'User'. Did you mean 'name'?` | 同左 | `...on type 'User'.`（无候选词） | validate |
| P4 未知参数 `user(uid:1)` | `Unknown argument 'uid' ... Did you mean 'id'?` | 同左 | 无候选词，仍回显 `Query.user` | validate |
| P5 非法 enum 字面量 `role: ADMIM` | `Value 'ADMIM' does not exist in 'Role' enum. Did you mean ... 'ADMIN'?` | 同左 | 无候选词，仍回显 enum 名 `Role` | validate |
| P6 非法 enum **变量值** | 通过校验，执行期 `Variable '$r' has invalid value: ...ADMIM... Did you mean 'ADMIN'?` | 同左 | 执行期报错，无候选词 | **execute** |
| P7 变量类型不匹配 `$count:String` 传给 `Int` | `Variable '$count' of type 'String' used in position expecting type 'Int'.` | 同左 | 同左（本就无建议词） | validate |
| P8 变量运行期强转失败 `$id:Int` 传 `"not-an-int"` | 通过校验，执行期 `Int cannot represent non-integer value` | 同左 | 同左 | **execute** |
| P9 语法错误 `{ viewer {` | `Syntax Error: Expected Name, found <EOF>.` | 同左 | 同左（parse 与两开关无关） | **parse** |
| P10 正常公开查询 | 成功 | 成功 | 成功 | execute |

读表要点：

- 三套配置里，**只有 P1（和 `__type`）的行为发生了变化**；P2–P9 的阶段判定完全一致。
- `hide_suggestions` 仅改变 P3/P4/P5/P6 的文案，P7/P8/P9 本就不含建议词，P1 的内省拦截消息也不含建议词。
- P6/P8 证明：校验通过 ≠ 执行安全，变量值合法性要到执行期才暴露，且错误同样回显类型信息。

## 4. 同一 schema、两个入口：规则不能因复用文档而串用

`entrypoints.py` 用同一 `schema` 构建 `internal`（PLAIN）与 `external`（NO_INTROSPECTION_HIDDEN）两个入口。

- **A 公开查询两入口都正常**：`user(id:2)` 在 internal/external 返回完全相同的数据，
  说明无需靠“禁止全部查询”来实现收敛。
- **B 同一条 `__schema`**：internal 验证放行，external 验证拦截——差异完全来自各入口传入的规则。
- **C 反例（必须避免）**：把 internal 已 `validate` 通过的文档缓存后，external 直接 `execute`，
  跳过了自己的规则，`__schema.queryType.name = 'Query'` 被外部拿到。
- **D 正确做法**：缓存只存与规则无关的**解析 AST**；每个入口对同一 AST 用自己的规则重新 `validate`，
  结果是 internal 放行、external 拦截，互不污染。

原则：**可共享的是 schema 和 AST，不可共享的是“已验证”这一结论**；验证规则、`hide_suggestions`、
错误策略必须按入口、按请求独立应用。

## 5. 关闭内省后仍然存在的信息范围（`residual_exposure.py`）

在最严格的 external 预设（关内省 + hide_suggestions）下实测仍可获得：

- **R1** `__type(name:"User")` 被拦截——两条内省入口确实都被挡住。
- **R2** 业务字段 `internalAuditLog` 可被外部直接查询并返回 `secret-audit-entries`：
  内省规则不做任何字段授权，未公开字段只要名字被猜到就能取数。
- **R3** `__typename` 照常回显具体对象类型（`User`），可用于判别 interface/union 的实际类型。
- **R4** 校验错误回显父类型名：`Cannot query field 'nope' on type 'User'.`，
  无需内省即可确认 `Query`/`User` 等类型存在。
- **R5** 若只关内省、未开 `hide_suggestions`，错误直接给出候选字段
  （`Did you mean 'name'?`），等于逐字段提示 schema。
- **R6** 别名可一次探测多个字段，错误按字段返回，可批量枚举字段是否存在（字段枚举/“fuzzing”）。
- 此外 P5/P7 的错误回显 enum 名 `Role`、参数期望类型 `Int`，参数与类型结构同样可推断。

即：**内省关掉的是“一次性导出完整 schema”的便捷通道，不是 schema 的保密性。**

## 6. 要更强的接口收敛，应在哪层限制可执行操作

不要把关闭内省包装成字段授权。收敛应在**验证阶段的自定义 `ValidationRule`（操作/字段白名单）**，
必要时再叠加执行层的 resolver 级鉴权。`allowlist_rule.py` 给出可运行示例：

```python
from graphql.validation import ValidationRule, specified_rules, NoSchemaIntrospectionCustomRule
from graphql import GraphQLError

class AllowedOperationsRule(ValidationRule):
    allowed_root_fields = frozenset({"viewer", "user", "users"})  # 按调用方身份传入

    def enter_field(self, node, _key, _parent, _path, ancestors):
        is_root = len(ancestors) >= 2 and \
            getattr(ancestors[-2], "kind", None) == "operation_definition"
        if is_root and node.name.value not in self.allowed_root_fields:
            self.report_error(GraphQLError(
                f"Root field '{node.name.value}' is not permitted on this entrypoint.", node))

external_rules = [*specified_rules, NoSchemaIntrospectionCustomRule, AllowedOperationsRule]
```

实测对照（`allowlist_demo.py`）：白名单内 `viewer` 放行；`internalAuditLog` 被
`Root field 'internalAuditLog' is not permitted` 拦截；`__schema` 同时被内省规则和白名单拦截。

分层建议：

- **验证层白名单**：限制可执行的 operation / 根字段 / mutation，按入口或身份给不同集合，fail-closed。
- **resolver/数据层鉴权**：对象级、字段级、行级授权（如 `email` 对某些身份脱敏），这是白名单管不到的粒度。
- **错误策略**：外部入口固定 `hide_suggestions=True`，并考虑统一错误文案，减少类型/字段回显。
- **成本与复杂度控制**：配合 `max_errors`、token/深度限制等防探测与 DoS，与授权是独立议题。
- 内省开关保留为纵深防御的一环，但**不得作为“schema 已保密/字段已授权”的依据**。

## 7. 复现方式

```bash
cd introspection_boundary_check
./run_all.sh                 # 依次跑探针、双入口、信息范围、白名单
# 或分步：
../.venv/bin/python probes.py             # 生成 probe_results.md
../.venv/bin/python entrypoints.py
../.venv/bin/python residual_exposure.py
../.venv/bin/python allowlist_demo.py
```

文件清单：`schema.py`（共享 schema）、`configs.py`（三套预设+两入口）、
`probes.py`/`probe_results.md`（分阶段探针）、`entrypoints.py`（隔离与复用反例）、
`residual_exposure.py`（残余信息）、`allowlist_rule.py`/`allowlist_demo.py`（授权层示例）。
