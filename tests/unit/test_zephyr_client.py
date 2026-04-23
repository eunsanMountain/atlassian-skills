from __future__ import annotations

import json

import httpx
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import AuthError, NotFoundError
from atlassian_skills.zephyr.client import ZephyrClient
from atlassian_skills.zephyr.models import TestStepRequest as StepRequest
from atlassian_skills.zephyr.models import ZephyrTestCase, ZephyrTestPlan, ZephyrTestResult, ZephyrTestRun

BASE_URL = "https://jira.example.com"
API = "/rest/atm/1.0"


def _client() -> ZephyrClient:
    return ZephyrClient(BASE_URL, Credential(method="pat", token="test-token"))


TESTCASE = {
    "key": "JQA-T1",
    "name": "Login works",
    "projectKey": "JQA",
    "status": "Draft",
    "priority": "High",
    "labels": ["auth"],
    "testScript": {
        "type": "STEP_BY_STEP",
        "steps": [{"id": 10, "description": "Open login", "testData": "user", "expectedResult": "Form opens"}],
    },
}


@respx.mock
def test_get_testcase() -> None:
    respx.get(f"{BASE_URL}{API}/testcase/JQA-T1").mock(return_value=httpx.Response(200, json=TESTCASE))

    result = _client().get_testcase("JQA-T1")

    assert isinstance(result, ZephyrTestCase)
    assert result.key == "JQA-T1"
    assert result.project_key == "JQA"


@respx.mock
def test_search_testcases_supports_direct_array() -> None:
    route = respx.get(f"{BASE_URL}{API}/testcase/search").mock(return_value=httpx.Response(200, json=[TESTCASE]))

    result = _client().search_testcases(query='projectKey = "JQA"', fields="key,name", max_results=50)

    assert len(result) == 1
    assert result[0].name == "Login works"
    params = dict(route.calls[0].request.url.params)
    assert params["query"] == 'projectKey = "JQA"'
    assert params["fields"] == "key,name"
    assert params["maxResults"] == "50"


@respx.mock
def test_search_testplans_supports_wrapped_results() -> None:
    payload = {"results": [{"key": "JQA-P1", "name": "Plan", "projectKey": "JQA", "status": "Draft"}]}
    respx.get(f"{BASE_URL}{API}/testplan/search").mock(return_value=httpx.Response(200, json=payload))

    result = _client().search_testplans()

    assert isinstance(result[0], ZephyrTestPlan)
    assert result[0].key == "JQA-P1"


@respx.mock
def test_create_update_delete_testcase() -> None:
    create = respx.post(f"{BASE_URL}{API}/testcase").mock(return_value=httpx.Response(201, json={"key": "JQA-T2"}))
    update = respx.put(f"{BASE_URL}{API}/testcase/JQA-T2").mock(return_value=httpx.Response(204))
    delete = respx.delete(f"{BASE_URL}{API}/testcase/JQA-T2").mock(return_value=httpx.Response(204))
    client = _client()

    key = client.create_testcase({"name": "New", "projectKey": "JQA"})
    client.update_testcase(key, {"status": "Approved"})
    client.delete_testcase(key)

    assert key == "JQA-T2"
    assert json.loads(create.calls[0].request.content)["projectKey"] == "JQA"
    assert json.loads(update.calls[0].request.content)["status"] == "Approved"
    assert delete.called


@respx.mock
def test_testrun_and_results_operations() -> None:
    respx.get(f"{BASE_URL}{API}/testrun/JQA-R1").mock(
        return_value=httpx.Response(200, json={"key": "JQA-R1", "name": "Run", "projectKey": "JQA", "status": "Done"})
    )
    respx.post(f"{BASE_URL}{API}/testrun").mock(return_value=httpx.Response(201, json={"key": "JQA-R2"}))
    respx.get(f"{BASE_URL}{API}/testrun/search").mock(
        return_value=httpx.Response(200, json=[{"key": "JQA-R1", "name": "Run", "projectKey": "JQA"}])
    )
    respx.get(f"{BASE_URL}{API}/testrun/JQA-R1/testresults").mock(
        return_value=httpx.Response(200, json=[{"id": 7, "testCaseKey": "JQA-T1", "projectKey": "JQA", "status": "Pass"}])
    )
    client = _client()

    assert isinstance(client.get_testrun("JQA-R1"), ZephyrTestRun)
    assert client.create_testrun({"name": "Run 2", "projectKey": "JQA"}) == "JQA-R2"
    assert client.search_testruns()[0].key == "JQA-R1"
    assert isinstance(client.get_testrun_results("JQA-R1")[0], ZephyrTestResult)


