import json
import os
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


class QueueLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, user, json_mode=False):
        self.calls.append((system, user, json_mode))
        return self.replies.pop(0)


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
    llm = FakeLLM(json.dumps({"action": "patch", "patch": {"fight": {"stage": "CE-6"}}}))
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "reply"
    rr2 = router.route(_msg("刷CE-6"))
    assert rr2.kind == "reply" and "CE-6" in rr2.reply
    rr3 = router.route(_msg("确认"))
    assert rr3.kind == "execute" and rr3.plan.fight.stage == "CE-6"


def test_pending_confirm_update_keeps_daily_and_changes_fight_stage(tmp_path):
    llm = FakeLLM(
        json.dumps({"action": "patch", "patch": {"fight": {"stage": "CE-6"}}})
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("daily")).kind == "reply"

    rr = router.route(_msg("ok, but change the fight stage to CE-6"))
    assert rr.kind == "reply" and "CE-6" in rr.reply
    assert "TaskPlan JSON" in llm.calls[0][1]

    rr2 = router.route(_msg("1"))
    assert rr2.kind == "execute"
    assert rr2.plan.fight.stage == "CE-6"
    assert rr2.plan.recruit.enable is True
    assert rr2.plan.infrast.enable is True


def test_pending_confirm_update_preserves_disabled_tasks(tmp_path):
    llm = QueueLLM(
        [
            json.dumps(
                {
                    "action": "run",
                    "recruit": {"enable": False},
                    "infrast": {"enable": False},
                    "mall": {"enable": False},
                    "award": {"enable": False},
                    "fight": {"enable": True, "stage": ""},
                }
            ),
            json.dumps({"action": "patch", "patch": {"fight": {"stage": "CE-6"}}}),
        ]
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("fight only")).kind == "reply"
    rr = router.route(_msg("change to CE-6"))
    assert rr.kind == "reply" and "CE-6" in rr.reply

    rr2 = router.route(_msg("1"))
    assert rr2.kind == "execute"
    assert rr2.plan.fight.stage == "CE-6"
    assert rr2.plan.recruit.enable is False
    assert rr2.plan.infrast.enable is False


def test_pending_confirm_pure_ok_words_execute_without_llm_loop(tmp_path):
    llm = FakeLLM("SHOULD_NOT_BE_CALLED")
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "reply"
    rr = router.route(_msg("可以"))
    assert rr.kind == "execute"
    assert llm.calls == []


def test_pending_confirm_approve_action_executes(tmp_path):
    llm = FakeLLM(json.dumps({"action": "approve"}))
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "reply"
    rr = router.route(_msg("yes please"))
    assert rr.kind == "execute"
    assert rr.plan.recruit.enable is True


