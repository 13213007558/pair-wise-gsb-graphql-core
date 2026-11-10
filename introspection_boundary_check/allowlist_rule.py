"""字段/操作授权的正确层级：自定义 ValidationRule 做操作白名单。

这一层回答“谁可以执行哪个操作/取哪个字段”，与“是否允许 __schema 内省”是两件事。
关闭内省永远替代不了这里；即便内省关闭，未在白名单内的操作也会在验证阶段被拒。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphql import GraphQLError
from graphql.validation import ValidationRule

if TYPE_CHECKING:
    from graphql.language import FieldNode, Node, OperationDefinitionNode


# 外部入口只暴露这些根操作；internalAuditLog 不在其中。
EXTERNAL_ALLOWED_ROOT_FIELDS = frozenset({"viewer", "user", "users"})


class AllowedOperationsRule(ValidationRule):
    """仅允许白名单内的根 Query 字段，可按调用方身份传入不同集合。"""

    allowed_root_fields: frozenset[str] = EXTERNAL_ALLOWED_ROOT_FIELDS

    def enter_operation_definition(
        self, node: OperationDefinitionNode, *_args: Any
    ) -> None:
        if node.operation.value != "query":
            self.report_error(
                GraphQLError(
                    f"Operation type '{node.operation.value}' is not permitted "
                    "on this entrypoint.",
                    node,
                )
            )

    def enter_field(
        self,
        node: FieldNode,
        _key: Any,
        _parent: Any,
        _path: Any,
        ancestors: list[Node],
    ) -> None:
        # 只约束根字段：字段的祖父节点是 OperationDefinition 时即为根选择。
        is_root = (
            len(ancestors) >= 2
            and getattr(ancestors[-2], "kind", None) == "operation_definition"
        )
        if is_root and node.name.value not in self.allowed_root_fields:
            self.report_error(
                GraphQLError(
                    f"Root field '{node.name.value}' is not permitted on this "
                    "entrypoint.",
                    node,
                )
            )
