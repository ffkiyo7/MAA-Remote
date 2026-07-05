# LLM 迁移 deepseek-v4-flash + thinking/effort 显式配置 Implementation Plan(2026-07-05)

> **For agentic workers(Codex 等):** 按 Task 顺序执行,TDD(先跑失败测试再实现),每个 Task 一个 commit。禁改 `CONTEXT.md`/`SPEC.md`/`prompts/`/`schemas/`/`evals/`。测试命令:`.venv/Scripts/python -m pytest <file> -v`,每个 Task 收尾跑全量 `.venv/Scripts/python -m pytest tests -q`。**Claude 负责逐任务 review。**

**Goal:** 把 LLM 调用显式迁移到 `deepseek-v4-flash`,默认开启 thinking(`thinking.type = "enabled"`)并显式设置 `reasoning_effort = "high"`,全部走 config 可配。

**Why now(硬依据,2026-07-05 查证官方文档):**
- 官方公告:`deepseek-chat` 与 `deepseek-reasoner` 将于 **2026-07-24 彻底下线**(当前只是临时路由到 v4-flash)。我们 `config.toml` 现在写的正是 `deepseek-chat`——**不迁,7 月 24 日后服务直接挂**。
- V4-Flash/Pro 均支持 Thinking / Non-Thinking 双模式与 `reasoning_effort`(取值 `"high"` | `"max"`;官方兼容映射:low/medium→high,xhigh→max)。

**Architecture:** 只动三处——`config.py`(新增两个 [llm] 字段)、`llm.py`(payload 增加两个顶层参数)、`__main__.py`(把新配置传进 LLMClient)。行为完全由 `config.toml` 驱动,零硬编码。

## Global Constraints(本计划红线)

- **payload 形状(最容易踩的坑)**:本项目用裸 httpx POST,`thinking` 参数放**请求体顶层**:`{"thinking": {"type": "enabled"}, "reasoning_effort": "high", ...}`。官方文档示例里的 `extra_body` 是 OpenAI SDK 的包装概念(SDK 会把它合并进请求体顶层),**绝对不要**在 payload 里嵌套一个字面量的 `"extra_body"` 键。测试里要断言 `"extra_body" not in payload`。
- `reasoning_effort` **只在 thinking 为 enabled 时发送**(non-thinking 模式下该参数无意义,不发最稳)。
- `thinking` 键**始终显式发送**(配置什么发什么)——官方默认虽是 enabled,但显式发送保证配置 `disabled` 时真的能关(这是 JSON 模式若出问题时的降级开关)。
- 向后兼容:config 里缺 `thinking`/`reasoning_effort` 键时默认 `"enabled"`/`"high"`,老配置不炸。
- 响应解析不变:仍取 `choices[0].message.content`;thinking 的思维链在同级 `reasoning_content` 字段,**忽略它**,不要改解析逻辑。
- 已知风险(官方文档明示):JSON 模式偶发返回空 content。Router 对无效 JSON 已有重试(`max_retries`),不需要新代码;但 Task 3 冒烟必须真调 API 验证 thinking+JSON 组合。
- 超时:thinking 模式明显变慢,`request_timeout_s` 从 30 提到 **120**(config.example.toml 和本机 config.toml 都改)。

---

### Task 1: config 新增 [llm] thinking / reasoning_effort + 切模型

**Files:**
- Modify: `maa_remote/config.py`
- Modify: `config.example.toml`
- Modify: 本机 `config.toml`(未被 git 跟踪)
- Test: `tests/test_config.py`(追加)

**Interfaces:**
- Produces:`LLMConfig` 新增字段 `thinking: str`(`"enabled"` | `"disabled"`,默认 `"enabled"`)、`reasoning_effort: str`(`"high"` | `"max"`,默认 `"high"`)。Task 2/3 依赖 `cfg.llm.thinking` / `cfg.llm.reasoning_effort`。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_config.py`;自包含,不依赖文件里其他 helper)

```python
def _example_llm_without(tmp_path, *keys):
    body = open("config.example.toml", encoding="utf-8").read()
    lines = [
        line for line in body.splitlines()
        if not any(line.strip().startswith(k) for k in keys)
    ]
    p = tmp_path / "config.toml"
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def test_llm_thinking_defaults_when_keys_missing(tmp_path):
    path = _example_llm_without(tmp_path, "thinking", "reasoning_effort")
    cfg = load_config(path, env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "y"})
    assert cfg.llm.thinking == "enabled"
    assert cfg.llm.reasoning_effort == "high"


