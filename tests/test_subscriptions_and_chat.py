"""Testes dos conectores por assinatura (com programas falsos) e do Modo Chat.

Nenhum teste aqui chama uma assinatura de verdade nem gasta cota.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from verify_bridge import subscriptions
from verify_bridge.models import AdapterError, CorrectionPrompt, CreatorSubmission
from verify_bridge.orchestrator import verify_once
from verify_bridge.sanitizer import collect_and_sanitize
from verify_bridge.subscriptions import SubscriptionCreator, SubscriptionVerifier
from verify_bridge.web.server import _draw_fish_png, create_server

POSIX = os.name != "nt"

APPROVED_JSON = json.dumps(
    {
        "criterios": [{"id": "C1", "requisito": "x", "peso": "essencial", "atendido": True}],
        "falhas": [],
    }
)

FAKE_CLAUDE = r'''#!{python}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["FAKE_LOG"])
if args[:2] == ["auth", "status"]:
    print(os.environ.get("FAKE_STATUS", '{{"loggedIn": true, "authMethod": "claude.ai"}}'))
    sys.exit(0)
stdin = sys.stdin.read()
log.write_text(json.dumps({{
    "argv": args,
    "cwd_files": sorted(os.listdir(".")),
    "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
    "stdin": stdin,
}}))
if "acceptEdits" in args:
    for name in os.listdir("."):
        if name.startswith("out_") and name.endswith(".txt"):
            Path(name).write_text("texto corrigido pelo criador")
    print("PRONTO")
else:
    print("Aqui esta: " + os.environ["FAKE_REPLY"])
'''


class TestSubscriptionAdapters(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        script = FAKE_CLAUDE.format(python=sys.executable)
        if POSIX:
            fake = self.bin / "claude"
            fake.write_text(script, encoding="utf-8")
            fake.chmod(0o755)
        else:
            # No Windows o Claude Code costuma ser instalado como claude.cmd.
            (self.bin / "claude_fake.py").write_text(script, encoding="utf-8")
            (self.bin / "claude.cmd").write_text(
                f'@"{sys.executable}" "%~dp0claude_fake.py" %*\r\n', encoding="utf-8"
            )
        self.log = Path(self.tmp.name) / "log.json"
        self.env = mock.patch.dict(
            os.environ,
            {
                "PATH": str(self.bin) + (os.pathsep + os.environ.get("SystemRoot", "") + "\\System32" if not POSIX else ""),
                "FAKE_LOG": str(self.log),
                "FAKE_REPLY": APPROVED_JSON,
                "ANTHROPIC_API_KEY": "sk-nao-pode-vazar",
            },
        )
        self.env.start()
        subscriptions.invalidate_status_cache()

    def tearDown(self) -> None:
        self.env.stop()
        subscriptions.invalidate_status_cache()
        self.tmp.cleanup()

    def _packet(self):
        work = Path(self.tmp.name) / "entrada"
        work.mkdir()
        ref = work / "minha_referencia_secreta.png"
        ref.write_bytes(_draw_fish_png(32, 32, gold=True, noisy=False, corner_marks=False))
        out = work / "entrega_v1.png"
        out.write_bytes(_draw_fish_png(32, 32, gold=False, noisy=False, corner_marks=False))
        sub = CreatorSubmission(
            markdown_text="# Pedido Inicial\nPeixe dourado.",
            reference_files=(ref,),
            deliverable_files=(out,),
            base_dir=work,
        )
        return sub

    def test_verifier_is_blind_isolated_and_never_uses_api_key(self) -> None:
        sub = self._packet()
        _, _, report, _ = verify_once(sub, SubscriptionVerifier(tool_id="claude"))
        self.assertTrue(report.approved)

        call = json.loads(self.log.read_text())
        self.assertFalse(call["has_api_key"], "chave de API vazou para a assinatura")
        # So os arquivos renomeados de forma neutra, nada do nome original.
        self.assertEqual(call["cwd_files"], ["OUT-1.png", "REF-1.png"])
        self.assertNotIn("minha_referencia_secreta", call["stdin"])
        self.assertIn("REF-1.png", call["stdin"])
        # Sem hooks/memorias do usuario e so com ferramenta de leitura.
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")
        self.assertNotIn("acceptEdits", argv)
        self.assertNotIn("--bare", argv)

    def test_creator_edits_deliverable_in_place(self) -> None:
        work = Path(self.tmp.name) / "run"
        work.mkdir()
        out = work / "out_1_resumo.txt"
        out.write_text("texto com erro", encoding="utf-8")
        sub = CreatorSubmission(markdown_text="x", deliverable_files=(out,), base_dir=work)
        corr = CorrectionPrompt(iteration=1, prompt_text="CORRIGIR: resumo", blocking_count=1, acceptable_count=0)
        creator = SubscriptionCreator(tool_id="claude", workdir=work)
        result = creator.revise(corr, sub, report=None)  # type: ignore[arg-type]
        self.assertEqual(out.read_text(), "texto corrigido pelo criador")
        self.assertEqual(result.deliverable_files, (out,))
        call = json.loads(self.log.read_text())
        self.assertIn("acceptEdits", call["argv"])
        self.assertFalse(call["has_api_key"])
        self.assertIn("out_1_resumo.txt", call["stdin"])

    def test_status_detects_subscription_api_and_logged_out(self) -> None:
        st = subscriptions.check_status("claude", use_cache=False)
        self.assertTrue(st["installed"])
        self.assertTrue(st["logged_in"])

        with mock.patch.dict(os.environ, {"FAKE_STATUS": '{"loggedIn": true, "authMethod": "console"}'}):
            st = subscriptions.check_status("claude", use_cache=False)
        self.assertFalse(st["logged_in"])
        self.assertIn("API", st["detail"])

        with mock.patch.dict(os.environ, {"FAKE_STATUS": '{"loggedIn": false, "authMethod": "none"}'}):
            st = subscriptions.check_status("claude", use_cache=False)
        self.assertFalse(st["logged_in"])

        st = subscriptions.check_status("codex", use_cache=False)
        self.assertFalse(st["installed"])

    def test_missing_program_gives_clear_error_and_login_opens_install_page(self) -> None:
        sub = self._packet()
        packet, _ = collect_and_sanitize(sub)
        with self.assertRaises(AdapterError) as ctx:
            SubscriptionVerifier(tool_id="codex").verify(packet, "sys")
        self.assertIn("nao esta instalado", str(ctx.exception))

        with mock.patch("verify_bridge.subscriptions.webbrowser.open") as opened:
            info = subscriptions.start_login("codex")
        self.assertFalse(info["started"])
        opened.assert_called_once()

    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(AdapterError):
            subscriptions.get_tool("nao-existe")


class TestChatMode(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0, demo_delay=0.0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.cleanup()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=raw, headers={"Content-Type": "application/json"} if raw else {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else {})

    def _wait_event(self, session, name: str, count: int = 1, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            evs = [e["data"] for e in list(session.events) if e["event"] == name]
            if len(evs) >= count:
                return evs[count - 1]
            time.sleep(0.05)
        self.fail(f"evento {name} nao chegou")

    def _start(self, verifier: dict, creator: dict, extra: dict | None = None):
        png = _draw_fish_png(32, 32, gold=True, noisy=False, corner_marks=False)
        body = {
            "markdown": "# Pedido Inicial\nPeixe dourado pequeno.",
            "reference_files": [{"name": "ref_secreta.png", "data_b64": base64.b64encode(png).decode()}],
            "deliverable_files": [{"name": "entrega.png", "data_b64": base64.b64encode(png).decode()}],
            "verifier": verifier,
            "creator": creator,
            "max_iterations": 3,
            **(extra or {}),
        }
        status, started = self._req("POST", "/api/runs", body)
        self.assertEqual(status, 202, started)
        session = self.server.get_session(started["run_id"])
        assert session is not None
        return started["run_id"], session

    def test_chat_verifier_waits_for_pasted_reply_and_approves(self) -> None:
        run_id, session = self._start({"provider": "chat", "chat": "chatgpt"}, {"provider": "demo"})
        req = self._wait_event(session, "awaiting_input")
        self.assertEqual(req["role"], "verifier")
        self.assertEqual(req["chat_url"], "https://chatgpt.com/")
        self.assertIn("PEDIDO_INICIAL", req["prompt"])
        self.assertNotIn("ref_secreta", req["prompt"])
        names = sorted(f["name"] for f in req["files"])
        self.assertEqual(names, ["OUT-1.png", "REF-1.png"])

        # O arquivo para anexar no chat pode ser baixado pela tela.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", req["files"][0]["url"])
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.read().startswith(b"\x89PNG"))
        conn.close()

        status, _ = self._req("POST", f"/api/runs/{run_id}/reply", {"request_id": "errado", "text": APPROVED_JSON})
        self.assertEqual(status, 409)

        status, _ = self._req(
            "POST",
            f"/api/runs/{run_id}/reply",
            {"request_id": req["request_id"], "text": "```json\n" + APPROVED_JSON + "\n```"},
        )
        self.assertEqual(status, 200)
        done = self._wait_event(session, "done")
        self.assertTrue(done["aprovado"])

    def test_chat_creator_accepts_uploaded_revised_file(self) -> None:
        failing = json.dumps(
            {
                "criterios": [{"id": "C1", "requisito": "dourado", "peso": "essencial", "atendido": False}],
                "falhas": [{"id": "F1", "criterio_id": "C1", "severidade": "critico", "observado": "azul",
                            "esperado": "dourado", "acao": "Pintar de dourado", "evidencia": "OUT-1"}],
            }
        )
        run_id, session = self._start({"provider": "chat"}, {"provider": "chat", "chat": "gemini"})
        v1 = self._wait_event(session, "awaiting_input", 1)
        self._req("POST", f"/api/runs/{run_id}/reply", {"request_id": v1["request_id"], "text": failing})

        c1 = self._wait_event(session, "awaiting_input", 2)
        self.assertEqual(c1["role"], "creator")
        self.assertTrue(c1["expects_files"])
        self.assertIn("Pintar de dourado", c1["prompt"])
        new_png = _draw_fish_png(40, 40, gold=True, noisy=False, corner_marks=False)
        status, _ = self._req(
            "POST",
            f"/api/runs/{run_id}/reply",
            {"request_id": c1["request_id"], "files": [{"name": "peixe_novo.png", "data_b64": base64.b64encode(new_png).decode()}]},
        )
        self.assertEqual(status, 200)

        v2 = self._wait_event(session, "awaiting_input", 3)
        self.assertEqual(v2["role"], "verifier")
        it2_out = [f for f in v2["files"] if f["name"] == "OUT-1.png"][0]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", it2_out["url"])
        self.assertEqual(conn.getresponse().read(), new_png)
        conn.close()

        self._req("POST", f"/api/runs/{run_id}/reply", {"request_id": v2["request_id"], "text": APPROVED_JSON})
        self.assertTrue(self._wait_event(session, "done")["aprovado"])

    def test_stop_while_waiting_for_chat_reply_cancels(self) -> None:
        run_id, session = self._start({"provider": "chat"}, {"provider": "demo"})
        self._wait_event(session, "awaiting_input")
        status, _ = self._req("POST", f"/api/runs/{run_id}/stop")
        self.assertEqual(status, 200)
        self.assertEqual(self._wait_event(session, "done")["motivo_parada"], "cancelado")

    def test_empty_reply_is_rejected(self) -> None:
        run_id, session = self._start({"provider": "chat"}, {"provider": "demo"})
        req = self._wait_event(session, "awaiting_input")
        status, body = self._req("POST", f"/api/runs/{run_id}/reply", {"request_id": req["request_id"], "text": "  "})
        self.assertEqual(status, 400)
        self.assertIn("Cole a resposta", body["error"])

    def test_subscriptions_endpoint_lists_tools_and_chats(self) -> None:
        with mock.patch(
            "verify_bridge.web.server.subscriptions.check_all_status",
            return_value=[{"id": "claude", "logged_in": True}],
        ):
            status, body = self._req("GET", "/api/subscriptions")
        self.assertEqual(status, 200)
        self.assertEqual(body["tools"][0]["id"], "claude")
        self.assertEqual({c["id"] for c in body["chats"]}, {"claude", "chatgpt", "gemini"})

    def test_cross_site_cannot_trigger_login(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/subscriptions/claude/login", body=b"{}",
                     headers={"Content-Type": "application/json", "Origin": "https://site-malicioso.example"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()


if __name__ == "__main__":
    unittest.main()
