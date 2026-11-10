"""同一 schema、两个入口：验证规则与错误策略必须按请求隔离，不能复用已验证文档。

演示四件事：
  A. 一条公开查询，内部/外部入口都正常返回（不靠禁止全部查询达成）。
  B. __schema 在内部入口放行、在外部入口被各自规则拦截（规则不串用）。
  C. 反例：把“内部入口已验证通过”的文档直接交给外部执行，外部规则被绕过。
  D. 正确做法：缓存只存解析结果(AST)，每个入口对同一 AST 用自己的规则重新验证。
"""
from __future__ import annotations

from graphql import execute, parse, validate

from configs import EXTERNAL_ENTRY, INTERNAL_ENTRY
from schema import schema

PUBLIC_QUERY = "query($id: Int){ user(id: $id) { id name role } }"
PUBLIC_VARS = {"id": 2}
INTROSPECTION_QUERY = "{ __schema { queryType { name } } }"


def serve_correctly(entry, query, variables=None):
    """每个入口独立完成 parse -> validate(自己的规则) -> execute。"""
    document = parse(query)
    errors = validate(
        schema,
        document,
        entry.preset.rules,
        hide_suggestions=entry.preset.hide_suggestions,
    )
    if errors:
        return {"blocked_at": "validate",
                "errors": [" ".join(e.message.split()) for e in errors]}
    result = execute(
        schema, document, variable_values=variables,
        hide_suggestions=entry.preset.hide_suggestions,
    )
    return {"blocked_at": None, "data": result.data,
            "errors": [e.message for e in (result.errors or [])]}


def demo_a_public_query_both_entries():
    print("\n[A] 公开查询在两个入口都正常工作")
    for entry in (INTERNAL_ENTRY, EXTERNAL_ENTRY):
        out = serve_correctly(entry, PUBLIC_QUERY, PUBLIC_VARS)
        print(f"  {entry.name:8s} -> {out}")


def demo_b_introspection_per_entry():
    print("\n[B] __schema 按入口各自的规则处理（不串用）")
    for entry in (INTERNAL_ENTRY, EXTERNAL_ENTRY):
        out = serve_correctly(entry, INTROSPECTION_QUERY)
        print(f"  {entry.name:8s} -> blocked_at={out['blocked_at']} "
              f"first_error={out['errors'][0] if out['errors'] else None}")


def demo_c_unsafe_shared_validated_document():
    print("\n[C] 反例：复用内部入口已验证的文档 -> 外部内省规则被绕过")
    # 内部入口完成 parse + validate（内部无内省规则）
    document = parse(INTROSPECTION_QUERY)
    internal_errors = validate(schema, document, INTERNAL_ENTRY.preset.rules)
    assert internal_errors == []
    # 危险做法：外部入口拿到“已验证文档”后直接执行，跳过自己的规则
    leaked = execute(schema, document)
    print("  外部入口直接执行内部已验证文档，泄露 data 键：",
          list(leaked.data["__schema"].keys()))
    print("  queryType =", leaked.data["__schema"]["queryType"])


def demo_d_safe_ast_cache():
    print("\n[D] 正确做法：只缓存 AST，每个入口用自己的规则重新验证")
    # 解析结果与规则无关，可安全共享
    shared_ast = parse(INTROSPECTION_QUERY)
    for entry in (INTERNAL_ENTRY, EXTERNAL_ENTRY):
        errors = validate(
            schema, shared_ast, entry.preset.rules,
            hide_suggestions=entry.preset.hide_suggestions,
        )
        verdict = "放行" if not errors else f"拦截({len(errors)} 条)"
        print(f"  {entry.name:8s} 对同一 AST 重新验证 -> {verdict}")


if __name__ == "__main__":
    demo_a_public_query_both_entries()
    demo_b_introspection_per_entry()
    demo_c_unsafe_shared_validated_document()
    demo_d_safe_ast_cache()