@respx.mock
def test_testresult_create_and_latest() -> None:
    respx.post(f"{BASE_URL}{API}/testresult").mock(return_value=httpx.Response(201, json={"id": 42}))
    respx.get(f"{BASE_URL}{API}/testcase/JQA-T1/testresult/latest").mock(
        return_value=httpx.Response(200, json={"id": 42, "testCaseKey": "JQA-T1", "projectKey": "JQA", "status": "Pass"})
    )
    client = _client()

    assert client.create_testresult({"testCaseKey": "JQA-T1", "status": "Pass"}) == 42
    assert client.get_testcase_latest_result("JQA-T1").id == 42


@respx.mock
def test_latest_result_returns_none_on_404() -> None:
    respx.get(f"{BASE_URL}{API}/testcase/JQA-T1/testresult/latest").mock(return_value=httpx.Response(404))

    assert _client().get_testcase_latest_result("JQA-T1") is None


@respx.mock
def test_testrun_result_write_variants() -> None:
    create = respx.post(f"{BASE_URL}{API}/testrun/JQA-R1/testcase/JQA-T1/testresult").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    update = respx.put(f"{BASE_URL}{API}/testrun/JQA-R1/testcase/JQA-T1/testresult").mock(
        return_value=httpx.Response(200, json={"id": 2})
    )
    bulk = respx.post(f"{BASE_URL}{API}/testrun/JQA-R1/testresults").mock(
        return_value=httpx.Response(201, json={"ids": [3, 4]})
    )
    client = _client()

    assert client.create_testrun_result("JQA-R1", "JQA-T1", {"status": "Pass"}, environment="QA") == 1
    assert client.update_testrun_result("JQA-R1", "JQA-T1", {"status": "Fail"}, user_key="alice") == 2
    assert client.create_bulk_testrun_results("JQA-R1", [{"testCaseKey": "JQA-T1"}]) == [3, 4]
    assert dict(create.calls[0].request.url.params)["environment"] == "QA"
    assert dict(update.calls[0].request.url.params)["userKey"] == "alice"
    assert json.loads(bulk.calls[0].request.content)[0]["testCaseKey"] == "JQA-T1"


@respx.mock
def test_test_steps_append_rewrites_test_script() -> None:
    respx.get(f"{BASE_URL}{API}/testcase/JQA-T1").mock(return_value=httpx.Response(200, json=TESTCASE))
    route = respx.put(f"{BASE_URL}{API}/testcase/JQA-T1").mock(return_value=httpx.Response(200, json=TESTCASE))

    created = _client().add_test_step("JQA-T1", "10000", StepRequest(step="Submit", result="Logged in"))

    assert created.order_id == 2
    sent = json.loads(route.calls[0].request.content)
    assert sent["testScript"]["type"] == "STEP_BY_STEP"
    assert sent["testScript"]["steps"][1]["description"] == "Submit"


@respx.mock
def test_environments_and_issue_testcases() -> None:
    env_route = respx.get(f"{BASE_URL}{API}/environments").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1, "name": "QA"}]})
    )
    respx.post(f"{BASE_URL}{API}/environment").mock(return_value=httpx.Response(201, json={"id": 2}))
    respx.get(f"{BASE_URL}{API}/issuelink/JQA-1/testcases").mock(return_value=httpx.Response(200, json=[TESTCASE]))
    client = _client()

    assert client.get_environments("JQA")[0]["name"] == "QA"
    assert client.create_environment({"name": "Prod", "projectKey": "JQA"}) == 2
    assert client.get_issue_testcases("JQA-1")[0].key == "JQA-T1"
    assert dict(env_route.calls[0].request.url.params)["projectKey"] == "JQA"


@respx.mock
def test_404_and_401_mapping() -> None:
    respx.get(f"{BASE_URL}{API}/testcase/MISSING").mock(return_value=httpx.Response(404, text="missing"))
    respx.get(f"{BASE_URL}{API}/testcase/NOAUTH").mock(return_value=httpx.Response(401, text="no auth"))
    client = _client()

    try:
        client.get_testcase("MISSING")
    except NotFoundError:
        pass
    else:
        raise AssertionError("Expected NotFoundError")

    try:
        client.get_testcase("NOAUTH")
    except AuthError:
        pass
    else:
        raise AssertionError("Expected AuthError")
