from dataclasses import dataclass, field
from typing import List, Generator, Optional, AsyncGenerator
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
from snakemake_executor_plugin_kubernetes import (
    Executor as KubernetesExecutor,
    ExecutorSettings as KubernetesExecutorSettings,
)

from snakemake_executor_plugin_openstack import (
    Executor as OpenstackExecutor,
    ExecutorSettings as OpenstackExecutorSettings,
)


# Optional:
# Define additional settings for your executor.
# They will occur in the Snakemake CLI as --<executor-name>-<param-name>
# Omit this class if you don't need any.
# Make sure that all defined fields are Optional and specify a default value
# of None or anything else that makes sense in your case.
@dataclass
class ExecutorSettings(KubernetesExecutorSettings):
    primary_comp_env: Optional[str] = field(
        default="kubernetes:current-context",
        metadata={
            "help": "Select the primary compute environment to use. This is where jobs not to be offloaded "
            "will be executed. Define like this: <env-type>:<context>. env-type can be one of "
            "{'kubernetes', 'openstack'}. context is the name of the Kubernetes context in your kubeconfig. "
            "Set to 'current-context' to use the current context. "
            "If no secondary-comp-env is provided, offloading will be inactive. "
            "If neither primary-comp-env nor secondary-comp-env are provided, offloading will be inactive and "
            "kubernetes with the currently active context will be used as default. "
        },
    )
    secondary_comp_env: Optional[str] = field(
        default=None,
        metadata={
            "help": "Select the secondery compute environment to use. This is where jobs to be offloaded "
            "will be executed. Define like this: <env-type>:<context>. env-type can be one of "
            "{'kubernetes', 'openstack'}. context is the name of the Kubernetes context in your kubeconfig. "
            "Set to 'current-context' to use the current context. "
            "If only primary-comp-env is provided, offloading will be inactive. "
            "If neither primary-comp-env nor secondary-comp-env are provided, offloading will be inactive and "
            "kubernetes with the currently active context will be used as the compute environment. "
        },
    )
    jobs: Optional[str] = field(
        default=None,
        metadata={
            "help": "Jobs to be offloaded. Specify as comma-separated list of job ids (e.g. '1,2,3')."
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
    # NFS / shared-filesystem mode: head and remote workers all see the
    # same working tree, so we do not imply no-shared-fs and we do not
    # require FTP / default-storage deployment.
    implies_no_shared_fs=False,
    # whether to deploy workflow sources to default storage provider before execution
    job_deploy_sources=False,
    # whether arguments for setting the storage provider shall be passed to jobs
    pass_default_storage_provider_args=False,
    # whether arguments for setting default resources shall be passed to jobs
    pass_default_resources_args=True,
    # whether environment variables shall be passed to jobs (if False, use
    # self.envvars() to obtain a dict of environment variables and their values
    # and pass them e.g. as secrets to the execution backend)
    pass_envvar_declarations_to_cmd=True,
    # whether the default storage provider shall be deployed before the job is run on
    # the remote node. Usually set to True if the executor does not assume a shared fs
    auto_deploy_default_storage_provider=False,
    # specify initial amount of seconds to sleep before checking for job status
    init_seconds_before_status_checks=0,
)


# Required:
# Implementation of your executor
class Executor(RemoteExecutor):
    def __post_init__(self):
        self.primary_comp_env = None
        self.secondary_comp_env = None
        self.primary_comp_env, self.secondary_comp_env = self._parse_comp_envs()
        self.active_job_ids_primary_env = set()
        self.active_job_ids_secondary_env = set()
        self.jobs_to_offload = self.workflow.executor_settings.jobs
        if self.jobs_to_offload:
            self.logger.info(
                f"The following jobs were marked for offloading: {[int(j) for j in self.jobs_to_offload.split(',')]}"
            )
        else:
            self.logger.info("No jobs were marked for offloading")

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
        if self.secondary_comp_env and self._is_job_offloaded(job):
            self.secondary_comp_env.run_job(job)
            self.active_job_ids_secondary_env.add(job.jobid)
        else:
            self.primary_comp_env.run_job(job)
            self.active_job_ids_primary_env.add(job.jobid)

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
        primary_comp_env = getattr(self, "primary_comp_env", None)
        if primary_comp_env is None:
            for jobinfo in active_jobs:
                yield jobinfo
            return

        active_jobs_primary_env = [
            jobinfo
            for jobinfo in active_jobs
            if jobinfo.job.jobid in self.active_job_ids_primary_env
        ]
        yielded_job_ids_primary_env = set()

        async for jobinfo in primary_comp_env.check_active_jobs(
            active_jobs_primary_env
        ):
            yielded_job_ids_primary_env.add(jobinfo.job.jobid)
            yield jobinfo
        # Remove jobs that were not yielded, i.e. have finished or errored
        self.active_job_ids_primary_env -= {
            jobinfo.job.jobid
            for jobinfo in active_jobs_primary_env
            if jobinfo.job.jobid not in yielded_job_ids_primary_env
        }

        if self.secondary_comp_env:
            active_jobs_secondary_env = [
                jobinfo
                for jobinfo in active_jobs
                if jobinfo.job.jobid in self.active_job_ids_secondary_env
            ]
            yielded_job_ids_secondary_env = set()

            async for jobinfo in self.secondary_comp_env.check_active_jobs(
                active_jobs_secondary_env
            ):
                yielded_job_ids_secondary_env.add(jobinfo.job.jobid)
                yield jobinfo

            self.active_job_ids_secondary_env -= {
                jobinfo.job.jobid
                for jobinfo in active_jobs_secondary_env
                if jobinfo.job.jobid not in yielded_job_ids_secondary_env
            }

    def cancel_jobs(self, active_jobs: List[SubmittedJobInfo]):
        # Cancel all active jobs.
        # This method is called when Snakemake is interrupted.
        active_jobs_primary_env = [
            jobinfo
            for jobinfo in active_jobs
            if jobinfo.job.jobid in self.active_job_ids_primary_env
        ]
        primary_comp_env = getattr(self, "primary_comp_env", None)
        if primary_comp_env:
            primary_comp_env.cancel_jobs(active_jobs_primary_env)

        secondary_comp_env = getattr(self, "secondary_comp_env", None)
        if secondary_comp_env:
            active_jobs_secondary_env = [
                jobinfo
                for jobinfo in active_jobs
                if jobinfo.job.jobid in self.active_job_ids_secondary_env
            ]
            secondary_comp_env.cancel_jobs(active_jobs_secondary_env)

    def shutdown(self):
        primary_comp_env = getattr(self, "primary_comp_env", None)
        if primary_comp_env:
            primary_comp_env.shutdown()
        secondary_comp_env = getattr(self, "secondary_comp_env", None)
        if secondary_comp_env:
            secondary_comp_env.shutdown()

    def _parse_comp_envs(self) -> (RemoteExecutor, RemoteExecutor):
        primary_env_type, primary_context = (
            self.workflow.executor_settings.primary_comp_env.split(":")
        )
        if primary_env_type == "kubernetes":
            if primary_context != "current-context":
                self.workflow.executor_settings.context = primary_context
            else:
                self.workflow.executor_settings.context = (
                    None  # None means current context is loaded
                )
            primary_comp_env = KubernetesExecutor(self.workflow, self.logger)
        elif primary_env_type == "openstack":
            primary_comp_env = OpenstackExecutor(self.workflow, self.logger)
        else:
            raise ValueError(
                f"Unsupported primary compute environment type: {primary_env_type}"
            )

        if self.workflow.executor_settings.secondary_comp_env:
            secondary_env_type, secondary_context = (
                self.workflow.executor_settings.secondary_comp_env.split(":")
            )
            if secondary_env_type == "kubernetes":
                if secondary_context != "current-context":
                    self.workflow.executor_settings.context = secondary_context
                else:
                    self.workflow.executor_settings.context = (
                        None  # None means current context is loaded
                    )
                secondary_comp_env = KubernetesExecutor(self.workflow, self.logger)
            elif secondary_env_type == "openstack":
                secondary_comp_env = OpenstackExecutor(self.workflow, self.logger)
            else:
                raise ValueError(
                    f"Unsupported secondary compute environment type: {secondary_env_type}"
                )
        else:
            secondary_comp_env = None

        self.logger.info(
            f"Primary compute environment: {primary_env_type} (Context: {primary_context})"
        )
        if secondary_comp_env:
            self.logger.info(
                f"Secondary compute environment: {secondary_env_type} (Context: {secondary_context})"
            )
        else:
            self.logger.info(
                "No secondary compute environment provided. Offloading will be inactive."
            )

        return primary_comp_env, secondary_comp_env

    def _is_job_offloaded(self, job: JobExecutorInterface) -> bool:
        if self.jobs_to_offload:
            jobs_to_offload = self.jobs_to_offload.split(",")
            jobs_to_offload = [int(j) for j in jobs_to_offload]
            if job.jobid in jobs_to_offload:
                self.logger.info(f"Job {job.jobid} is offloaded.")
                return True
            else:
                self.logger.info(f"Job {job.jobid} is not offloaded.")
                return False
        # for testing
        else:
            if job.jobid % 2 == 0:
                self.logger.info(f"Job {job.jobid} is offloaded.")
                return True
            else:
                self.logger.info(f"Job {job.jobid} is not offloaded.")
                return False
