import json
import re

from collections.abc import Iterator
from textwrap import dedent
from typing import TYPE_CHECKING, Any, Optional, Union

import yaml

from pydantic import StrictInt, StrictStr

from snowflake.core import PollingOperation
from snowflake.core._operation import PollingOperations

from .._common import (
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from .._internal.telemetry import api_telemetry
from .._internal.utils import deprecated


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource

from snowflake.core.service._generated import (
    FetchServiceLogs200Response,
    FetchServiceStatus200Response,
    GrantOf,
    ServiceApi,
    ServiceContainer,
    ServiceEndpoint,
    ServiceInstance,
    ServiceRole,
    ServiceRoleGrantTo,
)
from snowflake.core.service._generated.api_client import StoredProcApiClient
from snowflake.core.service._generated.models import JobService, Service, ServiceSpecInlineText, ServiceSpecStageFile


class ServiceCollection(SchemaObjectCollectionParent["ServiceResource"]):
    """Represents the collection operations on the Snowflake Snowpark Container Service resource.

    With this collection, you can create, update, iterate through, and fetch SPCSs that you have access to in the
    current context.

    Examples
    ________
    Creating a service instance:

    >>> my_service = Service(
    ...     name="my_service",
    ...     min_instances=1,
    ...     max_instances=2,
    ...     compute_pool="my_compute_pool",
    ...     spec=ServiceSpec("@my_stage/my_service_spec.yaml")
    ...    )
    >>> root.databases["my_db"].schemas["my_schema"].services.create(my_service)
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, ServiceResource)
        self._api = ServiceApi(
            root=self.root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
    ) -> Iterator[Service]:
        """Iterate through ``Service`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).
        starts_with: str, optional
            String used to filter the command output based on the string of characters that appear
            at the beginning of the object name. Uses case-sensitive pattern matching.
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.
        from_name: str, optional
            Fetch rows only following the first row whose object name matches
            the specified string. This is case-sensitive and does not have to be the full name.

        Examples
        ________
        Showing all services that you have access to see:

        >>> services = service_collection.iter()

        Showing information of the exact service you want to see:

        >>> services = service_collection.iter(like="your-service-name")

        Showing services starting with 'your-service-name-':

        >>> services = service_collection.iter(like="your-service-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for service in services:
        >>>    print(service.name)
        """
        services = self._api.list_services(
            self.database.name,
            self.schema.name,
            StrictStr(like) if like is not None else None,
            StrictStr(starts_with) if starts_with else None,
            limit,
            from_name,
            async_req=False,
        )

        return iter(services)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
    ) -> PollingOperation[Iterator[Service]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_services(
            self.database.name,
            self.schema.name,
            StrictStr(like) if like is not None else None,
            StrictStr(starts_with) if starts_with else None,
            limit,
            from_name,
            async_req=True,
        )
        return PollingOperations.iterator(future)

    @api_telemetry
    def create(
        self,
        service: Service,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "ServiceResource":
        """Create a Snowpark Container service in Snowflake.

        Parameters
        __________
        service: Service
            The ``Service`` object, together with the ``Service``'s properties:
            name, compute_pool, spec; auto_resume, min_instances, max_instances, status, external_access_integrations,
            query_warehouse, comment are optional.
        mode: CreateMode, optional
        One of the following enum values.

        ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
        if the service already exists in Snowflake.  Equivalent to SQL ``create service <name> ...``.

        ``CreateMode.or_replace``: Replace if the service already exists in Snowflake. Equivalent to SQL
        ``create or replace service <name> ...``.

        ``CreateMode.if_not_exists``: Do nothing if the service already exists in Snowflake.
        Equivalent to SQL ``create service <name> if not exists...``

        Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a service, replacing any existing service with the same name:

        >>> services = root.databases["my_db"].schemas["my_schema"].services
        >>> my_service = Service(
        ...     name="my_service",
        ...     compute_pool="my_compute_pool",
        ...     spec=ServiceSpec("@my_stage/my_service_spec.yaml")
        ... )
        >>> services.create(my_service, mode=CreateMode.or_replace)
        """
        if mode == CreateMode.or_replace:
            raise ValueError(f"{mode} is not a valid value for this resource")
        real_mode = CreateMode[mode].value
        self._api.create_service(
            self.database.name,
            self.schema.name,
            service,
            StrictStr(real_mode),
            async_req=False,
        )
        return self[service.name]

    @api_telemetry
    def create_async(
        self,
        service: Service,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["ServiceResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        if mode == CreateMode.or_replace:
            raise ValueError(f"{mode} is not a valid value for this resource")
        real_mode = CreateMode[mode].value
        future = self._api.create_service(
            self.database.name,
            self.schema.name,
            service,
            StrictStr(real_mode),
            async_req=True,
        )
        return PollingOperation(future, lambda _: self[service.name])

    @api_telemetry
    def execute_job(self, job_service: JobService) -> "ServiceResource":
        """Execute a Snowpark Container job service in Snowflake.

        Parameters
        __________
        job_service: JobService
            The ``JobService`` object, together with the ``JobService``'s properties:
            name, compute_pool, spec; status, external_access_integrations, query_warehouse, comment are optional

        Examples
        ________
        Executing a job service:

        >>> job_service = JobService(
        ...     name="my_job_service",
        ...     compute_pool="my_cp",
        ...     spec=ServiceSpec("@my_stage/my_service_spec.yaml"),
        ... )
        >>> services.execute_job(job_service)
        """
        self._api.execute_job_service(
            self.database.name,
            self.schema.name,
            job_service,
            async_req=False,
        )
        return self[job_service.name]

    @api_telemetry
    def execute_job_async(self, job_service: JobService) -> PollingOperation["ServiceResource"]:
        """An asynchronous version of :func:`execute_job`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """  # noqa: D401
        future = self._api.execute_job_service(
            self.database.name,
            self.schema.name,
            job_service,
            async_req=True,
        )
        return PollingOperation(future, lambda _: self[job_service.name])


class ServiceResource(SchemaObjectReferenceMixin[ServiceCollection]):
    """Represents a reference to a Snowflake Snowpark Container Service.

    With this service reference, you can create, alter, and fetch information about services, as well
    as perform certain actions on them.
    """

    def __init__(self, name: str, collection: ServiceCollection) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def create_or_alter(self, service: Service) -> None:
        """Create a service in Snowflake or alter one if it already exists.

        Parameters
        __________
        service: Service
            The ``Service`` object, together with the ``Service``'s properties:
            name, compute_pool, spec; auto_resume, min_instances, max_instances, status, external_access_integrations,
            query_warehouse, comment are optional.

        Examples
        ________
        Creating or updating a service in Snowflake:

        >>> service_parameters = Service(
        ...     name="your-service-name",
        ...     compute_pool="my_cp"
        ...     spec=ServiceSpecStageFile(stage="stage_name", spec_file=spec_file),
        ...)
        >>> services = root.databases["my_db"].schemas["my_schema"].services
        >>> services["your-service-name"].create_or_alter(service_parameters)
        """
        self.collection._api.create_or_alter_service(
            self.database.name, self.schema.name, self.name, service, async_req=False
        )

    @api_telemetry
    def create_or_alter_async(self, service: Service) -> PollingOperation[None]:
        """An asynchronous version of :func:`create_or_alter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.create_or_alter_service(
            self.database.name, self.schema.name, self.name, service, async_req=True
        )
        return PollingOperations.empty(future)

    @api_telemetry
    @deprecated("drop")
    def delete(self) -> None:
        self.drop()

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop the service.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this service before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a service using its reference:

        >>> service_reference.drop()
        """
        self.collection._api.delete_service(self.database.name, self.schema.name, self.name, if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_service(
            self.database.name, self.schema.name, self.name, if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def fetch(self) -> Service:
        """Fetch the details of a service.

        Examples
        ________
        Fetching a service using its reference:

        >>> service_reference.fetch()
        """
        return self.collection._api.fetch_service(self.database.name, self.schema.name, self.name, async_req=False)

    @api_telemetry
    def fetch_async(self) -> PollingOperation[Service]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_service(self.database.name, self.schema.name, self.name, async_req=True)
        return PollingOperations.identity(future)

    @api_telemetry
    def suspend(self, if_exists: Optional[bool] = None) -> None:
        """Suspend the service.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this service before suspending it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Suspending a service using its reference:

        >>> service_reference.suspend()
        """
        self.collection._api.suspend_service(
            self.database.name, self.schema.name, self.name, if_exists, async_req=False
        )

    @api_telemetry
    def suspend_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`suspend`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.suspend_service(
            self.database.name, self.schema.name, self.name, if_exists, async_req=True
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def resume(self, if_exists: Optional[bool] = None) -> None:
        """Resumes the service.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this service before resuming it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Resuming a service using its reference:

        >>> service_reference.resume()
        """
        self.collection._api.resume_service(self.database.name, self.schema.name, self.name, if_exists, async_req=False)

    @api_telemetry
    def resume_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`resume`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.resume_service(
            self.database.name, self.schema.name, self.name, if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def get_endpoints(self) -> Iterator[ServiceEndpoint]:
        """Show the endpoints corresponding to this service.

        Examples
        ________
        Showing the endpoints of a service using its reference:

        >>> service_reference.get_endpoints()
        """
        return iter(
            self.collection._api.show_service_endpoints(
                self.database.name, self.schema.name, self.name, async_req=False
            )
        )

    @api_telemetry
    def get_endpoints_async(self) -> PollingOperation[Iterator[ServiceEndpoint]]:
        """An asynchronous version of :func:`get_endpoints`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.show_service_endpoints(
            self.database.name, self.schema.name, self.name, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def get_containers(self) -> Iterator[ServiceContainer]:
        """Show the containers corresponding to this service.

        Examples
        ________
        Showing the containers of a service using its reference:

        >>> service_reference.get_containers()
        """
        return iter(
            self.collection._api.list_service_containers(
                self.database.name, self.schema.name, self.name, async_req=False
            )
        )

    @api_telemetry
    def get_containers_async(self) -> PollingOperation[Iterator[ServiceContainer]]:
        """An asynchronous version of :func:`get_containers`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_service_containers(
            self.database.name, self.schema.name, self.name, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def get_instances(self) -> Iterator[ServiceInstance]:
        """Show the instances corresponding to this service.

        Examples
        ________
        Showing the instances of a service using its reference:

        >>> service_reference.get_instances()
        """
        return iter(
            self.collection._api.list_service_instances(
                self.database.name, self.schema.name, self.name, async_req=False
            )
        )

    @api_telemetry
    def get_instances_async(self) -> PollingOperation[Iterator[ServiceInstance]]:
        """An asynchronous version of :func:`get_instances`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_service_instances(
            self.database.name, self.schema.name, self.name, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def get_roles(self) -> Iterator[ServiceRole]:
        """Show the roles corresponding to this service.

        Examples
        ________
        Showing the roles of a service using its reference:

        >>> service_reference.get_roles()
        """
        return iter(
            self.collection._api.list_service_roles(self.database.name, self.schema.name, self.name, async_req=False)
        )

    @api_telemetry
    def get_roles_async(self) -> PollingOperation[Iterator[ServiceRole]]:
        """An asynchronous version of :func:`get_roles`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_service_roles(
            self.database.name, self.schema.name, self.name, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def iter_grants_of_service_role(self, role_name: str) -> Iterator[GrantOf]:
        """Show the grants of the service role associated with this service.

        Parameters
        __________
        role_name: str
            Name of the service role.

        Examples
        ________
        Showing the grants of the service role associated with a service using the service reference:

        >>> service_reference.iter_grants_of_service_role("all_endpoints_usage")
        """
        return iter(
            self.collection._api.list_service_role_grants_of(
                self.database.name, self.schema.name, self.name, role_name, async_req=False
            )
        )

    @api_telemetry
    def iter_grants_of_service_role_async(self, role_name: str) -> PollingOperation[Iterator[GrantOf]]:
        """An asynchronous version of :func:`iter_grants_of_service_role`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_service_role_grants_of(
            self.database.name, self.schema.name, self.name, role_name, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def iter_grants_to_service_role(self, role_name: str) -> Iterator[ServiceRoleGrantTo]:
        """Show the grants given to the service role associated with this service.

        Parameters
        __________
        role_name: str
            Name of the service role.

        Examples
        ________
        Showing the grants given to the service role associated with a service using the service reference:

        >>> service_reference.iter_grants_to_service_role("all_endpoints_usage")
        """
        return iter(
            self.collection._api.list_service_role_grants_to(
                self.database.name, self.schema.name, self.name, role_name, async_req=False
            )
        )

    @api_telemetry
    def iter_grants_to_service_role_async(self, role_name: str) -> PollingOperation[Iterator[ServiceRoleGrantTo]]:
        """An asynchronous version of :func:`iter_grants_to_service_role`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_service_role_grants_to(
            self.database.name, self.schema.name, self.name, role_name, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    @deprecated("get_containers")
    def get_service_status(self, timeout: int = 0) -> list[dict[str, Any]]:
        """Get the status of the service.

        Parameters
        __________
        timeout: int
          Number of seconds to wait for the service to reach a steady state (for example, READY)
          before returning the status. If the service does not reach steady state within the specified time,
          Snowflake returns the current state.

          If not specified or ``0``, Snowflake returns the current state immediately.

          Default: ``0`` seconds.

        Examples
        ________
        Getting the status of a service using its reference:

        >>> service_reference.get_service_status()

        Getting the status of a service using its reference with a timeout:

        >>> service_reference.get_service_status(timeout=10)
        """
        status = self.collection._api.fetch_service_status(
            self.database.name,
            self.schema.name,
            self.name,
            StrictInt(timeout),
            async_req=False,
        )
        if status.systemget_service_status is None:
            return list()
        return json.loads(status.systemget_service_status)

    @api_telemetry
    @deprecated("get_containers_async")
    def get_service_status_async(self, timeout: int = 0) -> PollingOperation[list[dict[str, Any]]]:
        """An asynchronous version of :func:`get_service_status`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_service_status(
            self.database.name,
            self.schema.name,
            self.name,
            StrictInt(timeout),
            async_req=True,
        )
        def transform(status: FetchServiceStatus200Response) -> list[dict[str, Any]]:
            if status.systemget_service_status is None:
                return list()
            return json.loads(status.systemget_service_status)
        return PollingOperation(future, transform)

    @api_telemetry
    def get_service_logs(self, instance_id: str, container_name: str, num_lines: Optional[int] = None) -> str:
        """Get the service logs of the service.

        Parameters
        __________
        instance_id: str
            Instance ID of the service.
        container_name: str
            Container name of the service.
        num_lines: int, optional
            Number of the most recent log lines to retrieve.


        :meth:`get_service_status` returns the ``instance_id`` and ``container_name`` as a part of its results.

        Examples
        ________
        Getting the logs of a service using its reference:

        >>> service_reference.get_service_logs(instance_id="instance_id", container_name="container_name")
        """
        logs = self.collection._api.fetch_service_logs(
            self.database.name,
            self.schema.name,
            self.name,
            StrictInt(instance_id),
            StrictStr(container_name),
            num_lines,
            async_req=False,
        )
        if logs.systemget_service_logs is None:
            return ""
        return logs.systemget_service_logs


    @api_telemetry
    def get_service_logs_async(
        self,
        instance_id: str,
        container_name: str,
        num_lines: Optional[int] = None
    ) -> PollingOperation[str]:
        """An asynchronous version of :func:`get_service_logs`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_service_logs(
            self.database.name,
            self.schema.name,
            self.name,
            StrictInt(instance_id),
            StrictStr(container_name),
            num_lines,
            async_req=True,
        )
        def transform(logs: FetchServiceLogs200Response) -> str:
            if logs.systemget_service_logs is None:
                return ""
            return logs.systemget_service_logs
        return PollingOperation(future, transform)


def ServiceSpec(spec: str) -> Union[ServiceSpecInlineText, ServiceSpecStageFile]:
    """Infers whether a specification is a stage file or inline text.

    Any spec that starts with '@' is parsed as a stage file, otherwise it is passed as an inline text.
    """
    spec = dedent(spec).rstrip()
    if spec.startswith("@"):
        stage, spec_file = _parse_spec_path(spec[1:])
        return ServiceSpecStageFile(stage=stage, spec_file=spec_file)
    else:
        if _validate_inline_spec(spec):
            return ServiceSpecInlineText(spec_text=spec)
        else:
            raise ValueError(f"{spec} is not a valid service spec inline text")


def _validate_inline_spec(spec_str: str) -> bool:
    try:
        spec_data = yaml.safe_load(spec_str)
    except yaml.YAMLError:
        return False
    if not isinstance(spec_data, dict) or "spec" not in spec_data:
        return False
    return True


def _parse_spec_path(spec_path: str) -> tuple[str, str]:
    # this pattern tries to match a file path depth of at least two (needs a stage and file name at minimum)
    pattern = r'^[^<>:"|?*\/\n]+(?:\/[^<>:"|?*\/\n]+)+$'
    if not re.fullmatch(pattern, spec_path):
        raise ValueError(f"{spec_path} is not a valid stage file path")
    stage, path = spec_path.split("/", 1)
    return stage, path
