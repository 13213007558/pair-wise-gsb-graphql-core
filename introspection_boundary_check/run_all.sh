#!/usr/bin/env bash
# 一键重放全部检查。要求 graphql-core 3.3.x（本仓库自带 src 即可）。
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-../.venv/bin/python}"
[ -x "$PY" ] || PY=python3

echo "=================== 1) 分阶段探针（parse/validate/execute） ==================="
"$PY" probes.py >/dev/null
echo "已生成 probe_results.md"
"$PY" probes.py

echo "=================== 2) 双入口隔离与文档复用反例 ==================="
"$PY" entrypoints.py

echo "=================== 3) 关闭内省后仍存在的信息范围 ==================="
"$PY" residual_exposure.py

echo "=================== 4) 操作白名单（真正的字段授权层） ==================="
"$PY" allowlist_demo.py
