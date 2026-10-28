"""``main.py`` 命令行入口的契约测试。

被测对象是**呈现层**，所以断言只集中在三件事：

    ① **输入错误的提示可读** —— 这是本文件存在的首要理由。pydantic 原样抛出的
       内容带 ``errors.pydantic.dev`` 链接和 ``extra_forbidden`` 这类内部类型名，
       对填表的人没有信息量；本文件守住「中文 + 指明字段 + 给出可用字段名」。
    ② **输出事实与 ``run_preop`` 一致** —— 呈现层不得自行推断任何结论。
    ③ **副作用边界** —— ``--no-persist`` 真的不写盘。

刻意不断言排版字符（分隔线、缩进）：版式会改，事实不会。
"""

from __future__ import annotations

import json
from pathlib import Path

import main as cli
from src.models.preop.case import DOMAIN_FIELD_COUNTS

EXAMPLE = Path(cli.REPO_ROOT) / "cases" / "example.json"


# ---------------------------------------------------------------------------
# 输入：合法 / 非法
# ---------------------------------------------------------------------------
def test_example_case_routes_to_guideline(capsys) -> None:
    """示例病例必须走通并落到指南模式 —— 它是 ``cases/`` 目录的存在理由。"""
    assert cli.main(["--case", str(EXAMPLE), "--no-persist"]) == 0
    out = capsys.readouterr().out
    assert "指南模式" in out
    assert "G04.R14" in out
    assert "证据链" in out


def test_missing_file_reports_chinese_message(tmp_path, capsys) -> None:
    assert cli.main(["--case", str(tmp_path / "nope.json")]) == 1
    err = capsys.readouterr().err
    assert "病例文件不存在" in err
    assert "Traceback" not in err


def test_invalid_json_reports_line_and_column(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{\n  "case_id": "X",\n  oops\n}\n', encoding="utf-8")
    assert cli.main(["--case", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "不是合法 JSON" in err
    assert "第 3 行" in err


def test_unknown_field_lists_available_names(tmp_path, capsys) -> None:
    """拼错字段名是最常见的录入错误，提示必须直接给出该域有哪些合法字段。"""
    bad = tmp_path / "typo.json"
    bad.write_text(json.dumps({"case_id": "X", "p": {"bogus": 1}}), encoding="utf-8")
    assert cli.main(["--case", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "p.bogus" in err
    assert "人口学域可用" in err
    assert "age" in err


def test_wrong_type_is_explained_in_chinese(tmp_path, capsys) -> None:
    bad = tmp_path / "type.json"
    bad.write_text(json.dumps({"case_id": "X", "p": {"age": "七十二"}}), encoding="utf-8")
    assert cli.main(["--case", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "p.age" in err
    assert "应为整数" in err


# ---------------------------------------------------------------------------
# 输出：事实一致性与分层
# ---------------------------------------------------------------------------
def test_json_and_text_come_from_the_same_route_result(capsys) -> None:
    cli.main(["--case", str(EXAMPLE), "--no-persist"])
    text = capsys.readouterr().out

    cli.main(["--case", str(EXAMPLE), "--no-persist", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["route_result"]["rule_id"] in text
    # 证据链首条永远是结论条目，其 rule_ref 必须等于命中规则
    assert (
        payload["final_recommendation"]["evidence_chain"][0]["rule_ref"]
        == (payload["route_result"]["rule_id"])
    )


def test_evidence_chain_covers_every_evaluated_rule(capsys) -> None:
    """证据链的规则条目数必须等于 trace 长度 —— 被否决的规则也要在链上。"""
    cli.main(["--case", str(EXAMPLE), "--no-persist", "--json"])
    payload = json.loads(capsys.readouterr().out)
    trace_len = len(payload["route_result"]["trace"])
    chain = payload["final_recommendation"]["evidence_chain"]
    rule_items = [item for item in chain[1:] if item["source"] is None]
    assert len(rule_items) == trace_len


def test_verbose_strictly_adds_lines(capsys) -> None:
    def lines(flags: list[str]) -> int:
        cli.main(["--case", str(EXAMPLE), "--no-persist", *flags])
        return len(capsys.readouterr().out.splitlines())

    assert lines([]) < lines(["-v"]) < lines(["-vv"])


def test_gaps_are_grouped_by_domain(tmp_path, capsys) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text('{"case_id": "EMPTY"}', encoding="utf-8")
    cli.main(["--case", str(empty), "--no-persist"])
    out = capsys.readouterr().out
    assert "资料缺口（40 项）" in out
    assert "CTA 报告（" in out  # 域中文名出现在分组标题里，而非裸域键 c


# ---------------------------------------------------------------------------
# 副作用与用法
# ---------------------------------------------------------------------------
def test_no_persist_is_reflected_in_output(capsys) -> None:
    cli.main(["--case", str(EXAMPLE), "--no-persist"])
    assert "落盘    否" in capsys.readouterr().out


def test_dump_template_lists_every_domain_field(capsys) -> None:
    assert cli.main(["--dump-template"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = sum(len(payload[domain]) for domain in DOMAIN_FIELD_COUNTS)
    assert rendered == sum(DOMAIN_FIELD_COUNTS.values())
    assert set(DOMAIN_FIELD_COUNTS) <= set(payload)


def test_no_arguments_prints_help_and_exits_2(capsys) -> None:
    """IDE 里点「运行」不该看到异常 —— 给用法比抛错有用。"""
    assert cli.main([]) == 2
    assert "usage:" in capsys.readouterr().out
