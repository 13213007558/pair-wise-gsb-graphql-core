# graphql-core 3.3.2 abort_signal 取消覆盖审计

调用方向 `execute()` / `subscribe()` 传入 `AbortController.signal` 后，
“GraphQL 调用返回了 aborted 错误”并不等于所有底层工作都已停止。本目录用可
重放的事件账本，逐类工作、逐个取消时点测量取消真正传播到了哪里。

## 如何运行

```sh
.venv/bin/python -m cancel_audit.run_audit          # 生成账本与汇总表
.venv/bin/python -m cancel_audit.safe_integration   # 安全接入示例
```

- `ledger.py`：线程安全的结构化事件账本（started / cancel_notice /
  signal_observed / finally / aclose / delivered / completed …）。
- `work.py`：四类被埋点的工作单元。
- `audit_cases.py`：四类工作 × 四个取消时点的全部场景。
- `run_audit.py`：运行器，输出汇总表并写出可重放的 `ledger.json`。
- `safe_integration.py`：可直接照抄的安全接入模式。
- `ledger.json`：上一次运行的完整有序事件账本（可 diff、可重放）。

## 取消机制实际覆盖到哪里

信号只有两个被轮询的接口：`signal.aborted`（同步布尔）与 `signal.reason`，
外加可 await 的 `signal.wait()`。执行器在下列位置消费它：

| 位置 | 源码 | 行为 |
| --- | --- | --- |
| 操作入口 | `Executor.execute_operation()` 开头 | 已 abort 则直接抛 reason，任何 resolver 都不启动 |
| 串行字段之间 | `execute_fields_serially()` 的 reducer | 每个 mutation 字段前检查，abort 后后续字段不启动 |
| awaitable 字段 | `with_abort_signal()` | `ensure_future(awaitable)` 后与 `signal.wait()` 竞速；信号先到则 `task.cancel()` 并抛 reason |
| 列表项 / 抽象类型 / `is_type_of` | 同上，经 `complete_awaitable_value()` 等 | 库拥有的 Task 被取消 |
| 异步迭代 | `cancellable_iterable()` 包装 `__anext__` | 挂起的 `__anext__` 经 `with_abort_signal` 被即时取消 |
| 顶层操作 | `with_aborted_execution_error()` | 不整体取消操作任务，而是让在途 resolver 各自取消，后台结算出部分结果 |
| 同步收尾 | `finish()` | 纯同步执行期间发生的 abort，只在收尾检查时以 aborted 错误暴露 |
| 增量交付 | `IncrementalExecutor.abort()` / `StreamItemQueue` | 取消挂起的 defer/stream 任务，并对异步源调 `aclose()` |

关键事实：**执行器从不取消顶层操作任务本身**，也不会遍历取消它不拥有的
对象。它只能取消自己经 `ensure_future` 包装出来的 Task，以及在有挂起
`__anext__` 时关闭它包装的迭代器。

## 四类工作的实测结论

### 1. 普通协程 resolver（库拥有）

`with_abort_signal` 把 resolver 协程包成库拥有的 Task。abort 时该 Task 收到
`CancelledError`，`finally` 确定性执行（见 `coro-t2-while-waiting`、
`coro-t3-after-field-done`，均为 started=1 / cancel_notice=1 / finally=1）。

- 开始前 abort：resolver 协程根本不启动（`coro-t1-before-start`，全 0）。
- 协作式写法：resolver 自己 `await info.abort_signal.wait()`，则不收到
  `CancelledError`，而是观察到信号后从 `finally` 正常返回
  （`coro-t2-cooperative`：cancel_notice=0、signal_observed=1、finally=1）。

### 2. 调用方预先创建的 Future（调用方拥有）

这是所有权最容易踩坑的地方，账本给出三种写法的硬证据：

- **直接返回共享 Future**（`returns_future_directly`）：
  `ensure_future(已存在的 Future)` 返回该对象本身，abort 时
  `task.cancel()` 取消的就是业务持有的同一个 Future
  （state: `shared_future_cancelled: true`）。
- **在协程里普通 `await` 共享 Future**（`awaits_future`）：同样不安全。
  库取消包装 Task 时，`CancelledError` 沿裸 `await` 传播，内部共享 Future
  依然被取消（state: `shared_future_cancelled: true`）。
- **`asyncio.shield(shared_future)`**（`shields_future`）：只有 shield 外层
  被取消，内部业务 Future 保持未取消，业务侧之后可正常
  `set_result`（state: `shared_future_cancelled: false`,
  `shared_future_owner_settled: true`）。

