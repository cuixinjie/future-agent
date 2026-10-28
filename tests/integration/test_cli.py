"""Smoke checks for the installed command, without external services."""

import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import NARRATIVE, complete_question, make_draft

from future_agent.config import Settings
from future_agent.domain.events import SourceText


def run_cli(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("future-agent")
    assert executable is not None, "Install the project with uv sync --locked first"
    environment = {
        key: value for key, value in os.environ.items() if key.lower() not in Settings.model_fields
    }
    return subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**environment, "PYTHONUTF8": "1", **(extra_env or {})},
        timeout=20,
    )


def test_installed_cli_help() -> None:
    result = run_cli("--help")
    assert result.returncode == 0
    assert "usage: future-agent" in result.stdout
    assert all(command in result.stdout for command in ("chat", "protocol", "evaluation"))


def test_analyze_requires_input() -> None:
    result = run_cli("analyze")
    assert result.returncode == 2
    assert "--input" in result.stderr


def checked(*args, extra_env=None):
    result = run_cli(*args, extra_env=extra_env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@contextmanager
def offline_http_model():
    """Local synthetic service, not a real model or compatibility certification."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            user = json.loads(request["messages"][1]["content"])
            source = SourceText.model_validate(user["sources"][0])
            draft = make_draft(source)
            payload = json.dumps(
                {
                    "choices": [
                        {"message": {"content": draft.model_dump_json()}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 45},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "LLM_MODEL": "offline-fixture-not-a-real-model",
            "NO_PROXY": "127.0.0.1",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_model_clarification_edit_confirmation_and_history(tmp_path):
    args = ("--no-env", "--data-dir", str(tmp_path / "data"))
    narrative = tmp_path / "event.txt"
    with narrative.open("w", encoding="utf-8", newline="") as file:
        file.write(NARRATIVE)
    with offline_http_model() as environment:
        session = checked(*args, "analyze", "--input", str(narrative), extra_env=environment)
        assert session["status"] == "NEEDS_INPUT" and session["consumed_tokens"] == 168
        session_id = session["id"]
        first = checked(*args, "show", session_id)
        assert first["origin"] == "model"
        sources = checked(*args, "show", session_id, "--sources")
        assert sources[0]["raw"] == NARRATIVE
        answer = tmp_path / "answer.txt"
        answer.write_text("留存率未知。", encoding="utf-8")
        answered = checked(
            *args,
            "answer",
            session_id,
            "--expected-revision",
            "1",
            "--input",
            str(answer),
            "--keep-unknown",
            first["unknowns"][0]["id"],
        )
        assert answered["current_revision"] == 2
        resumed = checked(*args, "resume", session_id, "--read", extra_env=environment)
        assert resumed["current_revision"] == 3
        draft = checked(*args, "show", session_id, "--editable")
        assert draft["unknowns"][0]["resolution_status"] == "kept_unknown"
        draft["question"] = complete_question().model_dump(mode="json")
        edited = tmp_path / "draft.json"
        edited.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        checked(*args, "edit", session_id, "--expected-revision", "3", "--input", str(edited))
        confirmed = checked(*args, "confirm", session_id, "--expected-revision", "4")
        assert confirmed["status"] == "STRUCTURED" and confirmed["current_revision"] == 5
    history = checked(*args, "history", session_id)
    assert len(history) == 5 and not history[0]["confirmed"] and history[-1]["confirmed"]
    state = checked(*args, "resume", session_id)
    assert state["status"] == "STRUCTURED" and len(state["attempts"]) == 2


def test_cli_protocol_create_lock_reload_and_reject_changes(tmp_path, protocol_fixture):
    args = ("--no-env", "--data-dir", str(tmp_path / "data"), "protocol")
    file = tmp_path / "protocol.json"
    file.write_text(protocol_fixture.model_dump_json(), encoding="utf-8")
    checked(*args, "validate", "--input", str(file))
    protocol = checked(*args, "create", "--input", str(file))
    locked = checked(*args, "lock", protocol["id"], "--expected-version", "1")
    assert locked["status"] == "locked" and locked["purpose"] == "fixture"
    assert checked(*args, "show", protocol["id"]) == locked
    rejected = run_cli(*args, "edit", "--input", str(file), "--expected-version", "2")
    assert rejected.returncode == 2


def test_cli_errors_and_doctor_do_not_disclose_credentials(tmp_path):
    args = ("--no-env", "--data-dir", str(tmp_path / "data"))
    result = checked(*args, "doctor", extra_env={"LLM_API_KEY": "sensitive-test-key"})
    assert result["model_service"] == "not_tested"
    assert "sensitive-test-key" not in json.dumps(result)
    invalid = run_cli(
        *args,
        "doctor",
        extra_env={
            "LLM_BASE_URL": "https://user:sensitive-test-key@example.com/v1",
        },
    )
    assert invalid.returncode == 2 and "sensitive-test-key" not in invalid.stderr