def test_llm_thinking_keys_override(tmp_path):
    path = _example_llm_without(tmp_path, "thinking", "reasoning_effort")
    body = open(path, encoding="utf-8").read().replace(
        "[llm]", '[llm]\nthinking = "disabled"\nreasoning_effort = "max"', 1
    )
    open(path, "w", encoding="utf-8").write(body)
    cfg = load_config(path, env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "y"})
    assert cfg.llm.thinking == "disabled"
    assert cfg.llm.reasoning_effort == "max"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_config.py -v`
Expected: 新用例 FAIL(`LLMConfig` 无 `thinking` 属性)。

- [ ] **Step 3: 实现**

`maa_remote/config.py`:`LLMConfig` 增加两个字段:
```python
@dataclass
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    request_timeout_s: int
    max_retries: int
    cache_system_prompt: bool
    thinking: str
    reasoning_effort: str
```
`load_config` 的 `LLMConfig(...)` 构造处追加:
```python
            thinking=llm.get("thinking", "enabled"),
            reasoning_effort=llm.get("reasoning_effort", "high"),
```

`config.example.toml` 的 `[llm]` 节改为:
```toml
[llm]
provider            = "deepseek"
model               = "deepseek-v4-flash"  # deepseek-chat/reasoner 2026-07-24 官方下线,勿再使用
base_url            = "https://api.deepseek.com"
api_key_env         = "DEEPSEEK_API_KEY"
request_timeout_s   = 120                  # thinking 模式更慢,30s 不够
max_retries         = 1
cache_system_prompt = true
thinking            = "enabled"            # enabled | disabled(JSON 输出异常时的降级开关)
reasoning_effort    = "high"               # high | max(仅 thinking=enabled 时生效)
```
本机 `config.toml` 的 `[llm]` 节做同样修改(model / request_timeout_s / 新增两键)。

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_config.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add maa_remote/config.py config.example.toml tests/test_config.py
git commit -m "feat: llm thinking/effort config and switch to deepseek-v4-flash"
```

---

### Task 2: llm.py 请求体增加 thinking / reasoning_effort

**Files:**
- Modify: `maa_remote/llm.py`
- Test: `tests/test_llm.py`(追加;旧用例不用改——新参数带默认值,旧断言只查存在性)

**Interfaces:**
- Consumes: Task 1 的配置字段(由 Task 3 接线传入)。
- Produces:`LLMClient(base_url, api_key, model, timeout_s, post=None, thinking="enabled", reasoning_effort="high")`——新参数**必须是带默认值的关键字参数**,保持现有位置参数调用兼容;`chat()` 行为见测试。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_llm.py`)

```python
def test_chat_sends_thinking_and_effort_at_top_level_by_default():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-v4-flash", 120, post=make_post(cap)
    )
    client.chat("SYS", "USER", json_mode=True)
    assert cap["payload"]["thinking"] == {"type": "enabled"}
    assert cap["payload"]["reasoning_effort"] == "high"
    # 官方文档的 extra_body 是 OpenAI SDK 包装,裸 HTTP 禁止出现该键
    assert "extra_body" not in cap["payload"]
    # thinking 与 JSON 模式共存
    assert cap["payload"]["response_format"] == {"type": "json_object"}


def test_chat_thinking_disabled_omits_reasoning_effort():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-v4-flash", 120,
        post=make_post(cap), thinking="disabled",
    )
    client.chat("SYS", "USER")
    assert cap["payload"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in cap["payload"]


def test_chat_effort_max_passthrough():
    cap = {}
    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-v4-flash", 120,
        post=make_post(cap), reasoning_effort="max",
    )
    client.chat("SYS", "USER")
    assert cap["payload"]["reasoning_effort"] == "max"


def test_chat_ignores_reasoning_content_field():
    def post_with_reasoning(url, headers, payload, timeout):
        return {
            "choices": [
                {"message": {"content": '{"action":"run"}', "reasoning_content": "思考过程..."}}
            ]
        }

    client = LLMClient(
        "https://api.deepseek.com", "sk-1", "deepseek-v4-flash", 120, post=post_with_reasoning
    )
    assert client.chat("SYS", "USER") == '{"action":"run"}'
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -v`
Expected: 新用例 FAIL(payload 无 thinking 键 / 构造器不接受新参数)。

- [ ] **Step 3: 实现**

`maa_remote/llm.py` 的 `LLMClient`:
```python
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: int,
        post: Callable[..., dict[str, Any]] | None = None,
        thinking: str = "enabled",
        reasoning_effort: str = "high",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self._post = post or _httpx_post
