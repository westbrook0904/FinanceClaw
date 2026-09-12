# Stage 11 上下文与容量验证

日期：2026-09-12。测试仅使用虚构用户输入和独立临时 SQLite/工件目录；未触碰已有业务数据库。

## 已实现及验证

- 共享 `shared/llm` 模型工厂、冻结总窗口/独立输入cap/估算器、主模型与fallback共同容量；Provider输入cap不重复扣输出。
- 系统、工具Schema、记忆和规范工作摘要使用共同完整请求估算器；最终每个模型尝试复验容量与privacy epoch。
- `WorkingContext`仅在原生state保留一份正文，模型请求临时渲染；旧完成执行中段可移除，真实用户输入、澄清和完整调用配对保留。
- 先归档唯一工具结果，后调用摘要模型；同一输入fingerprint摘要失败最多两次，模型前/摘要提交前复验隐私版本。
- 回答和摘要Manifest分别记录估算输入、可用容量、估算器版本与Provider实际输入/输出用量。未知用量为空，非零假值。
- 记忆派生工件绑定owner/epoch/确切版本引用，读取正文前后检查；遗忘后不能用`read_artifact`重新注入旧画像，真实业务动作回执保持可读。

## 实际执行的原生探针

`tests/stage11/test_context_native.py` 使用公开 `create_agent`、`AgentFactory`、`add_messages`、`InMemorySaver(factory=PersistentDict)` 和 `interrupt/Command`。

1. 正式AgentFactory装配并实际运行压缩节点，最终state保留原用户锚点。
2. 同一Turn内完整工具中段被压缩；原文归档先于删除，工作摘要及消息更新进入native checkpoint。
3. 关闭checkpoint saver，将其官方PersistentDict落盘，再建立新saver和新Agent；从checkpoint读取并恢复相同摘要、用户锚点、工具结果引用。
4. 真正原生工具interrupt跨新Agent实例恢复；工具只在收到resume后完成一次，待答批次保留。
5. 未完成并行工具批次不压缩；摘要期间privacy epoch变化作废输出；归档阶段发生遗忘则在摘要传输前阻断。
6. 摘要返回非法JSON时原消息保持，重复fingerprint尝试被限制。

这是公开原生图和checkpoint序列化/恢复验证。它不等同于部署后的AgentServer API + Redis + PostgreSQL worker多进程或kill容器验证。

## 测试命令与结果

```sh
.venv/bin/pytest -q tests/stage11/test_context_budget.py tests/stage11/test_context_native.py tests/stage11/test_context_evidence.py tests/stage9/test_context.py tests/stage1/test_agent.py tests/stage7/test_agent.py tests/stage1/test_models_settings_architecture.py tests/stage1/test_deepseek_thinking.py tests/stage8_hotfix/test_context_capacity.py tests/stage2/test_artifacts.py
```

完整集合结果：62 passed in 10.88s。后续隐私失效异常恢复的小修改再次通过20项定向上下文回归。

运行版本：LangChain 1.3.18、LangGraph 1.2.11、langgraph-sdk 0.4.4、langgraph-api 0.14.0。

## 尚未验证

- 当前标准 `FinanceClawSettings(environment=test, debug_full_io=False)` 未解析到Provider API key，只检查存在性而未打印凭据。未执行真实Provider模型质量集。
- 工具原文归档、原生reducer及恢复结构测试不能证明真实模型能持续正确保留金额、日期、否定和待确认语义。
- 未由本探针执行真实AgentServer多进程容量、容器kill恢复及长期质量/延迟测试。

补充边界：若隐私版本恰在before_model准备完成之后、实际模型attempt/retry之前改变，最终门禁安全拒绝旧请求并返回明确异常；此极窄竞态不会在request wrapper里改checkpoint或自动重跑业务工具。常规下一before_model边界会清除派生内容并继续。

## 真实 AgentServer HTTP 离线贯通补充

已使用 `langgraph dev 0.14.0`、`langgraph_runtime_inmem 0.34.0`，在随机回环端口、新建临时应用SQLite、独立 `.langgraph_api` 和工件目录执行。未启用 `--allow-blocking`，未使用原有数据库或服务。探针服务已正常停止。

- 初次探针发现 `async finance_agent` 同步冷编译触发 `BlockingError: os.getcwd`，第一个测试Turn失败。已修复为 `await asyncio.to_thread(_graph)`。
- 修复后两个新Turn均completed，耗时2.615s与1.335s（仅样本，不是性能SLO）。
- Journal角色严格为 user/assistant/user/assistant，4条可信来源各有独立派生permit。
- 每轮完成均产生一个 `memory_extract` / `memory.extract.prepare` pending意图，每个闭合两条source引用；本探针没有启动Memory Worker，不能将pending写成已提取。
- 两轮之间通过真实 `/v1/memories` API保存语言偏好，回执committed、revision=1。
- 第一轮2次模型尝试Manifest没有记忆引用；第二轮2次模型尝试均带正确SQL profile ID/revision=1和owner revision=1。

不含原始正文和令牌的机器证据：[native-http-offline.json](native-http-offline.json)。此验证覆盖实际HTTP、进程内SDK、原生queue、最终Journal以及下一轮SQL记忆注入；仍不能替代生产PostgreSQL/Redis多进程故障、长轮压缩HTTP恢复和真实模型质量集。

## 最终独立复核与补充回归

S46复核发现固定记忆额度没有在完整请求接近容量时退让。已将共用容量规划器接入MemoryRecall，在归档/摘要之前按完整user、system、tools、输出Schema、WorkingContext和fallback共同窗口，依次让出目录、任务详情、画像。剔除的是完整可选记录，真实用户输入不截断；选中的SQL版本与预算剔除清单保存到native state，实际请求不偷偷补回被剔除的记录。Manifest保留每项省略原因；仅用于本地审计的省略元数据不消耗模型输入额度。

S07/S10复核发现同一原始用户锚点会直接命中缓存，已接受澄清无法改变L1查询。现以原始用户输入和可信交互内容的hash判断查询是否变化；空结果继续缓存，新澄清只刷新L1。对应明确偏好只有在当前profile最新支持来源确实为本Turn已接受回答时才替换，其他后台更新仍保留原默认版本。普通问题模板中的问号不再使持续性偏好答案被误判为非断言，临时/假设等限定仍生效。

新增真实`create_agent`探针运行before_model、原生reducer、checkpoint及FinalContext：超过共同窗口的可选画像被剔除后正常回答，checkpoint保留原始用户与剔除清单，SQL Manifest无未发送的记忆引用且省略记录完整。

在前述完整命令中增加`tests/stage11/test_recall_snapshot.py`，最终相关集合为`71 passed in 12.00s`（新增原生省略、澄清缓存、选择性偏好刷新、本地诊断预算及S40无敏感正文降级日志回归）。对应文件Ruff与格式检查、`git diff --check`通过。

S40的语义索引异常、缺失、空结果或候选全部失效，会记录有界`reason`、`elapsed_seconds`、SQL候选数和结果数。日志不包含query、owner、记忆正文或供应商原始错误；SQL有界降级功能保持可用。

第二次真实HTTP复验使用另一套全新空SQLite/native state及工件目录，未显式配置`processing_region`，验证两个Turn快照和四条source均为默认`global`。两个Turn均completed（2.056s、1.148s，仅样本），中间记忆API committed，两个闭合提取意图各有两条source，下一Turn两次实际Manifest带SQL profile revision=1。服务正常停止；仍未运行Memory Worker，因此提取意图是pending。证据：[native-http-final.json](native-http-final.json)。
