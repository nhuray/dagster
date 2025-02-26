# pylint: disable=protected-access
import os

import mock
from click.testing import CliRunner
from dagster import DagsterEventType, job, op, reconstructable
from dagster._cli import api
from dagster._cli.api import ExecuteRunArgs, ExecuteStepArgs, verify_step
from dagster._core.execution.plan.state import KnownExecutionState
from dagster._core.execution.retries import RetryState
from dagster._core.execution.stats import RunStepKeyStatsSnapshot
from dagster._core.host_representation import JobHandle
from dagster._core.test_utils import create_run_for_test, environ, instance_for_test
from dagster._serdes import serialize_value

from dagster_tests.api_tests.utils import get_bar_repo_handle, get_foo_job_handle


def runner_execute_run(runner, cli_args):
    result = runner.invoke(api.execute_run_command, cli_args)
    if result.exit_code != 0:
        # CliRunner captures stdout so printing it out here
        raise Exception(
            (
                "dagster runner_execute_run commands with cli_args {cli_args} "
                'returned exit_code {exit_code} with stdout:\n"{stdout}"'
                '\n exception: "\n{exception}"'
                '\n and result as string: "{result}"'
            ).format(
                cli_args=cli_args,
                exit_code=result.exit_code,
                stdout=result.stdout,
                exception=result.exception,
                result=result,
            )
        )
    return result


def test_execute_run():
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_foo_job_handle(instance) as job_handle:
            runner = CliRunner()

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            input_json = serialize_value(
                ExecuteRunArgs(
                    pipeline_origin=job_handle.get_python_origin(),
                    pipeline_run_id=run.run_id,
                    instance_ref=instance.get_ref(),
                )
            )

            result = runner_execute_run(
                runner,
                [input_json],
            )

            assert "PIPELINE_SUCCESS" in result.stdout, "no match, result: {}".format(result.stdout)

            # Framework errors (e.g. running a run that has already run) still result in a non-zero error code
            result = runner.invoke(api.execute_run_command, [input_json])
            assert result.exit_code == 0


@op
def needs_env_var():
    if os.getenv("FOO") != "BAR":
        raise Exception("Missing env var")


@job
def needs_env_var_job():
    needs_env_var()


def test_execute_run_with_secrets_loader():
    recon_job = reconstructable(needs_env_var_job)
    runner = CliRunner()

    # Restore original env after test
    with environ({"FOO": None}):
        with instance_for_test(
            overrides={
                "compute_logs": {
                    "module": "dagster._core.storage.noop_compute_log_manager",
                    "class": "NoOpComputeLogManager",
                },
                "secrets": {
                    "custom": {
                        "module": "dagster._core.test_utils",
                        "class": "TestSecretsLoader",
                        "config": {"env_vars": {"FOO": "BAR"}},
                    }
                },
            }
        ) as instance:
            run = create_run_for_test(
                instance,
                pipeline_name="needs_env_var_job",
                run_id="new_run",
                pipeline_code_origin=recon_job.get_python_origin(),
            )

            input_json = serialize_value(
                ExecuteRunArgs(
                    pipeline_origin=recon_job.get_python_origin(),
                    pipeline_run_id=run.run_id,
                    instance_ref=instance.get_ref(),
                )
            )

            result = runner_execute_run(
                runner,
                [input_json],
            )

            assert "PIPELINE_SUCCESS" in result.stdout, "no match, result: {}".format(result.stdout)

    # Without a secrets loader the run fails due to missing env var
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            },
        }
    ) as instance:
        run = create_run_for_test(
            instance,
            pipeline_name="needs_env_var_job",
            run_id="new_run",
            pipeline_code_origin=recon_job.get_python_origin(),
        )

        input_json = serialize_value(
            ExecuteRunArgs(
                pipeline_origin=recon_job.get_python_origin(),
                pipeline_run_id=run.run_id,
                instance_ref=instance.get_ref(),
            )
        )

        result = runner_execute_run(
            runner,
            [input_json],
        )

        assert (
            "PIPELINE_FAILURE" in result.stdout and "Exception: Missing env var" in result.stdout
        ), "no match, result: {}".format(result.stdout)


