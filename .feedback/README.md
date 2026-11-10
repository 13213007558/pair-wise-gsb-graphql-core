# Feedback task starting environment

Upstream: https://github.com/graphql-python/graphql-core
Upstream commit: `1d505c19bf5fb8da0205165b78af6094d637009e`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= -o asyncio_mode=auto tests/validation/test_no_fragment_cycles.py tests/execution/test_variables.py tests/execution/test_nonnull.py tests/language/test_parser.py
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-10): `248 passed in 0.66s`
Whole-command wall time: 1.006 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
