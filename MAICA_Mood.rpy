# -*- coding: utf-8 -*-
# ============================================================================
#  本地后端切换（MAICA 心情插件）v1
#
#  1. 往官方「服务提供节点」列表里加一个节点（id 9998 → 127.0.0.1:5100/6100）
#  2. 心情：收莫妮卡回复里的情绪标签 → 引擎 → 写进官方控制台
#  3. session_len_limit 随节点自动切换（我们的节点 10240，切回官方还原 8192）
#  4. 掩掉官方 status 撞名导致的 is_task_failed() 误判
#
#  ★ 不修改任何官方文件；卸载 = 删掉本文件夹
#  ★ 心情值走官方已有的自由 dict（savefile → 后端 content_temp），
#    而且只在自己的节点上发送，官方节点收不到任何多余字段
# ============================================================================

init -999 python:
    # ★ 2026-10-20：清掉【上次异常退出】留下的单实例锁。
    #
    # 背景：MAS 的 python-packages/singleton.py 用 os.open(O_CREAT|O_EXCL) 建一个
    # 临时文件当锁，在 init 0（game/0utils.rpy）里创建；文件已存在就抛
    # SingleInstanceException("Another instance is already running")，表现为
    # 「双击没反应 / 游戏打不开」。
    #
    # 触发链（2026-10-20 实测踩到）：游戏被强杀时，插件拉起的后端 python 进程
    # 还活着，而 Python 2 的 os.open 句柄在 Windows 上会被子进程继承 →
    # 游戏进程没了，文件仍被后端占着 → 删不掉 → 之后每次启动都在 init 阶段退出。
    #
    # 这里在它之前（-999 < 0）尝试 unlink：
    #   · 能删掉  = 没有进程持有它 = 是残留，清掉即可
    #   · 删不掉  = 真有实例在跑 = 不能动（保住单实例保护）
    try:
        import os as _os, sys as _sys, tempfile as _tf
        _mm_lockbase = _os.path.splitext(_os.path.abspath(_sys.argv[0]))[0]
        _mm_lockbase = _mm_lockbase.replace("/", "-").replace(":", "").replace("\\", "-") + "-.lock"
        _mm_lockfile = _os.path.normpath(_os.path.join(_tf.gettempdir(), _mm_lockbase))
        if _os.path.isfile(_mm_lockfile):
            try:
                _os.unlink(_mm_lockfile)
                print("[mood-plugin] removed stale single-instance lock: %s" % _mm_lockfile)
            except OSError:
                pass
    except Exception:
        pass

init -985 python:
    import os
    import sys
    _mm_dir = os.path.join(renpy.config.basedir, "game", "Submods", "MAICA_Mood")
    for _p in (_mm_dir, os.path.join(_mm_dir, "lib")):
        if os.path.isdir(_p) and _p not in sys.path:
            sys.path.insert(0, _p)

init -984 python:
    import maica_mood_lib as maica_mood
    store.maica_mood = maica_mood
    # ★ 自愈：venv 的 pyvenv.cfg 里是绝对路径，插件被拷到新位置后自带 Python 会去找
    #   原目录。这里每次加载都校正一次 → 整个文件夹拷到哪都能跑，不依赖安装器。
    store._mm_runtime_ok = maica_mood.ensure_runtime()
    store._mm_state = {
        "last_tick": 0.0, "ready": None, "pending": [],
        "engine": None, "engine_mod": None,
        "node_ok": False, "failmask_ok": False, "harvest_ok": False,
        "sess_ok": False, "last_shown": None,
    }

init -990 python in mas_submod_utils:
    # ★ 标准注册写法：MAS 子模组框架要求 Submod() 在 init -990、mas_submod_utils 里创建，
    #   否则依赖检查不会执行（官方 wiki: Interfacing with the Submod Framework）。
    #   老版本 MAS 的 Submod 不认 settings_pane / dependencies / version_updates →
    #   逐级回退，绝不能让插件在 init 阶段崩掉。
    _mm_submod_kw = dict(
        author=u"Luna4682",
        name=u"MAICA_Mood（本地后端）",
        description=u"在官方节点列表里增加一个本地后端节点：自定义人设、心情系统与分层记忆，并让会话长度门槛随节点自动切换。",
        version="1.0.2",
    )
    try:
        Submod(settings_pane="maica_mood_settings", dependencies={},
               version_updates={}, **_mm_submod_kw)
    except TypeError:
        try:
            Submod(settings_pane="maica_mood_settings", **_mm_submod_kw)
        except TypeError:
            Submod(**_mm_submod_kw)

init -983 python:
    try:
        with open(store.maica_mood.log_path(), "a") as _f:
            _f.write("INIT: MAICA_Mood.rpy loaded; basedir=" + str(renpy.config.basedir) + "\n")
    except Exception:
        pass

init -4 python:
    # ★ 2026-10-24：自己注册「API Keys」里需要的字段。
    #   原来这 14 个字段全由 MAICA_ServerSubmod 注册（那个子模组主要是给
    #   "本地部署"用的，我们并不用它）。现在只注册我们真正会用的 4 个，
    #   玩家只装 ChatSubmod 也能填服务商信息，ServerSubmod 变成可选。
    #   其余字段（MFocus / MVista / MNerve / 搜索代理）不注册：
    #   我们的节点用不到；如果玩家自己装了 ServerSubmod，它们照样存在，
    #   而且下面 _MM_API_MAP 仍然会把它们读进 .env（高级用法不受影响）。
    #   mas_registerAPIKey 自带「已注册就跳过」的保护（zz_apikeys.rpy:299），
    #   放在 init -4 是为了让 ServerSubmod 的 init -5 先跑。
    _MM_API_KEY_FIELDS = (
        ("maica_core_url", u"服务商 url"),
        ("maica_core_key", u"服务商 api key"),
        ("maica_core_model", u"模型名称"),
        ("maica_tp_apis", u"第三方 API 配置（可选，JSON）"),
    )
    _mm_api_reg_ok = 0
    _mm_api_reg_skip = 0
    for _mm_k, _mm_n in _MM_API_KEY_FIELDS:
        try:
            if store.mas_registerAPIKey(_mm_k, _mm_n):
                _mm_api_reg_ok += 1
            else:
                _mm_api_reg_skip += 1
        except Exception:
            _mm_api_reg_skip += 1
    try:
        with open(store.maica_mood.log_path(), "a") as _f:
            _f.write("INIT: api key fields registered=%d skipped=%d\n"
                     % (_mm_api_reg_ok, _mm_api_reg_skip))
    except Exception:
        pass

    # ★ 说明文字要跟着"实际显示的名字"走：
    #   自己注册成功 → 字段叫「服务商 url」；
    #   官方 MAICA_ServerSubmod 先注册了 → 字段叫「核心服务商 url」。
    #   两边共用同一份 api_keys.json，所以填哪个都一样。
    _mm_api_short = bool(_mm_api_reg_ok)
    _mm_f_url = u"服务商 url" if _mm_api_short else u"核心服务商 url"
    _mm_f_key = u"服务商 api key" if _mm_api_short else u"核心服务商 api key"
    _mm_f_model = u"模型名称" if _mm_api_short else u"核心模型名称"
    _mm_f_tp = u"第三方 API 配置" if _mm_api_short else u"第三方API配置"
    _MM_API_HELP = [
        u"API 设置填法（设置 → API Keys）：",
        u"· %s —— 例如 https://api.deepseek.com/v1（填到 /v1 为止）" % _mm_f_url,
        u"· %s —— sk- 开头那一串（和下面「%s」二选一，填一处就够）" % (_mm_f_key, _mm_f_tp),
        u"· %s —— 例如 deepseek-flash" % _mm_f_model,
        u"· %s —— ① 放 sk- 开头的 key ② 或写 JSON 开联网搜索/天气，例如 {{\"SEARXNG_ADDR\": \"http://127.0.0.1:8888\"}}；留空不影响聊天" % _mm_f_tp,
        u"改完要重启游戏才生效",
    ]

