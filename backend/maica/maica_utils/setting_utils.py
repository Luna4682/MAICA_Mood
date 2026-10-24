"""Import layer 2"""
import asyncio
from typing import *
from pydantic import (
    BaseModel,
    PrivateAttr,
    Field,
    ConfigDict,
    model_validator,
)
from .gvars import *
from .maica_utils import *

_Bt = BilingualText

class SettingsModel(BaseModel):
    """Just a template."""
    model_config = ConfigDict(
        validate_assignment=True
    )

class IdentitySettingsModel(SettingsModel, PydHardResetMixin): ...

def create_prop(
        name: str,
        getter_ext: list[Callable]=None,
        getter_kwargs: dict=None,
        setter_ext: list[Callable]=None,
        setter_kwargs: dict=None,
    ):
    """We still have to use the old way somewhere, because pydantic does not offer a better solution."""
    private_name = f"_{name}"
    if not isinstance(getter_kwargs, dict):
        getter_kwargs = {}
    if not isinstance(setter_kwargs, dict):
        setter_kwargs = {}
    
    def getter(self: SettingsModel):
        value = getattr(self, private_name, None)
        if getter_ext:
            for func in getter_ext:
                value = func(self, n=name, v=value, **getter_kwargs)
        return value
    
    def setter(self: SettingsModel, value):
        if setter_ext:
            for func in setter_ext:
                value = func(self, n=name, v=value, **setter_kwargs)
        setattr(self, private_name, value)
    
    return property(getter, setter)

def read_exist(self, n, v, **kwargs):
    """Value must exist on get."""
    if not v:
        raise MaicaPermissionError(f"{n} must be assigned before access")
    return v

def set_locked(self, n, v, **kwargs):
    """Value can only be rewritten from None."""
    prv_n = f"_{n}"
    if getattr(self, prv_n) is not None:
        raise MaicaPermissionError(f"{n} is locked")
    return v

def set_literal(self, n, v, valid: list[any], **kwargs):
    """Value must in valid list on set."""
    if v not in valid:
        raise MaicaPermissionError(f"{n} must be one of {valid}")
    return v

def set_range(self, n, v, lower: Union[int, float], upper: Union[int, float], soft_limit: bool=False, **kwargs):
    """Value must in range on set."""
    if isinstance(lower, str):
        lower = numeric(getattr(G.A, lower))
    if isinstance(upper, str):
        upper = numeric(getattr(G.A, upper))
    if isinstance(lower, float) or isinstance(upper, float):
        v = float(v)
    else:
        v = int(v)
    if soft_limit:
        new_v = max(lower, min(upper, v))
        if v != new_v:
            sync_messenger(info=f"{n}={v} out of range [{lower}, {upper}], limiting to {new_v}")
            v = new_v
    if not lower <= v <= upper:
        raise MaicaPermissionError(f"{n} must be between {lower} and {upper}")
    return v

def set_instance(self, n, v, types: list[type], **kwargs):
    """Value must be of desired type."""
    for t in types:
        if t:
            if isinstance(v, t):
                break
        elif t is None:
            if v is None:
                break
    else:
        raise MaicaPermissionError(f"{v} must be in types {types}")
    return v

class ConfigurableSettingsModel(SettingsModel, PydUpdateMixin, PydSoftResetMixin):
    """Do not use on cridentials."""
    @model_validator(mode="before")
    @classmethod
    def none_is_default(cls, data: Any):
        if isinstance(data, dict):
            for field_name, field_info in cls.model_fields.items():
                value = data.get(field_name)
                if field_name in data and value is None:
                    default_value = field_info.get_default(call_default_factory=True)
                    if default_value is not None:
                        data[field_name] = default_value
        return data

