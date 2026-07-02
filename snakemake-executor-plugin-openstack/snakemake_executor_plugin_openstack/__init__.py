from dataclasses import dataclass, field
from typing import List, Generator, Optional
from snakemake_interface_executor_plugins.executors.base import SubmittedJobInfo
from snakemake_interface_executor_plugins.executors.remote import RemoteExecutor
from snakemake_interface_executor_plugins.settings import (
    ExecutorSettingsBase,
    CommonSettings,
)
from snakemake_interface_executor_plugins.workflow import WorkflowExecutorInterface
from snakemake_interface_executor_plugins.logging import LoggerExecutorInterface
from snakemake_interface_executor_plugins.jobs import (
    JobExecutorInterface,
)
from snakemake_interface_common.exceptions import WorkflowError

from os import environ as env
from zunclient import client as zunclient
import uuid


# Optional:
# Define additional settings for your executor.
# They will occur in the Snakemake CLI as --<executor-name>-<param-name>
# Omit this class if you don't need any.
# Make sure that all defined fields are Optional and specify a default value
# of None or anything else that makes sense in your case.
@dataclass
class ExecutorSettings(ExecutorSettingsBase):
    default_volume_size: Optional[int] = field(
        default=10,
        metadata={
            "help": "Set the default size in GB for cinder volumes created for OpenStack containers. Default: 10"
        },
    )


# Required:
# Specify common settings shared by various executors.
common_settings = CommonSettings(
    # define whether your executor plugin executes locally
    # or remotely. In virtually all cases, it will be remote execution
    # (cluster, cloud, etc.). Only Snakemake's standard execution
    # plugins (snakemake-executor-plugin-dryrun, snakemake-executor-plugin-local)
    # are expected to specify False here.
    non_local_exec=True,
    # Whether the executor implies to not have a shared file system
    implies_no_shared_fs=True,
    # whether to deploy workflow sources to default storage provider before execution
    job_deploy_sources=True,
    # whether arguments for setting the storage provider shall be passed to jobs
    pass_default_storage_provider_args=True,
    # whether arguments for setting default resources shall be passed to jobs
    pass_default_resources_args=True,
    # whether environment variables shall be passed to jobs (if False, use
    # self.envvars() to obtain a dict of environment variables and their values
    # and pass them e.g. as secrets to the execution backend)
    pass_envvar_declarations_to_cmd=False,
    # whether the default storage provider shall be deployed before the job is run on
    # the remote node. Usually set to True if the executor does not assume a shared fs
    auto_deploy_default_storage_provider=True,
    # specify initial amount of seconds to sleep before checking for job status
    init_seconds_before_status_checks=0,
)


# Required:
# Implementation of your executor
class Executor(RemoteExecutor):
    def __post_init__(self):
        # access workflow
        # self.workflow
        # access executor specific settings
        # self.workflow.executor_settings

        # IMPORTANT: in your plugin, only access methods and properties of
        # Snakemake objects (like Workflow, Persistence, etc.) that are
        # defined in the interfaces found in the
        # snakemake-interface-executor-plugins and the
        # snakemake-interface-common package.
        # Other parts of those objects are NOT guaranteed to remain
        # stable across new releases.

        # To ensure that the used interfaces are not changing, you should
        # depend on these packages as >=a.b.c,<d with d=a+1 (i.e. pin the
        # dependency on this package to be at least the version at time
        # of development and less than the next major version which would
        # introduce breaking changes).

        # In case of errors outside of jobs, please raise a WorkflowError

        self.zun = zunclient.Client(
            "1.40",
            auth_url=env["OS_AUTH_URL"],
            username=env["OS_USERNAME"],
            password=env["OS_PASSWORD"],
            project_name=env["OS_PROJECT_NAME"],
            user_domain_name=env["OS_USER_DOMAIN_NAME"],
            project_domain_id=env["OS_PROJECT_DOMAIN_ID"],
        )

        self.container_image = self.workflow.remote_execution_settings.container_image
        self.logger.info(f"Using {self.container_image} as image for Zun containers.")

    def run_job(self, job: JobExecutorInterface):
        # Implement here how to run a job.
        # You can access the job's resources, etc.
        # via the job object.
        # After submitting the job, you have to call
        # self.report_job_submission(job_info).
        # with job_info being of type
        # snakemake_interface_executor_plugins.executors.base.SubmittedJobInfo.
        # If required, make sure to pass the job's id to the job_info object, as keyword
        # argument 'external_job_id'.

        exec_job = self.format_job_exec(job)
        self.logger.info(f"Executing job: {exec_job}")

        try:
            result = self.zun.containers.run(
                name=f"snakejob-{job.jobid}-{job.attempt}",
                labels={"app": "snakemake"},
                image=self.container_image,
                command=["/bin/sh", "-c", exec_job],
                environment=self.envvars()
                | {"GOOGLE_APPLICATION_CREDENTIALS": "/root/service-account-key.json"},
                workdir="/workdir",
                mounts=[
                    {
                        "destination": "/workdir",
                        "size": str(
                            self.workflow.executor_settings.default_volume_size
                        ),
                    },
                    {
                        "type": "bind",
                        "source": env["GCS_SERVICE_ACCOUNT_KEY"],
                        "destination": "/root/service-account-key.json",
                    },
                ],
            )
        except Exception as e:
            raise WorkflowError(e)
        self.report_job_submission(
            SubmittedJobInfo(job=job, external_jobid=result.uuid)
        )

    async def check_active_jobs(
        self, active_jobs: List[SubmittedJobInfo]
    ) -> Generator[SubmittedJobInfo, None, None]:
        # Check the status of active jobs.

        # You have to iterate over the given list active_jobs.
        # If you provided it above, each will have its external_jobid set according
        # to the information you provided at submission time.
        # For jobs that have finished successfully, you have to call
        # self.report_job_success(active_job).
        # For jobs that have errored, you have to call
        # self.report_job_error(active_job).
        # This will also take care of providing a proper error message.
        # Usually there is no need to perform additional logging here.
        # Jobs that are still running have to be yielded.
        #
        # For queries to the remote middleware, please use
        # self.status_rate_limiter like this:
        #
        # async with self.status_rate_limiter:
        #    # query remote middleware here
        #
        # To modify the time until the next call of this method,
        # you can set self.next_sleep_seconds here.
        self.logger.info(f"Checking status of {len(active_jobs)} jobs")

        for j in active_jobs:
            async with self.status_rate_limiter:
                try:
                    result = self.zun.containers.get(j.external_jobid)
                except Exception as e:
                    raise WorkflowError(e)
                if result.status == "Stopped":
                    if result.status_detail.startswith("Exited(0)"):
                        self.report_job_success(j)

                        # delete container
                        try:
                            self.zun.containers.delete(j.external_jobid)
                        except Exception as e:
                            raise WorkflowError(e)

                    # Exit code unequal 0
                    else:
                        msg = (
                            f"For details, please issue:\nzun show {j.external_jobid}\n"
                        )
                        self.report_job_error(j, msg=msg)
                else:
                    # still active
                    yield j

    def cancel_jobs(self, active_jobs: List[SubmittedJobInfo]):
        # Cancel all active jobs.
        # This method is called when Snakemake is interrupted.
        for j in active_jobs:
            try:
                self.zun.containers.delete(j.external_jobid)
            except Exception as e:
                raise WorkflowError(e)
