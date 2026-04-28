from collections.abc import Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Annotated, Literal, Optional, Union, overload

from pydantic import Field, StrictStr

from snowflake.core import FQN, PollingOperation
from snowflake.core._common import (
    Clone,
    CreateMode,
    PointOfTime,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from ._generated import SuccessResponse
from ._generated.api import AlertApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.alert import Alert
from ._generated.models.alert_clone import AlertClone
from ._generated.models.point_of_time import PointOfTime as AlertPointOfTime


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource

class AlertCollection(SchemaObjectCollectionParent["AlertResource"]):
    """Represents the collection operations on the Snowflake Alert resource.

    With this collection, you can create, update, iterate through, and fetch alerts that you have access to in the
    current context.
    """

    def __init__(
        self,
        schema: "SchemaResource",
    ):
        super().__init__(schema, AlertResource)
        self._api = AlertApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )

    @overload
    @api_telemetry
    def create(
        self, alert: str,
        *,
        clone_alert: Union[str, Clone],
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> "AlertResource":
        ...
    @overload
    @api_telemetry
    def create(
        self, alert: Alert,
        *,
        clone_alert: None,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> "AlertResource":
        ...
    @api_telemetry
    def create(
        self, alert: Union[Alert, str],
        *,
        clone_alert: Optional[Union[str, Clone]] = None,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> "AlertResource":
        """Create an alert in Snowflake.

        There are two ways to create an alert: by cloning or by building from scratch.

        **Cloning an existing alert**

        Parameters
        __________
        alert: str
            The new alert's name
        clone_alert: str or Clone object
            The name of alert to be cloned, or a ``Clone`` object which would contain the name of the alert
            with support to clone at a specific time.
        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the alert already exists in Snowflake.  Equivalent to SQL ``create alert <name> ...``.

            ``CreateMode.or_replace``: Replace if the alert already exists in Snowflake. Equivalent to SQL
            ``create or replace alert <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the alert already exists in Snowflake.
            Equivalent to SQL ``create alert <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Cloning an alert instance:

        >>> alerts = schema.alerts
        >>> alerts.create(
        ...     new_alert_name,
        ...     clone_alert=alert_name_to_be_cloned,
        ...     mode=CreateMode.if_not_exists,
        ... )

        **Creating an alert from scratch**

        Parameters
        __________
        alert: Alert
            The details of ``Alert`` object, together with ``Alert``'s properties:
            name, schedule, condition, action ;
            comment, warehouse are optional.

        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the alert already exists in Snowflake.  Equivalent to SQL ``create alert <name> ...``.

            ``CreateMode.or_replace``: Replace if the alert already exists in Snowflake. Equivalent to SQL
            ``create or replace alert <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the alert already exists in Snowflake.
            Equivalent to SQL ``create alert <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating an alert instance:

        >>> alerts.create(
        ...     Alert(
        ...         name="my_alert",
        ...         warehouse="my_warehouse",
        ...         schedule="MinutesSchedule(minutes=1)",
        ...         condition="SELECT COUNT(*) FROM my_table > 100",
        ...         action="DROP TABLE my_table",
        ...     ),
        ...     mode=CreateMode.if_not_exists,
        ... )
        """
        self._create(
            alert=alert,
            clone_alert=clone_alert,
            mode=mode,
            async_req=False,
        )
        return AlertResource(alert.name if isinstance(alert, Alert) else alert, self)

    @overload
    @api_telemetry
    def create_async(
        self, alert: str,
        *,
        clone_alert: Union[str, Clone],
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> PollingOperation["AlertResource"]:
        ...

    @overload
    @api_telemetry
    def create_async(
        self, alert: Alert,
        *,
        clone_alert: None,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> PollingOperation["AlertResource"]:
        ...

    @api_telemetry
    def create_async(
        self, alert: Union[Alert, str],
        *,
        clone_alert: Optional[Union[str, Clone]] = None,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["AlertResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._create(
            alert=alert,
            clone_alert=clone_alert,
            mode=mode,
            async_req=True,
        )
        return PollingOperation(future,
                                lambda _: AlertResource(alert.name if isinstance(alert, Alert) else alert, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[StrictStr] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None,
    ) -> Iterator[Alert]:
        """Iterate through ``Alert`` objects from Snowflake, filtering on any optional 'like' pattern.

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
        Showing all alerts that you have access to see:

        >>> alerts = alert_collection.iter()

        Showing information of the exact alert you want to see:

        >>> alerts = alert_collection.iter(like="your-alert-name")

        Showing alerts starting with 'your-alert-name-':

        >>> alerts = alert_collection.iter(like="your-alert-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for alert in alerts:
        >>>     print(alert.name, alert.condition, alert.action)
        """
        alerts = self._api.list_alerts(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            starts_with=starts_with,
            show_limit=show_limit,
            from_name=from_name,
            async_req=False,
        )
        return iter(alerts)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[StrictStr] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None,
    ) -> PollingOperation[Iterator[Alert]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_alerts(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            starts_with=starts_with,
            show_limit=show_limit,
            from_name=from_name,
            async_req=True,
        )
        return PollingOperations.iterator(future)

    @overload
    def _create(
        self, alert: Union[Alert, str],
        clone_alert: Optional[Union[str, Clone]],
        mode: CreateMode,
        async_req: Literal[True],
    ) -> Future[SuccessResponse]:
        ...

    @overload
    def _create(
        self, alert: Union[Alert, str],
        clone_alert: Optional[Union[str, Clone]],
        mode: CreateMode,
        async_req: Literal[False],
    ) -> SuccessResponse:
        ...

    def _create(
        self, alert: Union[Alert, str],
        clone_alert: Optional[Union[str, Clone]],
        mode: CreateMode,
        async_req: bool,
    ) -> Union[SuccessResponse, Future[SuccessResponse]]:
        real_mode = CreateMode[mode].value

        if clone_alert:
            # create alert by clone
            if not isinstance(alert, str):
                raise TypeError("Alert has to be str for clone")

            pot: Optional[AlertPointOfTime] = None
            if isinstance(clone_alert, Clone) and isinstance(clone_alert.point_of_time, PointOfTime):
                pot = AlertPointOfTime.from_dict(clone_alert.point_of_time.to_dict())
            real_clone = Clone(source=clone_alert) if isinstance(clone_alert, str) else clone_alert
            req = AlertClone(
                point_of_time=pot,
                name=alert,
            )

            source_alert_fqn = FQN.from_string(real_clone.source)
            return self._api.clone_alert(
                source_alert_fqn.database or self.database.name,
                source_alert_fqn.schema or self.schema.name,
                source_alert_fqn.name,
                alert_clone=req,
                create_mode=StrictStr(real_mode),
                async_req=async_req,
                target_database=self.database.name,
                target_schema=self.schema.name,
            )

        if not isinstance(alert, Alert):
            raise TypeError("alert has to be Alert object")
        return self._api.create_alert(
            self.database.name,
            self.schema.name,
            alert,
            create_mode=StrictStr(real_mode),
            async_req=async_req,
        )


class AlertResource(SchemaObjectReferenceMixin[AlertCollection]):
    """Represents a reference to a Snowflake Alert resource.

    With this alert reference, you can create, update, delete, and fetch information about alerts, as well
    as perform certain actions on them.
    """

    def __init__(
        self,
        name: StrictStr,
        collection: AlertCollection
    ) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> Alert:
        """Fetch the details of an alert.

        Examples
        ________
        Fetching an alert reference to print its name and query properties:

        >>> my_alert = alert_reference.fetch()
        >>> print(my_alert.name, my_alert.condition, my_alert.action)
        """
        return self.collection._api.fetch_alert(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[Alert]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_alert(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(
        self,
        if_exists: bool = False,
    ) -> None:
        """Drop this alert.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this alert before drop.
            The default value is ``False``.

        Examples
        ________
        Deleting an alert using its reference, erroring if it doesn't exist:

        >>> alert_reference.drop()

        Deleting an alert using its reference if it exists:

        >>> alert_reference.drop(if_exists=True)
        """
        self.collection._api.delete_alert(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists = if_exists,
            async_req=False,
        )

    @api_telemetry
    def drop_async(self, if_exists: bool = False) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_alert(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists = if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def execute(self) -> None:
        """Execute an alert.

        Examples
        ________
        Use an alert reference to execute it:

        >>> alert_reference.execute()
        """
        self.collection._api.execute_alert(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=False,
        )

    @api_telemetry
    def execute_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`execute`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.execute_alert(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=True,
        )
        return PollingOperations.empty(future)