init python:
    import time
    import traceback

    def _mm_log(msg):
        try:
            store.mas_submod_utils.submod_log.info("[mood-plugin] " + msg)
        except Exception:
            pass
        try:
            with open(store.maica_mood.log_path(), "a") as _f:
                _f.write(time.strftime("%H:%M:%S") + " " + msg + "\n")
        except Exception:
            pass

    def _mm_ai():
        try:
            return store.maica.maica_instance
        except Exception:
            return None

    def _mm_affection():
        """当前好感度（取不到返回 None）。

        ★ 2026-10-22：开发期覆盖 —— **存档目录**里的 `dev_affection.txt` 写一个数字，
        就用它当好感度（测「74 看不到节点／100 能看到」这种门槛用，只影响那个存档）。
        文件不存在时走真实值。发布包里不能带这个文件（deploy 也不会自动建）。
        """
        _ov = _mm_dev_affection()
        if _ov is not None:
            return _ov
        try:
            return float(store._mas_getAffection())
        except Exception:
            return None

    def _mm_dev_affection():
        """开发期好感覆盖：文件放在**存档目录**里，只影响那一个存档。

        ★ 2026-10-22 改：一开始放在插件目录，结果所有启动方式都读到它 ——
          用 exe 直接开主存档（真实好感 110+）时也被压成 74，节点凭空消失。
          现在放 `renpy.config.savedir/dev_affection.txt`：mood-test 那套测试存档
          和 APPDATA 的主存档互不干扰。
        """
        try:
            import os as _os
            d = getattr(renpy.config, "savedir", None)
            if not d:
                return None
            p = _os.path.join(d, "dev_affection.txt")
            if _os.path.isfile(p):
                with open(p, "r") as f:
                    v = (f.read() or "").strip()
                if v:
                    return float(v)
        except Exception:
            pass
        return None

    def _mm_affection_ok():
        """★ 好感门槛：与官方一致 —— 好感 ≥ 100 才能进 MAICA。

        依据：官方前端 README「提示」第 1 条「完整的功能只会在好感度达到 100 后开放」。
        判定以【数值 100 为准】（不是档位函数）：mas_isMoniAff 对应哪个档位随 MAS 版本
        可能变，而 README 写的是 100，直接比数值最稳。

        开发期绕过：插件目录里放一个 `dev_bypass_affection` 文件即放行（打包前删掉）。
        """
        if _mm_dev_bypass():
            return True
        try:
            # 卸载进行中：把我们的节点从列表里撤掉，别再让玩家选回去
            if store._mm_state.get("uninstall_mode"):
                return False
        except Exception:
            pass
        a = _mm_affection()
        if a is None:
            # 读不到好感度时【不放行】—— 宁可让玩家先刷好感，也不要绕过设计
            return False
        return a >= float(getattr(store.maica_mood, "AFFECTION_MIN", 100))

    def _mm_dev_bypass():
        """开发期开关：<插件>\\dev_bypass_affection 存在 → 跳过好感门槛。"""
        try:
            import os as _os
            p = _os.path.join(store.maica_mood.plugin_root(), "dev_bypass_affection")
            return _os.path.isfile(p)
        except Exception:
            return False

    def _mm_our_node():
        try:
            if int(persistent.maica_setting_dict.get("provider_id")) != store.maica_mood.NODE_ID:
                return False
            return _mm_affection_ok()
        except Exception:
            return False

    def _mm_official_pid():
        """官方默认节点 id（= 前端 header.rpy:237 的 `1 if renpy.windows else 2`）。"""
        try:
            return int(store.maica_mood.official_fallback_pid())
        except Exception:
            return 1 if renpy.windows else 2

    def _mm_prepare_uninstall():
        """卸载握手：bat 放下 .mm_uninstall_request → 这里把一切还给官方，然后放行删除。

        ★ 为什么要插件来做：session_len_limit / 采样参数都在 persistent 里，
          目录一删就没有代码能把它们改回去（bat 读不了 pickle）。
          所以卸载脚本会先建请求文件、启动游戏，让插件自己还原，再删目录。

        顺序（少一步都会留残局）：
          ① provider_id 切回官方默认节点
          ② 采样参数还原（_mm_super_restore：只还我们写过的键）
          ③ session_len_limit 还原（_mm_session_limit 按新 pid 立即算一次）
          ④ 停掉我们拉起的三个服务（不占端口、不占内存）
          ⑤ 写"还原完成"凭据 .mm_uninstall_done（卸载脚本只认它）+ 通知玩家
        """
        st = store._mm_state
        try:
            if not store.maica_mood.uninstall_requested():
                return
        except Exception:
            return
        if st.get("uninstall_done"):
            return
        st["uninstall_done"] = True
        st["uninstall_mode"] = True

        d = persistent.maica_setting_dict
        fallback = _mm_official_pid()
        # ① 切回官方节点（直接写值：不要走 sync_provider_id —— 它会去重连，
        #    而这时我们的后端正要被关掉，重连只会制造失败日志）
        try:
            cur = d.get("provider_id")
            if int(cur) == store.maica_mood.NODE_ID:
                d["provider_id"] = fallback
                ai = _mm_ai()
                if ai is not None:
                    ai.provider_id = fallback
                _mm_log("uninstall: provider %s -> %s (official default)" % (cur, fallback))
        except Exception:
            _mm_log("uninstall: switch provider FAILED: %r" % (sys.exc_info()[1],))
        # ② 采样参数
        try:
            _mm_super_restore()
        except Exception:
            _mm_log("uninstall: super restore FAILED: %r" % (sys.exc_info()[1],))
        # ③ 会话门槛（现在 pid 已不是我们的 → wanted_session_len 会给出官方值）
        try:
            _mm_session_limit()
        except Exception:
            _mm_log("uninstall: session limit FAILED: %r" % (sys.exc_info()[1],))
        # ④ 服务
        # ★ 2026-10-24：卸载路径必须【等进程真的退出】再写完成凭据。
        #   原来 stop_all(wait=False) 只是发了终止请求，卸载脚本立刻 rmdir，
        #   而 watchdog / 本地 python 的可执行文件还映射着 → 每次都报“文件被占用”。
        try:
            _mm_extractor_stop(wait=True)
        except Exception:
            _mm_log("uninstall: stop services FAILED: %r" % (sys.exc_info()[1],))
        try:
            time.sleep(1.5)   # 再留一点时间给句柄释放
        except Exception:
            pass
        # ⑤ 标记：先落盘，再写"还原完成"凭据（卸载脚本只认这个文件）
        try:
            store.maica_mood.set_node_marker(False, d.get("provider_id"),
                                             d.get("session_len_limit"))
        except Exception:
            _mm_log("uninstall: marker FAILED: %r" % (sys.exc_info()[1],))
        try:
            # 脚本下一步就要删目录，先把 persistent 落盘
            renpy.save_persistent()
        except Exception:
            pass
        try:
            store.maica_mood.clear_uninstall_request()
            ok = store.maica_mood.mark_uninstall_done()
            _mm_log("uninstall: ready (provider=%s limit=%s done=%s)"
                    % (d.get("provider_id"), d.get("session_len_limit"), ok))
        except Exception:
            _mm_log("uninstall: done marker FAILED: %r" % (sys.exc_info()[1],))
        try:
            renpy.notify(u"设置已还原成官方，现在可以退出游戏了（卸载会继续）")
        except Exception:
            pass

    def _mm_affection_guard():
        """已选中我们的节点但好感不够 → 切回官方默认节点（只提示一次）。

        两种情况会走到这里：
          ① 玩家先到 100 选了我们的节点，后来好感掉下去；
          ② 存档被改/迁移导致 provider_id 残留。
        """
        st = store._mm_state
        try:
            pid = int(persistent.maica_setting_dict.get("provider_id"))
        except Exception:
            return
        if pid != store.maica_mood.NODE_ID:
            st["aff_guard_notified"] = False
            return
        if _mm_affection_ok():
            st["aff_guard_notified"] = False
            return
        # 切回官方默认节点（Windows=1，其它=2，与官方 header.rpy 默认一致）
        fallback = _mm_official_pid()
        try:
            sync_provider_id(fallback)
        except Exception:
            try:
                persistent.maica_setting_dict["provider_id"] = fallback
                ai = _mm_ai()
                if ai is not None:
                    ai.provider_id = fallback
            except Exception:
                pass
        if not st.get("aff_guard_notified"):
            st["aff_guard_notified"] = True
            _mm_log("affection gate: %.1f < %d, switched back to official node %d"
                    % (_mm_affection() or 0.0, getattr(store.maica_mood, "AFFECTION_MIN", 100), fallback))
            try:
                renpy.notify(u"好感度达到 100 后才会解锁「MAICA_Mood 本地后端」节点")
            except Exception:
                pass

    # ---------------- 挂钩（幂等） ----------------
    def _mm_ensure():
        st = store._mm_state
        # ★ 2026-10-22：清残留服务（netstat + wmic/powershell 子进程）挪到后台线程了。
        #   原来在主线程第一个 tick 里跑，冷启动能卡好几秒，正好撞上 Ren'Py 的
        #   开局性能自检（5 帧 / 0.333 秒）→ 玩家每次启动都看到"性能影响警告"。
        #   现在由 start_service_thread 的线程负责，结果通过 st["stale_killed"] 回报。
        if st.get("stale_killed") and not st.get("stale_logged"):
            st["stale_logged"] = True
            _mm_log("startup: cleaned stale services %r" % (st["stale_killed"],))
        if not st["node_ok"]:
            try:
                from maica_provider_manager import MaicaProviderManager
                store.maica_mood.install_node(MaicaProviderManager, allow=_mm_affection_ok)
                st["node_ok"] = True
                _mm_log("node registered (id=%d, affection gate=%s)"
                        % (store.maica_mood.NODE_ID, _mm_affection_ok()))
            except Exception:
                _mm_log("node hook FAILED: %r" % (sys.exc_info()[1],))
        if not st["failmask_ok"]:
            try:
                import maica_tasker
                store.maica_mood.install_failed_mask(maica_tasker.MaicaTaskManager)
                st["failmask_ok"] = True
                _mm_log("is_task_failed mask installed")
            except Exception:
                _mm_log("failmask FAILED: %r" % (sys.exc_info()[1],))
        if not st["harvest_ok"]:
            ai = _mm_ai()
            target = getattr(ai, "MoodStatus", None) if ai is not None else None
            orig = getattr(target, "analyze", None)
            if orig is not None:
                def _wrapped(message, *a, **kw):
                    try:
                        _mm_collect(message)
                    except Exception:
                        pass
                    return orig(message, *a, **kw)
                try:
                    target.analyze = _wrapped
                    st["harvest_ok"] = True
                    _mm_log("harvest hooked on MoodStatus.analyze")
                except Exception:
                    _mm_log("harvest hook FAILED: %r" % (sys.exc_info()[1],))
            else:
                _mm_log("harvest: MoodStatus.analyze not found yet")

    def _mm_collect(text):
        st = store._mm_state
        known = None
        if st["engine_mod"] is not None:
            known = getattr(st["engine_mod"], "KEYWORD_DELTA", None)
        for w in store.maica_mood.harvest_tags(text, known):
            if w not in st["pending"]:
                st["pending"].append(w)

    # ---------------- 心情引擎 ----------------
    def _mm_engine():
        st = store._mm_state
        if st["engine"] is not None:
            return st["engine"]
        try:
            import monika_mood_engine as me
            st["engine_mod"] = me
            eng = me.MonikaMood(0)
            # ★ 2026-10-22：跨会话恢复（离线按 24 小时半衰期回落）
            try:
                saved = getattr(persistent, "_mm_mood_state", None)
                if isinstance(saved, dict):
                    eng.import_state(saved)
                    _mm_log("mood restored: dev=%+.1f" % eng.deviation)
            except Exception:
                pass
            st["engine"] = eng
            _mm_log("mood engine created")
        except Exception as e:
            _mm_log("engine load failed: %r" % (e,))
        return st["engine"]

    def _mm_save_mood(eng):
        try:
            persistent._mm_mood_state = eng.export_state()
        except Exception:
            pass

    def _mm_praise(text):
        """玩家这句话在夸她吗：是 → 记一次事件冲击（带防刷）。"""
        if not text:
            return False
        eng = _mm_engine()          # 第一次发消息时引擎可能还没建
        if eng is None:
            return False
        try:
            import mood_praise as mp
            weight, word, why = mp.detect(text)
            if not word:
                return False
            if eng.add_praise(word, weight):
                _mm_log("praise: +%.1f word=%s turn=%d" % (weight, word, eng.turns))
                return True
            _mm_log("praise: ignored (spam) word=%s turn=%d" % (word, eng.turns))
        except Exception:
            _mm_log("praise detect FAILED: %r" % (sys.exc_info()[1],))
        return False

    def _mm_show(eng):
        try:
            line = u"[mood] {} ({:+d})  base={:+d}  dev={:+.1f}  turns={}".format(
                eng.label(), eng.value(), int(eng.baseline()),
                eng.deviation, eng.turns)
            if line == store._mm_state["last_shown"]:
                return
            store._mm_state["last_shown"] = line
            import bot_interface
            bot_interface.write_unicode_command(store.mas_ptod, line)
        except Exception:
            pass

    def _mm_payload(eng):
        try:
            return {"value": eng.value(), "label": eng.label(),
                    "dev": round(float(eng.deviation), 2), "reason": eng.reason()}
        except Exception:
            return {"value": 0}

    def _mm_hook_sender():
        st = store._mm_state
        if st["sess_ok"]:
            return
        try:
            import maica_tasker_sub_sessionsender as snd
        except Exception:
            _mm_log("sender IMPORT FAILED: %r" % (sys.exc_info()[1],))
            return

        def _make_wrapper(_orig):
            def build_request(query, session, triggers, visions=None, pprt=False, **kw):
                # ★ 2026-10-18：玩家发消息 = 有对话活动 → 刷新时间戳 + 保证提取器该在跑
                try:
                    _svc = store._mm_state.get("svc")
                    if _svc is not None:
                        _svc["last_chat_at"] = time.time()
                        _svc["chat_active"] = True
                except Exception:
                    pass
                # ★ 2026-10-22：玩家这句是不是在夸她（只在我们的节点上算）
                try:
                    if _mm_our_node():
                        _mm_praise(query)
                except Exception:
                    pass
                data = _orig(query, session, triggers, visions, pprt, **kw)
                try:
                    if _mm_our_node():
                        sf = data.get("savefile")
                        if not isinstance(sf, dict):
                            sf = {}
                        eng = store._mm_state["engine"]
                        if eng is not None:
                            sf["maica_mood"] = _mm_payload(eng)
                            data["savefile"] = sf
                except Exception:
                    pass
                return data
            return build_request

        try:
            names = []
            for _name in dir(snd):
                _obj = getattr(snd, _name)
                if isinstance(_obj, type) and "build_request" in getattr(_obj, "__dict__", {}):
                    _obj.build_request = staticmethod(_make_wrapper(_obj.build_request))
                    names.append(_name)
            if names:
                st["sess_ok"] = True
                _mm_log("sender hooked on: " + ", ".join(names))
            else:
                st["sender_tries"] = st.get("sender_tries", 0) + 1
                if st["sender_tries"] <= 3:
                    _mm_log("sender: no class with build_request (try %d)" % st["sender_tries"])
        except Exception:
            st["sender_tries"] = st.get("sender_tries", 0) + 1
            if st["sender_tries"] <= 3:
                _mm_log("sender HOOK FAILED: %r" % (sys.exc_info()[1],))


    def _mm_panel_update(eng):
        try:
            p = store._mm_panel
            p["have"] = True
            p["label"] = eng.label()
            p["value"] = eng.value()
            # ★ 2026-10-22：进度条改成按 dev（偏离量）画，中点 = 平常心情。
            #   以前按绝对心情（-100..100），基线 +30 让它永远停在 65%~75%，
            #   玩家看着像"不动"。现在 dev=0 是半格，±40 是两端。
            _fl = float(getattr(eng, "MAX_DEV_DOWN", -40.0))
            _li = float(getattr(eng, "MAX_DEV_UP", 40.0))
            p["floor"] = _fl
            p["limit"] = _li
            _m = float(eng.deviation)
            p["ratio"] = max(0.0, min(1.0, (_m - _fl) / (_li - _fl))) if _li > _fl else 0.5
            _mm_log("panel: value=%s label=%s dev=%+.1f ratio=%.2f"
                    % (p["value"], p["label"], eng.deviation, p["ratio"]))
            _bk = eng.bucket()[0]
            p["bucket"] = _bk
            p["reason"] = eng.reason()
            p["detail"] = u"base {:+d}   dev {:+.1f}   turns {}".format(
                int(eng.baseline()), eng.deviation, eng.turns)
        except Exception:
            pass
    # ---------------- 回合边界 ----------------
    def _mm_turn():
        ai = _mm_ai()
        if ai is None:
            return
        eng = _mm_engine()
        if eng is None:
            return
        try:
            ready = bool(ai.is_ready_to_input())
        except Exception:
            ready = False
        prev = store._mm_state["ready"]
        store._mm_state["ready"] = ready
        if prev is False and ready:
            # ★ 2026-10-18：一轮对话结束也算活动 → 让提取器在聊天的两分钟内持续在线
            try:
                _svc = store._mm_state.get("svc")
                if _svc is not None:
                    _svc["last_chat_at"] = time.time()
            except Exception:
                pass
            words = store._mm_state["pending"]
            store._mm_state["pending"] = []
            try:
                if words:
                    eng.update_keywords(list(words))
                eng.set_affection(store._mas_getAffection())
                eng.on_turn()
                _mm_save_mood(eng)
            except Exception:
                _mm_log("mood update failed: %s" % traceback.format_exc())
            _mm_show(eng)
            _mm_panel_update(eng)
            try:
                renpy.restart_interaction()
            except Exception:
                pass

    # ---------------- 会话门槛 ----------------
    def _mm_session_limit():
        d = persistent.maica_setting_dict
        pid = d.get("provider_id")
        cur = d.get("session_len_limit", 8192)
        saved = getattr(persistent, "_mm_saved_session_len", None)
        new, own, keep = store.maica_mood.wanted_session_len(pid, cur, saved)
        if new != cur:
            d["session_len_limit"] = new
            try:
                store.maica.maica_instance.max_history_token = min(
                    new, store.maica_mood.SESSION_HARD_MAX)
            except Exception:
                pass
            _mm_log("session_len_limit %s -> %s (provider=%s)" % (cur, new, pid))
        persistent._mm_saved_session_len = keep

    # ---------------- 采样默认值：温度 1.0 / 输出 4096 ----------------
    _MM_SUPER_WANT = (("temperature", 1.0), ("max_tokens", 4096))

    def _mm_super_take():
        """在我们的节点上接管采样参数（并先记下玩家原值，供切走时还原）。

        ★ 2026-10-20：100 轮长会话实测 —— 1.0 稳定（均 41.7 字、问句 16%、零崩坏），
          1.5 从第 6 轮开始自回授、最后是词沙拉，0.22 太死板。前端默认是 0.22。
        max_tokens 一起设到 4096：deepseek-flash 是 thinking-first，
        前端默认 1600 会被思考吃掉，回复被截成一句。

        ★ 为什么要每个 tick 都跑、而不是只来一次：
          1) 切节点（sync_provider_id → accessable → GET /defaults）之后，
             前端的 modelconfig 可能被清空/回到服务端默认，温度就"不生效"了；
          2) 启动迁移里 `use_custom_model_config` 为 False 时会把 modelconfig 整个清成 {}，
             高级设置（含温度）就一个都不发 —— 这时 4096 看起来"生效"只是因为
             后端默认刚好也是 4096，而温度会掉回后端的 0.75。
        所以这里做三件事：
          · 第一次接管前，把玩家原来的「总开关 + 两个采样值」存进 _mm_super_snapshot
          · 保证 use_custom_model_config = True（否则高级设置根本不外发）
          · 只要 status 还是 False（= 没人手动设过）就重新写回 1.0/4096 并 apply
        玩家在高级设置里手动改过的项（status True 且值不是旧默认）一律不动。
        """
        try:
            adv = persistent.maica_advanced_setting
            sta = persistent.maica_advanced_setting_status
            d = persistent.maica_setting_dict
            changed = []

            # ⓪ 第一次接管：先给玩家原值拍快照（切回官方要按它还原）
            if not getattr(persistent, "_mm_super_snapshot", None):
                persistent._mm_super_snapshot = {
                    "cfg": bool(d.get("use_custom_model_config", False)),
                    "had": dict((k, (k in adv)) for k, _ in _MM_SUPER_WANT),
                    "adv": dict((k, adv.get(k)) for k, _ in _MM_SUPER_WANT),
                    "sta": dict((k, bool(sta.get(k, False))) for k, _ in _MM_SUPER_WANT),
                }

            # ① 高级设置的总开关：不开它，modelconfig 会被清空，什么都不发
            if not d.get("use_custom_model_config", False):
                d["use_custom_model_config"] = True
                persistent._mm_super_owned_cfg = True
                changed.append("use_custom_model_config")

            # ② 采样值
            #    owned = 我们写过的键。若玩家在 UI 里手动改成了"别的值"，交还控制权；
            #    若值被重置回旧默认（0.22/0.75/1600），视为被清掉 → 重新写回。
            _LEGACY = {"temperature": (0.22, 0.75), "max_tokens": (1600,)}
            owned = list(getattr(persistent, "_mm_super_owned", None) or [])
            for key, val in _MM_SUPER_WANT:
                cur = adv.get(key)
                status = bool(sta.get(key, False))
                if key in owned:
                    if status and cur not in (None, val) and cur not in _LEGACY[key]:
                        owned.remove(key)          # 玩家接管
                        changed.append(key + "(release)")
                        continue
                    if cur == val:
                        continue
                    adv[key] = val
                    sta[key] = True
                    changed.append(key + "(restore)")
                else:
                    if status:
                        continue                   # 玩家设过，不碰
                    adv[key] = val
                    sta[key] = True
                    owned.append(key)
                    changed.append(key)
            persistent._mm_super_owned = owned

            # ③ 一次性迁移：0.22（前端默认）/0.75（上一版用过）→ 1.0
            if not getattr(persistent, "_mm_super_migrated", False):
                persistent._mm_super_migrated = True
                _cur_t = adv.get("temperature")
                try:
                    _cur_t = float(_cur_t)
                except Exception:
                    _cur_t = None
                if _cur_t is not None and _cur_t in (0.22, 0.75):
                    adv["temperature"] = 1.0
                    sta["temperature"] = True
                    if "temperature" not in changed:
                        changed.append("temperature(migrate)")

            if not changed:
                return
            try:
                store.maica_apply_advanced_setting()
            except Exception:
                _mm_log("apply_advanced_setting FAILED: %r" % (sys.exc_info()[1],))
            try:
                ai = _mm_ai()
                if ai is not None:
                    ai.use_custom_model_config = True
                    ai.max_history_token = min(
                        d.get("session_len_limit", 8192),
                        store.maica_mood.SESSION_HARD_MAX)
            except Exception:
                pass
            st = store._mm_state
            if not st.get("super_seeded_notified"):
                st["super_seeded_notified"] = True
                try:
                    renpy.notify(u"MAICA: 采样已设为 温度 1.0 / 输出 4096")
                except Exception:
                    pass
            _mm_log("super tuned: %s" % ",".join(changed))
        except Exception:
            _mm_log("tune super FAILED: %r" % (sys.exc_info()[1],))

    def _mm_super_restore():
        """离开我们的节点时，把「我们改过的」采样参数还原成玩家原值。

        ★ 原则：我们的任何设置都不影响玩家用官方节点。
          只还原我们确实写过的键（_mm_super_owned）和总开关（_mm_super_owned_cfg）；
          玩家自己手动改过的值不在 owned 里，不动。
        """
        snap = getattr(persistent, "_mm_super_snapshot", None)
        owned = list(getattr(persistent, "_mm_super_owned", None) or [])
        owned_cfg = bool(getattr(persistent, "_mm_super_owned_cfg", False))
        if not snap and not owned and not owned_cfg:
            return
        try:
            adv = persistent.maica_advanced_setting
            sta = persistent.maica_advanced_setting_status
            d = persistent.maica_setting_dict
            changed = []

            if owned_cfg:
                d["use_custom_model_config"] = bool(snap.get("cfg", False)) if snap else False
                persistent._mm_super_owned_cfg = False
                changed.append("use_custom_model_config")

            if snap:
                for k in owned:
                    if snap.get("had", {}).get(k):
                        adv[k] = snap["adv"].get(k)
                    else:
                        adv.pop(k, None)
                    sta[k] = bool(snap.get("sta", {}).get(k, False))
                    if k not in changed:
                        changed.append(k + "(orig)")

            persistent._mm_super_owned = []
            persistent._mm_super_snapshot = None
            if not changed:
                return
            try:
                store.maica_apply_advanced_setting()
            except Exception:
                _mm_log("apply_advanced_setting FAILED: %r" % (sys.exc_info()[1],))
            # 总开关恢复成 False 时，前端应回到"不发高级设置"的官方行为
            try:
                ai = _mm_ai()
                if ai is not None:
                    ai.use_custom_model_config = bool(d.get("use_custom_model_config", False))
                    if not d.get("use_custom_model_config", False):
                        ai.modelconfig = {}
            except Exception:
                pass
            _mm_log("super restored: %s" % ",".join(changed))
        except Exception:
            _mm_log("super restore FAILED: %r" % (sys.exc_info()[1],))

    def _mm_tune_super_defaults():
        """每个 tick：我们的节点 → 接管；别的节点 → 还原。"""
        try:
            try:
                pid = int(persistent.maica_setting_dict.get("provider_id"))
            except Exception:
                pid = None
            if pid == store.maica_mood.NODE_ID:
                _mm_super_take()
            else:
                _mm_super_restore()
        except Exception:
            _mm_log("tune super FAILED: %r" % (sys.exc_info()[1],))

    # ---------------- 前端 API 设置 → 后端 .env（取代安装器） ----------------
    # ★ 2026-10-21：key / 地址 / 模型名玩家在前端「设置 → API Keys」里填（由官方
    #   MAICA_ServerSubmod 注册），存进 %APPDATA%\RenPy\<存档>\api_keys.json。
    #   这里每 tick 读一遍交给 lib，生成 .env 时叠在模板之上 —— 玩家填一次，
    #   官方节点和我们的节点共用同一份配置，也不需要安装器。
    _MM_API_MAP = (
        ("MAICA_MCORE_ADDR", "maica_core_url"),
        ("MAICA_MCORE_KEY", "maica_core_key"),
        ("MAICA_MCORE_CHOICE", "maica_core_model"),
        ("MAICA_MFOCUS_ADDR", "maica_mfocus_url"),
        ("MAICA_MFOCUS_KEY", "maica_mfocus_key"),
        ("MAICA_MFOCUS_CHOICE", "maica_mfocus_model"),
        ("MAICA_MVISTA_ADDR", "maica_mvista_addr"),
        ("MAICA_MVISTA_KEY", "maica_mvista_key"),
        ("MAICA_MVISTA_CHOICE", "maica_mvista_choice"),
        ("MAICA_MNERVE_ADDR", "maica_mnerve_addr"),
        ("MAICA_MNERVE_KEY", "maica_mnerve_key"),
        ("MAICA_MNERVE_CHOICE", "maica_mnerve_choice"),
        ("MAICA_PROXY_ADDR", "maica_proxy_addr"),
        ("MAICA_TP_APIS", "maica_tp_apis"),
    )

    # 兜底来源 → 界面上的人话名字（黄字提示用）
    _MM_API_SRC_LABEL = {
        "MAICA_TP_APIS": u"第三方API配置",
        "MAICA_MFOCUS_KEY": u"MFocus服务商api key",
        "MAICA_MVISTA_KEY": u"MVista服务商api key",
        "MAICA_MNERVE_KEY": u"MNerve服务商api key",
    }


    def _mm_api_sanity(applied):
        """看一眼前端填的 API 设置，返回一句人话（空串 = 没问题）。

        ★ 2026-10-22：实测玩家把「url / key / 模型名」填串行——
          api_keys.json 里 maica_core_url 放着 sk-…、maica_core_model 放着 https://…
          结果插件认为"没填 key"。这里做最基本的形状检查，直接说出来。
        """
        key = (applied.get("MAICA_MCORE_KEY") or "").strip()
        url = (applied.get("MAICA_MCORE_ADDR") or "").strip()
        model = (applied.get("MAICA_MCORE_CHOICE") or "").strip()
        if not key and not url:
            return u"还没填 API 设置（url / key / 模型名）"
        if not key:
            if url.lower().startswith("sk-"):
                return u"核心服务商url 里填的是 key —— url 和 key 可能填串行了"
            return u"还没填 API key（填到「第三方API配置」那一格；也可以填「核心服务商api key」）"
        # ★ 2026-10-23：模板不再给 url 默认值，所以这里要明确提示"url 没填"，
        #   否则后端启动会直接抛 "MAICA_MCORE_ADDR is required"，前端只看到"未登录"。
        if not url:
            return u"还没填核心服务商 url（形如 https://api.deepseek.com/v1）"
        if "://" in key or key.lower().startswith("http"):
            return u"「核心服务商api key」里填的是网址 —— url 和 key 可能填串行了"
        if url and url.lower().startswith("sk-"):
            return u"「核心服务商url」里填的是 key —— url 和 key 可能填串行了"
        if url and "://" not in url:
            return u"「核心服务商url」看起来不是网址"
        # ★ 2026-10-24：实测把 url 也粘进了「模型名称」，后端报
        #   400 "you passed https://api.deepseek.com/v1"，前端只看到"服务器有问题"。
        if "://" in model or model.lower().startswith("http"):
            return u"「模型名称」里填的是网址 —— 这里要填模型名，例如 deepseek-flash"
        if not model:
            return u"还没填模型名称（例如 deepseek-flash）"
        # ★ 2026-10-24：模型名区分大小写。实测填成 "deepseek-fLash"（大写 L）时
        #   接口报 400 "you passed deepseek-fLash"，前端只显示"服务器有问题"。
        if model != model.lower():
            return u"模型名称里有大写字母 —— 模型名区分大小写，请照服务商文档填（例如 deepseek-flash）"
        return ""

    def _mm_read_api_key(name):
        """读一条 API 设置：先走 MAS 的注册表，读不到就直接读 api_keys.json。"""
        try:
            v = store.mas_getAPIKey(name)
            if v:
                return str(v).strip()
        except Exception:
            pass
        try:
            import os as _os, json as _json
            p = _os.path.join(renpy.config.savedir, "api_keys.json")
            with open(p, "r") as f:
                d = _json.load(f)
            v = d.get(name)
            return str(v).strip() if v else ""
        except Exception:
            return ""

    def _mm_sync_api_settings():
        st = store._mm_state
        # ★ 2026-10-22：这两个标记是"这一轮读出来的结论"，必须每 tick 重算。
        #   原来只写不清：玩家把 key 填回正确位置后，黄字还挂在那里（实测 19:14 填好、
        #   19:38 还在提示"没找到核心 key"）。清掉再重新判定，界面才会跟着变。
        _prev_from = st.pop("api_key_from", None)
        st.pop("api_rotated", None)
        st.pop("api_misplaced", None)
        raw = {}
        for env_key, mas_key in _MM_API_MAP:
            v = _mm_read_api_key(mas_key)
            if v:
                raw[env_key] = v
        # 只填了核心？MFocus / MVista 跟着核心走 —— 否则"明明填了 key 却用不了视觉/记忆"
        for prefix in ("MAICA_MFOCUS", "MAICA_MVISTA"):
            for suffix, src in (("_ADDR", "MAICA_MCORE_ADDR"),
                                ("_KEY", "MAICA_MCORE_KEY"),
                                ("_CHOICE", "MAICA_MCORE_CHOICE")):
                if not raw.get(prefix + suffix) and raw.get(src):
                    raw[prefix + suffix] = raw[src]
        # ★ 2026-10-22：实测常见——玩家在 API Keys 界面里把三格填串了：
        #   url 那格放的是 key、model 那格放的是 url、第三方API 那格放的是模型名。
        #   这个形态是确定的，就地纠正（只影响生成的 .env，不改玩家的 api_keys.json）。
        _k = (raw.get("MAICA_MCORE_KEY") or "").strip()
        _u = (raw.get("MAICA_MCORE_ADDR") or "").strip()
        _m = (raw.get("MAICA_MCORE_CHOICE") or "").strip()
        _t = (raw.get("MAICA_TP_APIS") or "").strip()
        if (not _k) and _u.lower().startswith("sk-") and _m and "://" in _m:
            raw["MAICA_MCORE_KEY"] = _u
            raw["MAICA_MCORE_ADDR"] = _m
            if _t and "{" not in _t:
                raw["MAICA_MCORE_CHOICE"] = _t
                raw.pop("MAICA_TP_APIS", None)
            else:
                raw["MAICA_MCORE_CHOICE"] = "deepseek-flash"
            st["api_rotated"] = True
            if not st.get("api_rotated_notified"):
                st["api_rotated_notified"] = True
                _mm_log("api: detected rotated settings (url<->key<->model); auto-corrected")
                try:
                    renpy.notify(u"MAICA: 检测到 API 设置的 url / key / 模型名填串行了，已自动对位")
                except Exception:
                    pass

        # ★ 2026-10-22（用户定）：**主 key 的推荐位置是「第三方API配置」**（maica_tp_apis）。
        #   理由：官方节点（provider 1）也会吃到「核心服务商api key」那一格，玩家切回官方
        #   节点时自己的 key 会被一起带过去；而「第三方API配置」官方只当 SERP 配置读，
        #   不会拿去当主服务商的 key。所以这里优先用 TP_APIS 里的 sk-，核心那格只作兼容。
        _tp = (raw.get("MAICA_TP_APIS") or "").strip()
        if _tp.startswith("sk-") and len(_tp) >= 20:
            raw["MAICA_MCORE_KEY"] = _tp
            raw.pop("MAICA_TP_APIS", None)      # 那一格放的是 key，不是 JSON 配置
        # ★ 2026-10-24：原来这里会在「key 填在核心那格」时提示"建议挪到第三方API配置"，
        #   理由是"切回官方节点会把 key 带过去"。核对前端代码后确认这个理由不成立：
        #   ChatSubmod 和 maica.py 全文都没有读 maica_core_key / maica_tp_apis，
        #   连官方节点只用 Maica_Token；这两个字段只有本地后端（我们的 / ServerSubmod 的）会读。
        #   所以两格填哪格都一样，提示删掉，免得误导玩家。

        # 兜底：两格都没有钥匙，就从别的格子里找一把像 key 的。
        #   ★ 2026-10-22 实测：玩家把 core key 填进了「第三方API配置」那一行（maica_tp_apis），
        #     文件里明明有一把 sk-… 的钥匙，插件却认为"没填 key"。
        #     本地只有一个服务商时，随便哪一格里的 sk- 都是同一把 —— 拿来用，并说明来源。
        if not (raw.get("MAICA_MCORE_KEY") or "").strip():
            for _src in ("MAICA_MFOCUS_KEY", "MAICA_MVISTA_KEY", "MAICA_MNERVE_KEY", "MAICA_PROXY_ADDR"):
                _v = (raw.get(_src) or "").strip()
                if _v.startswith("sk-") and len(_v) >= 20:
                    raw["MAICA_MCORE_KEY"] = _v
                    st["api_key_from"] = _src
                    if _src != _prev_from:
                        _mm_log("api: no key in core/tp_apis -> took it from %s" % _src)
                    break

        # ★ 2026-10-23：MFocus/MVista 的兜底必须再跑一次。
        #   上面的兜底（844 行那段）在「key 填在第三方API配置」时拿不到钥匙 —— 那一刻
        #   MCORE_KEY 还是空的，等 879 行把 TP_APIS 提升成 MCORE_KEY 之后才有人可跟。
        #   不补这一遍，生成的 .env 里 MAICA_MFOCUS_KEY 是空串，后端启动会直接
        #   openai.OpenAIError: Missing credentials（实测 21:37，前端表现为"未登录"）。
        for prefix in ("MAICA_MFOCUS", "MAICA_MVISTA"):
            for suffix, src in (("_ADDR", "MAICA_MCORE_ADDR"),
                                ("_KEY", "MAICA_MCORE_KEY"),
                                ("_CHOICE", "MAICA_MCORE_CHOICE")):
                if not raw.get(prefix + suffix) and raw.get(src):
                    raw[prefix + suffix] = raw[src]

        # ★ 2026-10-24：第三方 API 配置是 JSON，而后端是 `json.loads` 直接解析、
        #   没有兜底（maica/mtools/api_keys.py:10）—— 写坏一个引号会让后端在 import
        #   阶段直接退出，前端只看到"连不上服务器"。这里先解析一次，不合法就不传下去，
        #   并在设置页提示；最坏情况只是搜索/天气不可用。
        st.pop("api_tp_bad", None)
        _tp_json = (raw.get("MAICA_TP_APIS") or "").strip()
        if _tp_json:
            _tp_ok = False
            if _tp_json.startswith("{"):
                try:
                    import json as _json
                    _tp_ok = isinstance(_json.loads(_tp_json), dict)
                except Exception:
                    _tp_ok = False
            if not _tp_ok:
                raw.pop("MAICA_TP_APIS", None)
                st["api_tp_bad"] = True
                if not st.get("api_tp_bad_notified"):
                    st["api_tp_bad_notified"] = True
                    _mm_log("api: maica_tp_apis is not valid JSON -> ignored")

        # ★ 2026-10-24：lite 包（不含 extractor\）→ 事实抽取改走核心服务商。
        #   模板里默认是 http://127.0.0.1:{EXTRACT}/v1 + r8；这里覆盖成玩家填的服务商。
        st.pop("extract_mode", None)
        _mm_was_extract_remote = st.get("extract_mode_prev") == "remote"
        try:
            _mm_has_local_extract = store.maica_mood.extractor_bundle_present()
        except Exception:
            _mm_has_local_extract = True
        if not _mm_has_local_extract:
            st["extract_mode"] = "remote"
            for _dst, _src in (("MAICA_EXTRACT_ADDR", "MAICA_MCORE_ADDR"),
                               ("MAICA_EXTRACT_KEY", "MAICA_MCORE_KEY"),
                               ("MAICA_EXTRACT_CHOICE", "MAICA_MCORE_CHOICE")):
                if raw.get(_src):
                    raw[_dst] = raw[_src]
            # 这个函数每个 tick 都跑 —— 只在"刚切成远端"时记一行，否则日志会被刷爆
            if not _mm_was_extract_remote:
                _mm_log("extract: no local bundle -> using provider (%s)" %
                        (raw.get("MAICA_EXTRACT_CHOICE") or "?"))
        st["extract_mode_prev"] = st.get("extract_mode")

        try:
            applied = store.maica_mood.set_env_overrides(raw)
        except Exception:
            applied = {}

        # API 设置体检：缺 key / 填错格子（很常见：url 和 key 串行、模型名填进第三方API）
        problem = _mm_api_sanity(applied)
        st["api_sanity"] = problem
        if problem and not st.get("api_warned") and _mm_our_node():
            st["api_warned"] = True
            _mm_log("api: " + problem)
            try:
                renpy.notify(u"MAICA: " + problem + u"（设置 → API Keys）")
            except Exception:
                pass

        # 值变了：.env 已经生成过，要重启后端才吃到（不自动重启：会打断聊天）
        sig = (applied.get("MAICA_MCORE_ADDR"), applied.get("MAICA_MCORE_KEY"),
               applied.get("MAICA_MCORE_CHOICE"))
        old = st.get("api_sig")
        if old is not None and sig != old and st.get("svc") is not None:
            _mm_log("api: settings changed -> takes effect after backend restart")
            try:
                renpy.notify(u"MAICA: API 设置已更改，切回官方节点再切回来（或重启游戏）后生效")
            except Exception:
                pass
        st["api_sig"] = sig
        st["api_applied"] = applied

    # ---------------- 问卷网页（快速填写 / 查看已记录） ----------------
    def _mm_webui_open(which="fill"):
        """起本地网页服务并用系统浏览器打开。

        which = "fill"（快速填写信息）/ "facts"（查看已记录的信息）
        Ren'Py 6.99 的 py2 没有 sqlite3、也没有 HTTP 服务，游戏内输入框还不能输入
        中文 —— 所以真正的界面在浏览器里，游戏里只负责起服务 + 打开。
        """
        st = store._mm_state
        w = st.get("webui")
        if w is None:
            w = {}
            st["webui"] = w
        try:
            ok, msg, info = store.maica_mood.start_webui(
                ports=store.maica_mood.load_ports(),
                log_path=store.maica_mood.log_path(u"webui.log"),
                last_json=store.maica_mood.log_path(u"webui_last.json"),
                state=w)
        except Exception:
            ok, msg, info = False, "except:%r" % (sys.exc_info()[1],), {}
        if not ok or not info.get("port"):
            _mm_log("webui start FAILED: %s" % msg)
            try:
                renpy.notify(u"MAICA: 问卷网页没起来（%s）" % msg)
            except Exception:
                pass
            return
        # 记下这次启动时间：早于它的 webui_last.json 是上一次会话留下的，别误报"已保存"
        w["started_at"] = time.time()
        path = "" if which == "fill" else "facts"
        url = "http://127.0.0.1:%d/%s?t=%s" % (int(info["port"]), path, info["token"])
        st["webui_url"] = url
        _mm_log("webui: %s -> %s" % (which, url))
        try:
            import webbrowser
            webbrowser.open_new(url)
        except Exception:
            _mm_log("webui: webbrowser FAILED: %r" % (sys.exc_info()[1],))
            try:
                renpy.notify(u"MAICA: 打不开浏览器，请手动打开：%s" % url)
            except Exception:
                pass

    def _mm_webui_tick():
        """网页保存后，把结果提示到游戏里（读网页写下的 webui_last.json）。"""
        st = store._mm_state
        w = st.get("webui")
        if not w or not store.maica_mood.webui_alive(w):
            return
        try:
            import os as _os, json as _json
            p = store.maica_mood.log_path(u"webui_last.json")
            if not _os.path.isfile(p):
                return
            mtime = _os.path.getmtime(p)
            # 早于本次启动时间的文件是上一次会话留下的，别误报"已保存"
            if mtime <= float(w.get("started_at") or 0):
                return
            if w.get("last_mtime") == mtime:
                return
            w["last_mtime"] = mtime
            with open(p, "r") as f:
                d = _json.load(f)
            saved = int(d.get("saved") or 0)
            skipped = int(d.get("skipped") or 0)
            errors = d.get("errors") or []
            _mm_log("webui saved: %d, skipped: %d, errors: %d" % (saved, skipped, len(errors)))
            if errors:
                try:
                    renpy.notify(u"MAICA: 已保存 %d 条，%d 条没通过校验" % (saved, len(errors)))
                except Exception:
                    pass
            else:
                try:
                    renpy.notify(u"MAICA: 问卷已保存 %d 条，进聊天后会自动记进她的记忆" % saved)
                except Exception:
                    pass
        except Exception:
            pass

    def _mm_webui_stop():
        w = store._mm_state.get("webui")
        if not w:
            return
        try:
            store.maica_mood.webui_stop(w)
            _mm_log("quit: webui stopped")
        except Exception:
            _mm_log("quit: webui stop FAILED: %r" % (sys.exc_info()[1],))

    # ---------------- 本地三件套（嵌入服务 / 后端 / 提取器）----------------
    def _mm_extractor():
        """我们的节点被选中 → 后台自动拉起三件服务；切走 → 自动全关。

        ★ 2026-10-17 改：真正的探活/拉起/重试跑在【后台线程】里
          （store.maica_mood.start_service_thread）。原先直接在 periodic_callbacks
          里等启动，后端首次要 20~60 秒，等于把游戏主线程卡死。
          这里只做两件极轻的事：设置目标状态、读状态写日志。

        顺序：嵌入服务 → 后端 → 提取器（后端启动时要连 :embed 与 :extract）。
        端口全部来自 插件/ports.json，后端 .env 由插件启动前现生成。
        """
        st = store._mm_state
        svc = st.get("svc")
        if svc is None:
            # ★ 2026-10-22：端口探测 / 残留清理都挪进后台线程了（见 start_service_thread）。
            #   主线程这里只做一次"读 ports.json"（纯文件读），免得开局性能自检被
            #   netstat/wmic 子进程卡掉 —— 那正是"性能影响警告"每次弹出来的原因。
            ports = store.maica_mood.load_ports()
            lp = store.maica_mood.log_path
            svc = store.maica_mood.start_service_thread(ports=ports, log_paths={
                "embed": lp(u"mood-embed.log"),
                "backend": lp(u"mood-backend.log"),
                "extract": lp(u"mood-extractor.log"),
                "watchdog": lp(u"mood-watchdog.log"),
            })
            st["svc"] = svc
            st["svc_ports"] = ports
            _mm_log("service thread started ｜ ports ws=%s http=%s embed=%s extract=%s" % (
                ports["ws"], ports["http"], ports["embed"], ports["extract"]))

        want = _mm_our_node()
        # ★ 2026-10-22：没填 API Key 时后端会在 import 阶段直接崩（OpenAIError），
        #   前端只会显示"连不上服务器"。这里干脆不启动服务，并给一句能看懂的提示。
        if want and st.get("api_sanity"):
            want = False
            if not st.get("api_key_warned"):
                st["api_key_warned"] = True
                _mm_log("api key missing -> local backend NOT started")
                try:
                    renpy.notify(u"MAICA: 还没填 API Key，本地后端没启动 —— 设置 → API Keys → "
                                 u"「第三方API配置」")
                except Exception:
                    pass
        svc["want"] = bool(want)

        # 后台线程顺延端口 / 清残留后回报结果，提示必须在主线程发
        moved = svc.get("ports_moved")
        if moved and not svc.get("ports_moved_notified"):
            svc["ports_moved_notified"] = True
            _mm_log("ports bumped (occupied by other program): %s" % (
                ", ".join("%s %s->%s" % (k, a, b) for k, a, b in moved)))
            try:
                renpy.notify(u"MAICA: 端口被占用，已顺延到 %s" % ", ".join(
                    "%s=%s" % (k, b) for k, a, b in moved))
            except Exception:
                pass

        # ★ 2026-10-18：提取器按聊天生命周期启停 —— 这里只上报两个状态，
        #   真正的启停判断在后台线程里（避免主线程做 IO）：
        #     chat_active  = 聊天界面是否开着
        #     last_chat_at = 最近一次对话的时间（进入聊天/发消息/收到回复都会刷新）
        try:
            _vis_now = bool(store._mm_panel.get("have")) and \
                (renpy.get_screen("mas_py_console_teaching") is not None)
            if _vis_now != svc.get("chat_active"):
                svc["chat_active"] = bool(_vis_now)
                svc["last_chat_at"] = time.time()
                _mm_log("chat_active -> %s (extractor lifecycle)" % bool(_vis_now))
            elif _vis_now and not svc.get("last_chat_at"):
                svc["last_chat_at"] = time.time()
        except Exception:
            pass

        msg = svc.get("text") or ""
        if msg != st.get("extractor_logged"):
            st["extractor_logged"] = msg
            _mm_log("services: %s" % msg)

        # 后端刚就绪 → 让前端重试一次连接（玩家切节点后不必手动点"重新检测"）
        if want and svc.get("backend_ready") and not st.get("reconnected_for_switch"):
            st["reconnected_for_switch"] = True
            try:
                ai = _mm_ai()
                if ai is not None and not ai.is_connected():
                    ai.accessable()
                    _mm_log("backend ready -> asked frontend to re-check")
            except Exception:
                _mm_log("re-check FAILED: %r" % (sys.exc_info()[1],))
        if not want:
            st["reconnected_for_switch"] = False

    def _mm_extractor_stop(wait=False):
        """退出游戏时把三件服务停掉。

        ★ 2026-10-18 修两个 bug（实测：正常退出后服务仍在跑）：
          ① 原来把 `store._mm_state` 传给 stop_all —— 里面根本没有
             backend_proc/embedding_proc/extractor_proc 这三个键，
             真正的进程表在 svc["procs"] 里。所以那次调用什么都没停。
          ② 原来先设 svc["stop"]=True，守护线程看到就直接退出了，
             根本没机会执行 stop_all —— 顺序也反了。
          现在：先按 svc["procs"] 停，再按端口兜底扫一遍，最后才让线程退出。
        """
        svc = store._mm_state.get("svc")
        if svc is not None:
            svc["want"] = False
            try:
                store.maica_mood.stop_all(svc.get("procs") or {}, wait=wait)
                _mm_log("quit: stop_all(procs) done (wait=%s)" % (wait,))
            except Exception:
                _mm_log("quit: stop_all FAILED: %r" % (sys.exc_info()[1],))
            svc["stop"] = True
        # 兜底：不管 procs 记录有没有漏，按端口扫一遍只杀我们目录下的 exe
        try:
            killed = store.maica_mood.kill_own_on_ports()
            _mm_log("quit: killed by port = %r" % (killed,))
        except Exception:
            _mm_log("quit: kill_own_on_ports FAILED: %r" % (sys.exc_info()[1],))

    # ★ 2026-10-17 改：Ren'Py 7 里【没有】config.quit_callbacks（只有 config.quit_action），
    #   写 config.quit_callbacks.append(...) 会在 init 阶段直接抛 AttributeError 把插件搞挂。
    #   官方两个 submod（MAICA_ServerSubmod / MAICA_ChatSubmod）都用 MAS 的 _quit 插件点，
    #   这是这套 build 里被验证过的唯一退出钩子。
    @store.mas_submod_utils.functionplugin("_quit")
    def _mm_on_quit():
        # 正常退出只关服务；节点 / 会话长度 / 采样参数保持原样（玩家下次还要用）
        _mm_webui_stop()
        _mm_extractor_stop()

    # ---------------- 周期回调（主线程） ----------------
    def _mm_tick():
        st = store._mm_state
        now = time.time()
        # ★ 2026-10-24：开局先空转 2 秒。Ren'Py 启动后会测前 5 帧（0.333 秒），
        #   而这里第一个 tick 要在主线程 import MAS 模块、安装节点、拉服务线程，
        #   核显机器上很容易把这 5 帧拖慢 → 弹「性能影响警告」。
        #   让自检过去之后再干活，玩家只是晚一两秒看到服务就绪。
        if not st.get("boot_at"):
            st["boot_at"] = now
            return
        if now - st["boot_at"] < 2.0:
            return
        if now - st["last_tick"] < 0.5:
            return
        st["last_tick"] = now
        if not st.get("first_tick_logged"):
            st["first_tick_logged"] = True
            _mm_log("tick alive (first run)")
        if (now - st.get("state_at", 0.0)) > 10.0:
            st["state_at"] = now
            try:
                _ai = _mm_ai()
                _eng = st.get("engine")
                _adv = persistent.maica_advanced_setting
                _sta = persistent.maica_advanced_setting_status
                _mc = getattr(_ai, "modelconfig", None) or {}
                _mm_log("state: provider=%s limit=%s connected=%s ready=%s mood=%s/'%s' turns=%s panel=%s temp=%s/%s cfg=%s mc_temp=%s mc_max=%s" % (
                    persistent.maica_setting_dict.get("provider_id"),
                    persistent.maica_setting_dict.get("session_len_limit"),
                    (bool(_ai.is_connected()) if _ai is not None else None),
                    (bool(_ai.is_ready_to_input()) if _ai is not None else None),
                    (int(_eng.value()) if _eng is not None else None),
                    (_eng.label() if _eng is not None else u""),
                    (getattr(_eng, "turns", None) if _eng is not None else None),
                    store._mm_panel.get("visible"),
                    _adv.get("temperature"), _sta.get("temperature"),
                    persistent.maica_setting_dict.get("use_custom_model_config"),
                    _mc.get("temperature"), _mc.get("max_tokens")))
            except Exception:
                _mm_log("state log FAILED: %r" % (sys.exc_info()[1],))
            # ★ 卸载握手标记：让 installer\卸载.bat 知道"现在能不能直接删目录"。
            #   只在状态变化时写日志，避免刷屏。
            try:
                _pid = persistent.maica_setting_dict.get("provider_id")
                _own = (int(_pid) == store.maica_mood.NODE_ID)
                if store.maica_mood.set_node_marker(
                        _own, _pid,
                        persistent.maica_setting_dict.get("session_len_limit")):
                    _mm_log("node marker -> ours=%s (uninstall guard)" % _own)
            except Exception:
                pass
        # 面板可见性：只在天堂森林聊天时显示（该控制台屏由前端自己显隐）
        # ★ 2026-10-22：加"必须在我们自己的节点上" —— 心情是本地后端的人设参数，
        #   切回官方节点后引擎照样能从聊天里算出数值，图标就跟着挂在左上角，
        #   玩家会以为官方节点也在用我们的心情系统（实测 19:58 官方节点上还在显示）。
        try:
            _vis = bool(store._mm_panel.get("have")) and _mm_our_node() and \
                   (renpy.get_screen("mas_py_console_teaching") is not None)
            if _vis != store._mm_panel.get("visible"):
                store._mm_panel["visible"] = _vis
                _mm_log("panel visible -> %s" % _vis)
                renpy.restart_interaction()
        except Exception:
            pass
        for _name, _fn in (("uninstall", _mm_prepare_uninstall),
                           ("ensure", _mm_ensure), ("sender", _mm_hook_sender),
                           ("api", _mm_sync_api_settings),
                           ("webui", _mm_webui_tick),
                           ("affection", _mm_affection_guard),
                           ("session", _mm_session_limit),
                           ("super", _mm_tune_super_defaults),
                           ("extractor", _mm_extractor),
                           ("turn", _mm_turn)):
            try:
                _fn()
            except Exception:
                _mm_log("tick step %s FAILED: %r" % (_name, sys.exc_info()[1]))

    config.periodic_callbacks.append(_mm_tick)


