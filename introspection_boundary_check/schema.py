"""同一个被内部入口与外部入口共享的业务 schema。

刻意同时包含：
- 公开字段（viewer/user/users）
- 枚举与输入参数（用于触发 enum / 变量类型检查）
- 一个仅内部应可达的字段 internalAuditLog（内省开关对它没有任何保护作用）
"""
from __future__ import annotations

from enum import Enum

from graphql import (
    GraphQLArgument,
    GraphQLEnumType,
    GraphQLField,
    GraphQLInt,
    GraphQLList,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
)


class Role(str, Enum):
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    GUEST = "GUEST"


RoleEnum = GraphQLEnumType("Role", Role)

_USERS = {
    1: {
        "id": 1,
        "name": "Ada",
        "email": "ada@example.com",
        "role": Role.ADMIN,
    },
    2: {
        "id": 2,
        "name": "Lin",
        "email": "lin@example.com",
        "role": Role.MEMBER,
    },
}


def _resolve_viewer(_root, _info):
    return _USERS[1]


def _resolve_user(_root, _info, id):
    return _USERS.get(id)


def _resolve_users(_root, _info, role=None):
    if role is None:
        return list(_USERS.values())
    return [u for u in _USERS.values() if u["role"] == role]


def _resolve_audit_log(_root, _info):
    return "secret-audit-entries"


UserType = GraphQLObjectType(
    "User",
    lambda: {
        "id": GraphQLField(GraphQLInt),
        "name": GraphQLField(GraphQLString),
        "email": GraphQLField(GraphQLString),
        "role": GraphQLField(RoleEnum),
    },
)

QueryType = GraphQLObjectType(
    "Query",
    {
        "viewer": GraphQLField(UserType, resolve=_resolve_viewer),
        "user": GraphQLField(
            UserType,
            args={"id": GraphQLArgument(GraphQLInt)},
            resolve=_resolve_user,
        ),
        "users": GraphQLField(
            GraphQLList(UserType),
            args={"role": GraphQLArgument(RoleEnum)},
            resolve=_resolve_users,
        ),
        # 业务上只允许内部入口访问，但内省开关并不会拦截它
        "internalAuditLog": GraphQLField(GraphQLString, resolve=_resolve_audit_log),
    },
)

schema = GraphQLSchema(query=QueryType)
