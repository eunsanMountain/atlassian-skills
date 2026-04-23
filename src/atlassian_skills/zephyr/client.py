from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.core.errors import NotFoundError
from atlassian_skills.zephyr.models import (
    TestStep,
    TestStepRequest,
    ZephyrTestCase,
    ZephyrTestPlan,
    ZephyrTestResult,
    ZephyrTestRun,
    ZephyrTestSteps,
)

T = TypeVar("T", bound=BaseModel)


class ZephyrClient(BaseClient):
    """Zephyr Scale Server/DC REST API client.

    API base: /rest/atm/1.0
    """

    API = "/rest/atm/1.0"

    def __init__(
        self,
        base_url: str,
        credential: Credential,
        timeout: float = 30.0,
        verify: str | bool = True,
    ) -> None:
        super().__init__(base_url, credential, timeout, verify=verify)

    def _api(self, path: str) -> str:
        return f"{self.API}/{path.lstrip('/')}"

    def _search(
        self,
        path: str,
        model: type[T],
        *,
        query: str | None = None,
        fields: str | None = None,
        start_at: int = 0,
        max_results: int = 200,
    ) -> list[T]:
        params: dict[str, Any] = {}
        if query:
            params["query"] = query
        if fields:
            params["fields"] = fields
        if start_at > 0:
            params["startAt"] = start_at
        if max_results != 200:
            params["maxResults"] = max_results
        data = self.get(self._api(path), params=params).json()
        items = data if isinstance(data, list) else data.get("results", [])
        return [model.model_validate(item) for item in items]

    # ------------------------------------------------------------------
    # Test cases
    # ------------------------------------------------------------------

    def get_testcase(self, key: str, *, fields: str | None = None) -> ZephyrTestCase:
        params = {"fields": fields} if fields else None
        return ZephyrTestCase.model_validate(self.get(self._api(f"testcase/{key}"), params=params).json())

    def create_testcase(self, data: dict[str, Any]) -> str:
        result = self.post(self._api("testcase"), json=data).json()
        return str(result.get("key", ""))

    def update_testcase(self, key: str, data: dict[str, Any]) -> None:
        self.put(self._api(f"testcase/{key}"), json=data)

    def delete_testcase(self, key: str) -> None:
        self.delete(self._api(f"testcase/{key}"))

    def search_testcases(
        self,
        *,
        query: str | None = None,
        fields: str | None = None,
        start_at: int = 0,
        max_results: int = 200,
    ) -> list[ZephyrTestCase]:
        return self._search(
            "testcase/search",
            ZephyrTestCase,
            query=query,
            fields=fields,
            start_at=start_at,
            max_results=max_results,
        )

    # ------------------------------------------------------------------
    # Test plans
    # ------------------------------------------------------------------

    def get_testplan(self, key: str, *, fields: str | None = None) -> ZephyrTestPlan:
        params = {"fields": fields} if fields else None
        return ZephyrTestPlan.model_validate(self.get(self._api(f"testplan/{key}"), params=params).json())

    def create_testplan(self, data: dict[str, Any]) -> str:
        result = self.post(self._api("testplan"), json=data).json()
        return str(result.get("key", ""))

    def search_testplans(
        self,
        *,
        query: str | None = None,
        fields: str | None = None,
        start_at: int = 0,
        max_results: int = 200,
    ) -> list[ZephyrTestPlan]:
        return self._search(
            "testplan/search",
            ZephyrTestPlan,
            query=query,
            fields=fields,
            start_at=start_at,
            max_results=max_results,
        )

    # ------------------------------------------------------------------
    # Test runs
    # ------------------------------------------------------------------

    def get_testrun(self, key: str, *, fields: str | None = None) -> ZephyrTestRun:
        params = {"fields": fields} if fields else None
        return ZephyrTestRun.model_validate(self.get(self._api(f"testrun/{key}"), params=params).json())

    def create_testrun(self, data: dict[str, Any]) -> str:
        result = self.post(self._api("testrun"), json=data).json()
        return str(result.get("key", ""))

    def search_testruns(
        self,
        *,
        query: str | None = None,
        fields: str | None = None,
        start_at: int = 0,
        max_results: int = 200,
    ) -> list[ZephyrTestRun]:
        return self._search(
            "testrun/search",
            ZephyrTestRun,
            query=query,
            fields=fields,
            start_at=start_at,
            max_results=max_results,
        )

    # ------------------------------------------------------------------
    # Test results
    # ------------------------------------------------------------------

    def create_testresult(self, data: dict[str, Any]) -> int:
        result = self.post(self._api("testresult"), json=data).json()
        return int(result.get("id", 0) or 0)

    def get_testcase_latest_result(self, test_case_key: str) -> ZephyrTestResult | None:
        try:
            data = self.get(self._api(f"testcase/{test_case_key}/testresult/latest")).json()
        except NotFoundError:
            return None
        return ZephyrTestResult.model_validate(data)

    def get_testrun_results(self, test_run_key: str) -> list[ZephyrTestResult]:
        data = self.get(self._api(f"testrun/{test_run_key}/testresults")).json()
        items = data if isinstance(data, list) else data.get("results", [])
        return [ZephyrTestResult.model_validate(item) for item in items]

    def create_testrun_result(
        self,
        test_run_key: str,
        test_case_key: str,
        data: dict[str, Any],
        *,
        environment: str | None = None,
        user_key: str | None = None,
    ) -> int:
        params: dict[str, Any] = {}
        if environment or user_key:
            if environment:
                params["environment"] = environment
            if user_key:
                params["userKey"] = user_key
        result = self.request(
            "POST",
            self._api(f"testrun/{test_run_key}/testcase/{test_case_key}/testresult"),
            params=params or None,
            json=data,
        )
        payload = result.json()
        return int(payload.get("id", 0) or 0)

    def update_testrun_result(
        self,
        test_run_key: str,
        test_case_key: str,
        data: dict[str, Any],
        *,
        environment: str | None = None,
        user_key: str | None = None,
    ) -> int:
        params: dict[str, Any] = {}
        if environment:
            params["environment"] = environment
        if user_key:
            params["userKey"] = user_key
        payload = self.request(
            "PUT",
            self._api(f"testrun/{test_run_key}/testcase/{test_case_key}/testresult"),
            params=params or None,
            json=data,
        ).json()
        return int(payload.get("id", 0) or 0)

    def create_bulk_testrun_results(
        self,
        test_run_key: str,
        data: list[dict[str, Any]],
        *,
        environment: str | None = None,
        user_key: str | None = None,
    ) -> list[int]:
        params: dict[str, Any] = {}
        if environment:
            params["environment"] = environment
        if user_key:
            params["userKey"] = user_key
        payload = self.request(
            "POST",
            self._api(f"testrun/{test_run_key}/testresults"),
            params=params or None,
            json=data,
        ).json()
        return [int(item) for item in payload.get("ids", [])]

    # ------------------------------------------------------------------
    # Test steps
    # ------------------------------------------------------------------

    def get_test_steps(self, issue_id: str, project_id: str) -> ZephyrTestSteps:
        try:
            data = self.get(self._api(f"testcase/{issue_id}"), params={"fields": "key,name,testScript"}).json()
        except NotFoundError:
            return ZephyrTestSteps(issue_id=issue_id, project_id=project_id, steps=[])
        test_script = data.get("testScript", {}) or {}
        steps: list[TestStep] = []
        if test_script.get("type") == "STEP_BY_STEP":
            for index, step_data in enumerate(test_script.get("steps", []), start=1):
                steps.append(TestStep.from_script_step(step_data, index))
        elif test_script.get("type") == "PLAIN_TEXT" and test_script.get("text"):
            steps.append(TestStep(orderId=1, step=str(test_script.get("text")), data="", result=""))
        return ZephyrTestSteps(issue_id=issue_id, project_id=project_id, steps=steps)

    def add_test_step(self, issue_id: str, project_id: str, step_request: TestStepRequest) -> TestStep:
        return self.add_multiple_test_steps(issue_id, project_id, [step_request])[0]

    def add_multiple_test_steps(
        self,
        issue_id: str,
        project_id: str,
        step_requests: list[TestStepRequest],
    ) -> list[TestStep]:
        current = self.get_test_steps(issue_id, project_id)
        next_order = len(current.steps) + 1
        new_steps = [step.to_step(next_order + index) for index, step in enumerate(step_requests)]
        script_steps = [step.to_script_step() for step in [*current.steps, *new_steps]]
        payload = {"testScript": {"type": "STEP_BY_STEP", "steps": script_steps}}
        self.put(self._api(f"testcase/{issue_id}"), json=payload)
        return new_steps

    # ------------------------------------------------------------------
    # Environments and issue links
    # ------------------------------------------------------------------

    def get_environments(self, project_key: str) -> list[dict[str, Any]]:
        data = self.get(self._api("environments"), params={"projectKey": project_key}).json()
        if isinstance(data, list):
            return data
        return list(data.get("results", data.get("values", [])))

    def create_environment(self, data: dict[str, Any]) -> int | str:
        result = self.post(self._api("environment"), json=data).json()
        value = result.get("id", result.get("key", ""))
        return value if isinstance(value, int) else str(value)

    def get_issue_testcases(self, issue_key: str, *, fields: str | None = None) -> list[ZephyrTestCase]:
        params = {"fields": fields} if fields else None
        data = self.get(self._api(f"issuelink/{issue_key}/testcases"), params=params).json()
        items = data if isinstance(data, list) else data.get("results", [])
        return [ZephyrTestCase.model_validate(item) for item in items]
