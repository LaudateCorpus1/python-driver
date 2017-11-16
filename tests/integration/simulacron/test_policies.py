# Copyright 2013-2017 DataStax, Inc.
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
try:
    import unittest2 as unittest
except ImportError:
    import unittest  # noqa

from cassandra import OperationTimedOut, WriteTimeout
from cassandra.cluster import Cluster, ExecutionProfile
from cassandra.query import SimpleStatement
from cassandra.policies import ConstantSpeculativeExecutionPolicy, RoundRobinPolicy, RetryPolicy, WriteType

from tests.integration import PROTOCOL_VERSION, greaterthancass21, requiressimulacron, SIMULACRON_JAR
from tests.integration.simulacron.utils import start_and_prime_singledc, prime_query, \
    stop_simulacron, NO_THEN, clear_queries


class BadRoundRobinPolicy(RoundRobinPolicy):

    def make_query_plan(self, working_keyspace=None, query=None):
        pos = self._position
        self._position += 1

        hosts = []
        for _ in range(10):
            hosts.extend(self._live_hosts)

        return hosts


# This doesn't work well with Windows clock granularity
@requiressimulacron
class SpecExecTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if SIMULACRON_JAR is None:
            return

        start_and_prime_singledc()
        cls.cluster = Cluster(protocol_version=PROTOCOL_VERSION, compression=False)
        cls.session = cls.cluster.connect(wait_for_all_pools=True)

        spec_ep_brr = ExecutionProfile(load_balancing_policy=BadRoundRobinPolicy(),
                                       speculative_execution_policy=ConstantSpeculativeExecutionPolicy(1, 6),
                                       request_timeout=12)
        spec_ep_rr = ExecutionProfile(speculative_execution_policy=ConstantSpeculativeExecutionPolicy(.5, 10),
                                      request_timeout=12)
        spec_ep_rr_lim = ExecutionProfile(load_balancing_policy=BadRoundRobinPolicy(),
                                          speculative_execution_policy=ConstantSpeculativeExecutionPolicy(0.5, 1),
                                          request_timeout=12)
        spec_ep_brr_lim = ExecutionProfile(load_balancing_policy=BadRoundRobinPolicy(),
                                           speculative_execution_policy=ConstantSpeculativeExecutionPolicy(4, 10))

        cls.cluster.add_execution_profile("spec_ep_brr", spec_ep_brr)
        cls.cluster.add_execution_profile("spec_ep_rr", spec_ep_rr)
        cls.cluster.add_execution_profile("spec_ep_rr_lim", spec_ep_rr_lim)
        cls.cluster.add_execution_profile("spec_ep_brr_lim", spec_ep_brr_lim)

    @classmethod
    def tearDownClass(cls):
        if SIMULACRON_JAR is None:
            return

        cls.cluster.shutdown()
        stop_simulacron()

    def tearDown(self):
        clear_queries()

    @greaterthancass21
    def test_speculative_execution(self):
        """
        Test to ensure that speculative execution honors LBP, and that they retry appropriately.

        This test will use various LBP, and ConstantSpeculativeExecutionPolicy settings and ensure the proper number of hosts are queried
        @since 3.7.0
        @jira_ticket PYTHON-218
        @expected_result speculative retries should honor max retries, idempotent state of queries, and underlying lbp.

        @test_category metadata
        """
        query_to_prime = "INSERT INTO test3rf.test (k, v) VALUES (0, 1);"
        prime_query(query_to_prime, then={"delay_in_ms": 10000})

        statement = SimpleStatement(query_to_prime, is_idempotent=True)
        statement_non_idem = SimpleStatement(query_to_prime, is_idempotent=False)

        # This LBP should repeat hosts up to around 30
        result = self.session.execute(statement, execution_profile='spec_ep_brr')
        self.assertEqual(7, len(result.response_future.attempted_hosts))

        # This LBP should keep host list to 3
        result = self.session.execute(statement, execution_profile='spec_ep_rr')
        self.assertEqual(3, len(result.response_future.attempted_hosts))
        # Spec_execution policy should limit retries to 1
        result = self.session.execute(statement, execution_profile='spec_ep_rr_lim')

        self.assertEqual(2, len(result.response_future.attempted_hosts))

        # Spec_execution policy should not be used if  the query is not idempotent
        result = self.session.execute(statement_non_idem, execution_profile='spec_ep_brr')
        self.assertEqual(1, len(result.response_future.attempted_hosts))

        # Default policy with non_idem query
        result = self.session.execute(statement_non_idem, timeout=12)
        self.assertEqual(1, len(result.response_future.attempted_hosts))

        # Should be able to run an idempotent query against default execution policy with no speculative_execution_policy
        result = self.session.execute(statement, timeout=12)
        self.assertEqual(1, len(result.response_future.attempted_hosts))

        # Test timeout with spec_ex
        with self.assertRaises(OperationTimedOut):
            self.session.execute(statement, execution_profile='spec_ep_rr', timeout=.5)

        prepared_query_to_prime = "SELECT * FROM test3rf.test where k = ?"
        when = {"params": {"k": "0"}, "param_types": {"k": "ascii"}}
        prime_query(prepared_query_to_prime, when=when, then={"delay_in_ms": 4000})

        # PYTHON-736 Test speculation policy works with a prepared statement
        prepared_statement = self.session.prepare(prepared_query_to_prime)
        # non-idempotent
        result = self.session.execute(prepared_statement, ("0",), execution_profile='spec_ep_brr')
        self.assertEqual(1, len(result.response_future.attempted_hosts))
        # idempotent
        prepared_statement.is_idempotent = True
        result = self.session.execute(prepared_statement, ("0",), execution_profile='spec_ep_brr')
        self.assertLess(1, len(result.response_future.attempted_hosts))

    def test_speculative_and_timeout(self):
        """
        Test to ensure the timeout is honored when using speculative execution
        @since 3.10
        @jira_ticket PYTHON-750
        @expected_result speculative retries be schedule every fixed period, during the maximum
        period of the timeout.

        @test_category metadata
        """
        query_to_prime = "INSERT INTO testkeyspace.testtable (k, v) VALUES (0, 1);"
        prime_query(query_to_prime, then=NO_THEN)

        statement = SimpleStatement(query_to_prime, is_idempotent=True)

        # An OperationTimedOut is placed here in response_future,
        # that's why we can't call session.execute,which would raise it, but
        # we have to directly wait for the event
        response_future = self.session.execute_async(statement, execution_profile='spec_ep_brr_lim',
                                                     timeout=14)
        response_future._event.wait(16)
        self.assertIsInstance(response_future._final_exception, OperationTimedOut)

        # This is because 14 / 4 + 1 = 4
        self.assertEqual(len(response_future.attempted_hosts), 4)