class MaicaSettings(BaseModel):
    """All the per-client settings for MAICA."""

    # class Identity(IdentitySettingsModel):
    #     """Note that this identity is not verified and not safe to use in most cases. Use verification for those."""

    #     _user_id: int = None
    #     _username: str = None
    #     _nickname: Optional[str] = None
    #     _email: str = None

    #     user_id = create_prop('user_id', getter_ext=[read_exist], setter_ext=[set_instance], setter_kwargs={"types": [int]})
    #     username = create_prop('username', getter_ext=[read_exist], setter_ext=[set_instance], setter_kwargs={"types": [str]})
    #     nickname = create_prop('nickname', setter_ext=[set_instance], setter_kwargs={"types": [str, None]})
    #     email = create_prop('email', getter_ext=[read_exist], setter_ext=[set_instance], setter_kwargs={"types": [str]})

    class Verification(IdentitySettingsModel):
        """Verified identity, safe to use."""

        _user_id: int = None
        _username: str = None
        _nickname: Optional[str] = None
        _email: str = None

        # Keep type declarations visible to static analyzers without exposing them
        # to Pydantic, which would treat each property object as a field default.
        if TYPE_CHECKING:
            user_id: int
            username: str
            nickname: Optional[str]
            email: str
        else:
            user_id = create_prop('user_id', getter_ext=[read_exist], setter_ext=[set_locked, set_instance], setter_kwargs={"types": [int]})
            username = create_prop('username', getter_ext=[read_exist], setter_ext=[set_locked, set_instance], setter_kwargs={"types": [str]})
            nickname = create_prop('nickname', setter_ext=[set_instance], setter_kwargs={"types": [str, None]})
            email = create_prop('email', getter_ext=[read_exist], setter_ext=[set_locked, set_instance], setter_kwargs={"types": [str]})

    class Basic(ConfigurableSettingsModel):
        """Major params that decide MAICA's behavior."""

        stream_output: bool = True
        """Use stream output."""
        enable_mf: bool = True
        """Enable MFocus."""
        enable_mt: bool = True
        """Enable MTrigger."""
        savefile_access: bool = True
        """Enable savefile extraction."""
        target_lang: TargetLangType = 'zh'
        """Target language."""
        tz: Optional[str] = None
        """Timezone."""
        session_len_limit: int = Field(
            default=8192,
            ge=512,
            le=28672,
        )
        """Max session length."""

    class Extra(ConfigurableSettingsModel):
        """Params that aren't that important, but affect MAICA's behavior."""

        prompt_pname_repl: bool = False
        """Use name from savefile instead of [player] in prompts."""
        prompt_monika_nickname: bool = False
        """Add Monika's given nickname to prompt."""
        prompt_allow_nickname: bool = True
        """Allow model to generate [player_nickname]."""
        mf_llm_concl: bool = False
        """Use agent model's final output instead of instructed guidance."""
        mf_sf_access_impl: Literal[0, 1, 2] = 2
        """Use RAG/Reranker to acquire info from persistent instead of traditional MFocus impl.

        This is a client-side setting the game normally sends over the params
        message; the backend cannot override it through MAICA_MFOCUS_EXTRA.
        The upstream default is 1 (reranker), but fsc_late.real_sf_access_impl
        only resolves to 1 when a reranker is configured -- otherwise it falls
        through to 0 (filter_llm, which dumps the whole savefile into an LLM).
        Defaulting to 2 uses the vector store when available and still degrades
        to 0 automatically when it is not, which is what a RAG-enabled
        self-hosted backend wants.
        """
        mf_const_sf_access: Literal[0, 1, 2] = 0
        """Add persistent extraction to MFocus instructed guidance even if not called."""
        mf_const_tools: Literal[0, 1, 2] = 1
        """Add information to MFocus instructed guidance even if no tool used."""
        esearch_llm_concl: bool = True
        """Force agent to resort information acquired from Internet."""
        mf_precheck_mt: bool = True
        """Add MTrigger toollist to MFocus tools for a precheck."""
        memory_concl_arc: Literal[0, 1, 2] = 1
        """Conclude archived / purged sessions into summarizations."""
        nsfw_acceptive: bool = True
        """Alter prompt to ask model to handle toxic topics positively."""
        mf_context_rnds: Literal[0, 1, 2, 3, 4, 5] = 3
        """Add history rounds for MFocus to understand the conversation.

        ★ 2026-10-12 由 0 改为 3。理由：MFocus 在 0 轮时**完全看不到对话历史**，
          只看得到当前这一句 —— 玩家说"我还是喜欢之前那个"时它不知道"那个"是什么，
          于是判断发飘、工具该调不调。给它 3 轮上下文是为了让它判断得准。

        代价（已实测，见 _tools/架构与打包约束.md §5.6）：
          · 增量 = 最后 3 轮（6 条消息）→ 短句 ≈ +109 / 中句 ≈ +247 / 长句 ≈ +475 tokens/请求
          · 固定部分（system + 工具 schema）约 1200-4000 tokens，所以相对增幅约 **3%~15%**
          · 这些消息在工具循环的每一轮都会被重发（mf_session 只增不减），
            但 mf_disable_loop 默认 True → 实际只发 1 次，不会被放大

        ★ 这是【每客户端可配置项】（update_settings 会按客户端传值覆盖）：
          改这里只影响"客户端没显式传值"的情况；客户端存过值的话仍以客户端为准。
        """
        mt_context_rnds: Literal[0, 1, 2, 3, 4, 5] = 0
        """Add history rounds for MFocus to understand the conversation."""
        gen_quality_chk: bool = False
        """Check and warn about context quality descalation using MNerve."""
        mf_disable_loop: bool = True
        """Disable MFocus sequential toolcall to save time."""
        mt_disable_loop: bool = True
        """Disable MTrigger sequential toolcall to save time."""
        gen_enforce_lang: bool = True
        """Enforce target language (only applies to English currently)."""

    class Super(ConfigurableSettingsModel):
        """Passthrough params to core LLM."""

        max_tokens: int = Field(
            default=4096,
            ge=1,
            le=8192,
        )
        """Output token budget, silently forwarded as max_output_tokens.

        Raised from 1600 because deepseek-flash is a thinking-first model:
        its hidden reasoning already consumes roughly 1100-1600 tokens per
        request and those tokens count against this budget. With the old
        default the whole budget could be spent on reasoning, which truncated
        the visible reply to a single sentence (or to nothing at all) with
        finish_reason="length". Thinking cannot actually be disabled -- the
        model advertises effort levels low/high/max only, and every attempt to
        switch it off is either ignored or rejected; see the notes in
        MAICA_MCORE_EXTRA. A larger ceiling merely lets the model finish its
        answer, it does not make replies longer: short questions still get
        short answers.
        """
        seed: Optional[int] = None
        top_p: float = Field(
            default=0.7,
            ge=0.1,
            le=1.0,
        )
        temperature: float = Field(
            default=0.75,
            ge=0.0,
            le=1.0,
        )
        """Sampling temperature.

        Raised from 0.22. At 0.22 the token distribution was sharp enough that
        asking the same thing twice produced almost the same reply, because the
        previous identical exchange is also still in the session history. 0.75
        keeps Monika's tone stable while making repeated questions vary. top_p
        stays at 0.7 to bound the tail.
        """
        frequency_penalty: float = Field(
            default=0.44,
            ge=0.0,
            le=1.0,
        )
        presence_penalty: float = Field(
            default=0.34,
            ge=0.0,
            le=1.0,
        )

    class Temp(ConfigurableSettingsModel):
        """
        Should be reset after each round of completion.
        Caution: This does not verify many things itself, especially when it comes to session -1 or things.
        Related verifications should be in ws_config.
        """

        class MSpire(ConfigurableSettingsModel):

            class MsFromCacheResult(BaseModel):
                hash: Optional[str] = None
                prompt: Optional[str] = None
                result: Optional[str] = None

            type: Literal[
                "precise_page",
                "fuzzy_page",
                "in_precise_category",
                "in_fuzzy_category",
                "in_fuzzy_all",
            ] = "in_precise_category"
            sample: int = Field(
                default=250,
                ge=2,
                le=250,
            )
            ctg_weight: int = Field(
                default=10,
                ge=1,
                le=100,
            )
            title: Union[str, list[str | BilingualText]] = Field(
                default_factory=lambda: [
                    _Bt('自然', 'Natural_environment'),
                    _Bt('自然科学', 'Natural_sciences'),
                    _Bt('社会', 'Society'),
                    _Bt('人文學科', 'Humanities'),
                    _Bt('世界', 'World'),
                    _Bt('生活', 'Health'),
                    _Bt('艺术', 'The_arts'),
                    _Bt('文化', 'Culture'),
                ]
            )
            use_cache: bool = False
            _mfc_m: Optional[MsFromCacheResult] = None

            @model_validator(mode="after")
            def enhanced_defaults(self):
                if isinstance(self.title, str):
                    self.title = [self.title]

                return self

        class MPostal(ConfigurableSettingsModel):
            header: Optional[str] = None
            content: Optional[str] = None

        class MVista(ConfigurableSettingsModel):
            mv_imgs: Optional[list] = Field(
                default=None,
                max_length=3,
            )
            """List of MVista images urls."""

        class Common(ConfigurableSettingsModel):
            """These are used by several m-things."""
            bypass_mf: bool = False
            """Bypass MFocus once."""
            bypass_mt: bool = False
            """Bypass MTrigger once."""
            bypass_stream: bool = False
            """Bypass stream output once."""
            twk_super: bool = False
            """Tweak super params (for written language)."""
            twk_info: bool = False
            """Tweak mf_const_tools info (for ms and mp)."""
            strict_conv: bool = True
            """Restrict conversation schema."""

        chat_session: int = Field(
            default=0,
            ge=-1,
            le=9,
        )
        """This is standalone because it's important."""
        activated: Literal["query", "mspire", "mpostal"] = "query"

        mspire: MSpire = Field(default_factory=MSpire)
        mpostal: MPostal = Field(default_factory=MPostal)
        mvista: MVista = Field(default_factory=MVista)
        common: Common = Field(default_factory=Common)

    # identity: Identity = Field(
    #     default_factory=Identity,
    #     exclude=True,
    # )
    verification: Verification = Field(
        default_factory=Verification,
        exclude=True,
    )
    basic: Basic = Field(default_factory=Basic)
    extra: Extra = Field(default_factory=Extra)
    super: Super = Field(default_factory=Super)
    temp: Temp = Field(
        default_factory=Temp,
        exclude=True,
    )

    @property
    def use_mf_now(self):
        return (
            self.basic.enable_mf
            and self.prompt_writable
            and not self.temp.common.bypass_mf
        )

    @property
    def use_mt_now(self):
        return (
            self.basic.enable_mt
            and not self.temp.common.bypass_mt
        )

    @property
    def mf_concl_now(self):
        return (
            self.extra.mf_llm_concl
            and not self.extra.mf_disable_loop
        )
    
    @property
    def use_stream_now(self):
        return (
            self.basic.stream_output
            and not self.temp.common.bypass_stream
        )

    @property
    def pname_repl_now(self):
        return (
            self.prompt_writable
            and self.extra.prompt_pname_repl
        )

    @property
    def monika_nickname_now(self):
        return (
            self.prompt_writable
            and self.extra.prompt_monika_nickname
        )

    @property
    def prompt_system_writable(self):
        """Allow writing from system level, like zsco."""
        return (
            self.temp.chat_session >= 0
        )

    @property
    def prompt_writable(self):
        """Allow writing from user level and above (system), like mf."""
        return (
            self.prompt_system_writable
            and not (
                self.temp.activated == "mspire"
                and self.temp.mspire.use_cache
            )
        )
    
    @property
    def super_writable(self):
        return (
            not self.temp.mspire.use_cache
        )
    
    @property
    def skip_generation(self):
        if (
            self.temp.mspire._mfc_m
            and self.temp.mspire._mfc_m.result
        ):
            return self.temp.mspire._mfc_m.result
        return False

    def reset(self):
        """This resets temp together with soft_reset."""
        self.temp = self.Temp()
        self.soft_reset()

    def soft_reset(self):
        """This resets only passable params."""
        self.basic, self.extra, self.super = self.Basic(), self.Extra(), self.Super()

    def update_settings(self, **kwargs):
        """
        Used for handling manual settings. Note that this is different with update().
        """
        accepted_params = set()

        for k, v in kwargs.items():
            for settings_name in ('basic', 'extra', 'super'):
                settings: ConfigurableSettingsModel = getattr(self, settings_name)
                model_fields = settings.__class__.model_fields
                if k in model_fields:
                    setattr(settings, k, v)
                    accepted_params.add(k)

        return len(accepted_params)

if __name__ == "__main__":
    from maica import init
    init()
    ms = MaicaSettings()
    print(ms.super.top_p)

    some_data = {
        "bypass_stream": True,
        "common": {
            "strict_conv": True,
        }
    }
    
    ms.temp.update(some_data)
    print(ms.temp)

    # ms.basic.savefile_access = 123

