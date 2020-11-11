# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from typing import Tuple

import requests
from ogr.abstract import CommitStatus, GitProject
from ogr.utils import RequestResponse
from packit.config.job_config import JobConfig
from packit.config.package_config import PackageConfig
from packit.exceptions import PackitConfigException

from packit_service.config import ServiceConfig
from packit_service.models import CoprBuildModel, TFTTestRunModel, TestingFarmResult
from packit_service.sentry_integration import send_to_sentry
from packit_service.service.events import EventData
from packit_service.worker.build import CoprBuildJobHelper
from packit_service.worker.result import TaskResults

logger = logging.getLogger(__name__)


class TestingFarmJobHelper(CoprBuildJobHelper):
    def __init__(
        self,
        service_config: ServiceConfig,
        package_config: PackageConfig,
        project: GitProject,
        metadata: EventData,
        db_trigger,
        job_config: JobConfig,
    ):
        super().__init__(
            service_config=service_config,
            package_config=package_config,
            project=project,
            metadata=metadata,
            db_trigger=db_trigger,
            job_config=job_config,
        )

        self.session = requests.session()
        adapter = requests.adapters.HTTPAdapter(max_retries=5)
        self.insecure = False
        self.session.mount("https://", adapter)
        self.tft_api_url: str = self.service_config.testing_farm_api_url
        if not self.tft_api_url.endswith("/"):
            self.tft_api_url += "/"

    def _payload(self, build_id: int, chroot: str) -> dict:
        """
        Testing Farm API: https://testing-farm.gitlab.io/api/
        """
        compose, arch = self.get_compose_arch(chroot)
        return {
            "api_key": self.service_config.testing_farm_secret,
            "test": {
                "fmf": {
                    "url": self.metadata.project_url,
                    "ref": self.metadata.commit_sha,
                },
            },
            "environments": [
                {
                    "arch": arch,
                    "os": {"compose": compose},
                    "artifacts": [
                        {
                            "id": f"{build_id}:{chroot}",
                            "type": "fedora-copr-build",
                        }
                    ],
                }
            ],
            "notification": {
                "webhook":
                    {
                        "url": f"{self.api_url}/testing-farm/results",
                    }
            }
        }

    def get_compose_arch(self, chroot) -> Tuple[str, str]:
        # fedora-33-x86_64 -> Fedora-33, x86_64
        compose, arch = chroot.rsplit("-", 1)
        compose = compose.title()

        response = self.send_testing_farm_request(f"{self.tft_api_url}composes")
        if response.status_code == 200:
            # {'composes': [{'name': 'Fedora-33'}, {'name': 'Fedora-Rawhide'}]}
            composes = [c["name"] for c in response.json()["composes"]]
            if compose not in composes:
                logger.error(f"Can't map {compose} (from {chroot}) to {composes}")

        return compose, arch

    def report_missing_build_chroot(self, chroot: str):
        self.report_status_to_test_for_chroot(
            state=CommitStatus.error,
            description=f"No build defined for the target '{chroot}'.",
            chroot=chroot,
        )

    def run_testing_farm_on_all(self):
        copr_builds = CoprBuildModel.get_all_by_owner_and_project(
            owner=self.job_owner, project_name=self.job_project
        )
        if not copr_builds:
            return TaskResults(
                success=False,
                details={
                    "msg": f"No copr builds for {self.job_owner}/{self.job_project}"
                },
            )
        latest_copr_build_id = int(list(copr_builds)[0].build_id)

        failed = {}
        for chroot in self.tests_targets:
            result = self.run_testing_farm(build_id=latest_copr_build_id, chroot=chroot)
            if not result["success"]:
                failed[chroot] = result.get("details")

        if not failed:
            return TaskResults(success=True, details={})

        return TaskResults(
            success=False,
            details={"msg": f"Failed testing farm targets: '{failed.keys()}'."}.update(
                failed
            ),
        )

    def run_testing_farm(self, build_id: int, chroot: str) -> TaskResults:
        if chroot not in self.tests_targets:
            # Leaving here just to be sure that we will discover this situation if it occurs.
            # Currently not possible to trigger this situation.
            msg = f"Target '{chroot}' not defined for tests but triggered."
            logger.error(msg)
            send_to_sentry(PackitConfigException(msg))
            return TaskResults(
                success=False,
                details={"msg": msg},
            )

        if chroot not in self.build_targets:
            self.report_missing_build_chroot(chroot)
            return TaskResults(
                success=False,
                details={
                    "msg": f"Target '{chroot}' not defined for build. "
                    "Cannot run tests without build."
                },
            )

        self.report_status_to_test_for_chroot(
            state=CommitStatus.pending,
            description="Build succeeded. Submitting the tests ...",
            chroot=chroot,
        )

        logger.info("Sending testing farm request...")
        payload = self._payload(build_id, chroot)
        url = f"{self.tft_api_url}requests"
        logger.debug(f"POSTing {payload} to {url}")
        req = self.send_testing_farm_request(
            url=url,
            method="POST",
            data=payload,
        )
        logger.debug(f"Request sent: {req}")

        if not req:
            msg = "Failed to post request to testing farm API."
            logger.debug("Failed to post request to testing farm API.")
            self.report_status_to_test_for_chroot(
                state=CommitStatus.error,
                description=msg,
                chroot=chroot,
            )
            return TaskResults(success=False, details={"msg": msg})

        # success set check on pending
        if req.status_code != 200:
            # something went wrong
            if req.json() and "message" in req.json():
                msg = req.json()["message"]
            else:
                msg = f"Failed to submit tests: {req.reason}"
                logger.error(msg)
            self.report_status_to_test_for_chroot(
                state=CommitStatus.failure,
                description=msg,
                chroot=chroot,
            )
            return TaskResults(success=False, details={"msg": msg})

        # Response: {"id": "9fa3cbd1-83f2-4326-a118-aad59f5", ...}

        pipeline_id = req.json()["id"]
        logger.debug(
            f"Submitted ({req.status_code}) to testing farm as request {pipeline_id}"
        )

        TFTTestRunModel.create(
            pipeline_id=pipeline_id,
            commit_sha=self.metadata.commit_sha,
            status=TestingFarmResult.new,
            target=chroot,
            web_url=None,
            trigger_model=self.db_trigger,
        )

        self.report_status_to_test_for_chroot(
            state=CommitStatus.pending,
            description="Tests have been submitted ...",
            url=f"{self.tft_api_url}requests/{pipeline_id}",
            chroot=chroot,
        )

        return TaskResults(success=True, details={})

    def send_testing_farm_request(
        self, url: str, method: str = None, params: dict = None, data=None
    ):
        method = method or "GET"
        try:
            response = self.get_raw_request(
                method=method, url=url, params=params, data=data
            )
        except requests.exceptions.ConnectionError as er:
            logger.error(er)
            raise Exception(f"Cannot connect to url: `{url}`.", er)
        return response

    def get_raw_request(
        self,
        url,
        method="GET",
        params=None,
        data=None,
    ) -> RequestResponse:

        response = self.session.request(
            method=method,
            url=url,
            params=params,
            json=data,
            verify=not self.insecure,
        )

        try:
            json_output = response.json()
        except ValueError:
            logger.debug(response.text)
            json_output = None

        return RequestResponse(
            status_code=response.status_code,
            ok=response.ok,
            content=response.content,
            json=json_output,
            reason=response.reason,
        )