结论：库的接口只表达“取消我拿到的那个 awaitable”，无法替你判断 Future 是否
还被别处持有。需要共享时，用 `asyncio.shield` 或让一个独立的业务任务持有
Future；不要把仍被业务引用的 Future 直接交出去或裸 await。

### 3. 同步 resolver（不可中断）

同步函数没有挂起点，事件循环无法在其执行期间介入：

- 纯并行 query 内，字段之间没有信号检查。`a` 返回后 abort，`b` 仍会跑完，
  abort 只在 `finish()` 暴露（`sync-t3-parallel-inline-abort`：a、b 均
  completed；`sync-t3-parallel-while-blocking` 中 `time.sleep` 期间 abort
  也要等阻塞结束）。
- 串行 mutation 的 reducer 在字段间有检查：`m1` 内 abort 后 `m2` 不启动
  （`sync-serial-mutation`：只有 m1 started/completed）。
- 开始前 abort：同步 resolver 完全不被调用（`sync-t1-before-start`，全 0）。

边界如实记录，不承诺强制终止：单个同步 resolver 一旦开始，除非它自己协作式
轮询 `info.abort_signal.aborted` 提前返回，否则必然跑到结束。CPU/阻塞型
resolver 应自行分块并检查信号。

### 4. 订阅 async iterator

源经 `cancellable_iterable` 包装，响应流由 `map_async_iterable` 产生（其
`aclosing` 会在关闭时转发 `aclose()`）：

- abort 时有挂起的 `__anext__`：该调用被即时取消，源收到
  `CancelledError`；消费方随后 `aclose()` 时源 `finally` 确定性运行
  （`sub-t2-generator-waiting`、`sub-t2-class-waiting`，
  cancel_notice=1、aclose=1、finally=1）。
- 事件交付后 abort：下一次 `__anext__` 抛 abort reason，`aclose()` 关闭源
  （`sub-t4-after-event`）。
- 开始前 abort：源不启动，首次 `__anext__` 即失败
  （`sub-t1-before-start`）。
- **空闲 abort 且消费方不 `aclose()`**（`sub-t4-abandoned`）：此时没有挂起
  的 `__anext__`，库没有可取消的在途调用、也没有关闭钩子可触发。账本记录
  `pre_gc_source_suspended: true`——源生成器在消费方丢弃后仍挂起；只有随后
  async-generator 的 GC 钩子才回收它（`post_gc ... suspended: false`）。
  这不是库能取消的库属任务，而是消费方的清理缺口：**务必 `aclose()`**。

## 是否发现库属任务/迭代器漏清理

没有发现需要修改库源码的硬泄漏：

- 所有 query/mutation 场景的库属 Task 在 abort 后均被取消并跑完 `finally`，
  无遗留 pending 任务（汇总表 orphans 列全为 0）。
- 即使消费方捕获 `AbortedGraphQLExecutionError` 后不 await
  `aborted_result`，`settle_in_background()` 仍会取消并结算在途库属任务，
  实测无孤儿任务、无 “never awaited” 警告。
- `@stream` / `@defer` 增量路径：abort 时在途任务收到 `CancelledError`，
  异步源 `aclose()` 恰好一次，无孤儿任务（280 个增量测试全过）。

唯一边界是上面第 4 节的“空闲 abort + 不关闭流”：库在该时刻不持有任何可
取消的在途句柄，无法替消费方关闭源。对同步不可中断代码同样只记录边界、
不承诺强制终止。因此本审计**未改动 `src/graphql` 任何代码**。

## 安全接入要点（详见 safe_integration.py）

1. 协程/Task resolver：把清理放 `finally` 或 `async with`，abort 时必执行。
2. 共享 Future：用 `asyncio.shield`（或独立业务任务）隔离，不要直接返回或
   裸 await 仍被业务持有的 Future。
3. 同步 resolver：分块处理并轮询 `info.abort_signal.aborted` 协作退出。
4. 订阅：用 `try/finally` 保证 `await stream.aclose()`；abort reason 在
   订阅流上是直接从 `__anext__` 抛出，而非 `AbortedGraphQLExecutionError`。
5. query 的 abort：捕获 `AbortedGraphQLExecutionError`，可按需 await
   `error.aborted_result` 取部分结果；不 await 也不会泄漏库属任务。
