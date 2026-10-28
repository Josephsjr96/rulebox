from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from rulebox.core import CodexAdapter, Store, START, END, draft_rule
from rulebox.lora import encode_example, load_examples
from rulebox.server import make_server


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "private/rules.sqlite3")
        self.adapter = CodexAdapter(self.root)

    def add(self, value="Keep answers brief."):
        return self.store.save({"title":"Style", "source":value, "instruction":value})

    def test_store_survives_restart_and_updates(self):
        rule_id = self.add()
        store = Store(self.store.path)
        self.assertEqual(store.all()[0]["instruction"], "Keep answers brief.")
        store.save({"title":"Updated", "source":"Original", "instruction":"Ask first.", "enabled":False}, rule_id)
        self.assertEqual(store.all()[0]["enabled"], 0)
        store.delete(rule_id)
        self.assertEqual(store.all(), [])

    def test_preserves_unmanaged_bytes_and_backs_up(self):
        old = "# Existing\r\nKeep Persian text: فارسی\r\n".encode()
        self.adapter.path.write_bytes(old)
        self.add()
        preview = self.adapter.preview(self.store.all())
        result = self.adapter.apply(self.store.all(), preview["revision"])
        self.assertEqual(Path(result["backup"]).read_bytes(), old)
        self.assertTrue(self.adapter.path.read_bytes().startswith(old))
        again = self.adapter.preview(self.store.all())
        self.assertFalse(again["changed"])

    def test_disable_all_removes_managed_block_only(self):
        rule_id = self.add()
        self.adapter.path.write_text("# Existing\n\n" + START + "\nold\n" + END + "\nTail\n")
        row = self.store.all()[0]
        self.store.save({**row, "enabled":False},rule_id)
        preview = self.adapter.preview(self.store.all())
        self.assertEqual(preview["after"], "# Existing\n\n\nTail\n")

    def test_stale_file_and_rule_previews_are_rejected(self):
        self.add()
        preview = self.adapter.preview(self.store.all())
        self.adapter.path.write_text("Changed outside Rulebox")
        with self.assertRaises(ValueError):
            self.adapter.apply(self.store.all(),preview["revision"])
        preview = self.adapter.preview(self.store.all())
        self.add("Use Python.")
        with self.assertRaises(ValueError):
            self.adapter.apply(self.store.all(),preview["revision"])

    def test_symlinks_overrides_and_bad_markers_are_rejected(self):
        outside = self.root / "outside.md"
        outside.write_text("Do not touch")
        self.adapter.path.symlink_to(outside)
        with self.assertRaises(ValueError): self.adapter.preview([])
        self.adapter.path.unlink()
        override = self.root / "AGENTS.override.md"
        override.write_text("Override")
        with self.assertRaises(ValueError): self.adapter.preview([])
        override.unlink()
        for content in (START, END + START, START + START + END):
            self.adapter.path.write_text(content)
            with self.assertRaises(ValueError): self.adapter.preview([])
        self.assertEqual(outside.read_text(),"Do not touch")

    def test_draft_preserves_conditions_and_blocks_markers(self):
        value = "Only reply when I say Lantern, except in emergencies."
        self.assertEqual(draft_rule(value)["instruction"], value)
        with self.assertRaises(ValueError): self.add(START)
        with self.assertRaises(ValueError): draft_rule(" ")

    def test_training_data_validation(self):
        data = self.root / "pairs.jsonl"
        data.write_text('{"source":"Please be brief", "instruction":"Be brief."}\n')
        self.assertEqual(len(load_examples(data)),1)
        class Tokenizer:
            eos_token_id = 99
            def encode(self, value, **kwargs): return [1,2] if kwargs.get("add_special_tokens") else [3]
        item = encode_example(Tokenizer(), load_examples(data)[0],10)
        self.assertEqual(item["labels"],[-100,-100,3,99])
        with self.assertRaises(ValueError): encode_example(Tokenizer(),load_examples(data)[0],2)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(self.temp.name,Path(self.temp.name)/"rules.sqlite3")
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def request(self,path,data=None,token=True,origin=None,host=None):
        conn = http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=5)
        headers = {"Host": host or self.server.authority}
        if origin: headers["Origin"] = origin
        if token: headers["X-Rulebox-Token"] = self.server.token
        if data is not None: headers["Content-Type"] = "application/json"
        conn.request("POST" if data is not None else "GET",path,body=json.dumps(data) if data is not None else None,headers=headers)
        response=conn.getresponse(); body=response.read(); code=response.status
        conn.close()
        return code,body

    def test_origin_host_and_mutation_token(self):
        self.assertEqual(self.request("/api/preview",{},token=False)[0],403)
        self.assertEqual(self.request("/api/state",origin="http://evil.test")[0],403)
        self.assertEqual(self.request("/api/state",host="evil.test")[0],403)
        self.assertEqual(self.request("/api/state")[0],200)
        self.assertEqual(self.request("/api/draft",{"source":"Be brief"})[0],200)

    def test_full_save_preview_apply_flow(self):
        code, body = self.request("/api/save",{"title":"Style", "source":"Be brief", "instruction":"Be brief"})
        self.assertEqual(code,200)
        preview=json.loads(self.request("/api/preview",{})[1])
        self.assertIn("Be brief",preview["after"])
        self.assertEqual(self.request("/api/apply",{"revision":preview["revision"]})[0],200)
        self.assertIn("Be brief",(Path(self.temp.name)/"AGENTS.md").read_text())
        self.assertEqual(self.request("/../LICENSE")[0],404)


if __name__ == "__main__":
    unittest.main()
