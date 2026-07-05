import json
import shutil

from maa_remote.config import load_config
from maa_remote.models import Msg, StageInfo, TaskPlan
from maa_remote.router import Router


def _cfg(tmp_path):
    shutil.copy("config.example.toml", tmp_path / "config.toml")
    return load_config(
        str(tmp_path / "config.toml"),
        env={"DEEPSEEK_API_KEY": "k", "LOCALAPPDATA": "x", "APPDATA": "x"},
    )


SCHEMA = json.load(open("schemas/task_plan.schema.json", encoding="utf-8"))
PROMPT = open("prompts/router.system.md", encoding="utf-8").read()


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat(self, system, user, json_mode=False):
        self.calls.append((system, user, json_mode))
        return self.reply


def _msg(text):
    return Msg(
        text=text,
        chat_id="oc_1",
        message_id="om_1",
        sender_open_id="ou_1",
        create_time=0,
    )


def test_fast_path_daily_previews_then_confirm_executes(tmp_path):
    llm = FakeLLM("SHOULD_NOT_BE_CALLED")
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    rr = router.route(_msg("跑日常"))
    assert rr.kind == "reply" and "📋" in rr.reply and "基建" in rr.reply
    rr2 = router.route(_msg("1"))
    assert rr2.kind == "execute" and rr2.plan.recruit.enable is True
    assert llm.calls == []


def test_skip_prefix_bypasses_confirm(tmp_path):
    router = Router(_cfg(tmp_path), FakeLLM("SHOULD_NOT_BE_CALLED"), PROMPT, SCHEMA)
    rr = router.route(_msg("直接跑日常"))
    assert rr.kind == "execute" and rr.plan.recruit.enable is True