# ============================================================================
#  心情独立面板 v1（占位美术）
#  以后换素材只需改这三处：
#     background "#00000099"        -> 你的面板底图
#     right_bar  Solid("#ffffff30") -> 你的进度条底槽
#     left_bar   Solid("#ff9ecb")   -> 你的进度条填充
#  位置/尺寸改下面的 xpos/ypos/xysize 即可。
#
#  ★ 2026-10-17 改：原先用 `add Solid("#ff9ecb") xysize (...)` 手算宽度，
#    Ren'Py 7 直接报 "u'xysize' is not a keyword argument or valid child for the
#    add statement"（MAS-clean 的 errors.txt 实机证据），面板一有值就崩、面板永远不显示。
#    改用 Ren'Py 原生 bar + left_bar/right_bar：宽度交给引擎，不再手算。
# ============================================================================
init -982 python:
    store._mm_panel = {"visible": False, "have": False, "label": u"", "value": 0, "ratio": 0.0,
                       "detail": u"", "floor": -100.0, "limit": 100.0}

init python:
    if "maica_mood_panel" not in config.overlay_screens:
        config.overlay_screens.append("maica_mood_panel")

screen maica_mood_panel():
    zorder 90
    if store._mm_panel.get("visible"):
        frame:
            xpos 24
            ypos 24
            xpadding 20
            ypadding 14
            background "#00000099"
            vbox:
                spacing 6
                text u"\u83ab\u59ae\u5361\u7684\u5fc3\u60c5" size 18 color "#ffffffb0"
                text store._mm_panel.get("label", u"") size 30 color "#ffffff"
                bar:
                    value store._mm_panel.get("ratio", 0.0)
                    xysize (260, 14)
                    left_bar Solid("#ff9ecb")
                    right_bar Solid("#ffffff30")
                text store._mm_panel.get("detail", u"") size 15 color "#ffffff90"
                if store._mm_panel.get("reason"):
                    text (u"\u56e0\u4e3a" + store._mm_panel.get("reason")) size 15 color "#ffc46b"

