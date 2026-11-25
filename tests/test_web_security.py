"""Testes de seguranca da interface web local (CSRF, DNS rebinding, leitura de disco)."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from verify_bridge.models import CreatorSubmission
from verify_bridge.sanitizer import SanitizerConfig, collect_and_sanitize
from verify_bridge.web import server as server_mod
from verify_bridge.web.server import create_server


class TestWebSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0, demo_delay=0.0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = Path(self.tmp.name) / "comando_executado"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.cleanup()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=raw, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else {})

    def _cli_run_payload(self) -> dict:
        return {
            "markdown": "# Pedido Inicial\nGerar um resumo curto.",
            "verifier": {"provider": "cli", "cli_cmd": f"touch {self.marker}"},
            "creator": {"provider": "demo"},
            "max_iterations": 1,
        }

    def _assert_command_not_executed(self) -> None:
        time.sleep(0.5)
        self.assertFalse(self.marker.exists(), "comando CLI foi executado indevidamente")

    def test_cross_site_post_is_rejected_and_never_runs_command(self) -> None:
        status, body = self._request(
            "POST",
            "/api/runs",
            self._cli_run_payload(),
            {"Content-Type": "application/json", "Origin": "https://site-malicioso.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("Origem", body["error"])
        self._assert_command_not_executed()

    def test_text_plain_simple_request_is_rejected(self) -> None:
        # text/plain nao dispara preflight CORS; o servidor precisa recusar.
        status, _ = self._request(
            "POST", "/api/runs", self._cli_run_payload(), {"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        self._assert_command_not_executed()

    def test_foreign_host_header_is_rejected_dns_rebinding(self) -> None:
        status, _ = self._request("GET", "/api/env", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)
        status, _ = self._request(
            "POST",
            "/api/runs",
            self._cli_run_payload(),
            {"Content-Type": "application/json", "Host": f"evil.example:{self.port}"},
        )
        self.assertEqual(status, 403)
        self._assert_command_not_executed()

    def test_same_origin_browser_request_is_accepted(self) -> None:
        for origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            status, body = self._request(
                "POST",
                "/api/preview",
                {"markdown": "# Pedido Inicial\nGerar um resumo curto."},
                {"Content-Type": "application/json", "Origin": origin},
            )
            self.assertEqual(status, 200, origin)
            self.assertIn("raw_packet_text", body)

    def test_markdown_cannot_pull_files_from_local_disk(self) -> None:
        secret = Path(self.tmp.name) / "segredo.txt"
        secret.write_text("SEGREDO-NAO-PODE-SAIR", encoding="utf-8")
        md = (
            "# Pedido Inicial\nRevise o texto.\n\n"
            f"## Entrega\n- {secret}\n- ../../../{secret.name}\n"
        )
        status, body = self._request(
            "POST", "/api/preview", {"markdown": md}, {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("SEGREDO-NAO-PODE-SAIR", json.dumps(body))
        reasons = {r["reason"] for r in body["sanitization"]["rejected_files"]}
        self.assertIn("fora da pasta permitida", reasons)

    def test_unexpected_worker_failure_still_emits_done(self) -> None:
        with mock.patch.object(
            server_mod, "run_verification_loop", side_effect=RuntimeError("explodiu")
        ):
            status, started = self._request(
                "POST",
                "/api/runs",
                {"markdown": "# Pedido Inicial\nGerar um resumo curto."},
                {"Content-Type": "application/json"},
            )
            self.assertEqual(status, 202)
            session = self.server.get_session(started["run_id"])
            assert session is not None
            deadline = time.time() + 5
            while not session.finished and time.time() < deadline:
                time.sleep(0.05)
        self.assertTrue(session.finished)
        self.assertEqual(session.result["motivo_parada"], "erro_interno")
        self.assertIn("explodiu", session.result["erro"])


class TestSanitizerConfinement(unittest.TestCase):
    def test_confinement_off_by_default_keeps_cli_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as base:
            f = Path(outside) / "entrega.txt"
            f.write_text("conteudo externo", encoding="utf-8")
            sub = CreatorSubmission(
                markdown_text=f"# Pedido Inicial\nRevise.\n\n## Entrega\n- {f}\n",
                base_dir=Path(base),
            )
            packet, _ = collect_and_sanitize(sub)
            self.assertEqual(len(packet.artifacts), 1)

            packet, audit = collect_and_sanitize(
                sub, config=SanitizerConfig(confine_to_base_dir=True)
            )
            self.assertEqual(len(packet.artifacts), 0)
            self.assertEqual(audit.rejected_files[0].reason, "fora da pasta permitida")

    def test_symlink_escaping_base_dir_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as base:
            real = Path(outside) / "segredo.txt"
            real.write_text("segredo", encoding="utf-8")
            link = Path(base) / "atalho.txt"
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("sistema sem permissao para criar links simbolicos")
            sub = CreatorSubmission(
                markdown_text="# Pedido Inicial\nRevise.",
                deliverable_files=(link,),
                base_dir=Path(base),
            )
            packet, audit = collect_and_sanitize(
                sub, config=SanitizerConfig(confine_to_base_dir=True)
            )
            self.assertEqual(len(packet.artifacts), 0)
            self.assertEqual(audit.rejected_files[0].reason, "fora da pasta permitida")


if __name__ == "__main__":
    unittest.main()