def test_pending_confirm_patch_spend_requires_second_confirmation(tmp_path):
    llm = FakeLLM(
        json.dumps({"action": "patch", "patch": {"fight": {"stone": 1, "stage": "CE-6"}}})
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "reply"
    rr = router.route(_msg("可以，再碎一颗石头刷 CE-6"))
    assert rr.kind == "reply"
    assert "⚠️" in rr.reply and "CE-6" in rr.reply

    rr2 = router.route(_msg("确认"))
    assert rr2.kind == "execute"
    assert rr2.plan.fight.stone == 1


def test_confirm_to_stage_selection_pops_pending_confirm_and_uses_menu_choice(tmp_path):
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    llm = FakeLLM(json.dumps({"action": "ask_stage_selection"}))
    router = Router(
        _cfg(tmp_path),
        llm,
        PROMPT,
        SCHEMA,
        stage_loader=lambda path, client, now=None: stages,
    )
    assert router.route(_msg("跑日常")).kind == "reply"
    menu = router.route(_msg("换个活动关吧"))
    assert menu.kind == "reply" and "TT-8" in menu.reply
    assert "oc_1" not in router._pending_confirm

    rr = router.route(_msg("1"))
    assert rr.kind == "reply" and "TT-8" in rr.reply
    rr2 = router.route(_msg("确认"))
    assert rr2.kind == "execute"
    assert rr2.plan.fight.stage == "TT-8"
    assert rr2.plan.recruit.enable is True


def test_fresh_patch_or_approve_degrades_to_clarify(tmp_path):
    router = Router(_cfg(tmp_path), FakeLLM(json.dumps({"action": "approve"})), PROMPT, SCHEMA)
    rr = router.route(_msg("可以"))
    assert rr.kind == "reply" and "当前没有待确认" in rr.reply

    router2 = Router(
        _cfg(tmp_path),
        FakeLLM(json.dumps({"action": "patch", "patch": {"fight": {"stage": "CE-6"}}})),
        PROMPT,
        SCHEMA,
    )
    rr2 = router2.route(_msg("换成 CE-6"))
    assert rr2.kind == "reply" and "当前没有待确认" in rr2.reply


def test_advise_is_reply_only_and_rendered_from_snapshot(tmp_path):
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(
        cfg,
        FakeLLM(json.dumps({"action": "advise", "advise_refs": ["TT-8"]})),
        PROMPT,
        SCHEMA,
        stage_loader=lambda path, client, now=None: stages,
    )
    rr = router.route(_msg("当前活动能刷什么"))
    assert rr.kind == "reply"
    assert "TT-8" in rr.reply and "本关效率最高" in rr.reply


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


def test_llm_path_accepts_fight_series(tmp_path):
    llm = FakeLLM(
        json.dumps(
            {
                "action": "run",
                "fight": {"enable": True, "stage": "CE-6", "series": 6},
            }
        )
    )
    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(cfg, llm, PROMPT, SCHEMA)
    rr = router.route(_msg("刷 CE-6，固定六倍代理"))
    assert rr.kind == "execute"
    assert rr.plan.fight.series == 6


def test_llm_path_canonicalizes_alias_stage_output(tmp_path):
    llm = FakeLLM(
        json.dumps(
            {
                "action": "run",
                "fight": {"enable": True, "stage": "ce6"},
            }
        )
    )
    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    router = Router(cfg, llm, PROMPT, SCHEMA)
    rr = router.route(_msg("刷钱本"))
    assert rr.kind == "execute"
    assert rr.plan.fight.stage == "CE-6"


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


def test_pending_selection_has_priority_over_pending_confirm(tmp_path):
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
    rr = router.route(_msg("1"))
    assert rr.kind == "reply"
    assert "TT-8" in rr.reply


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


def test_schema_violation_missing_patch_payload_then_retry_success(tmp_path):
    llm = QueueLLM(
        [
            json.dumps({"action": "patch"}),
            json.dumps({"action": "patch", "patch": {"fight": {"stage": "CE-6"}}}),
        ]
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "reply"
    rr = router.route(_msg("换成 CE-6"))
    assert rr.kind == "reply" and "CE-6" in rr.reply
    assert len(llm.calls) == 2


def test_confirm_mode_run_output_then_retry_patch_success(tmp_path):
    llm = QueueLLM(
        [
            json.dumps({"action": "run", "fight": {"enable": True, "stage": "CE-6"}}),
            json.dumps({"action": "patch", "patch": {"fight": {"stage": "CE-6"}}}),
        ]
    )
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA)
    assert router.route(_msg("跑日常")).kind == "reply"
    rr = router.route(_msg("换成 CE-6"))
    assert rr.kind == "reply" and "CE-6" in rr.reply
    assert len(llm.calls) == 2


def test_validator_violation_then_retry_success(tmp_path):
    llm = QueueLLM(
        [
            json.dumps({"action": "run", "fight": {"enable": True, "stage": "SN-10"}}),
            json.dumps({"action": "ask_stage_selection"}),
        ]
    )
    stages = [StageInfo("测试当期", "TT-8", "本关效率最高", "x")]
    router = Router(
        _cfg(tmp_path),
        llm,
        PROMPT,
        SCHEMA,
        stage_loader=lambda path, client, now=None: stages,
    )
    rr = router.route(_msg("刷当前活动代币"))
    assert rr.kind == "reply" and "TT-8" in rr.reply
    assert len(llm.calls) == 2


def test_invalid_json_exhausts_retries_to_clarify(tmp_path):
    router = Router(_cfg(tmp_path), FakeLLM("still not json"), PROMPT, SCHEMA)
    rr = router.route(_msg("乱七八糟"))
    assert rr.kind == "reply" and "没太懂" in rr.reply


# =========================== 抄作业（copilot）#5 =============================

from maa_remote.copilot_catalog import Candidate, CatalogResult, StageResolutionError


def _cand(cid, passed=True, risky=False, title="", opers=None, rating=10):
    return Candidate(
        id=cid, passed=passed, risky=risky, score=50.0, title=title,
        issues=["✅ 无硬性缺口"], risks=(["⚠️ 技能等级未知"] if risky else []),
        opers=opers or ["山 ✅"], groups=[], doc_signals=[], difficulty="",
        uploader="u", upload_time="", views=0, hot_score=0.0,
        rating_level=rating, rating_ratio=0.0, likes=0,
    )


def _result(cands, contents=None):
    return CatalogResult(
        stage_display="HS-9", level_id="act/hs/level_hs_09", collision=[],
        total_fetched=len(cands), candidates=cands,
        contents=contents or {c.id: {"opers": [{"name": "山"}]} for c in cands},
    )


def _catalog_fn(result):
    def fn(stage, roster, level_id_override=None):
        return result
    return fn


def _copilot_router(tmp_path, result, llm_action=None, **cfg_over):
    cfg = _cfg(tmp_path)
    cfg.copilot.jobs_dir = str(tmp_path / "jobs")
    for k, v in cfg_over.items():
        setattr(cfg.copilot, k, v)
    action = llm_action or {"action": "copilot", "copilot": {"scope": "single", "stage": "HS-9"}}
    llm = FakeLLM(json.dumps(action))
    fn = _catalog_fn(result) if result is not None else _raise_catalog
    router = Router(cfg, llm, PROMPT, SCHEMA, copilot_catalog_fn=fn, roster_provider=lambda: None)
    return cfg, router


def test_router_copilot_single_stage_enters_confirm(tmp_path):
    _, router = _copilot_router(tmp_path, _result([_cand(101, title="低配三星"), _cand(102)]))
    rr = router.route(_msg("帮我抄作业打 HS-9"))
    assert rr.kind == "reply" and rr.plan is None
    assert "HS-9" in rr.reply and "#101" in rr.reply
    assert "其余候选" in rr.reply and "#102" in rr.reply


def test_router_copilot_does_not_fall_through_to_daily_tasks(tmp_path):
    # 阻断项回归：spend_only 下 copilot 决不能落到 from_llm_dict 被当日常直接 execute。
    _, router = _copilot_router(tmp_path, _result([_cand(101)]))
    router.cfg.confirm.mode = "spend_only"
    rr = router.route(_msg("帮我抄作业打 HS-9"))
    assert rr.kind == "reply" and rr.plan is None


def test_router_run_action_with_copilot_payload_is_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.confirm.mode = "spend_only"
    llm = FakeLLM(
        json.dumps({"action": "run", "copilot": {"scope": "single", "stage": "HS-9"}})
    )
    router = Router(
        cfg,
        llm,
        PROMPT,
        SCHEMA,
        copilot_catalog_fn=_raise_catalog,
        roster_provider=lambda: None,
    )

    rr = router.route(_msg("帮我抄作业打 HS-9"))

    assert rr.kind == "reply"
    assert rr.plan is None
    assert len(llm.calls) == cfg.llm.max_retries + 1


def test_router_copilot_select_top_candidate_executes(tmp_path):
    _, router = _copilot_router(tmp_path, _result([_cand(101), _cand(102)]))
    router.route(_msg("帮我抄作业打 HS-9"))
    rr = router.route(_msg("1"))
    assert rr.kind == "execute"
    assert rr.plan.copilot.enable is True
    assert rr.plan.copilot.jobs[0].job_id == 101
    assert os.path.exists(rr.plan.copilot.jobs[0].filename)


def test_router_copilot_select_alternate_number(tmp_path):
    _, router = _copilot_router(tmp_path, _result([_cand(101), _cand(102)]))
    router.route(_msg("帮我抄作业打 HS-9"))
    rr = router.route(_msg("2"))
    assert rr.kind == "execute" and rr.plan.copilot.jobs[0].job_id == 102


def test_router_copilot_cancel(tmp_path):
    _, router = _copilot_router(tmp_path, _result([_cand(101)]))
    router.route(_msg("帮我抄作业打 HS-9"))
    rr = router.route(_msg("取消"))
    assert rr.kind == "reply" and "取消" in rr.reply
    # 会话已清：再回 "1" 不应再触发执行。
    assert router.route(_msg("1")).kind == "reply"


def test_router_copilot_no_passing_candidates(tmp_path):
    _, router = _copilot_router(tmp_path, _result([_cand(101, passed=False)]))
    rr = router.route(_msg("帮我抄作业打 HS-9"))
    assert rr.kind == "reply" and "没有" in rr.reply
    assert router.route(_msg("1")).kind == "reply"  # 没建会话


def test_router_copilot_unknown_stage(tmp_path):
    cfg = _cfg(tmp_path)

    def boom(stage, roster, level_id_override=None):
        raise StageResolutionError("no")

    router = Router(
        cfg, FakeLLM(json.dumps({"action": "copilot", "copilot": {"scope": "single", "stage": "HS-9"}})),
        PROMPT, SCHEMA, copilot_catalog_fn=boom, roster_provider=lambda: None,
    )
    rr = router.route(_msg("帮我抄作业打 HS-9"))
    assert rr.kind == "reply" and "没找到关卡" in rr.reply


def test_router_copilot_missing_catalog_file(tmp_path):
    def boom(stage, roster, level_id_override=None):
        raise FileNotFoundError("stage_catalog.json")

    router = Router(
        _cfg(tmp_path),
        FakeLLM(json.dumps({"action": "copilot", "copilot": {"scope": "single", "stage": "HS-9"}})),
        PROMPT, SCHEMA, copilot_catalog_fn=boom, roster_provider=lambda: None,
    )
    rr = router.route(_msg("帮我抄作业打 HS-9"))
    assert rr.kind == "reply" and "索引" in rr.reply


def test_router_copilot_all_new_not_yet(tmp_path):
    llm = FakeLLM(json.dumps({"action": "copilot", "copilot": {"scope": "all_new", "stage": ""}}))
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA, roster_provider=lambda: None)
    rr = router.route(_msg("新活动出了，帮我抄作业把新关都打了"))
    assert rr.kind == "reply" and rr.plan is None and "批量" in rr.reply