class CustomRetryPolicy(RetryPolicy):
    def on_write_timeout(self, query, consistency, write_type,
                         required_responses, received_responses, retry_num):
        if retry_num != 0:
            return self.RETHROW, None
        elif write_type == WriteType.SIMPLE:
            return self.RETHROW, None
        elif write_type == WriteType.CDC:
            return self.IGNORE, None


@requiressimulacron
class RetryPolicyTets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SIMULACRON_JAR is None:
            return
        start_and_prime_singledc()

        cls.cluster = Cluster(protocol_version=PROTOCOL_VERSION, compression=False,
                              default_retry_policy=CustomRetryPolicy())
        cls.session = cls.cluster.connect(wait_for_all_pools=True)

    @classmethod
    def tearDownClass(cls):
        if SIMULACRON_JAR is None:
            return
        cls.cluster.shutdown()
        stop_simulacron()

    def tearDown(self):
        clear_queries()

    def test_retry_policy_ignores_and_rethrows(self):
        """
        Test to verify :class:`~cassandra.protocol.WriteTimeoutErrorMessage` is decoded correctly and that
        :attr:`.~cassandra.policies.RetryPolicy.RETHROW` and
        :attr:`.~cassandra.policies.RetryPolicy.IGNORE` are respected
        to localhost

        @since 3.12
        @jira_ticket PYTHON-812
        @expected_result the retry policy functions as expected

        @test_category connection
        """
        query_to_prime_simple = "SELECT * from simulacron_keyspace.simple"
        query_to_prime_cdc = "SELECT * from simulacron_keyspace.cdc"
        then = {
            "result": "write_timeout",
            "delay_in_ms": 0,
            "consistency_level": "LOCAL_QUORUM",
            "received": 1,
            "block_for": 2,
            "write_type": "SIMPLE"
          }
        prime_query(query_to_prime_simple, then=then)
        then["write_type"] = "CDC"
        prime_query(query_to_prime_cdc, then=then)

        with self.assertRaises(WriteTimeout):
            self.session.execute(query_to_prime_simple)
        #CDC should be ignored
        self.session.execute(query_to_prime_cdc)