"""分阶段探针：对每个预设分别走 parse -> validate -> execute，记录各阶段错误。

可重放：`python probes.py` 会把结果写到 probe_results.md，stdout 同步打印。
重点不是“__schema 是否被拒”，而是每个阶段实际泄露了什么。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from graphql import GraphQLError, execute, parse
from graphql.language import DocumentNode

from configs import PRESETS, ValidationPreset
from schema import schema


@dataclass(frozen=True)
class Probe:
    key: str
    title: str
    query: str
    variables: dict | None = None


PROBES: tuple[Probe, ...] = (
    Probe("P1", "完整内省 __schema",
          "{ __schema { queryType { name } types { name } } }"),
    Probe("P2", "元字段 __typename（根 + 对象）",
          "{ __typename viewer { __typename name } }"),
    Probe("P3", "未知字段（带近似名，触发建议词）",
          "{ viewer { nam } }"),
    Probe("P4", "未知参数（触发参数建议词）",
          "{ user(uid: 1) { id } }"),
    Probe("P5", "非法 enum 字面量（静态校验阶段）",
          "{ users(role: ADMIM) { name } }"),
    Probe("P6", "非法 enum 变量值（执行期强转）",
          "query($r: Role){ users(role: $r) { name } }",
          {"r": "ADMIM"}),
    Probe("P7", "变量类型不匹配（静态校验阶段）",
          "query($count: String){ user(id: $count) { id } }"),
    Probe("P8", "变量运行期强转失败（通过校验、执行报错）",
          "query($id: Int){ user(id: $id) { id name } }",
          {"id": "not-an-int"}),
    Probe("P9", "语法错误（解析阶段）",
          "{ viewer { "),
    Probe("P10", "正常公开查询（对照组，应成功）",
          "query($id: Int){ user(id: $id) { id name role } }",
          {"id": 1}),
)


@dataclass
class PhaseResult:
    phase: str
    ok: bool
    messages: list[str] = field(default_factory=list)
    data: object = None

    @property
    def stage_label(self) -> str:
        if self.phase == "parse":
            return "parse"
        if self.phase == "validate":
            return "validate"
        return "execute"


def _msg(err: GraphQLError) -> str:
    # 规范化换行，便于放进表格
    return " ".join(err.message.split())


def run_preset(probe: Probe, preset: ValidationPreset) -> list[PhaseResult]:
    results: list[PhaseResult] = []

    # ---- 解析阶段（与规则、hide_suggestions 完全无关）----
    document: DocumentNode | None = None
    try:
        document = parse(probe.query)
        results.append(PhaseResult("parse", True))
    except GraphQLError as exc:
        results.append(PhaseResult("parse", False, [_msg(exc)]))
        return results  # 解析失败，后续阶段无从谈起

    # ---- 验证阶段（规则 + hide_suggestions 在此生效）----
    from graphql import validate

    errors = validate(
        schema,
        document,
        preset.rules,
        hide_suggestions=preset.hide_suggestions,
    )
    if errors:
        results.append(PhaseResult("validate", False, [_msg(e) for e in errors]))
        return results  # 校验失败不会进入执行
    results.append(PhaseResult("validate", True))

    # ---- 执行阶段（仅对通过校验的文档运行）----
    result = execute(
        schema,
        document,
        variable_values=probe.variables,
        hide_suggestions=preset.hide_suggestions,
    )
    if result.errors:
        results.append(
            PhaseResult("execute", False, [_msg(e) for e in result.errors])
        )
    else:
        results.append(PhaseResult("execute", True, data=result.data))
    return results


def render() -> str:
    lines: list[str] = []
    for preset in PRESETS:
        lines.append(f"\n## 预设：{preset.label}\n")
        lines.append("| 探针 | 解析 | 验证 | 执行 | 各阶段错误消息（截断 220 字符） |")
        lines.append("|---|---|---|---|---|")
        for probe in PROBES:
            phases = run_preset(probe, preset)
            by_phase = {p.phase: p for p in phases}
            parse_cell = "OK" if by_phase["parse"].ok else "ERROR"
            if "validate" in by_phase:
                val = by_phase["validate"]
                val_cell = "OK" if val.ok else f"ERROR×{len(val.messages)}"
            else:
                val_cell = "—（未到达）"
            if "execute" in by_phase:
                exe = by_phase["execute"]
                exe_cell = "OK" if exe.ok else f"ERROR×{len(exe.messages)}"
            else:
                exe_cell = "—（未到达）"
            detail_msgs = [m for p in phases for m in p.messages]
            detail = " <br> ".join(detail_msgs) if detail_msgs else "（无错误）"
            if len(detail) > 220:
                detail = detail[:217] + "..."
            lines.append(
                f"| {probe.key} | {parse_cell} | {val_cell} | {exe_cell} | {detail} |"
            )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    output = render()
    print(output)
    with open("probe_results.md", "w", encoding="utf-8") as fh:
        fh.write("# 分阶段探针结果（由 probes.py 生成，勿手改）\n")
        fh.write(output)
