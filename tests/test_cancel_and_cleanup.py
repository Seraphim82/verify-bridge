"""Testes do botao Parar interrompendo agentes CLI e da limpeza das pastas temporarias."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from verify_bridge.adapters import CancelledByUser, CLIVerifier, _run_cancellable
from verify_bridge.models import AdapterError, CreatorSubmission, LoopStopReason
from verify_bridge.orchestrator import LoopConfig, run_verification_loop
from verify_bridge.adapters import CallableCreator
from verify_bridge.web.server import create_server

PY = sys.executable
SLEEP_30 = [PY, "-c", "import time; time.sleep(30)"]
# Processo pai que abre um filho dormindo e grava o PID do filho.
SPAWN_CHILD = (
    "import subprocess, sys, time; "
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
    "open(sys.argv[1], 'w').write(str(c.pid)); time.sleep(30)"
)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestRunCancellable(unittest.TestCase):
    def test_normal_command_returns_output_and_code(self) -> None:
        proc = _run_cancellable(
            [PY, "-c", "import sys; sys.stdout.write(sys.stdin.read()); sys.stderr.write('erro'); sys.exit(3)"],
            stdin_data="ola",
            cwd=tempfile.gettempdir(),
            timeout_seconds=10,
            cancel_event=threading.Event(),
        )
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, "ola")
        self.assertIn("erro", proc.stderr)

    def test_cancel_kills_command_and_its_children_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "filho.pid"
            cancel = threading.Event()
            threading.Timer(1.5, cancel.set).start()  # tempo para o filho nascer
            started = time.monotonic()
            with self.assertRaises(CancelledByUser):
                _run_cancellable(
                    [PY, "-c", SPAWN_CHILD, str(pid_file)],
                    stdin_data=None,
                    cwd=tmp,
                    timeout_seconds=60,
                    cancel_event=cancel,
                )
            self.assertLess(time.monotonic() - started, 8)
            child_pid = int(pid_file.read_text().strip())
            deadline = time.monotonic() + 5
            while _pid_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_pid_alive(child_pid), "processo filho continuou rodando")

    def test_timeout_still_raises_timeout_expired(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_cancellable(
                SLEEP_30,
                stdin_data=None,
                cwd=tempfile.gettempdir(),
                timeout_seconds=0.3,
                cancel_event=None,
            )

    def test_cli_verifier_timeout_keeps_error_message(self) -> None:
        verifier = CLIVerifier(command=tuple(SLEEP_30), timeout_seconds=0.3)
        from verify_bridge.sanitizer import collect_and_sanitize

        packet, _ = collect_and_sanitize(CreatorSubmission(markdown_text="Pedido qualquer."))
        with self.assertRaises(AdapterError) as ctx:
            verifier.verify(packet, "sys")
        self.assertIn("Falha ao executar CLI verificador", str(ctx.exception))

    def test_loop_reports_cancelled_when_stopped_mid_command(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        res = run_verification_loop(
            CreatorSubmission(markdown_text="Pedido qualquer."),
            CLIVerifier(command=tuple(SLEEP_30), cancel_event=cancel),
            CallableCreator(lambda c, s, r: s),
            config=LoopConfig(cancel_event=cancel),
        )
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(res.stop_reason, LoopStopReason.CANCELLED)


class TestWebStopAndCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(
            "127.0.0.1", 0, demo_delay=0.0, session_retention_seconds=0.3
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.cleanup()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _post(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request("POST", path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else {})

    def _wait_finished(self, run_id: str, timeout: float = 5.0):
        session = self.server.get_session(run_id)
        assert session is not None
        deadline = time.monotonic() + timeout
        while not session.finished and time.monotonic() < deadline:
            time.sleep(0.05)
        return session

    def test_run_workdir_is_deleted_after_retention(self) -> None:
        status, started = self._post(
            "/api/runs", {"markdown": "# Pedido Inicial\nGerar um resumo curto."}
        )
        self.assertEqual(status, 202)
        run_id = started["run_id"]
        session = self._wait_finished(run_id)
        self.assertTrue(session.finished)
        workdir = session.workdir

        deadline = time.monotonic() + 5
        while self.server.get_session(run_id) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(self.server.get_session(run_id))
        self.assertFalse(workdir.exists(), "pasta temporaria nao foi apagada")

    def test_stop_button_interrupts_running_cli_verifier(self) -> None:
        status, started = self._post(
            "/api/runs",
            {
                "markdown": "# Pedido Inicial\nGerar um resumo curto.",
                # Comando digitado como na tela, com o caminho do Python entre aspas.
                "verifier": {"provider": "cli", "cli_cmd": f'"{PY}" -c "import time; time.sleep(30)"'},
                "creator": {"provider": "demo"},
            },
        )
        self.assertEqual(status, 202)
        run_id = started["run_id"]
        session = self.server.get_session(run_id)
        assert session is not None
        time.sleep(0.4)  # deixa o comando comecar

        started_stop = time.monotonic()
        status, _ = self._post(f"/api/runs/{run_id}/stop")
        self.assertEqual(status, 200)
        session = self._wait_finished(run_id)
        self.assertLess(time.monotonic() - started_stop, 5)
        self.assertTrue(session.finished)
        self.assertEqual(session.result["motivo_parada"], "cancelado")


if __name__ == "__main__":
    unittest.main()