def test_router_copilot_single_without_stage_asks(tmp_path):
    llm = FakeLLM(json.dumps({"action": "copilot", "copilot": {"scope": "single", "stage": ""}}))
    router = Router(_cfg(tmp_path), llm, PROMPT, SCHEMA, roster_provider=lambda: None)
    rr = router.route(_msg("抄一份作业打活动"))
    assert rr.kind == "reply" and "哪一关" in rr.reply


def test_router_copilot_session_ttl_expires(tmp_path):
    clock = {"t": 1000.0}
    _, router = _copilot_router(tmp_path, _result([_cand(101)]))
    router.now_fn = lambda: clock["t"]
    router.route(_msg("帮我抄作业打 HS-9"))
    clock["t"] += router.cfg.copilot.confirm_ttl_s + 1  # 过期
    rr = router.route(_msg("1"))  # 过期后 "1" 不再选候选，落到 fresh 路由
    assert rr.kind == "reply"


# --- 失败后决策（§六②，injected failure event）---

def test_start_failure_decision_builds_message_and_pending(tmp_path):
    _, router = _copilot_router(tmp_path, _result([_cand(101)]))
    msg = router.start_failure_decision(
        "oc_1", "HS-7", _result([_cand(201), _cand(202)]),
        alternatives=[_cand(201), _cand(202)],
        remaining_stages=["HS-8", "HS-9"], detail="打到 2:31 暴毙，耗理智 15",
    )
    assert "HS-7" in msg and "换作业 #201" in msg and "跳过" in msg
    assert "HS-8" in msg  # 剩余关卡
    assert "自动续跑还没接入" in msg


