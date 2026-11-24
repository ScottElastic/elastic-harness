import json
import os
import tempfile
import unittest
from pathlib import Path

from elastic_harness.client import parse_sse
from elastic_harness.config import Config, Profile
from elastic_harness.guard import expand_mentions, is_secret_path
from elastic_harness.ingest import chunk_text, index_name, slugify
from elastic_harness.repl import match_models, pick_default_model

MODELS = [
    ".anthropic-claude-4.5-haiku-chat_completion",
    ".anthropic-claude-4.6-sonnet-chat_completion",
    ".anthropic-claude-5.5-sonnet-chat_completion",
    ".anthropic-claude-5.5-opus-chat_completion",
    ".google-gemini-3.1-pro-chat_completion",
]


class SSE(unittest.TestCase):
    def test_kibana_stream_with_padding_comments(self):
        raw = [b"event: conversation_id_set\n", b'data: {"data":{"conversation_id":"x"}}\n', b"\n",
               b": 0000000000\n", b"event: message_chunk\n", b'data: {"data":{"text_chunk":"hi"}}\n', b"\n"]
        self.assertEqual(list(parse_sse(raw)), [("conversation_id_set", '{"data":{"conversation_id":"x"}}'),
                                                ("message_chunk", '{"data":{"text_chunk":"hi"}}')])

    def test_elasticsearch_bom_and_done(self):
        raw = ["﻿event: message\n", 'data: {"a":1}\n', "\n", "﻿event: message\n", "data: [DONE]\n", "\n"]
        self.assertEqual([d for _, d in parse_sse(raw)], ['{"a":1}', "[DONE]"])

    def test_trailing_event_without_blank_line(self):
        self.assertEqual(list(parse_sse(["data: x\n"])), [("message", "x")])


class Guard(unittest.TestCase):
    def test_secret_paths(self):
        for p in ["creds.local.json", "/a/b/.env", "/a/.env.prod", "~/.ssh/config", "id_rsa", "x/server.pem",
                  "/Users/x/.config/elastic-harness/config.json", "env.sh", "/p/.aws/credentials"]:
            self.assertTrue(is_secret_path(p), p)
        for p in ["README.md", "notes/creds-howto.md", "src/app.py", "data.json"]:
            self.assertIsNone(is_secret_path(p), p)

    def test_expand_mentions(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d).resolve()
            (d / "a.txt").write_text("hello world")
            (d / "creds.local.json").write_text('{"API_KEY": "SECRET"}')
            (d / "sub").mkdir()
            (d / "sub" / "x.md").write_text("x")
            text, notes = expand_mentions("summarise @a.txt, and @creds.local.json and @sub and mail me@example.com @nobody", d)
            self.assertIn("hello world", text)
            self.assertNotIn("SECRET", text)
            self.assertIn("<attached_directory", text)
            self.assertIn("x.md", text)
            status = {Path(n["path"]).name: n["status"] for n in notes}
            self.assertEqual(status["a.txt"], "attached")
            self.assertTrue(status["creds.local.json"].startswith("refused"))
            self.assertEqual(status["sub"], "dir")
            self.assertEqual(len(notes), 3)  # the email and @nobody are not mentions

    def test_binary_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "b.bin").write_bytes(b"\x00\x01\x02")
            _, notes = expand_mentions("look at @b.bin", Path(d))
            self.assertTrue(notes[0]["status"].startswith("skipped"))


class Ingest(unittest.TestCase):
    def test_chunking(self):
        self.assertEqual(chunk_text("short"), ["short"])
        self.assertEqual(chunk_text("   "), [])
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(20))
        chunks = chunk_text(text, size=600, overlap=50)
        self.assertGreater(len(chunks), 5)
        self.assertTrue(all(len(c) <= 700 for c in chunks), max(len(c) for c in chunks))
        joined = " ".join(chunks)
        for i in range(20):
            self.assertIn(f"Paragraph {i}.", joined)

    def test_huge_single_paragraph_terminates(self):
        chunks = chunk_text("word " * 5000, size=1000, overlap=100)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 1100 for c in chunks))

    def test_index_names_are_prefixed(self):
        self.assertEqual(index_name("My Notes!"), "eh-local-my-notes")
        self.assertEqual(index_name("eh-local-x"), "eh-local-x")
        self.assertEqual(slugify("???"), "files")


class Models(unittest.TestCase):
    def test_match(self):
        self.assertEqual(match_models(MODELS, "sonnet 5.5"), [MODELS[2]])
        self.assertEqual(match_models(MODELS, "gemini"), [MODELS[4]])
        self.assertEqual(len(match_models(MODELS, "sonnet")), 2)
        self.assertEqual(match_models(MODELS, MODELS[3]), [MODELS[3]])
        self.assertEqual(match_models(MODELS, "gpt"), [])

    def test_default_pick(self):
        self.assertEqual(pick_default_model(MODELS), MODELS[2])
        self.assertEqual(pick_default_model(["x", "y"]), "x")
        self.assertEqual(pick_default_model([]), "")


class Profiles(unittest.TestCase):
    def test_creds_file_read_in_place_and_not_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            creds = Path(d) / "creds.local.json"
            creds.write_text(json.dumps({"KIBANA_URL": "https://kb.example/", "ES_URL": "https://es.example", "API_KEY": "k123"}))
            cfg = Config(Path(d) / "config.json")
            cfg.add(Profile(name="t", creds_file=str(creds), space="demo"))
            saved = (Path(d) / "config.json").read_text()
            self.assertNotIn("k123", saved)
            self.assertNotIn("kb.example", saved)
            self.assertEqual(oct(os.stat(Path(d) / "config.json").st_mode & 0o777), "0o600")
            r = Config(Path(d) / "config.json").get("t").resolved()
            self.assertEqual((r.kibana_url, r.es_url, r.api_key, r.space), ("https://kb.example", "https://es.example", "k123", "demo"))

    def test_env_overrides(self):
        os.environ["EH_SPACE"] = "other"
        try:
            self.assertEqual(Profile(name="x", kibana_url="u", api_key="k").resolved().space, "other")
        finally:
            del os.environ["EH_SPACE"]


if __name__ == "__main__":
    unittest.main()
