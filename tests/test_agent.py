"""The agent, driven by scripted stand-ins for the model API (no network, no key)."""
import json

from review_monitor.agent import run_agent, run_without_model
from review_monitor.sheet import load_prior
from tests.helpers import make_tools


class ScriptedModel:
    """Plays the model: fetches the new reviews, tags them all, then finishes."""

    def __init__(self):
        self.requests = []

    def create(self, **body):
        self.requests.append(json.loads(json.dumps(body)))
        turn = len(self.requests)
        last = body["messages"][-1]["content"]
        results = [json.loads(c["content"]) for c in last if isinstance(c, dict) and c.get("type") == "tool_result"] \
            if isinstance(last, list) else []

        def use(name, args):
            return {"stop_reason": "tool_use", "usage": {"input_tokens": 1000, "output_tokens": 50},
                    "content": [{"type": "tool_use", "id": f"t{turn}", "name": name, "input": args}]}
        if turn == 1:
            return use("get_new_reviews", {})
        if turn == 2:
            return use("save_review_tags", {"tags": [
                {"id": r["id"], "sentiment": "positive", "praise": ["Location"], "complaints": []}
                for r in results[0]["reviews"]]})
        return {"stop_reason": "end_turn", "usage": {"input_tokens": 1200, "output_tokens": 30},
                "content": [{"type": "text", "text": "Tagged 10 reviews; guests were happy."}]}


def test_the_model_tags_new_reviews_and_tokens_are_counted(tmp_path, fixtures):
    tools, model = make_tools(tmp_path, fixtures), ScriptedModel()
    final = run_agent(tools, client=model, model="test-model")
    ctx = tools.ctx
    assert tools.written and final == "Tagged 10 reviews; guests were happy."
    assert all(r["sentiment"] == "positive" for r in ctx.reviews) and ctx.model == "test-model"
    assert ctx.input_tokens == 2 * 1000 + 1200 and ctx.output_tokens == 2 * 50 + 30
    first = model.requests[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}       # caches tools + system prompt
    assert first["output_config"] == {"effort": "low"}
    assert {t["name"] for t in first["tools"]} == {"get_new_reviews", "save_review_tags"}  # nothing else
    # the only data the model reads is new review text: never pages, metrics or sheet rows
    fetched = json.loads(model.requests[1]["messages"][-1]["content"][0]["content"])
    assert set(fetched["reviews"][0]) == {"id", "rating", "out_of", "text"}


def test_a_run_with_nothing_new_never_calls_the_model(tmp_path, fixtures):
    run_without_model(make_tools(tmp_path, fixtures))  # first run tags and stores all 10 reviews
    prior, previous_tab = load_prior(make_tools(tmp_path, fixtures).backend)

    class NeverCalled:
        def create(self, **body):
            raise AssertionError("model called with nothing to tag")
    tools = make_tools(tmp_path, fixtures, prior=prior, previous_tab=previous_tab)
    final = run_agent(tools, client=NeverCalled(), model="test-model")
    assert "model was not called" in final and tools.written and tools.ctx.input_tokens == 0


def test_the_keyword_tagger_finishes_if_the_model_stops_early(tmp_path, fixtures):
    class Quits:
        def create(self, **body):
            return {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "done"}]}
    tools = make_tools(tmp_path, fixtures)
    final = run_agent(tools, client=Quits(), model="test-model")
    assert tools.written and "keyword tagger" in final and tools.ctx.model == "test-model + keyword tagger"
    assert all(r["sentiment"] for r in tools.ctx.reviews)


def test_an_api_failure_still_completes_the_run(tmp_path, fixtures):
    class Down:
        def create(self, **body):
            raise RuntimeError("Anthropic API 529: overloaded")
    tools = make_tools(tmp_path, fixtures)
    final = run_agent(tools, client=Down(), model="test-model")
    assert "529" in final and tools.written and all(r["sentiment"] for r in tools.ctx.reviews)


def test_code_sends_the_failure_summary(tmp_path, fixtures):
    for page in (fixtures / "airbnb" / "page").iterdir():
        page.unlink()  # every Airbnb job now errors: "Airbnb: 10/10 jobs failed"
    tools = make_tools(tmp_path, fixtures)
    final = run_agent(tools, client=None, model="test-model")  # nothing new, so no client is needed
    assert tools.ctx.alerts and tools.alert_sent and "Failure summary sent" in final
    assert (tmp_path / "alerts.log").exists()


def test_no_failure_summary_below_the_threshold(tmp_path, fixtures):
    tools = make_tools(tmp_path, fixtures)
    run_without_model(tools)
    assert tools.send_failure_summary("x")["sent"] is False


def test_tags_must_use_the_allowed_themes_and_only_the_two_tools_exist(tmp_path, fixtures):
    tools = make_tools(tmp_path, fixtures)
    tools.collect_airbnb()
    tools.collect_booking()
    rid = tools.get_new_reviews()["reviews"][0]["id"]
    tools.call("save_review_tags", {"tags": [{"id": rid, "sentiment": "mixed", "praise": ["Location", "Free cookies"],
                                              "complaints": ["Noise", "Location"]}]})
    row = next(r for r in tools.ctx.reviews if r["review_id"] == rid)
    assert (row["praise"], row["complaints"]) == ("", "Location, Noise")  # unknown theme dropped; no praise+complaint
    for name in ("write_run_to_sheet", "send_failure_summary", "collect_booking", "delete_everything"):
        assert "unknown tool" in json.loads(tools.call(name, {}))["error"]


def test_tools_refuse_to_repeat_or_run_out_of_order(tmp_path, fixtures):
    tools = make_tools(tmp_path, fixtures)
    assert "collect Airbnb first" in tools.write_run_to_sheet()["error"]   # nothing collected yet
    tools.collect_airbnb()
    assert "Already collected" in tools.collect_airbnb()["note"]          # no second fetch, no duplicate rows
    tools.collect_booking()
    assert len(tools.ctx.reviews) == len({r["review_id"] for r in tools.ctx.reviews}) == 10
    assert "untagged" in tools.write_run_to_sheet()["error"]              # new reviews must be tagged first
    assert tools.save_review_tags([{"id": "airbnb-x", "sentiment": "positive", "praise": [], "complaints": []}])["saved"] == 0
    run_without_model(tools)
    tab = tools.written["tab"]
    assert "Already written" in tools.write_run_to_sheet()["note"]        # once per run: no second run tab
    assert sum(t.startswith("Run ") for t in tools.backend.tab_names()) == 1 and tools.written["tab"] == tab


def test_a_model_that_stops_making_progress_is_cut_off(tmp_path, fixtures):
    class Stalls:
        calls = 0

        def create(self, **body):
            Stalls.calls += 1
            return {"stop_reason": "tool_use", "usage": {"input_tokens": 1000, "output_tokens": 50},
                    "content": [{"type": "tool_use", "id": f"t{Stalls.calls}", "name": "get_new_reviews", "input": {}}]}
    tools = make_tools(tmp_path, fixtures)
    final = run_agent(tools, client=Stalls(), model="test-model")
    assert Stalls.calls == 2 and "no tagging progress" in final                 # not 12 turns
    assert tools.written and all(r["sentiment"] for r in tools.ctx.reviews)    # the keyword tagger finished
