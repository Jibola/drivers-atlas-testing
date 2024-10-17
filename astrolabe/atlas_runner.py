# Copyright 2020-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import logging
import os
import re
import time as _time
from datetime import timezone
from time import monotonic, sleep

import junitparser
import yaml
from tabulate import tabulate

from astrolabe.commands import (
    delete_project,
    ensure_admin_user,
    ensure_connect_from_anywhere,
    ensure_project,
    get_organization_by_id,
    list_projects_in_org,
)
from astrolabe.exceptions import PollingTimeoutError
from astrolabe.utils import (
    DriverWorkloadSubprocessRunner,
    SingleTestXUnitLogger,
    assert_subset,
    get_cluster_name,
    get_test_name,
    parse_iso8601_time,
)
from atlasclient import AtlasApiError, JSONObject
from atlasclient.exceptions import AtlasClientError

from .timer import Timer
from .utils import mongo_client

LOGGER = logging.getLogger(__name__)


class AtlasTestCase:
    def __init__(
        self,
        *,
        client,
        admin_client,
        test_name,
        cluster_name,
        specification,
        workload,
        configuration,
    ):
        # Initialize.
        self.client = client
        self.admin_client = admin_client
        self.id = test_name
        self.cluster_name = cluster_name
        self.spec = specification
        self.workload = workload
        self.config = configuration
        self.failed = False
        self.expect_failure = specification.get("expectFailure", False)

        # Initialize attribute used for memoization of connection string.
        self.__connection_string = None

        # Initialize wrapper class for running workload executor.
        self.workload_runner = DriverWorkloadSubprocessRunner()

        self.project_name = self.config.project_name
        self.project = None

    @property
    def cluster_url(self):
        return self.client.groups[self.project.id].clusters[self.cluster_name]

    def get_connection_string(self):
        if self.__connection_string is None:
            cluster = self.cluster_url.get().data
            uri = re.sub(
                r"://",
                "://%s:%s@" % (self.config.database_username, self.config.database_password),
                cluster.srvAddress,
            )
            self.__connection_string = uri
        return self.__connection_string

    def __repr__(self):
        return f"<AtlasTestCase: {self.id}>"

    def is_cluster_state(self, goal_state):
        cluster_info = self.cluster_url.get().data
        return cluster_info.stateName.lower() == goal_state.lower()

    def verify_cluster_configuration_matches(self, expected_configuration):
        """Verify that the cluster config is what we expect it to be (based on
        maintenance status). Raises AssertionError."""
        cluster_config = self.cluster_url.get().data
        assert_subset(cluster_config, expected_configuration.clusterConfiguration)
        process_args = self.cluster_url.processArgs.get().data
        assert_subset(process_args, expected_configuration.processArgs)

    def initialize(self, no_create=False):
        """
        Initialize a cluster with the configuration required by the test
        specification.
        """

        if no_create:
            try:
                # If --no-create was specified and the cluster exists, skip
                # initialization. If the cluster does not exist, continue
                # with normal creation.
                self.cluster_url.get().data
                self.verify_cluster_configuration_matches(self.spec.initialConfiguration)
                return
            except AtlasApiError as exc:
                if exc.error_code != "CLUSTER_NOT_FOUND":
                    LOGGER.warning("Cluster was not found, will create one")
            except AssertionError as exc:
                LOGGER.warning("Configuration did not match: %s. Recreating the cluster", exc)

        LOGGER.info("Initializing cluster %r", self.cluster_name)

        cluster_config = self.spec.initialConfiguration.clusterConfiguration.copy()
        cluster_config["name"] = self.cluster_name
        try:
            self.client.groups[self.project.id].clusters.post(**cluster_config)
        except AtlasApiError as exc:
            if exc.error_code == "DUPLICATE_CLUSTER_NAME":
                # Cluster already exists. Simply re-configure it.
                # Cannot send cluster name when updating existing cluster.
                cluster_config.pop("name")
                self.client.groups[self.project.id].clusters[self.cluster_name].patch(
                    **cluster_config
                )
            else:
                raise

        # Apply processArgs if provided.
        process_args = self.spec.initialConfiguration.processArgs
        if process_args:
            self.client.groups[self.project.id].clusters[self.cluster_name].processArgs.patch(
                **process_args
            )

    def run(self, persist_cluster=False, startup_time=1):
        LOGGER.info("Running test %r on cluster %r", self.id, self.cluster_name)

        # Step-1: sanity-check the cluster configuration.
        self.verify_cluster_configuration_matches(self.spec.initialConfiguration)

        # Start the test timer.
        timer = Timer()
        timer.start()

        # Step-2: run driver workload.
        self.workload_runner.spawn(
            workload_executor=self.config.workload_executor,
            connection_string=self.get_connection_string(),
            driver_workload=self.workload,
            startup_time=startup_time,
        )

        try:
            for operation in self.spec.operations:
                if len(operation) != 1:
                    raise ValueError("Operation must have exactly one key: %s" % operation)

                op_name, op_spec = next(iter(operation.items()))

                if op_name == "setClusterConfiguration":
                    # Step-3: begin maintenance routine.
                    final_config = op_spec
                    cluster_config = final_config.clusterConfiguration
                    process_args = final_config.processArgs

                    if not cluster_config and not process_args:
                        raise RuntimeError("invalid maintenance plan")

                    if cluster_config:
                        LOGGER.info("Pushing cluster configuration update")
                        self.cluster_url.patch(**cluster_config)

                    if process_args:
                        LOGGER.info("Pushing process arguments update")
                        self.cluster_url.processArgs.patch(**process_args)

                    # There is sometimes a delay in the status change after
                    # initiating maintenance operations
                    # (https://jira.mongodb.org/browse/PRODTRIAGE-1232). The
                    # duration of the delay is inconsistent, so first wait for
                    # the cluster to have status "updating" before then waiting
                    # for status "idle".
                    self.wait_for_updating()

                    # Step-4: wait until maintenance completes (cluster status
                    # is "idle").
                    self.wait_for_idle()
                    self.verify_cluster_configuration_matches(final_config)
                    LOGGER.info("Cluster maintenance complete")

                elif op_name == "testFailover":
                    timer = Timer()
                    timer.start()
                    timeout = 300

                    # The timestamps returned by the "admin" API are UTC timestamps, so record the
                    # start time in the UTC timezone. All datetimes must be "offset-aware" so they
                    # can be compared.
                    start_time = datetime.datetime.now(timezone.utc)

                    # Account for possible clock drift between our system and
                    # evergreen infrastructure
                    _time.sleep(5)

                    # DRIVERS-1585: failover may fail due to the cluster not being
                    # ready. Retry failover up to a timeout if the
                    # CLUSTER_RESTART_INVALID error is returned from the call
                    while True:
                        try:
                            self.cluster_url["restartPrimaries"].post()
                        except AtlasApiError as exc:
                            if exc.error_code != "CLUSTER_RESTART_INVALID":
                                raise
                        else:
                            break

                        if timer.elapsed > timeout:
                            raise PollingTimeoutError(
                                "Could not test failover as cluster wasn't ready"
                            )
                        sleep(5)

                    self.wait_for_planning(start_time)
                    self.wait_for_idle()

                elif op_name == "sleep":
                    _time.sleep(op_spec)

                elif op_name == "waitForIdle":
                    self.wait_for_idle()

                elif op_name == "restartVms":
                    _ = (
                        self.admin_client.nds.groups[self.project.id]
                        .clusters[self.cluster_name]
                        .reboot.post(api_version="private")
                    )

                    # There is sometimes a delay in the status change after
                    # initiating maintenance operations
                    # (https://jira.mongodb.org/browse/PRODTRIAGE-1232). The
                    # duration of the delay is inconsistent, so first wait for
                    # the cluster to have status "updating" before then waiting
                    # for status "idle".
                    self.wait_for_updating()

                    self.wait_for_idle()

                elif op_name == "assertPrimaryRegion":
                    region = op_spec["region"]

                    cluster_config = self.cluster_url.get().data
                    timer = Timer()
                    timer.start()
                    timeout = op_spec.get("timeout", 90)

                    LOGGER.debug(
                        f"Waiting up to {timeout}s for primary node to be in region '{region}'"
                    )
                    with mongo_client(self.get_connection_string()) as mc:
                        ok = False
                        while timer.elapsed < timeout:
                            rsc = mc.admin.command("replSetGetConfig")
                            members = rsc["config"]["members"]
                            member = next(
                                m
                                for m in members
                                if m["horizons"]["PUBLIC"] == "%s:%s" % mc.primary
                            )
                            member_region = member["tags"]["region"]

                            if region == member_region:
                                ok = True
                                break

                            sleep(5)

                        # If the primary isn't in the target region by the
                        # timeout, log the cluster state every 5 seconds for the
                        # next 30 minutes to help us understand confusing
                        # behavior with the cluster state. After logging the
                        # cluster state for 30 minutes, check if the primary is
                        # in the target region again. If it still isn't in the
                        # target region, raise an exception.
                        #
                        # See https://jira.mongodb.org/browse/PRODTRIAGE-1232
                        # and https://jira.mongodb.org/browse/DRIVERS-2964 for
                        # more context.
                        #
                        # TODO: Figure out a more reliable way to check for
                        # cluster updates, or figure out why cluster state is
                        # unreliable, then remove this extra logging and extra
                        # region check.
                        if not ok:
                            LOGGER.error(
                                f"Primary node ({mc.primary}) not in expected region '{region}' within {timeout}s. (current region: '{member_region}'; all members: {members})"
                            )
                            LOGGER.info("Logging cluster state for 30m after assertPrimaryRegion failure, then checking primary region again.")
                            self.log_cluster_status(timeout=1800)

                            rsc = mc.admin.command("replSetGetConfig")
                            members = rsc["config"]["members"]
                            member = next(
                                m
                                for m in members
                                if m["horizons"]["PUBLIC"] == "%s:%s" % mc.primary
                            )
                            member_region = member["tags"]["region"]

                            if region != member_region:
                                raise Exception(
                                    f"Primary node ({mc.primary}) still not in expected region '{region}' after waiting an extra 30m. (current region: '{member_region}'; all members: {members})"
                                )

                    LOGGER.info(
                        f"Waited for {timer.elapsed}s for primary node to be in region '{region}'"
                    )

                else:
                    raise Exception("Unrecognized operation %s" % op_name)

            # Wait 10 seconds to ensure that the driver is not experiencing any
            # errors after the maintenance has concluded.
            sleep(10)

            # Step-5: interrupt driver workload and capture streams
            stats = self.workload_runner.stop()

            LOGGER.info("Workload Statistics: %s", stats)

            # Stop the timer
            timer.stop()

            # Step-6: compute xunit entry.
            junit_test = junitparser.TestCase(self.id)
            junit_test.time = timer.elapsed

            # Log that this test case expects workload failures.
            if self.expect_failure:
                LOGGER.info("Test case %r expects failures.", self.id)

            # Check for workload failures.
            failure = (
                stats["numErrors"] != 0 or stats["numFailures"] != 0 or stats["numSuccesses"] == 0
            )

            # If there are workload failures and the test case doesn't expect workload failures OR
            # if there are no failures and the test case does expect workload failures, mark the
            # test case as failed.
            if failure != self.expect_failure:
                LOGGER.info(
                    "FAILED: %r; Workload failure: %s; Expect failure: %s",
                    self.id,
                    failure,
                    self.expect_failure,
                )
                self.failed = True
                # Write xunit logs for failed tests.
                junit_test.result = junitparser.Failure(str(stats))
            else:
                LOGGER.info("SUCCEEDED: %r", self.id)
                # Directly log output of successful tests as xunit output
                # is only visible for failed tests.

            # Step 7: download logs asynchronously and delete cluster.
            # TODO: https://github.com/mongodb-labs/drivers-atlas-testing/issues/4
            if not persist_cluster:
                self.cluster_url.delete()
                LOGGER.info("Cluster %r marked for deletion.", self.cluster_name)

            return junit_test
        finally:
            self.workload_runner.terminate()

    def log_cluster_status(self, timeout=1800):
        timer = Timer()
        timer.start()

        LOGGER.info("Cluster %s: logging cluster state for %.1fs", self.cluster_name, timeout)

        while timer.elapsed < timeout:
            sleep(5)

            try:
                cluster_info = self.cluster_url.get().data
            except AtlasClientError as e:
                LOGGER.error("Cluster %s: Error getting cluster status: %e", self.cluster_name, e)
                continue

            LOGGER.info(
                "Cluster %s: state: %s; logged for %.1fs",
                self.cluster_name,
                cluster_info.stateName,
                timer.elapsed,
            )

    def wait_for_state(self, target_state):
        LOGGER.info(
            "Cluster %s: Waiting for cluster to be target state %s",
            self.cluster_name,
            target_state,
        )
        timer = Timer()
        timer.start()
        ok = False
        timeout = self.config.polling_timeout
        last_notified = 0
        while timer.elapsed < timeout:
            sleep(1.0 / self.config.polling_frequency)

            try:
                cluster_info = self.cluster_url.get().data
            except AtlasClientError as e:
                LOGGER.error("Cluster %s: Error getting cluster status: %e", self.cluster_name, e)
                continue

            actual_state = cluster_info.stateName.lower()
            if actual_state == target_state:
                ok = True
                break

            msg = "Cluster %s: current state: %s; target state: %s; waited for %.1fs" % (
                self.cluster_name,
                actual_state,
                target_state,
                timer.elapsed,
            )
            now = monotonic()
            # Notify once a minute, see DRIVERS-2013.
            if now - last_notified >= 60:
                last_notified = now
                LOGGER.info(msg)
            else:
                LOGGER.debug(msg)

        if not ok:
            raise PollingTimeoutError(
                "Cluster %s: Polling timed out after %.1fs; current state: %s, target state: %s"
                % (self.cluster_name, timer.elapsed, actual_state, target_state)
            )

        LOGGER.info(
            "Cluster %s: Reached target state %s after waiting %.1fs",
            self.cluster_name,
            target_state,
            timer.elapsed,
        )

    def wait_for_idle(self):
        self.wait_for_state("idle")

    def wait_for_updating(self):
        self.wait_for_state("updating")

    def wait_for_planning(self, start_time):
        timer = Timer()
        timer.start()
        timeout = self.config.polling_timeout
        ok = False
        LOGGER.info("Waiting for planning for cluster %s", self.cluster_name)
        last_notified = 0
        while timer.elapsed < timeout:
            data = self.admin_client.nds.groups[self.project.id].get(api_version="private").data
            planning_time = parse_iso8601_time(data["lastPlanningDate"])
            if planning_time > start_time:
                ok = True
                break

            msg = "Cluster %s: last planned: %s; wanted after: %s; waited for %.1f sec" % (
                self.cluster_name,
                planning_time,
                start_time,
                timer.elapsed,
            )
            now = monotonic()
            # Notify once a minute, see DRIVERS-2013.
            if now - last_notified >= 60:
                last_notified = now
                LOGGER.info(msg)
            else:
                LOGGER.debug(msg)

            _time.sleep(5)

        if not ok:
            raise PollingTimeoutError("Timed out waiting for planning after %s seconds" % timeout)


