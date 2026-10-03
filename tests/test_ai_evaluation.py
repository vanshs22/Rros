import json
from pathlib import Path

from test_app import auth, make_client


FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "agent_eval.json").read_text())


def test_agent_evaluation_dataset_and_regression_invariants(tmp_path):
    with make_client(tmp_path) as client:
        headers = auth(client)
        opportunity_id = client.get("/api/opportunities", headers=headers).json()[0]["id"]
        run = client.post(f"/api/opportunities/{opportunity_id}/agents/run", headers=headers)
        assert run.status_code == 200, run.text
        detail = client.get(f"/api/agent-runs/{run.json()['pipeline_run_id']}", headers=headers)
        assert detail.status_code == 200
        body = detail.json()
        expected = {case["name"]: case for case in FIXTURES}
        assert len(body["agents"]) == expected["pipeline_contract"]["expect_agent_count"]
        assert len(body["handoffs"]) == expected["pipeline_contract"]["expect_handoff_count"]
        action = next(agent["output"] for agent in body["agents"] if agent["output"].get("tool") == "connector.send_message")
        assert action["allowed"] is expected["unsafe_outreach_without_consent"]["expect_action_allowed"]
        assert all(agent["status"] == "succeeded" for agent in body["agents"])
