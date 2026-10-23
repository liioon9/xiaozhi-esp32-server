from plugins_func.functions import web_search as web_search_module


class _FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, response, request_record, **_kwargs):
        self.response = response
        self.request_record = request_record

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, json, headers):
        self.request_record.update(url=url, json=json, headers=headers)
        return self.response


async def test_search_aliyun_builds_request_and_formats_results(monkeypatch):
    request_record = {}
    response = _FakeResponse(
        200,
        {
            "result": {
                "search_result": [
                    {
                        "title": "示例标题",
                        "link": "https://example.com/article",
                        "snippet": "示例摘要",
                        "meta_info": {
                            "publishedTime": "2026-10-23T08:00:00+08:00"
                        },
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(
        web_search_module.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response, request_record, **kwargs),
    )

    result = await web_search_module._search_aliyun(
        "test-api-key",
        "测试查询",
        5,
        "https://example.opensearch.aliyuncs.com/",
    )

    assert request_record["url"] == (
        "https://example.opensearch.aliyuncs.com/v3/openapi/workspaces/"
        "default/web-search/ops-web-search-001"
    )
    assert request_record["json"] == {
        "query": "测试查询",
        "query_rewrite": True,
        "top_k": 5,
        "content_type": "snippet",
        "way": "pro",
    }
    assert request_record["headers"]["Authorization"] == "Bearer test-api-key"
    assert "示例标题" in result
    assert "https://example.com/article" in result
    assert "2026-10-23T08:00:00+08:00" in result
    assert "示例摘要" in result


async def test_search_aliyun_handles_api_error_without_exposing_message(monkeypatch):
    request_record = {}
    response = _FakeResponse(
        400,
        {"code": "InvalidParameter", "message": "sensitive upstream detail"},
    )
    monkeypatch.setattr(
        web_search_module.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response, request_record, **kwargs),
    )

    result = await web_search_module._search_aliyun(
        "test-api-key",
        "测试查询",
        5,
        "https://example.opensearch.aliyuncs.com",
    )

    assert result == "联网搜索请求失败，请检查阿里云联网搜索配置。"
    assert "sensitive upstream detail" not in result
