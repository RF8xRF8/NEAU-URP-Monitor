from monitor import fetch_all_scores


class _FakeResponse:
    def __init__(self, url: str, text: str = "", payload=None):
        self.url = url
        self.text = text
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/schemeScores/index"):
            return _FakeResponse(
                "https://zhjwxs.neau.edu.cn/student/integratedQuery/scoreQuery/token123/schemeScores/index",
                text="/scoreQuery/token123/schemeScores/index",
            )
        if url.endswith("/token123/schemeScores/callback"):
            return _FakeResponse(url, payload={"data": [1, 2, 3]})
        raise AssertionError(f"unexpected url: {url}")


def test_fetch_all_scores_uses_scheme_scores_paths():
    sess = _FakeSession()
    result = fetch_all_scores(sess, "https://zhjwxs.neau.edu.cn")

    assert result == {"data": [1, 2, 3]}
    assert sess.calls == [
        "https://zhjwxs.neau.edu.cn/student/integratedQuery/scoreQuery/schemeScores/index",
        "https://zhjwxs.neau.edu.cn/student/integratedQuery/scoreQuery/token123/schemeScores/callback",
    ]