# ============================================================================
#  子模组条目里的入口（settings_pane）
#
#  MAS 的子模组框架会把这里的内容渲染在我们条目的描述下面。
#  两个入口都只是"起本地网页服务 + 打开浏览器" —— 真正的界面在浏览器里，
#  因为游戏内既没有 sqlite3、也不能输入中文（详见 webui_server.py 头部注释）。
# ============================================================================
screen maica_mood_settings():
    vbox:
        xfill True
        xmaximum 1000
        spacing 6

        if store._mm_state.get("api_sanity"):
            text (u"⚠ " + store._mm_state.get("api_sanity") + u"（设置 → API Keys）") size 15 color "#ff6b6b"
        elif store._mm_state.get("api_key_from"):
            text (u"「第三方API配置」和「核心服务商api key」都是空的，暂时借用了「%s」里的那把钥匙 —— "
                  u"能用，但建议把 key 填到「第三方API配置」"
                  % store._MM_API_SRC_LABEL.get(store._mm_state.get("api_key_from"),
                                                store._mm_state.get("api_key_from"))) size 14 color "#ffc46b"
        elif store._mm_state.get("api_rotated"):
            text u"（检测到 API 设置填串行，已按 url / key / 模型名 自动对位；建议去设置里核对一遍）" size 14 color "#ffc46b"

        if store._mm_state.get("api_tp_bad"):
            text u"「第三方 API 配置」不是合法 JSON，已忽略（联网搜索/天气会不可用）；留空不影响聊天" size 14 color "#ffc46b"

        null height 2
        for _mm_api_help_line in store._MM_API_HELP:
            text _mm_api_help_line size 13 color "#ffffff80"
        if store._mm_state.get("extract_mode") == "remote":
            text u"事实抽取：使用你填的服务商（本包未附带本地模型，每次对话会多一次很小的 API 请求）" size 13 color "#ffc46b"
        null height 4

        text u"问卷与记忆（在浏览器里操作，保存后进聊天会自动记进她的记忆）" size 15 color "#ffffff90"

        hbox:
            spacing 8
            textbutton u"快速填写信息":
                action Function(_mm_webui_open, "fill")
            textbutton u"查看已记录的信息":
                action Function(_mm_webui_open, "facts")

        if store._mm_state.get("webui_url"):
            text (u"如果浏览器没自动打开，手动访问：" + str(store._mm_state.get("webui_url"))) size 13 color "#ffffff60"
