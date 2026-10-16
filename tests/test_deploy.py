"""Deployment pieces: Postgres storage and the login.

Postgres tests run only when TEST_DATABASE_URL points at a scratch database:
  TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres python -m unittest tests.test_deploy
"""
import importlib
import os
import time
import unittest

os.environ["DOCU_MOCK_LLM"] = "1"

from pipeline import store as store_mod  # noqa: E402

PG = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(PG, "set TEST_DATABASE_URL to run Postgres tests")
class PostgresStore(unittest.TestCase):
    def setUp(self):
        self.s = store_mod.PgStore(PG)
        self.s.delete("t-ep")
        self.s.delete("_topics")

    def tearDown(self):
        self.s.delete("t-ep")
        self.s.pool.close()

    def test_round_trip(self):
        self.assertIsNone(self.s.read("t-ep", "meta.json"))
        self.assertFalse(self.s.exists("t-ep"))
        self.s.write("t-ep", "meta.json", '{"a": 1}')
        self.s.write("t-ep", "meta.json", '{"a": 2}')  # upsert
        self.s.write("t-ep", "research/sources.json", "[]")
        self.s.write("_topics", "latest.json", "{}")
        self.assertEqual(self.s.read("t-ep", "meta.json"), '{"a": 2}')
        self.assertIn("t-ep", self.s.slugs())
        self.assertNotIn("_topics", self.s.slugs())  # internal rows hidden from the episode list
        many = self.s.read_many(["meta.json"])
        self.assertEqual(many["t-ep"], {"meta.json": '{"a": 2}'})
        self.assertNotIn("_topics", many)
        self.assertEqual(set(self.s.files("t-ep")), {"meta.json", "research/sources.json"})
        self.s.delete("t-ep")
        self.assertFalse(self.s.exists("t-ep"))

    def test_full_pipeline_on_postgres(self):
        os.environ["DATABASE_URL"] = PG
        store_mod.get_store.cache_clear()
        try:
            from pipeline import research, script
            ep = research.run("T Ep", offline=True)
            script.run(ep.slug)
            self.assertTrue(self.s.read("t-ep", "script.json"))
            self.assertIn('"status": "scripted"', self.s.read("t-ep", "meta.json"))
        finally:
            store_mod.get_store().pool.close()
            del os.environ["DATABASE_URL"]
            store_mod.get_store.cache_clear()


class Login(unittest.TestCase):
    def setUp(self):
        os.environ["DESK_PASSWORD"] = "correct horse"
        import app
        self.app = importlib.reload(app)
        from fastapi.testclient import TestClient
        self.c = TestClient(self.app.app)

    def tearDown(self):
        del os.environ["DESK_PASSWORD"]

    def test_locked_until_signed_in(self):
        self.assertEqual(self.c.get("/api/episodes").status_code, 401)
        r = self.c.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        self.assertEqual(self.c.get("/login").status_code, 200)

    def test_wrong_then_right_password(self):
        t = time.time()
        self.assertEqual(self.c.post("/api/login", json={"password": "nope"}).status_code, 401)
        self.assertGreaterEqual(time.time() - t, 0.9)  # slowed down
        self.assertEqual(self.c.post("/api/login", json={"password": "correct horse"}).status_code, 200)
        self.assertEqual(self.c.get("/api/episodes").status_code, 200)
        self.assertTrue(self.c.get("/api/status").json()["auth"])
        self.c.post("/api/logout")
        self.assertEqual(self.c.get("/api/episodes").status_code, 401)

    def test_refuses_to_run_public_without_password(self):
        del os.environ["DESK_PASSWORD"]
        os.environ["RENDER"] = "true"
        try:
            import app
            with self.assertRaises(SystemExit):
                importlib.reload(app)
        finally:
            del os.environ["RENDER"]
            os.environ["DESK_PASSWORD"] = "x"


if __name__ == "__main__":
    unittest.main()
