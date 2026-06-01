import requests

from monitor import fetch_all_scores


class _FakeResponse:
    def __init__(self, url: str, text: str = "", payload=None):
        self.url = url
        self.text = text
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


class _FakeSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/allTermScores/index"):
            return _FakeResponse(
                "https://zhjwxs.neau.edu.cn/student/integratedQuery/scoreQuery/token123/allTermScores/index",
                text="/scoreQuery/token123/allTermScores/index",
            )
        raise AssertionError(f"unexpected url: {url}")

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, data))
        if url.endswith("/token123/allTermScores/data"):
            if isinstance(data, dict) and data.get("pageNum") == "1":
                assert data == {
                    "zxjxjhh": "",
                    "cjlx": "1",
                    "kch": "",
                    "kcm": "",
                    "pageNum": "1",
                    "pageSize": "300",
                }
                return _FakeResponse(
                    url,
                    payload={
                        "list": {
                            "pageSize": 300,
                            "pageNum": 1,
                            "pageContext": {"totalCount": 2},
                            "records": [{"KCH": "09600919j", "KCM": "生物科学类专业导论", "XF": "1", "KCCJ": "90", "CJLRFSDM": "001", "DJM": "优秀"}],
                        }
                    },
                )
            assert data == {
                "zxjxjhh": "",
                "cjlx": "1",
                "kch": "",
                "kcm": "",
                "pageNum": "2",
                "pageSize": "300",
            }
            return _FakeResponse(
                url,
                payload={
                    "list": {
                        "pageSize": 300,
                        "pageNum": 2,
                        "pageContext": {"totalCount": 2},
                        "records": [{"KCH": "09600945j", "KCM": "动物学", "XF": "3", "KCCJ": "73", "CJLRFSDM": "001", "DJM": "中等"}],
                    }
                },
            )
        raise AssertionError(f"unexpected url: {url}")


def test_fetch_all_scores_uses_scheme_scores_paths():
    sess = _FakeSession()
    result = fetch_all_scores(sess, "https://zhjwxs.neau.edu.cn")

    assert result == [
        {
            "KCH": "09600919j",
            "KCM": "生物科学类专业导论",
            "XF": "1",
            "KCCJ": "90",
            "CJLRFSDM": "001",
            "DJM": "优秀",
            "kch": "09600919j",
            "courseNumber": "09600919j",
            "kcm": "生物科学类专业导论",
            "courseName": "生物科学类专业导论",
            "xf": "1",
            "credit": "1",
            "cj": "90",
            "score": "90",
            "gradeName": "优秀",
            "grade": "优秀",
            "cjlrfsdm": "001",
            "scoreEntryModeCode": "001",
        },
        {
            "KCH": "09600945j",
            "KCM": "动物学",
            "XF": "3",
            "KCCJ": "73",
            "CJLRFSDM": "001",
            "DJM": "中等",
            "kch": "09600945j",
            "courseNumber": "09600945j",
            "kcm": "动物学",
            "courseName": "动物学",
            "xf": "3",
            "credit": "3",
            "cj": "73",
            "score": "73",
            "gradeName": "中等",
            "grade": "中等",
            "cjlrfsdm": "001",
            "scoreEntryModeCode": "001",
        },
    ]
    assert sess.calls == [
        "https://zhjwxs.neau.edu.cn/student/integratedQuery/scoreQuery/allTermScores/index",
        ("https://zhjwxs.neau.edu.cn/student/integratedQuery/scoreQuery/token123/allTermScores/data",
         {
             "zxjxjhh": "",
             "cjlx": "1",
             "kch": "",
             "kcm": "",
             "pageNum": "1",
             "pageSize": "300",
         }),
        ("https://zhjwxs.neau.edu.cn/student/integratedQuery/scoreQuery/token123/allTermScores/data",
         {
             "zxjxjhh": "",
             "cjlx": "1",
             "kch": "",
             "kcm": "",
             "pageNum": "2",
             "pageSize": "300",
         }),
    ]
