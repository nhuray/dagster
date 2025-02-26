import time
from typing import Any, Dict, List, Optional

import kubernetes.config
import kubernetes.watch
from dagster import Field, In, Noneable, Nothing, OpExecutionContext, Permissive, StringSource, op
from dagster._annotations import experimental
from dagster._utils.merger import merge_dicts

from ..client import DEFAULT_JOB_POD_COUNT, DagsterKubernetesClient
from ..container_context import K8sContainerContext
from ..job import DagsterK8sJobConfig, construct_dagster_k8s_job, get_k8s_job_name
from ..launcher import K8sRunLauncher

K8S_JOB_OP_CONFIG = merge_dicts(
    DagsterK8sJobConfig.config_type_container(),
    {
        "image": Field(
            StringSource,
            is_required=True,
            description="The image in which to launch the k8s job.",
        ),
        "command": Field(
            [str],
            is_required=False,
            description="The command to run in the container within the launched k8s job.",
        ),
        "args": Field(
            [str],
            is_required=False,
            description="The args for the command for the container.",
        ),
        "namespace": Field(StringSource, is_required=False),
        "load_incluster_config": Field(
            bool,
            is_required=False,
            default_value=True,
            description="""Set this value if you are running the launcher
            within a k8s cluster. If ``True``, we assume the launcher is running within the target
            cluster and load config using ``kubernetes.config.load_incluster_config``. Otherwise,
            we will use the k8s config specified in ``kubeconfig_file`` (using
            ``kubernetes.config.load_kube_config``) or fall back to the default kubeconfig.""",
        ),
        "kubeconfig_file": Field(
            Noneable(str),
            is_required=False,
            default_value=None,
            description=(
                "The kubeconfig file from which to load config. Defaults to using the default"
                " kubeconfig."
            ),
        ),
        "timeout": Field(
            int,
            is_required=False,
            description="How long to wait for the job to succeed before raising an exception",
        ),
        "container_config": Field(
            Permissive(),
            is_required=False,
            description=(
                "Raw k8s config for the k8s pod's main container"
                " (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#container-v1-core)."
                " Keys can either snake_case or camelCase."
            ),
        ),
        "pod_template_spec_metadata": Field(
            Permissive(),
            is_required=False,
            description=(
                "Raw k8s config for the k8s pod's metadata"
                " (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#objectmeta-v1-meta)."
                " Keys can either snake_case or camelCase."
            ),
        ),
        "pod_spec_config": Field(
            Permissive(),
            is_required=False,
            description=(
                "Raw k8s config for the k8s pod's pod spec"
                " (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#podspec-v1-core)."
                " Keys can either snake_case or camelCase."
            ),
        ),
        "job_metadata": Field(
            Permissive(),
            is_required=False,
            description=(
                "Raw k8s config for the k8s job's metadata"
                " (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#objectmeta-v1-meta)."
                " Keys can either snake_case or camelCase."
            ),
        ),
        "job_spec_config": Field(
            Permissive(),
            is_required=False,
            description=(
                "Raw k8s config for the k8s job's job spec"
                " (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#jobspec-v1-batch)."
                " Keys can either snake_case or camelCase."
            ),
        ),
    },
)


