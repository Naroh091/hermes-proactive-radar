"""``hermes chat`` as the job runner uses it: the flags it passes and the result record it reads."""
from __future__ import annotations

import host_bootstrap  # noqa: F401  (skips this module without a host)

import asyncio
import io
import json
from contextlib import redirect_stdout

import hermes_cli._parser as host_parser
import hermes_cli.gateway_chat as gateway_chat
from hermes_cli.stream_json import StreamJsonEmitter

from radar.jobs.runner import JobRunner, build_argv, parse_stream


def _argv(**overrides):
    values = dict(hermes_bin="hermes", max_turns=7, workdir="/tmp", toolsets=["web"], skills=["x"], model="m")
    values.update(overrides)
    return build_argv(**values)


def test_the_host_parser_accepts_every_flag_the_runner_passes():
    parser = host_parser.build_top_level_parser()
    parser = parser[0] if isinstance(parser, tuple) else parser
    args = parser.parse_args(_argv()[1:])
    assert args.oneshot_exit is True and args.output_format == "stream-json" and args.max_turns == 7 and args.query_file == "-" and args.in_dir == "/tmp"


def test_the_runner_passes_nothing_the_gateway_refuses():
    assert "--run-budget" not in _argv() and "--worktree" not in _argv() and "-w" not in _argv()
    assert {"run_budget", "worktree"} <= set(gateway_chat._UNSUPPORTED)
    assert {"model", "toolsets", "skills", "max_turns"} <= set(gateway_chat._POLICY)


def _record(result, exit_code=0) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        StreamJsonEmitter(model="m", session_id="s1").emit_result(result, exit_code=exit_code)
    return out.getvalue()


def _run(stdout, code=0, **job):
    async def spawn(argv, stdin, timeout):
        return code, stdout, ""
    return asyncio.run(JobRunner(spawn=spawn, max_seconds=5).run({"id": "j", "prompt": "x", **job}))


def test_a_clean_result_record_is_read_as_a_success():
    final = json.dumps({"summary": "done", "findings": [], "effects": ["a"]})
    stdout = _record({"final_response": final})
    assert parse_stream(stdout)["text"] == final
    outcome = _run(stdout)
    assert outcome.status == "succeeded" and outcome.summary == "done" and outcome.session_ref == "s1"


def test_a_failed_or_interrupted_run_is_not_a_success_whatever_it_said():
    failed = _record({"failed": True, "error": "provider down", "final_response": "Something went wrong"}, exit_code=1)
    assert _run(failed, 1).status == "failed"
    assert _run(failed, 1, action_class="external").status == "unknown"
    interrupted = _record({"interrupted": True, "final_response": "halfway"}, exit_code=130)
    assert _run(interrupted, 130).status == "failed"
