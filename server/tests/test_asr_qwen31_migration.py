"""Qwen-Audio-3.1-ASR 迁移（P0/P1）回归——协议族分派/内联热词 vocabulary/调参接线/词表 target_model。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_asr_qwen31_migration.py -v
"""
from types import SimpleNamespace

import app.services.asr_service as asr_mod
from app.services.asr_service import ASRService
from app.services.asr_vocabulary_service import ASRVocabularyService


class _ModelStub(ASRService):
    """注入 dashscope_model 的最小桩（ASRService 全属性、无 __init__）。"""

    def __init__(self, model: str):
        self._model = model

    @property
    def dashscope_model(self) -> str:
        return self._model


class _VocabStub(ASRVocabularyService):
    def __init__(self, model_name: str | None, extra: dict | None = None, asr_cfg: dict | None = None,
                 api_key: str | None = None):
        self._asr_config = asr_cfg or {}
        self._ep = SimpleNamespace(model_name=model_name, extra=extra or {}, api_key=api_key)

    @property
    def _endpoint(self):
        return self._ep


# ── 1. run-task 协议族分派：qwen-audio-3.x-asr-*-streaming 必须走流式代理 ──


def test_qwen_audio_streaming_is_run_task_model():
    svc = _ModelStub("qwen-audio-3.1-asr-flash-streaming")
    assert svc._is_funasr_model, (
        "qwen-audio-3.1-asr-flash-streaming 与 fun-asr-realtime 同 run-task duplex 协议族，"
        "必须命中流式代理分派（否则听写面静默走错协议）"
    )


def test_legacy_models_still_match():
    assert _ModelStub("fun-asr-realtime")._is_funasr_model
    assert _ModelStub("paraformer-realtime-v2")._is_funasr_model


def test_filetrans_model_does_not_match_streaming_dispatch():
    assert not _ModelStub("qwen-audio-3.1-asr-flash-filetrans")._is_funasr_model


# ── 2. 内联热词 → 标准 vocabulary 对象（非 hotwords 串）──


def test_inline_hotwords_format_is_vocabulary_dict():
    got = ASRService()._format_funasr_hotwords(
        [{"text": "张三", "weight": 9}, {"text": "李四"}, {"text": "   "}]
    )
    assert isinstance(got, dict), "Qwen-Audio 族即时热词参数应为 vocabulary 对象（dict[str,int]）"
    assert got == {"张三": 5, "李四": 4}, "权重钳制 [1,5]、缺省 4、空白词剔除"


def test_inline_hotwords_super_weight_50_preserved():
    """协议值 50=超级热词，不得被钳成 5（A4.9 R1-I4）。"""
    got = ASRService()._format_funasr_hotwords([{"text": "超级词", "weight": 50}, {"text": "普通", "weight": 50.0}])
    assert got == {"超级词": 50, "普通": 50}


def test_inline_hotwords_empty_is_none():
    assert ASRService()._format_funasr_hotwords([]) is None
    assert ASRService()._format_funasr_hotwords(None) is None


# ── 3. vad_model / keep_dialect 调参接线（纯函数缝）──


def test_recognition_tuning_params_exists_and_maps_config():
    fn = getattr(asr_mod, "_recognition_tuning_params", None)
    assert fn is not None, "missing feature: _recognition_tuning_params（run-task 调参接线缝）"
    m31 = "qwen-audio-3.1-asr-flash-streaming"
    assert fn({}, m31) == {}
    assert fn({"vad_model": "near_meeting_16k", "keep_dialect": True}, m31) == {
        "vad_model": "near_meeting_16k",
        "keep_dialect": True,
    }
    assert fn({"vad_model": "far_field_meeting_16k"}, m31) == {"vad_model": "far_field_meeting_16k"}
    assert fn({"keep_dialect": False}, m31) == {"keep_dialect": False}, "显式 false 须下发（is not None 语义）"


def test_tuning_params_gated_on_31_model():
    """3.1 专属键不得随 fun-asr/paraformer 回退下发（A4.9 R2-I1）。"""
    fn = asr_mod._recognition_tuning_params
    cfg = {"vad_model": "near_meeting_16k", "keep_dialect": True}
    assert fn(cfg, "fun-asr-realtime") == {}, "fun-asr 回退不得收 3.1 专属键"
    assert fn(cfg, "paraformer-realtime-v2") == {}
    assert fn(cfg, "qwen-audio-3.0-asr-flash-streaming") == {}, "3.0 无 vad_model/keep_dialect"


