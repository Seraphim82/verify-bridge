"""Testes dos adapters HTTP do criador, cancelamento do loop e servidor web/SSE."""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from verify_bridge.adapters import (
    AnthropicCreator,
    CallableCreator,
    CallableVerifier,
    GeminiCreator,
    OpenAICompatibleCreator,
)
from verify_bridge.models import (
    CorrectionPrompt,
    CreatorSubmission,
    LoopStopReason,
)
from verify_bridge.orchestrator import LoopConfig, run_verification_loop
from verify_bridge.web.server import create_server

from tests._helpers import APPROVED, make_png, scripted_agents, verdict


class TestHttpCreatorsAndCancel(unittest.TestCase):
    def test_creator_payload_builders_include_correction_and_previous_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_txt = Path(tmp) / "entrega.txt"
            out_txt.write_text("Versao anterior com erro.", encoding="utf-8")
            sub = CreatorSubmission(
                markdown_text="Escreva um resumo limpo.",
                deliverable_files=(out_txt,),
                base_dir=Path(tmp),
            )
            corr = CorrectionPrompt(
                iteration=1,
                prompt_text="CORRECOES:\n1. [CRITICO] Corrigir resumo",
                blocking_count=1,
                acceptable_count=0,
            )

            oai = OpenAICompatibleCreator(model="gpt-4o-mini").build_payload(corr, sub)
            self.assertIn("Versao anterior com erro.", oai["messages"][1]["content"])
            self.assertIn("[CRITICO]", oai["messages"][1]["content"])

            ant = AnthropicCreator(model="claude-sonnet-4-20250514").build_payload(corr, sub)
            self.assertIn("Versao anterior com erro.", ant["messages"][0]["content"])

            gem = GeminiCreator(model="gemini-2.5-flash").build_payload(corr, sub)
            self.assertIn("Versao anterior com erro.", gem["contents"][0]["parts"][0]["text"])

    def test_loop_cancellation_stops_cleanly(self) -> None:
        cancel = threading.Event()
        cancel.set()
        res = run_verification_loop(
            CreatorSubmission(markdown_text="Pedido qualquer."),
            CallableVerifier(lambda _p, _s, _r: '{"criterios":[],"falhas":[]}'),
            CallableCreator(lambda _c, prev, _r: prev),
            config=LoopConfig(max_iterations=5, cancel_event=cancel),
        )
        self.assertFalse(res.approved)
        self.assertEqual(res.stop_reason, LoopStopReason.CANCELLED)
        self.assertEqual(len(res.iterations), 0)


class TestWebUIAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0)
        self.tmp = tempfile.TemporaryDirectory()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.cleanup()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def _get(self, path: str) -> tuple[int, bytes, str]:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_serves_html_static_env_and_no_example_route(self) -> None:
        status, body, ctype = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"Verify Bridge", body)

        status, _, ctype = self._get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)

        status, env_body, _ = self._get("/api/env")
        self.assertEqual(status, 200)
        env = json.loads(env_body.decode("utf-8"))
        self.assertIn("env_keys", env)
        # Apenas booleanos, nunca valores reais de chaves
        for v in env["env_keys"].values():
            self.assertIsInstance(v, bool)

        # O exemplo do peixe foi removido na 0.3.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/example")
        self.assertEqual(ctx.exception.code, 404)
        self.assertNotIn(b"btn-example", body)

    def test_preview_and_full_run_sse_stream_and_image_snapshots(self) -> None:
        b64 = lambda data: base64.b64encode(data).decode("ascii")  # noqa: E731
        ex = {
            "markdown": (
                "# Pedido Inicial\nImagem dourada pequena e limpa.\n\n"
                "## Justificativa do Agente Criador\nEu optei por azul.\n"
                "```python\nrender(cor='azul')\n```\n"
            ),
            "reference_files": [{"name": "referencia.png", "data_b64": b64(make_png(64, 64))}],
            "deliverable_files": [
                {"name": "entrega.png", "data_b64": b64(make_png(96, 96, (40, 90, 200)))},
                {"name": "script_gerador.py", "data_b64": b64(b"print('x')\n")},
            ],
        }

        # 1. Preview do filtro cego
        status, prev = self._post_json("/api/preview", ex)
        self.assertEqual(status, 200)
        self.assertNotIn("Justificativa", prev["raw_packet_text"])
        self.assertEqual(len(prev["sanitization"]["rejected_files"]), 1)

        # 2. Inicia conversa com IAs falsas: critico -> moderado -> aprovado
        run_payload = {
            **ex,
            **scripted_agents(
                Path(self.tmp.name),
                [verdict("critico", "moderado"), verdict("moderado"), verdict("aceitavel")],
            ),
            "max_iterations": 5,
            "stagnation_limit": 2,
        }
        status, started = self._post_json("/api/runs", run_payload)
        self.assertEqual(status, 202)
        run_id = started["run_id"]

        # 3. Consome SSE ate evento 'done'
        events: list[tuple[str, dict]] = []
        with urllib.request.urlopen(f"{self.base}/api/runs/{run_id}/events", timeout=10) as resp:
            cur_event = ""
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("event:"):
                    cur_event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = json.loads(line.split(":", 1)[1].strip())
                    events.append((cur_event, data))
                    if cur_event == "done":
                        break

        iter_events = [d for ev, d in events if ev == "iteration"]
        done_events = [d for ev, d in events if ev == "done"]
        self.assertEqual(len(iter_events), 3)
        self.assertEqual(len(done_events), 1)
        self.assertTrue(done_events[0]["aprovado"])
        self.assertEqual(done_events[0]["motivo_parada"], "aprovado")

        # 4. Verifica que a imagem snapshot da rodada 1 pode ser carregada pela UI
        img_url = iter_events[0]["previews"][0]["url"]
        status, png_bytes, ctype = self._get(img_url)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_run_without_chosen_agents_asks_to_choose(self) -> None:
        req = urllib.request.Request(
            f"{self.base}/api/runs",
            data=json.dumps({"markdown": "# Pedido Inicial\nResumo curto."}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("Escolha a IA", json.loads(ctx.exception.read())["error"])

    def test_invalid_empty_markdown_returns_400(self) -> None:
        req = urllib.request.Request(
            f"{self.base}/api/preview",
            data=json.dumps({"markdown": "## Justificativa\nEu fiz tudo."}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