@experimental
def execute_k8s_job(
    context: OpExecutionContext,
    image: str,
    command: Optional[List[str]] = None,
    args: Optional[List[str]] = None,
    namespace: Optional[str] = None,
    image_pull_policy: Optional[str] = None,
    image_pull_secrets: Optional[List[Dict[str, str]]] = None,
    service_account_name: Optional[str] = None,
    env_config_maps: Optional[List[str]] = None,
    env_secrets: Optional[List[str]] = None,
    env_vars: Optional[List[str]] = None,
    volume_mounts: Optional[List[Dict[str, Any]]] = None,
    volumes: Optional[List[Dict[str, Any]]] = None,
    labels: Optional[Dict[str, str]] = None,
    resources: Optional[Dict[str, Any]] = None,
    scheduler_name: Optional[str] = None,
    load_incluster_config: bool = True,
    kubeconfig_file: Optional[str] = None,
    timeout: Optional[int] = None,
    container_config: Optional[Dict[str, Any]] = None,
    pod_template_spec_metadata: Optional[Dict[str, Any]] = None,
    pod_spec_config: Optional[Dict[str, Any]] = None,
    job_metadata: Optional[Dict[str, Any]] = None,
    job_spec_config: Optional[Dict[str, Any]] = None,
):
    """
    This function is a utility for executing a Kubernetes job from within a Dagster op.

    Args:
        image (str): The image in which to launch the k8s job.
        command (Optional[List[str]]): The command to run in the container within the launched
            k8s job. Default: None.
        args (Optional[List[str]]): The args for the command for the container. Default: None.
        namespace (Optional[str]): Override the kubernetes namespace in which to run the k8s job.
            Default: None.
        image_pull_policy (Optional[str]): Allows the image pull policy to be overridden, e.g. to
            facilitate local testing with `kind <https://kind.sigs.k8s.io/>`_. Default:
            ``"Always"``. See:
            https://kubernetes.io/docs/concepts/containers/images/#updating-images.
        image_pull_secrets (Optional[List[Dict[str, str]]]): Optionally, a list of dicts, each of
            which corresponds to a Kubernetes ``LocalObjectReference`` (e.g.,
            ``{'name': 'myRegistryName'}``). This allows you to specify the ```imagePullSecrets`` on
            a pod basis. Typically, these will be provided through the service account, when needed,
            and you will not need to pass this argument. See:
            https://kubernetes.io/docs/concepts/containers/images/#specifying-imagepullsecrets-on-a-pod
            and https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.17/#podspec-v1-core
        service_account_name (Optional[str]): The name of the Kubernetes service account under which
            to run the Job. Defaults to "default"        env_config_maps (Optional[List[str]]): A list of custom ConfigMapEnvSource names from which to
            draw environment variables (using ``envFrom``) for the Job. Default: ``[]``. See:
            https://kubernetes.io/docs/tasks/inject-data-application/define-environment-variable-container/#define-an-environment-variable-for-a-container
        env_secrets (Optional[List[str]]): A list of custom Secret names from which to
            draw environment variables (using ``envFrom``) for the Job. Default: ``[]``. See:
            https://kubernetes.io/docs/tasks/inject-data-application/distribute-credentials-secure/#configure-all-key-value-pairs-in-a-secret-as-container-environment-variables
        env_vars (Optional[List[str]]): A list of environment variables to inject into the Job.
            Default: ``[]``. See: https://kubernetes.io/docs/tasks/inject-data-application/distribute-credentials-secure/#configure-all-key-value-pairs-in-a-secret-as-container-environment-variables
        volume_mounts (Optional[List[Permissive]]): A list of volume mounts to include in the job's
            container. Default: ``[]``. See:
            https://v1-18.docs.kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#volumemount-v1-core
        volumes (Optional[List[Permissive]]): A list of volumes to include in the Job's Pod. Default: ``[]``. See:
            https://v1-18.docs.kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#volume-v1-core
        labels (Optional[Dict[str, str]]): Additional labels that should be included in the Job's Pod. See:
            https://kubernetes.io/docs/concepts/overview/working-with-objects/labels
        resources (Optional[Dict[str, Any]]) Compute resource requirements for the container. See:
            https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
        scheduler_name (Optional[str]): Use a custom Kubernetes scheduler for launched Pods. See:
            https://kubernetes.io/docs/tasks/extend-kubernetes/configure-multiple-schedulers/
        load_incluster_config (bool): Whether the op is running within a k8s cluster. If ``True``,
            we assume the launcher is running within the target cluster and load config using
            ``kubernetes.config.load_incluster_config``. Otherwise, we will use the k8s config
            specified in ``kubeconfig_file`` (using ``kubernetes.config.load_kube_config``) or fall
            back to the default kubeconfig. Default: True,
        kubeconfig_file (Optional[str]): The kubeconfig file from which to load config. Defaults to
            using the default kubeconfig. Default: None.
        timeout (Optional[int]): Raise an exception if the op takes longer than this timeout in
            seconds to execute. Default: None.
        container_config (Optional[Dict[str, Any]]): Raw k8s config for the k8s pod's main container
            (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#container-v1-core).
            Keys can either snake_case or camelCase.Default: None.
        pod_template_spec_metadata (Optional[Dict[str, Any]]): Raw k8s config for the k8s pod's
            metadata (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#objectmeta-v1-meta).
            Keys can either snake_case or camelCase. Default: None.
        pod_spec_config (Optional[Dict[str, Any]]): Raw k8s config for the k8s pod's pod spec
            (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#podspec-v1-core).
            Keys can either snake_case or camelCase. Default: None.
        job_metadata (Optional[Dict[str, Any]]): aw k8s config for the k8s job's metadata
            (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#objectmeta-v1-meta).
            Keys can either snake_case or camelCase. Default: None.
        job_spec_config (Optional[Dict[str, Any]]): Raw k8s config for the k8s job's job spec
            (https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.20/#jobspec-v1-batch).
            Keys can either snake_case or camelCase.Default: None.
    """
    run_container_context = K8sContainerContext.create_for_run(
        context.pipeline_run,
        context.instance.run_launcher
        if isinstance(context.instance.run_launcher, K8sRunLauncher)
        else None,
        include_run_tags=False,
    )

    container_config = container_config.copy() if container_config else {}
    if command:
        container_config["command"] = command

    container_name = container_config.get("name", "dagster")

    op_container_context = K8sContainerContext(
        image_pull_policy=image_pull_policy,
        image_pull_secrets=image_pull_secrets,
        service_account_name=service_account_name,
        env_config_maps=env_config_maps,
        env_secrets=env_secrets,
        env_vars=env_vars,
        volume_mounts=volume_mounts,
        volumes=volumes,
        labels=labels,
        namespace=namespace,
        resources=resources,
        scheduler_name=scheduler_name,
        run_k8s_config={
            "container_config": container_config,
            "pod_template_spec_metadata": pod_template_spec_metadata,
            "pod_spec_config": pod_spec_config,
            "job_metadata": job_metadata,
            "job_spec_config": job_spec_config,
        },
    )

    container_context = run_container_context.merge(op_container_context)

    namespace = container_context.namespace

    user_defined_k8s_config = container_context.get_run_user_defined_k8s_config()

    k8s_job_config = DagsterK8sJobConfig(
        job_image=image,
        dagster_home=None,
        image_pull_policy=container_context.image_pull_policy,
        image_pull_secrets=container_context.image_pull_secrets,
        service_account_name=container_context.service_account_name,
        instance_config_map=None,
        postgres_password_secret=None,
        env_config_maps=container_context.env_config_maps,
        env_secrets=container_context.env_secrets,
        env_vars=container_context.env_vars,
        volume_mounts=container_context.volume_mounts,
        volumes=container_context.volumes,
        labels=container_context.labels,
        resources=container_context.resources,
    )

    job_name = get_k8s_job_name(context.run_id, context.get_step_execution_context().step.key)

    retry_number = context.retry_number
    if retry_number > 0:
        job_name = f"{job_name}-{retry_number}"

    job = construct_dagster_k8s_job(
        job_config=k8s_job_config,
        args=args,
        job_name=job_name,
        pod_name=job_name,
        component="k8s_job_op",
        user_defined_k8s_config=user_defined_k8s_config,
        labels={
            "dagster/job": context.pipeline_run.pipeline_name,
            "dagster/op": context.op.name,
            "dagster/run-id": context.pipeline_run.run_id,
        },
    )

    if load_incluster_config:
        kubernetes.config.load_incluster_config()
    else:
        kubernetes.config.load_kube_config(kubeconfig_file)

    # changing this to be able to be passed in will allow for unit testing
    api_client = DagsterKubernetesClient.production_client()

    context.log.info(f"Creating Kubernetes job {job_name} in namespace {namespace}...")

    start_time = time.time()

    api_client.batch_api.create_namespaced_job(namespace, job)

    context.log.info("Waiting for Kubernetes job to finish...")

    timeout = timeout or 0

    api_client.wait_for_job(
        job_name=job_name,
        namespace=namespace,
        wait_timeout=timeout,
        start_time=start_time,
    )

    pods = api_client.wait_for_job_to_have_pods(
        job_name,
        namespace,
        wait_timeout=timeout,
        start_time=start_time,
    )

    pod_names = [p.metadata.name for p in pods]

    if not pod_names:
        raise Exception("No pod names in job after it started")

    pod_to_watch = pod_names[0]
    watch = kubernetes.watch.Watch()  # consider moving in to api_client

    api_client.wait_for_pod(pod_to_watch, namespace, wait_timeout=timeout, start_time=start_time)

    log_stream = watch.stream(
        api_client.core_api.read_namespaced_pod_log,
        name=pod_to_watch,
        namespace=namespace,
        container=container_name,
    )

    while True:
        if timeout and time.time() - start_time > timeout:
            watch.stop()
            raise Exception("Timed out waiting for pod to finish")

        try:
            log_entry = next(log_stream)
            print(log_entry)  # pylint: disable=print-call
        except StopIteration:
            break

    if job_spec_config and job_spec_config.get("parallelism"):
        num_pods_to_wait_for = job_spec_config["parallelism"]
    else:
        num_pods_to_wait_for = DEFAULT_JOB_POD_COUNT
    api_client.wait_for_running_job_to_succeed(
        job_name=job_name,
        namespace=namespace,
        wait_timeout=timeout,
        start_time=start_time,
        num_pods_to_wait_for=num_pods_to_wait_for,
    )


@op(ins={"start_after": In(Nothing)}, config_schema=K8S_JOB_OP_CONFIG)
@experimental
def k8s_job_op(context):
    """
    An op that runs a Kubernetes job using the k8s API.

    Contrast with the `k8s_job_executor`, which runs each Dagster op in a Dagster job in its
    own k8s job.

    This op may be useful when:
      - You need to orchestrate a command that isn't a Dagster op (or isn't written in Python)
      - You want to run the rest of a Dagster job using a specific executor, and only a single
        op in k8s.

    For example:

    .. literalinclude:: ../../../../../../python_modules/libraries/dagster-k8s/dagster_k8s_tests/unit_tests/test_example_k8s_job_op.py
      :start-after: start_marker
      :end-before: end_marker
      :language: python

    You can create your own op with the same implementation by calling the `execute_k8s_job` function
    inside your own op.

    The service account that is used to run this job should have the following RBAC permissions:

    .. literalinclude:: ../../../../../../examples/docs_snippets/docs_snippets/deploying/kubernetes/k8s_job_op_rbac.yaml
       :language: YAML
    """
    execute_k8s_job(context, **context.op_config)