def test_execute_run_fail_pipeline():
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_bar_repo_handle(instance) as repo_handle:
            job_handle = JobHandle("fail", repo_handle)
            runner = CliRunner()

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            input_json = serialize_value(
                ExecuteRunArgs(
                    pipeline_origin=job_handle.get_python_origin(),
                    pipeline_run_id=run.run_id,
                    instance_ref=instance.get_ref(),
                )
            )

            result = runner_execute_run(
                runner,
                [input_json],
            )
            assert result.exit_code == 0

            assert "RUN_FAILURE" in result.stdout, "no match, result: {}".format(result)

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run_raise_on_error",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            input_json_raise_on_failure = serialize_value(
                ExecuteRunArgs(
                    pipeline_origin=job_handle.get_python_origin(),
                    pipeline_run_id=run.run_id,
                    instance_ref=instance.get_ref(),
                    set_exit_code_on_failure=True,
                )
            )

            result = runner.invoke(api.execute_run_command, [input_json_raise_on_failure])

            assert result.exit_code != 0, str(result.stdout)

            assert "RUN_FAILURE" in result.stdout, "no match, result: {}".format(result)

            with mock.patch(
                "dagster._core.execution.api.pipeline_execution_iterator"
            ) as _mock_pipeline_execution_iterator:
                _mock_pipeline_execution_iterator.side_effect = Exception("Framework error")

                run = create_run_for_test(
                    instance, pipeline_name="foo", run_id="new_run_framework_error"
                )

                input_json_raise_on_failure = serialize_value(
                    ExecuteRunArgs(
                        pipeline_origin=job_handle.get_python_origin(),
                        pipeline_run_id=run.run_id,
                        instance_ref=instance.get_ref(),
                        set_exit_code_on_failure=True,
                    )
                )

                # Framework errors also result in a non-zero error code
                result = runner.invoke(api.execute_run_command, [input_json_raise_on_failure])
                assert result.exit_code != 0, str(result.stdout)


def test_execute_run_cannot_load():
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_foo_job_handle(instance) as job_handle:
            runner = CliRunner()

            input_json = serialize_value(
                ExecuteRunArgs(
                    pipeline_origin=job_handle.get_python_origin(),
                    pipeline_run_id="FOOBAR",
                    instance_ref=instance.get_ref(),
                )
            )

            result = runner.invoke(
                api.execute_run_command,
                [input_json],
            )

            assert result.exit_code != 0

            assert "Pipeline run with id 'FOOBAR' not found for run execution" in str(
                result.exception
            ), "no match, result: {}".format(result.stdout)


def runner_execute_step(runner, cli_args, env=None):
    result = runner.invoke(api.execute_step_command, cli_args, env=env)
    if result.exit_code != 0:
        # CliRunner captures stdout so printing it out here
        raise Exception(
            (
                "dagster runner_execute_step commands with cli_args {cli_args} "
                'returned exit_code {exit_code} with stdout:\n"{stdout}"'
                '\n exception: "\n{exception}"'
                '\n and result as string: "{result}"'
            ).format(
                cli_args=cli_args,
                exit_code=result.exit_code,
                stdout=result.stdout,
                exception=result.exception,
                result=result,
            )
        )
    return result


def test_execute_step():
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_foo_job_handle(instance) as job_handle:
            runner = CliRunner()

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            args = ExecuteStepArgs(
                pipeline_origin=job_handle.get_python_origin(),
                pipeline_run_id=run.run_id,
                step_keys_to_execute=None,
                instance_ref=instance.get_ref(),
            )

            result = runner_execute_step(
                runner,
                args.get_command_args()[5:],
            )

        assert "STEP_SUCCESS" in result.stdout


def test_execute_step_with_secrets_loader():
    recon_job = reconstructable(needs_env_var_job)
    runner = CliRunner()

    # Restore original env after test
    with environ({"FOO": None}):
        with instance_for_test(
            overrides={
                "compute_logs": {
                    "module": "dagster._core.storage.noop_compute_log_manager",
                    "class": "NoOpComputeLogManager",
                },
                "python_logs": {
                    "dagster_handler_config": {
                        "handlers": {
                            # Importing this handler fails if REQUIRED_LOGGER_ENV_VAR not set
                            "testHandler": {
                                "class": (
                                    "dagster_tests.cli_tests.fake_python_logger_module.FakeHandler"
                                ),
                                "level": "INFO",
                            },
                        }
                    }
                },
                "secrets": {
                    "custom": {
                        "module": "dagster._core.test_utils",
                        "class": "TestSecretsLoader",
                        "config": {
                            "env_vars": {
                                "FOO": "BAR",
                                "REQUIRED_LOGGER_ENV_VAR": "LOGGER_ENV_VAR_VALUE",
                            }
                        },
                    }
                },
            }
        ) as instance:
            run = create_run_for_test(
                instance,
                pipeline_name="needs_env_var_job",
                run_id="new_run",
                pipeline_code_origin=recon_job.get_python_origin(),
            )

            args = ExecuteStepArgs(
                pipeline_origin=recon_job.get_python_origin(),
                pipeline_run_id=run.run_id,
                step_keys_to_execute=None,
                instance_ref=instance.get_ref(),
            )

            result = runner_execute_step(
                runner,
                args.get_command_args()[3:],
            )

            assert "STEP_SUCCESS" in result.stdout