```
`chat()` 组 payload 处,在 `json_mode` 判断之前加:
```python
        payload["thinking"] = {"type": self.thinking}
        if self.thinking == "enabled":
            payload["reasoning_effort"] = self.reasoning_effort
```
(其余不动:响应仍取 `choices[0].message.content`,`reasoning_content` 自然被忽略。)

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -v && .venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS(旧用例不改也应全绿——它们只断言已有键)。

- [ ] **Step 5: Commit**

```bash
git add maa_remote/llm.py tests/test_llm.py
git commit -m "feat: send thinking and reasoning_effort in llm payload"
```

---

### Task 3: __main__ 接线 + 真实 API 冒烟(需要 DEEPSEEK_API_KEY)

**Files:**
- Modify: `maa_remote/__main__.py`
- 冒烟产物: 无新文件,结果记录在 commit message 里

**Interfaces:**
- Consumes: Task 1 配置字段、Task 2 新构造参数。

- [ ] **Step 1: 接线**

`maa_remote/__main__.py` 的 `main()` 里 `LLMClient(...)` 调用改为:
```python
    llm = LLMClient(
        cfg.llm.base_url,
        cfg.llm.api_key,
        cfg.llm.model,
        cfg.llm.request_timeout_s,
        thinking=cfg.llm.thinking,
        reasoning_effort=cfg.llm.reasoning_effort,
    )
```

- [ ] **Step 2: 全量回归**

Run: `.venv/Scripts/python -m pytest tests -q`
Expected: 全部 PASS。

- [ ] **Step 3: 真实 API 冒烟——单次调用验证 thinking 生效**

前置:环境变量 `DEEPSEEK_API_KEY` 已设置(费用极低,flash 档单次调用不到一分钱)。

Run(项目根,一条命令):
```bash
.venv/Scripts/python -c "import os,httpx,json; r=httpx.post('https://api.deepseek.com/chat/completions', headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']}, json={'model':'deepseek-v4-flash','thinking':{'type':'enabled'},'reasoning_effort':'high','response_format':{'type':'json_object'},'messages':[{'role':'system','content':'只输出 JSON'},{'role':'user','content':'输出 json: {\"ok\": true}'}]}, timeout=120); d=r.json()['choices'][0]['message']; print('content=',d.get('content')); print('has_reasoning=',bool(d.get('reasoning_content')))"
```
Expected: `content=` 是合法 JSON(含 `ok`);`has_reasoning= True`(证明 thinking 真开了且与 JSON 模式共存)。若报参数错误或 `has_reasoning= False`,**停下报告**,不要自行猜参数改代码。

- [ ] **Step 4: 意图识别回归(真实 API 跑 evals)**

Run: `.venv/Scripts/python -m maa_remote.eval_router`
Expected: 通过率与迁移前持平或更好(逐条列出失败用例)。已知官方问题:JSON 模式偶发空 content——单条失败先重跑一次;稳定失败才算回归,停下报告。

- [ ] **Step 5: Commit**

```bash
git add maa_remote/__main__.py
git commit -m "feat: wire llm thinking/effort config into main, verified against live v4-flash"
```
(commit message 末尾附一行冒烟结果,如 `smoke: v4-flash thinking+json ok, evals 18/18`。)

---

## 附注(给使用者,非任务)

- **延迟**:thinking + high effort 会让"自定义指令→回复"的等待变长(数秒到几十秒);快速路径「跑日常」不走 LLM,不受影响。若嫌慢,`config.toml` 把 `thinking = "disabled"` 即回到旧体验,零代码。
- **成本**:flash 是便宜档,thinking 会多产出推理 token,但月常规用量下仍远低于 ¥10/月 预算红线;`eval_router` 全量跑一次也只有几分钱级别。
- **7 月 24 日**:官方下线 `deepseek-chat`/`deepseek-reasoner`。本计划落地后与此无关;若在那之前没落地,服务会在下线日直接报错——这是本计划的硬截止。