def test_failure_decision_change_job_executes(tmp_path):
    _, router = _copilot_router(tmp_path, None)
    router.cfg.copilot.jobs_dir = str(tmp_path / "jobs")
    router.start_failure_decision(
        "oc_1", "HS-7", _result([_cand(201), _cand(202)]),
        alternatives=[_cand(201), _cand(202)], remaining_stages=["HS-8"],
    )
    rr = router.route(_msg("1"))
    assert rr.kind == "execute" and rr.plan.copilot.jobs[0].job_id == 201


def test_failure_decision_skip(tmp_path):
    _, router = _copilot_router(tmp_path, None)
    router.start_failure_decision(
        "oc_1", "HS-7", _result([_cand(201)]), alternatives=[_cand(201)],
        remaining_stages=["HS-8", "HS-9"],
    )
    rr = router.route(_msg("跳过"))
    assert rr.kind == "reply" and "跳过 HS-7" in rr.reply and "HS-8" in rr.reply
    # 不能假装自动续跑：文案必须明说续跑还没接入（#6 未接线前的诚实边界）。
    assert "自动续跑还没接入" in rr.reply
    assert router.route(_msg("1")).kind == "reply"  # 会话已清，不残留续跑意图


def test_failure_decision_cancel(tmp_path):
    _, router = _copilot_router(tmp_path, None)
    router.start_failure_decision("oc_1", "HS-7", _result([_cand(201)]), alternatives=[_cand(201)])
    rr = router.route(_msg("取消"))
    assert rr.kind == "reply" and "收工" in rr.reply


def _raise_catalog(stage, roster, level_id_override=None):
    raise AssertionError("catalog_fn should not be called in this test")
