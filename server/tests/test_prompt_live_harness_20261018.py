"""Deterministic checks for the live harnesses (no provider calls)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_judge_label_parser_accepts_json_and_text():
    from scripts.prompt_judge_calibration import _label_from_raw

    assert _label_from_raw('{"label":"accept","reason":"ok"}') == "accept"
    assert _label_from_raw('{"label": "reject"}') == "reject"
    assert _label_from_raw("reject: missing citation") == "reject"
    assert _label_from_raw("not a label") == ""


def test_matrix_checks_are_deterministic():
    from scripts.prompt_model_matrix import _check

    assert _check("identity", "我是罗航的 AI 助手")[0]
    assert not _check("identity", "我是 OpenAI 开发的模型")[0]
    assert _check("judge_json", '{"verdict":"done","reason":"ok"}')[0]
    assert not _check("judge_json", "not json")[0]
    assert _check("arithmetic", "2")[0]
    assert not _check("arithmetic", "3")[0]


def test_judge_output_loader_accepts_dict_and_list(tmp_path):
    import json
    from scripts.prompt_judge_calibration import _load_predicted

    dict_path = tmp_path / "dict.json"
    dict_path.write_text(json.dumps({"mode": "live:deepseek", "results": [
        {"id": "cal-01", "label": "accept"}]}), encoding="utf-8")
    predicted, mode = _load_predicted(str(dict_path))
    assert predicted == {"cal-01": "accept"}
    assert mode == "live:deepseek"

    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps([
        {"id": "cal-02", "label": "reject"}]), encoding="utf-8")
    predicted, mode = _load_predicted(str(list_path))
    assert predicted == {"cal-02": "reject"}
    assert mode.startswith("judge:")


def test_matrix_alias_list_excludes_non_chat_endpoints():
    from scripts.prompt_model_matrix import _aliases

    aliases = _aliases()
    assert isinstance(aliases, list)
    for skip in ("asr", "tts", "embedding", "rerank", "vlm", "deathmatch.vlm"):
        assert skip not in aliases