def test_instant_vocabulary_gated_on_qwen_audio_family():
    assert asr_mod._supports_instant_vocabulary("qwen-audio-3.1-asr-flash-streaming")
    assert asr_mod._supports_instant_vocabulary("qwen-audio-3.0-asr-flash-streaming")
    assert not asr_mod._supports_instant_vocabulary("fun-asr-realtime")
    assert not asr_mod._supports_instant_vocabulary("paraformer-realtime-v2")


def test_tuning_params_wired_into_proxy_and_voice():
    import inspect

    proxy_src = inspect.getsource(ASRService.proxy_funasr_websocket_stream)
    assert "_recognition_tuning_params" in proxy_src, "听写流式代理须接线 _recognition_tuning_params"
    assert "_supports_instant_vocabulary" in proxy_src, "听写 vocabulary 下发须过族门控（A4.9 R3 缺口）"
    import app.services.voice_service as vs

    voice_src = inspect.getsource(vs._VoiceASR._funasr_connect)
    assert "_recognition_tuning_params" in voice_src, "语音 _VoiceASR 须接线 _recognition_tuning_params"
    assert "_supports_instant_vocabulary" in voice_src, "语音 vocabulary 下发须过族门控（A4.9 R3 缺口）"
    oneshot_src = inspect.getsource(ASRService._transcribe_dashscope_oneshot)
    assert "_recognition_tuning_params" in oneshot_src, "oneshot run-task 须接线 _recognition_tuning_params（A4.9 R3 缺口）"


# ── 4. 词表 target_model 必须跟随顶层 model_name（静默失效缺陷）──


def test_vocab_target_model_follows_endpoint_model_name():
    svc = _VocabStub("qwen-audio-3.1-asr-flash-streaming", extra={}, asr_cfg={})
    assert svc._target_model == "qwen-audio-3.1-asr-flash-streaming", (
        "target_model 须取 ep.model_name（单一模型名原则）；否则热词表仍绑旧模型=静默失效"
    )


def test_vocab_target_model_extra_fallback_then_config():
    svc = _VocabStub(None, extra={"dashscope_model": "from-extra"}, asr_cfg={})
    assert svc._target_model == "from-extra"
    # ep 在场但空 model_name/空 extra → 终端回落与 ASRService.dashscope_model
    # 逐跳一致（DEFAULT 常量），不再取 [asr]（A4.9 R1-I1 分歧修复）
    svc2 = _VocabStub(None, extra={}, asr_cfg={"dashscope_model": "from-cfg"})
    assert svc2._target_model == asr_mod.DEFAULT_DASSCOPE_ASR_MODEL, (
        "ep 在场空值时 _target_model 须与 dashscope_model 同终端回落（from-cfg 是分歧行为）"
    )


def test_vocab_api_key_follows_endpoint_top_level():
    """单一密钥原则：顶层 api_key 即当前 provider 密钥（否则词表服务静默 disabled）。"""
    svc = _VocabStub("m", extra={}, asr_cfg={}, api_key="sk-top")
    assert svc._api_key == "sk-top", "词表服务 _api_key 须读 ep.api_key（与 ASRService.dashscope_api_key 对齐）"
    assert svc.enabled
    svc2 = _VocabStub("m", extra={"dashscope_api_key": "sk-extra"}, asr_cfg={}, api_key=None)
    assert svc2._api_key == "sk-extra", "旧 extra.dashscope_api_key 兜底保留"


# ── 5. 近场旋钮镜像（P2：前端 PROX 0.04/6.0 → 后端 _MIC_ENERGY 同步）──


def test_mic_energy_near_mirror_matches_frontend_scale():
    from app.services import voice_service as vs

    assert abs(vs._MIC_ENERGY_NEAR_RATIO - 6.0) < 1e-9, "后端近场倍率须与前端 PROX_NEAR_RATIO=6.0 同步"
    assert abs(vs._MIC_ENERGY_NEAR_MIN / 32768.0 - 0.04) < 1e-6, "后端近场绝对下限须与前端 PROX_NEAR_MIN=0.04 同尺度"
