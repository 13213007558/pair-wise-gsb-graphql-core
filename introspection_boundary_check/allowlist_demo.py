"""对比：内省规则管不到 internalAuditLog，操作白名单规则可以。"""
from graphql import parse, validate
from graphql.validation import NoSchemaIntrospectionCustomRule, specified_rules

from allowlist_rule import AllowedOperationsRule
from schema import schema

external_rules = [*specified_rules, NoSchemaIntrospectionCustomRule,
                  AllowedOperationsRule]

CASES = {
    "白名单内公开查询": "{ viewer { name } }",
    "白名单外内部字段": "{ internalAuditLog }",
    "内省查询": "{ __schema { queryType { name } } }",
}

for title, query in CASES.items():
    errors = validate(schema, parse(query), external_rules, hide_suggestions=True)
    verdict = "放行" if not errors else " / ".join(
        " ".join(e.message.split()) for e in errors
    )
    print(f"{title:18s} -> {verdict}")
