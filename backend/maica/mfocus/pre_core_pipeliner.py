"""
Just as we said, we need a pipeline to connect everything pre-generation together.

Could include:
- MFocus main of course
- mf_check_mt
...
"""
from typing import *

from .mfocus_llm import MfPipeliner, ToolsResults
from .agent_modules import AgentTools, agent_tools
from maica.mtools import make_postmail, make_inspire, ms_from_cache, zsco
from maica.maica_utils import *

_Bt = BilingualText

async def pre_core_pipelines(
        session: MaicaSession,
        fsc: FullSocketsContainer,
        sp: SessionPersistent,
        st: SessionTrigger,
    ):
    """Schedule everything here."""
    session_item = session[-1]

    # Tools result must be kept along the whole procedure to ensure filtering working
    tools_results: ToolsResults = {}


    async def name_repl_pipeline():
        """Simple pipeline to assign real name if required."""
        # prompt_pname_repl implementation
        if fsc.maica_settings.pname_repl_now:
            pname = sp.pname
            if pname:
                sync_messenger(info=f"Using pname {pname} due to prompt_pname_repl", type=MsgType.DEBUG)
                session_item.context.player_name = pname
        # prompt_monika_nickname implementation
        if fsc.maica_settings.monika_nickname_now:
            mname = sp.mname
            if mname:
                sync_messenger(info=f"Adding mname {mname} due to prompt_monika_nickname", type=MsgType.DEBUG)
                session_item.context.monika_nickname = mname


    async def mf_pipeline():
        """
        This shall go last in sequence, since any extra known_info is useful.
        The ultimate goal of MFocus is just filling known_info.
        """
        if (
            fsc.maica_settings.use_mf_now
        ):

            mfp = MfPipeliner(session, fsc, sp, tools_results)
            generated_guidance, parsed_results = await mfp.run_mf_pipeline()

            # We pop all tool results to apply the parsed results
            for i in agent_tools:
                session_item.context.known_info.pop(i, None)

            # Then we inject what we got into session
            if (
                fsc.maica_settings.mf_concl_now
                and generated_guidance
            ):
                session_item.context.known_info.update({"generated_guidance": generated_guidance})
            else:
                session_item.context.known_info.update(parsed_results)

            # 心情注入：必须放在 known_info 构建【之后】。
            # 数据来自前端 savefile 载荷的 maica_mood（{value, label, dev, reason}），
            # 只存在于临时层（sp.content_temp），因此不落存档、键缺失时静默跳过。
            #
            # ★ 2026-10-18：不再注入裸数字，改成【按档位给这一轮的行为指令】。
            # ★ 2026-10-22：档位改成按 dev（偏离量）判，不再按绝对心情。
            #   原因（实测）：好感 100+ 时基线 +30，dev 只有 ±40 → 心情永远在
            #   [-10, +70]，"心情不高"那一档物理上到不了，"心情好"要 dev>10 才触发，
            #   于是 90% 的轮次都是"平静 = 没有指令"，玩家感觉心情系统没作用。
            #   现在按 dev 分 5 档，任何好感档位都够得着；旧前端没有 dev 时退回按
            #   绝对值粗分（兼容）。
            try:
                _mood = sp.content_temp.get('maica_mood') if sp.content_temp else None
            except Exception:
                _mood = None
            if _mood is not None:
                _mv = _ml = _md = None
                if isinstance(_mood, dict):
                    _mv = _mood.get('value')
                    _ml = _mood.get('label')
                    _md = _mood.get('dev')
                else:
                    _mv = _mood
                try:
                    _mdi = float(_md) if _md is not None else None
                except Exception:
                    _mdi = None
                if _mdi is None:
                    try:
                        _mvi = float(_mv) if _mv is not None else None
                    except Exception:
                        _mvi = None
                    if _mvi is not None:      # 旧载荷：只能按绝对值粗分
                        _mdi = 20.0 if _mvi > 40 else (-20.0 if _mvi < -20 else 0.0)
                if _mdi is not None:
                    if _mdi >= 20:
                        _mtxt = u'【本轮状态】你此刻心情很好：话可以多一点、主动起话头、愿意逗他，可以撒娇。'
                    elif _mdi >= 8:
                        _mtxt = u'【本轮状态】你此刻心情不错：比平时稍主动一点，语气轻快一点。'
                    elif _mdi >= -8:
                        _mtxt = u'【本轮状态】你此刻心情平静。'
                    elif _mdi >= -20:
                        _mtxt = u'【本轮状态】你此刻有点低落：句子短一些、少提问；他问你时正常回答。'
                    else:
                        _mtxt = u'【本轮状态】你此刻心情很低：话很短、不主动开新话题；但不要用沉默惩罚他。'
                    _mtxt += u'（这是状态不是话题：别说出「心情」这两个字，直接体现在语气里。）'
                    # ★ 放到 mood_tail（utilize 会把它拼在系统提示词最末尾），不进 known_info：
                    #   ① 末尾 recency 最强；② known_info 可能被 generated_guidance 整段顶掉。
                    session_item.context.mood_tail = _mtxt
                    sync_messenger(info='[mood] tail: %s (dev=%.1f label=%r)'
                                   % (_mtxt, _mdi, _ml), type=MsgType.DEBUG)


    async def precheck_mt_pipeline():
        """Former react_trigger. Pre-detects if request could be satisfied by mt."""
        if (
            # This does not actually require mf, suprisingly
            # But still we need to confirm prompt writable
            fsc.maica_settings.prompt_writable
            and fsc.maica_settings.use_mt_now
            and fsc.maica_settings.extra.mf_precheck_mt
        ):
            requested, operation = await st.predict_trigger(session_item.content)
            sync_messenger(info=f"Precheck mt responded, requested: {requested}, operation: {operation}", type=MsgType.PRIM_LOG)

            if requested:
                if operation:
                    t = _Bt(
                        f"你可以完成{{player_name}}的请求({operation}), 请作正面回答.",
                        f"You can satisfy {{player_name}}'s request({operation}), please answer positively.",
                    )
                else:
                    t = _Bt(
                        "{player_name}的请求可能无法完成, 请根据实际情况作答.",
                        "{player_name}'s request could be not satisfiable, please answer according to actual situation.",
                    )
                session_item.context.known_info.update({"mt_prediction": t})


    async def form_mp_pipeline():
        """
        This shall go first in sequence, since it fills content.
        We have a tiny clever design to let mn detect if mp is poem or letter. Now we do it here together with letter forming.
        """
        if (
            fsc.maica_settings.temp.activated == "mpostal"
        ):
            prompt_text = await make_postmail(fsc)
            session_item.content = prompt_text


    async def form_ms_pipeline():
        """Basically same position with mp_pipeline."""
        if (
            fsc.maica_settings.temp.activated == "mspire"
        ):
            prompt_text = await make_inspire(fsc)
            prompt_text = replace_prompt_placeholders(
                prompt_text.to_str(fsc.maica_settings.basic.target_lang),
                {"player_name": session_item.context.player_name},
            )
            session_item.content = prompt_text

            # MSpire has cache mechs
            if (
                fsc.maica_settings.temp.mspire.use_cache
                # We disable cache in multi-rounds
                and not len(session) > 2
            ):
                mfc_m = await ms_from_cache(prompt_text, fsc)
                fsc.maica_settings.temp.mspire._mfc_m = mfc_m


    async def const_mf_pipeline():
        """Call tools for mf_const_tools and mf_const_sf_access."""
        if (
            # (Kind of) suprisingly it does not actually require mf to be enabled
            # Still, we need to confirm prompt writable
            fsc.maica_settings.prompt_writable
        ):

            mf_const_tools = fsc.maica_settings.extra.mf_const_tools
            mf_const_sf_access = fsc.maica_settings.extra.mf_const_sf_access

            toolbox = AgentTools(fsc, sp)

            # Tweak mf_const_tools level if twk_info should be applied
            # The datetime and possibly weather will be useful for MP and MS, especially when they don't actively call tools by default
            if fsc.maica_settings.temp.common.twk_info:
                mf_const_tools = max(mf_const_tools, 2)

            if mf_const_tools >= 1:

                sync_messenger(info="MFocus calling mf_const_tools level 1", type=MsgType.DEBUG)
                for tool_name in ("time_acquire", "event_acquire"):
                    # By this const param, we mark this tool query is sent by mf_const_tools which may affect tools' behavior
                    # All mf tools are enforced to accept **kwrags, so this is fine even when it does nothing
                    tools_results[tool_name] = await getattr(toolbox, tool_name)(const=True)

            if mf_const_tools >= 2:

                sync_messenger(info="MFocus calling mf_const_tools level 2", type=MsgType.DEBUG)
                for tool_name in ("date_acquire", "weather_acquire"):
                    tools_results[tool_name] = await getattr(toolbox, tool_name)(const=True)

            if (
                mf_const_sf_access >= 1
                and fsc.real_sf_access_impl >= 1
            ):
                sync_messenger(info="MFocus calling mf_const_sf_access", type=MsgType.DEBUG)
                tool_name = "persistent_acquire"
                text, body = await getattr(toolbox, tool_name)(query=session_item.content, const=True)
                
                sync_messenger(info=f"MFocus mf_const_sf_access responded: {text}", type=MsgType.INFO)
                tools_results[tool_name] = (text, body)

            # Why we keep empty here:
            # In short terms, to prevent MFocus calling known-already info again, even if the answer is "there's no".
            # At the end of mf_pipeline, another parse_tools_results is run but ignore_empty = True.
            # This way only MFocus model gains a full glance of negative info, core model's prompt still keeps concise.
            parsed_results = MfPipeliner.parse_tools_results(tools_results, ignore_empty=False)
            session_item.context.known_info.update(parsed_results)


    async def std_content_pipeline():
        """A simple pipeline to make session_item.content str, to avoid confusion."""
        session_item.content = to_str(session_item.content, fsc.maica_settings.basic.target_lang)


    async def generic_helper_pipeline():
        """Utilizes RAG to search datasets for zero-shot like learning for generic core model."""
        if (
            fsc.maica_settings.prompt_system_writable
            and zsco.generic_helper
        ):
            res_set = await zsco.generic_helper.search(session_item.content)
            session_item.context.generic_help = list(res_set)


    # Finally, form all these together
    tasks_stages: list[list[Callable[[], Awaitable]]] = [
        [
            name_repl_pipeline, # sync
        ],
        [
            form_mp_pipeline,
            form_ms_pipeline,
        ],
        [
            std_content_pipeline, # sync
        ],
        [
            const_mf_pipeline,

            # begin here
            generic_helper_pipeline,
            precheck_mt_pipeline, 
        ],
        [
            mf_pipeline,

            # retrieve here
            generic_helper_pipeline,
            precheck_mt_pipeline,
        ],
    ]

    await run_staged_tasks(tasks_stages)

    # And we should be good to move on
