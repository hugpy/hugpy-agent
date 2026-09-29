from hugpy_agent import existing_system_benchmark as subject


class Client:
    def __init__(self):
        self.posts = []

    def request(self, path, method="GET", body=None):
        if path == "/models?verbose=1":
            return [{"model_key": "m", "effective_gguf": "q.gguf", "workers": [
                {"worker_id": "w1", "worker": "one", "status": "online",
                 "designated": True, "alloc_mode": "gpu-only"}]}]
        if path.startswith("/llm/serving/"):
            return {"gguf_file_by_worker": {"w1": "worker-q.gguf"}}
        assert path == "/v1/chat/completions"
        self.posts.append(body)
        prompt = body["messages"][0]["content"]
        answer = "ready" if "Reply only: ready" in prompt else expected(prompt)
        return {"choices": [{"message": {"content": answer}}],
                "usage": {"completion_tokens": 1}}


def expected(prompt):
    answers = {"15 + 22": "37", "17 * 23": "391", "3x + 12": "5",
               "5 apples": "8", "48 apples": "17", "Train A": "6",
               "Red Planet": "Mars", "chemical symbol": "Au", "penicillin": "Fleming",
               "first three prime": "2,3,5", "first five prime numbers in reverse": "11,7,5,3,2",
               "first five prime": "2,3,5,7,11", "bloops": "yes", "shortest": "c",
               "3-gallon": "yes", "BANANA, but in lowercase": "banana",
               "[BANANA_123]": "[BANANA_123]", "single word: BANANA": "BANANA",
               "sum()": "sum(xs)", "list comprehension": "[x for x in xs if x%2==0]",
               "recursive Python lambda": "fib=lambda n:n if n<2 else fib(n-1)+fib(n-2)",
               'key "a" containing': '{"a":[1,2]}', 'keys "a"': '{"a":1,"b":2}',
               'key "a"': '{"a":1}', "word tree": "2", "strawberry": "3",
               "mississippi": "4", "theoretical optics": "a sufficiently long structural response"}
    return next(value for needle, value in answers.items() if needle in prompt)


class Stop:
    def is_set(self): return False
    def cancelled(self, **_kwargs): return False


def test_cold_once_then_sequential_hot_variations_through_central(monkeypatch):
    monkeypatch.setattr(subject, "eligible", lambda _worker: True)
    client, reports = Client(), []
    subject.run_capacity_benchmark(client, [{"id": "w1", "name": "one"}], 32,
                                   Stop(), lambda kind, value: reports.append((kind, value)),
                                   model_ids=["m"])
    # Cognition runs once (27 calls); both fixed allocations get seating and a
    # throughput call. No variable max_gpu/max_ram placement is generated.
    assert len(client.posts) == 31
    assert {post["alloc"]["alloc_mode"] for post in client.posts} == {"gpu_only", "ram_only"}
    assert all(post["alloc"]["worker"] == "one" for post in client.posts)
    plan = next(value for kind, value in reports if kind == "plan")
    assert plan["rows"][0]["quant"] == "worker-q.gguf"
    results = [value for kind, value in reports if kind == "result"]
    assert len(results) == 2
    assert all(result["grade"] == "27/27" for result in results)
    assert results[0]["detail"]["math"]["tier"] == 3
    assert results[0]["cold_s"] is not None and results[0]["hot_load_s"] is None
    assert results[1]["cold_s"] == results[0]["cold_s"]
    assert results[1]["hot_load_s"] is not None


def test_tier_failure_exits_category_but_not_suite(monkeypatch):
    class FailingEasyMath(Client):
        def request(self, path, method="GET", body=None):
            if path == "/v1/chat/completions" and "15 + 22" in body["messages"][0]["content"]:
                self.posts.append(body)
                return {"choices": [{"message": {"content": "wrong"}}],
                        "usage": {"completion_tokens": 1}}
            return super().request(path, method, body)

    monkeypatch.setattr(subject, "eligible", lambda _worker: True)
    reports = []
    subject.run_capacity_benchmark(FailingEasyMath(), [{"id": "w1", "name": "one"}], 32,
                                   Stop(), lambda kind, value: reports.append((kind, value)), model_ids=["m"])
    calls = [value for kind, value in reports if kind == "call"]
    assert [c["task"] for c in calls if c["task"].startswith("math")] == ["math (easy)"]
    assert next(value for kind, value in reports if kind == "result")["detail"]["math"]["tier"] == 0


def test_matrix_uses_fixed_modes_and_marks_constraints():
    gib = 2 ** 30
    lane = {"size_bytes": 10 * gib,
            "model_record": {"is_4bit_capable": True, "is_moe_capable": True,
                             "moe_explicit_vram": 4 * gib, "moe_explicit_ram": 3 * gib},
            "joined": {}, "worker_record": {"max_vram_bytes": 8 * gib, "max_ram_bytes": 16 * gib}}
    rows = subject._variations(lane)
    assert {row[1] for row in rows} == {"gpu_only", "ram_only", "explicit"}
    assert not next(row for row in rows if row[:2] == ("standard", "gpu_only"))[3]
    assert next(row for row in rows if row[:2] == ("standard", "ram_only"))[3]
    assert next(row for row in rows if row[:2] == ("standard", "explicit"))[3]