def test_execute_step_with_env():
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_foo_job_handle(instance) as job_handle:
            runner = CliRunner()

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            args = ExecuteStepArgs(
                pipeline_origin=job_handle.get_python_origin(),
                pipeline_run_id=run.run_id,
                step_keys_to_execute=None,
                instance_ref=instance.get_ref(),
            )

            result = runner_execute_step(
                runner,
                args.get_command_args(skip_serialized_namedtuple=True)[5:],
                env={d["name"]: d["value"] for d in args.get_command_env()},
            )

        assert "STEP_SUCCESS" in result.stdout


def test_execute_step_non_compressed():
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_foo_job_handle(instance) as job_handle:
            runner = CliRunner()

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            args = ExecuteStepArgs(
                pipeline_origin=job_handle.get_python_origin(),
                pipeline_run_id=run.run_id,
                step_keys_to_execute=None,
                instance_ref=instance.get_ref(),
            )

            result = runner_execute_step(runner, [serialize_value(args)])

        assert "STEP_SUCCESS" in result.stdout


def test_execute_step_1():
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_foo_job_handle(instance) as job_handle:
            runner = CliRunner()

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            result = runner_execute_step(
                runner,
                ExecuteStepArgs(
                    pipeline_origin=job_handle.get_python_origin(),
                    pipeline_run_id=run.run_id,
                    step_keys_to_execute=None,
                    instance_ref=instance.get_ref(),
                ).get_command_args()[
                    5:
                ],  # the runner doesn't take the `dagster api execute_step` section
            )

        assert "STEP_SUCCESS" in result.stdout


def test_execute_step_verify_step():
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_foo_job_handle(instance) as job_handle:
            runner = CliRunner()

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            # Check that verify succeeds for step that has hasn't been fun (case 3)
            retries = RetryState()
            assert verify_step(instance, run, retries, step_keys_to_execute=["do_something"])

            # Check that verify fails when trying to retry with no original attempt (case 3)
            retries = RetryState()
            retries.mark_attempt("do_something")
            assert not verify_step(instance, run, retries, step_keys_to_execute=["do_something"])

            # Test trying to re-run a retry fails verify_step (case 2)
            with mock.patch("dagster.cli.api.get_step_stats_by_key") as _step_stats_by_key:
                _step_stats_by_key.return_value = {
                    "do_something": RunStepKeyStatsSnapshot(
                        run_id=run.run_id, step_key="do_something", attempts=2
                    )
                }

                retries = RetryState()
                retries.mark_attempt("do_something")
                assert not verify_step(
                    instance, run, retries, step_keys_to_execute=["do_something"]
                )

            runner_execute_step(
                runner,
                ExecuteStepArgs(
                    pipeline_origin=job_handle.get_python_origin(),
                    pipeline_run_id=run.run_id,
                    step_keys_to_execute=None,
                    instance_ref=instance.get_ref(),
                ).get_command_args()[5:],
            )

            # # Check that verify fails for step that has already run (case 1)
            retries = RetryState()
            assert not verify_step(instance, run, retries, step_keys_to_execute=["do_something"])


@mock.patch("dagster.cli.api.verify_step")
def test_execute_step_verify_step_framework_error(mock_verify_step):
    with instance_for_test(
        overrides={
            "compute_logs": {
                "module": "dagster._core.storage.noop_compute_log_manager",
                "class": "NoOpComputeLogManager",
            }
        }
    ) as instance:
        with get_foo_job_handle(instance) as job_handle:
            runner = CliRunner()

            mock_verify_step.side_effect = Exception("Unexpected framework error text")

            run = create_run_for_test(
                instance,
                pipeline_name="foo",
                run_id="new_run",
                pipeline_code_origin=job_handle.get_python_origin(),
            )

            result = runner.invoke(
                api.execute_step_command,
                ExecuteStepArgs(
                    pipeline_origin=job_handle.get_python_origin(),
                    pipeline_run_id=run.run_id,
                    step_keys_to_execute=["fake_step"],
                    instance_ref=instance.get_ref(),
                    should_verify_step=True,
                    known_state=KnownExecutionState(
                        {},
                        {
                            "blah": {"result": ["0", "1", "2"]},
                        },
                    ),
                ).get_command_args()[5:],
            )

            assert result.exit_code != 0

            # Framework error logged to event log
            logs = instance.all_logs(run.run_id, of_type=DagsterEventType.ENGINE_EVENT)

            log_entry = logs[0]
            assert (
                log_entry.message
                == "An exception was thrown during step execution that is likely a framework error,"
                " rather than an error in user code."
            )
            assert log_entry.step_key == "fake_step"

            assert "Unexpected framework error text" in str(
                log_entry.dagster_event.event_specific_data.error
            )