def test_skip_prefix_cannot_bypass_spend_confirmation(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.maa.fight.medicine = 999
    router = Router(cfg, FakeLLM("SHOULD_NOT_BE_CALLED"), PROMPT, SCHEMA)
    rr = router.route(_msg("直接跑日常"))
    assert rr.kind == "reply" and "⚠️" in rr.reply


def test_new_command_replaces_pending_confirm(tmp_path):
    llm = FakeLLM(json.dumps({"action": "run", "fight": {"enable": True, "stage": "CE-6"}}))
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "reply"
    rr2 = router.route(_msg("刷CE-6"))
    assert rr2.kind == "reply" and "CE-6" in rr2.reply
    rr3 = router.route(_msg("确认"))
    assert rr3.kind == "execute" and rr3.plan.fight.stage == "CE-6"


def test_confirm_mode_spend_only_executes_daily_directly(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(cfg, FakeLLM("SHOULD_NOT_BE_CALLED"), PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "execute"


def test_confirm_uses_confirm_ttl(tmp_path):
    clock = {"v": 0.0}
    cfg = _cfg(tmp_path)
    router = Router(cfg, FakeLLM("SHOULD_NOT_BE_CALLED"), PROMPT, SCHEMA, now_fn=lambda: clock["v"])
    assert router.route(_msg("跑日常")).kind == "reply"
    clock["v"] = 599.0
    assert router.route(_msg("1")).kind == "execute"


def test_llm_path_specific_stage(tmp_path):
    llm = FakeLLM(
        json.dumps(
            {
                "action": "run",
                "recruit": {"enable": False},
                "fight": {"enable": True, "stage": "CE-6", "times": 3},
            }
        )
    )
    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(cfg, llm, PROMPT, SCHEMA)
    rr = router.route(_msg("打CE-6三次别做公招"))
    assert rr.kind == "execute"
    assert rr.plan.fight.stage == "CE-6" and rr.plan.fight.times == 3
    assert rr.plan.recruit.enable is False
    assert rr.plan.startup is True


def test_reject_and_clarify_are_reply_only(tmp_path):
    router = Router(
        _cfg(tmp_path), FakeLLM(json.dumps({"action": "reject", "note": "x"})), PROMPT, SCHEMA
    )
    assert router.route(_msg("今天天气")).kind == "reply"

    router2 = Router(
        _cfg(tmp_path),
        FakeLLM(json.dumps({"action": "clarify", "clarify_question": "跑日常还是刷关?"})),
        PROMPT,
        SCHEMA,
    )
    rr = router2.route(_msg("帮我弄一下"))
    assert rr.kind == "reply" and "刷关" in rr.reply


def test_ask_stage_selection_then_pick_executes_with_startup(tmp_path):
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(
        cfg,
        FakeLLM(json.dumps({"action": "ask_stage_selection"})),
        PROMPT,
        SCHEMA,
        stage_loader=lambda path, client, now=None: stages,
    )
    rr = router.route(_msg("刷这期活动"))
    assert rr.kind == "reply" and "TT-8" in rr.reply

    rr2 = router.route(_msg("1"))
    assert rr2.kind == "execute"
    assert rr2.plan.fight.stage == "TT-8"
    assert rr2.plan.startup is True
    assert rr2.plan.recruit.enable is False


def test_ask_stage_selection_empty_replies_no_activity(tmp_path):
    router = Router(
        _cfg(tmp_path),
        FakeLLM(json.dumps({"action": "ask_stage_selection"})),
        PROMPT,
        SCHEMA,
        stage_loader=lambda path, client, now=None: [],
    )
    rr = router.route(_msg("刷这期活动"))
    assert rr.kind == "reply" and "没有" in rr.reply


def test_stage_selection_cancel_and_miss_do_not_execute(tmp_path):
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    router = Router(
        _cfg(tmp_path),
        FakeLLM(json.dumps({"action": "ask_stage_selection"})),
        PROMPT,
        SCHEMA,
        stage_loader=lambda path, client, now=None: stages,
    )
    router.route(_msg("刷这期活动"))
    assert router.route(_msg("99")).kind == "reply"
    rr = router.route(_msg("取消"))
    assert rr.kind == "reply" and "取消" in rr.reply


def test_hot_update_failure_still_uses_cached_stage_file(tmp_path):
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    router = Router(
        _cfg(tmp_path),
        FakeLLM(json.dumps({"action": "ask_stage_selection"})),
        PROMPT,
        SCHEMA,
        stage_loader=lambda path, client, now=None: stages,
        hot_update_fn=lambda maa_cli_path: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    rr = router.route(_msg("刷活动"))
    assert rr.kind == "reply" and "TT-8" in rr.reply


def test_stone_requires_confirmation_then_confirm_executes(tmp_path):
    llm = FakeLLM(
        json.dumps(
            {"action": "run", "fight": {"enable": True, "stage": "UR-8", "stone": 50}}
        )
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    rr = router.route(_msg("碎50颗石头刷UR-8"))
    assert rr.kind == "reply" and "确认" in rr.reply and "50" in rr.reply

    rr2 = router.route(_msg("确认"))
    assert rr2.kind == "execute" and rr2.plan.fight.stone == 50


def test_confirmation_has_priority_over_pending_selection(tmp_path):
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    llm = FakeLLM(json.dumps({"action": "ask_stage_selection"}))
    cfg = _cfg(tmp_path)
    router = Router(
        cfg,
        llm,
        PROMPT,
        SCHEMA,
        now_fn=lambda: 0.0,
        stage_loader=lambda path, client, now=None: stages,
    )
    router.route(_msg("刷活动"))
    router._pending_confirm["oc_1"] = (TaskPlan.daily(cfg.maa.fight, cfg.maa.daily_tasks), 300.0)
    rr = router.route(_msg("确认"))
    assert rr.kind == "execute"


def test_stone_confirmation_cancel(tmp_path):
    llm = FakeLLM(
        json.dumps(
            {"action": "run", "fight": {"enable": True, "stage": "UR-8", "stone": 50}}
        )
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    router.route(_msg("碎50颗石头刷UR-8"))
    rr = router.route(_msg("取消"))
    assert rr.kind == "reply" and "取消" in rr.reply

    rr2 = router.route(_msg("确认"))
    assert rr2.kind == "reply"


def test_medicine_requires_confirmation(tmp_path):
    llm = FakeLLM(
        json.dumps(
            {"action": "run", "fight": {"enable": True, "stage": "1-7", "medicine": 999}}
        )
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    rr = router.route(_msg("把囤药用了刷1-7"))
    assert rr.kind == "reply" and "确认" in rr.reply


def test_confirmation_expires_by_ttl(tmp_path):
    clock = {"v": 0.0}
    llm = FakeLLM(
        json.dumps(
            {"action": "run", "fight": {"enable": True, "stage": "1-7", "medicine": 999}}
        )
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA, now_fn=lambda: clock["v"])
    rr = router.route(_msg("把囤药用了刷1-7"))
    assert rr.kind == "reply" and "确认" in rr.reply

    clock["v"] = 601.0
    rr2 = router.route(_msg("确认"))
    assert rr2.kind == "reply"


def test_invalid_json_then_retry_success(tmp_path):
    class FlakyLLM:
        def __init__(self):
            self.n = 0

        def chat(self, system, user, json_mode=False):
            self.n += 1
            return (
                "not json"
                if self.n == 1
                else json.dumps({"action": "run", "fight": {"enable": True}})
            )

    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(cfg, FlakyLLM(), PROMPT, SCHEMA)
    rr = router.route(_msg("刷理智"))
    assert rr.kind == "execute"


def test_schema_violation_then_retry_success(tmp_path):
    class FlakyLLM:
        def __init__(self):
            self.n = 0

        def chat(self, system, user, json_mode=False):
            self.n += 1
            return (
                json.dumps({"action": "no_such_action"})
                if self.n == 1
                else json.dumps({"action": "run", "fight": {"enable": True}})
            )

    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(cfg, FlakyLLM(), PROMPT, SCHEMA)
    rr = router.route(_msg("刷理智"))
    assert rr.kind == "execute"


def test_invalid_json_exhausts_retries_to_clarify(tmp_path):
    router = Router(_cfg(tmp_path), FakeLLM("still not json"), PROMPT, SCHEMA)
    rr = router.route(_msg("乱七八糟"))
    assert rr.kind == "reply" and "没太懂" in rr.reply
