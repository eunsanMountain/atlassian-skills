from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TestStep(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    order_id: int = Field(alias="orderId")
    step: str
    data: str = ""
    result: str = ""
    step_id: int | str | None = Field(default=None, alias="id")

    @classmethod
    def from_script_step(cls, data: dict[str, Any], order_id: int) -> TestStep:
        return cls(
            orderId=data.get("orderId", order_id),
            step=data.get("step") or data.get("description", ""),
            data=data.get("data") or data.get("testData", ""),
            result=data.get("result") or data.get("expectedResult", ""),
            id=data.get("id"),
        )

    def to_script_step(self) -> dict[str, Any]:
        return {
            "description": self.step,
            "testData": self.data,
            "expectedResult": self.result,
        }

    def to_compact_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "order_id": self.order_id,
            "step": self.step,
        }
        if self.data:
            result["data"] = self.data
        if self.result:
            result["result"] = self.result
        if self.step_id is not None:
            result["step_id"] = self.step_id
        return result


class TestStepRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step: str
    data: str | None = None
    result: str | None = None

    def to_step(self, order_id: int) -> TestStep:
        return TestStep(orderId=order_id, step=self.step, data=self.data or "", result=self.result or "")


class ZephyrTestSteps(BaseModel):
    model_config = ConfigDict(extra="ignore")

    issue_id: str
    project_id: str
    steps: list[TestStep] = Field(default_factory=list)

    def to_compact_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "project_id": self.project_id,
            "total_steps": len(self.steps),
            "steps": [step.to_compact_dict() for step in self.steps],
        }


class ZephyrTestCase(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str = ""
    name: str = ""
    project_key: str = Field(default="", alias="projectKey")
    status: str = ""
    priority: str = ""
    component: str | None = None
    owner: str | None = None
    estimated_time: int | None = Field(default=None, alias="estimatedTime")
    folder: str | None = None
    labels: list[str] = Field(default_factory=list)
    objective: str | None = None
    precondition: str | None = None
    test_script: dict[str, Any] | None = Field(default=None, alias="testScript")
    parameters: dict[str, Any] | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")
    issue_links: list[Any] = Field(default_factory=list, alias="issueLinks")
    created_on: str = Field(default="", alias="createdOn")
    last_modified_on: str = Field(default="", alias="lastModifiedOn")
    created_by: str | None = Field(default=None, alias="createdBy")
    last_modified_by: str | None = Field(default=None, alias="lastModifiedBy")

    def to_compact_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "project_key": self.project_key,
            "status": self.status,
            "priority": self.priority,
            "folder": self.folder,
            "labels": self.labels,
        }
        for attr in ("component", "owner", "estimated_time", "objective", "precondition", "created_on"):
            value = getattr(self, attr)
            if value not in (None, "", [], {}):
                result[attr] = value
        return result


class ZephyrTestPlan(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str = ""
    name: str = ""
    project_key: str = Field(default="", alias="projectKey")
    status: str = ""
    folder: str | None = None
    owner: str | None = None
    labels: list[str] = Field(default_factory=list)
    objective: str | None = None
    test_runs: list[dict[str, Any]] = Field(default_factory=list, alias="testRuns")
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")
    issue_links: list[Any] = Field(default_factory=list, alias="issueLinks")
    created_on: str = Field(default="", alias="createdOn")
    last_modified_on: str = Field(default="", alias="lastModifiedOn")
    created_by: str | None = Field(default=None, alias="createdBy")
    last_modified_by: str | None = Field(default=None, alias="lastModifiedBy")

    def to_compact_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "project_key": self.project_key,
            "status": self.status,
            "folder": self.folder,
            "labels": self.labels,
            "test_runs_count": len(self.test_runs),
        }
        if self.owner:
            result["owner"] = self.owner
        if self.objective:
            result["objective"] = self.objective
        return result


class ZephyrTestRun(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str = ""
    name: str = ""
    project_key: str = Field(default="", alias="projectKey")
    status: str = ""
    folder: str | None = None
    owner: str | None = None
    version: str | None = None
    iteration: str | None = None
    environment: str | None = None
    planned_start_date: str | None = Field(default=None, alias="plannedStartDate")
    planned_end_date: str | None = Field(default=None, alias="plannedEndDate")
    actual_start_date: str | None = Field(default=None, alias="actualStartDate")
    actual_end_date: str | None = Field(default=None, alias="actualEndDate")
    test_plan_key: str | None = Field(default=None, alias="testPlanKey")
    issue_key: str | None = Field(default=None, alias="issueKey")
    items: list[dict[str, Any]] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")
    issue_links: list[Any] = Field(default_factory=list, alias="issueLinks")
    created_on: str = Field(default="", alias="createdOn")
    last_modified_on: str = Field(default="", alias="lastModifiedOn")
    created_by: str | None = Field(default=None, alias="createdBy")
    last_modified_by: str | None = Field(default=None, alias="lastModifiedBy")

    def to_compact_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "project_key": self.project_key,
            "status": self.status,
            "folder": self.folder,
            "items_count": len(self.items),
        }
        for attr in ("owner", "version", "iteration", "environment", "test_plan_key", "issue_key"):
            value = getattr(self, attr)
            if value:
                result[attr] = value
        return result


class ZephyrTestResult(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int | None = None
    test_case_key: str = Field(default="", alias="testCaseKey")
    project_key: str = Field(default="", alias="projectKey")
    status: str = ""
    environment: str | None = None
    executed_by: str | None = Field(default=None, alias="executedBy")
    actual_start_date: str | None = Field(default=None, alias="actualStartDate")
    actual_end_date: str | None = Field(default=None, alias="actualEndDate")
    comment: str | None = None
    test_run_key: str | None = Field(default=None, alias="testRunKey")
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")
    steps: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    created_on: str = Field(default="", alias="createdOn")
    last_modified_on: str = Field(default="", alias="lastModifiedOn")

    def to_compact_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "test_case_key": self.test_case_key,
            "project_key": self.project_key,
            "status": self.status,
            "executed_by": self.executed_by,
            "steps_count": len(self.steps),
            "attachments_count": len(self.attachments),
        }
        for attr in ("environment", "comment", "test_run_key"):
            value = getattr(self, attr)
            if value:
                result[attr] = value
        return result
