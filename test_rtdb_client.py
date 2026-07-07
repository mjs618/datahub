import rt_db_client
from rt_db_client import RTDBClient


class FakeResponse:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "code": 0,
            "message": "success",
            "data": [0, -1],
        }


def test_write_realtime_data_treats_nonzero_item_result_as_failure():
    original_post = rt_db_client.requests.post
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return FakeResponse()

    rt_db_client.requests.post = fake_post
    try:
        client = RTDBClient("http://example.test", token="token", max_retries=1)
        result = client.write_realtime_data([
            {"nodeId": "AGC17", "value": 1},
            {"nodeId": "AGC18", "value": 2},
        ])
    finally:
        rt_db_client.requests.post = original_post

    assert len(calls) == 1
    assert result["code"] == 0
    assert result["results"] == [0, -1]
    assert result["success"] is False


if __name__ == "__main__":
    test_write_realtime_data_treats_nonzero_item_result_as_failure()
    print("rtdb client tests passed")
