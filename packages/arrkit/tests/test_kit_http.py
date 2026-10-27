import unittest

from arrkit.http import JSON, Request, Response, Router


class RequestTest(unittest.TestCase):
    def test_query_and_params(self):
        req = Request("get", "/api/x", query={"a": "1", "b": ""}, headers={})
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.q("a"), "1")
        self.assertEqual(req.q("b", "dft"), "dft")     # 空串按缺省处理
        self.assertEqual(req.qi("a"), 1)
        self.assertEqual(req.qi("zz", 7), 7)
        req.params = {"id": "9"}
        self.assertEqual(req.param("id"), "9")

    def test_json_body(self):
        req = Request("POST", "/x", body=b'{"k": 2}')
        self.assertEqual(req.json_body(), {"k": 2})
        self.assertEqual(Request("POST", "/x", body=b"not json").json_body(), {})
        self.assertEqual(Request("POST", "/x", body=b"[1,2]").json_body(), {})

    def test_base_url(self):
        req = Request("GET", "/", headers={"Host": "1.2.3.4:80"})
        self.assertEqual(req.base_url(), "http://1.2.3.4:80")


class ResponseTest(unittest.TestCase):
    def test_dict_becomes_json(self):
        r = Response({"a": 1})
        self.assertEqual(r.ctype, JSON)
        self.assertIn(b'"a": 1', r.body)          # ensure_ascii=False 且带空格
        self.assertEqual(Response.json({"a": 1}).status, 200)

    def test_str_and_err(self):
        r = Response.text("hi")
        self.assertEqual(r.body, b"hi")
        e = Response.err("坏了", 409)
        self.assertEqual(e.status, 409)
        self.assertIn(b'"ok": false', e.body)

    def test_with_headers_is_copy(self):
        a = Response.text("x")
        b = a.with_headers(**{"X-A": "1"})
        self.assertNotIn("X-A", a.headers)
        self.assertEqual(b.headers["X-A"], "1")


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.r = Router()
        self.r.get("/api/items", lambda req: Response.json({"n": 1}))
        self.r.get("/api/items/{iid}", lambda req: Response.json(req.params))
        self.r.get("/plug/{mid}/{path:path}", lambda req: Response.json(req.params))
        self.r.post("/api/items", lambda req: Response.json({"made": True}, 201))

    def _req(self, method, path):
        return Request(method, path)

    def test_exact_and_param(self):
        self.assertEqual(self.r.dispatch(self._req("GET", "/api/items")).status, 200)
        got = self.r.dispatch(self._req("GET", "/api/items/42")).body
        self.assertIn(b'"iid": "42"', got)

    def test_path_wildcard(self):
        got = self.r.dispatch(self._req("GET", "/plug/clock/a/b.js")).body
        self.assertIn(b'"path": "a/b.js"', got)

    def test_head_falls_back_to_get(self):
        self.assertIsNotNone(self.r.dispatch(self._req("HEAD", "/api/items")))

    def test_wrong_method_is_miss(self):
        self.assertIsNone(self.r.dispatch(self._req("DELETE", "/api/items")))
        self.assertIsNone(self.r.dispatch(self._req("GET", "/nope")))

    def test_trailing_slash(self):
        self.assertIsNotNone(self.r.dispatch(self._req("GET", "/api/items/")))

    def test_decorator_form(self):
        r = Router()

        @r.get("/x")
        def h(req):
            return Response.text("ok")

        self.assertEqual(r.dispatch(self._req("GET", "/x")).body, b"ok")

    def test_paths_and_find(self):
        self.assertIn("GET /api/items", self.r.paths())
        self.assertIsNotNone(self.r.find("POST", "/api/items"))
        self.assertIsNone(self.r.find("POST", "/api/items/1"))
        self.assertEqual(len(self.r), 4)


if __name__ == "__main__":
    unittest.main()