class SpecTestRunnerBase:
    """Base class for spec test runners."""

    def __init__(
        self,
        *,
        client,
        admin_client,
        test_locator_token,
        workload_file,
        configuration,
        xunit_output,
        persist_clusters,
        no_create,
        workload_startup_time,
    ):
        self.cases = []
        self.client = client
        self.admin_client = admin_client
        self.config = configuration
        self.xunit_logger = SingleTestXUnitLogger(output_directory=xunit_output)
        self.persist_clusters = persist_clusters
        self.no_create = no_create
        self.workload_startup_time = workload_startup_time
        # Hardcoded to 2 hours, can be configurable in the future
        self.project_expiration_threshold_seconds = 2 * 60 * 60

        # Set up Atlas for tests.
        # Step-1: ensure validity of the organization.
        # Note: organizations can only be created by via the web UI.
        org_id = self.config.organization_id
        LOGGER.info("Verifying organization id: %r", org_id)
        org = get_organization_by_id(client=self.client, org_id=org_id)
        LOGGER.info("Successfully verified organization %r", org.name)

        # Step-2: clean old projects with same name base from organization.
        if not no_create:
            LOGGER.info("Cleaning old projects")
            self.clean_old_projects(org.id)
            LOGGER.info("Successfully old projects")
        else:
            LOGGER.info("Skipping cleaning old projects")

        with open(workload_file) as f:
            workload = JSONObject.from_dict(yaml.safe_load(f))

        for full_path in self.find_spec_tests(test_locator_token):
            # Step-1: load test specification.
            with open(full_path) as spec_file:
                test_spec = JSONObject.from_dict(yaml.safe_load(spec_file))

            # Step-2: generate test name.
            test_name = get_test_name(full_path, workload_file)

            # Step-3: generate unique cluster name.
            cluster_name = get_cluster_name(test_name, self.config.name_salt)

            atlas_test_case = AtlasTestCase(
                client=self.client,
                admin_client=self.admin_client,
                test_name=test_name,
                cluster_name=cluster_name,
                specification=test_spec,
                workload=workload,
                configuration=self.config,
            )
            self.cases.append(atlas_test_case)

            # Set up Atlas for tests.
            # Step-1: check that the project exists or else create it.
            pro_name = atlas_test_case.project_name
            LOGGER.info("Verifying project %r", pro_name)
            project = ensure_project(
                client=self.client, project_name=pro_name, organization_id=org.id
            )
            atlas_test_case.project = project
            LOGGER.info("Successfully verified project %r", pro_name)

            # Step-2: create a user under the project.
            # Note: all test operations will be run as this user.
            uname = self.config.database_username
            LOGGER.info("Verifying user %r", uname)
            ensure_admin_user(
                client=self.client,
                project_id=project.id,
                username=uname,
                password=self.config.database_password,
            )
            LOGGER.info("Successfully verified user %r", uname)

            # Step-3: populate project IP whitelist to allow access from anywhere.
            LOGGER.info("Enabling access from anywhere on project %r", pro_name)
            ensure_connect_from_anywhere(client=self.client, project_id=project.id)
            LOGGER.info("Successfully enabled access from anywhere on project %r", pro_name)

        # Log test plan.
        LOGGER.info(self.get_printable_test_plan())

    @staticmethod
    def find_spec_tests(test_locator_token):
        raise NotImplementedError

    def clean_old_projects(self, org_id):
        current_timestamp = int(_time.time())
        projects_res = list_projects_in_org(client=self.client, org_id=org_id)
        LOGGER.info("looking for %s", self.config.project_base_name)
        for project in projects_res["results"]:
            if project.name.startswith(self.config.project_base_name):
                try:
                    project_timestamp = project.name.split("-")[-2]
                except Exception:
                    project_timestamp = project.name.split("-")[-1]
                if (
                    project_timestamp.isnumeric()
                    and int(project_timestamp)
                    < current_timestamp - self.project_expiration_threshold_seconds
                ):
                    try:
                        LOGGER.info("Deleting project %r, id: %r", project.name, project.id)
                        delete_project(client=self.client, project_id=project.id)
                        LOGGER.info(
                            "Successfully deleted project %r, id: %r", project.name, project.id
                        )
                    except AtlasApiError as esc:
                        # the project may have been deleted by another test just now.
                        if esc.error_code == "GROUP_NOT_FOUND":
                            LOGGER.warning(esc)
                        else:
                            raise
                else:
                    LOGGER.info("Skipping deleting project %r, id: %r", project.name, project.id)
                    LOGGER.info(
                        f"{project_timestamp=}, {current_timestamp=}, {self.project_expiration_threshold_seconds=}"  # noqa: G004
                    )

    def get_printable_test_plan(self):
        table_data = []
        for test_case in self.cases:
            table_data.append([test_case.id, test_case.cluster_name])
        table_txt = "Astrolabe Test Plan\n{}\n"
        return table_txt.format(
            tabulate(table_data, headers=["Test name", "Atlas cluster name"], tablefmt="rst")
        )

    def run(self):
        # Step-0: sentinel flag to track failure/success.
        all_ok = True

        # Step-1: initialize tests clusters
        for case in self.cases:
            case.initialize(no_create=self.no_create)

        # Step-2: run tests round-robin until all have been run.
        remaining_test_cases = self.cases.copy()
        while remaining_test_cases:
            active_case = remaining_test_cases[0]

            # Select a case whose cluster is ready.
            active_case.wait_for_idle()
            LOGGER.info("Test cluster %r is ready", active_case.cluster_name)

            # Run the case.
            xunit_test = active_case.run(
                persist_cluster=self.persist_clusters,
                startup_time=self.workload_startup_time,
            )
            # Write xunit entry for case.
            self.xunit_logger.write_xml(test_case=xunit_test, filename=active_case.id)
            # Remove completed case from list.
            remaining_test_cases.remove(active_case)

            # Update tracker.
            if active_case.failed:
                all_ok = False

            LOGGER.info(
                "Test case %r done; Failed: %s, All OK: %s",
                active_case.id,
                active_case.failed,
                all_ok,
            )

        return not all_ok


class SingleTestRunner(SpecTestRunnerBase):
    """Run the spec test file named ``test_locator_token``."""

    @staticmethod
    def find_spec_tests(test_locator_token):
        """
        Verify that the given file is a spec test file and return its
        absolute path.
        """
        LOGGER.info("Loading spec test from file %r", test_locator_token)
        full_path = os.path.realpath(test_locator_token)
        if os.path.isfile(full_path) and test_locator_token.lower().endswith((".yml", "yaml")):
            yield full_path


class MultiTestRunner(SpecTestRunnerBase):
    """Run all spec test files in the ``test_locator_token`` directory."""

    @staticmethod
    def find_spec_tests(test_locator_token):
        LOGGER.info("Scanning directory %r for spec tests", test_locator_token)
        for root, _, files in os.walk(test_locator_token):
            for file in files:
                full_path = os.path.join(root, file)
                if os.path.isfile(full_path) and file.lower().endswith((".yml", "yaml")):
                    LOGGER.debug("Loading spec test from file %r", full_path)
                    yield full_path
