from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from snowflake.core import PollingOperation
from snowflake.core._common import AccountObjectCollectionParent, CreateMode, ObjectReferenceMixin
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._operation import PollingOperations
from snowflake.core.notification_integration._generated import (
    NotificationIntegration,
    NotificationIntegrationApi,
)
from snowflake.core.notification_integration._generated.api_client import StoredProcApiClient


if TYPE_CHECKING:
    from snowflake.core._root import Root


class NotificationIntegrationCollection(AccountObjectCollectionParent["NotificationIntegrationResource"]):
    """Represents the collection operations on the Snowflake Notification Integration resource.

    With this collection, you can create, update, and iterate through notification integrations that you have access
    to in the current context.

    Examples
    ________
    Creating a notification integrations instance:

    >>> # This example assumes that mySecret already exists
    >>> notification_integrations = root.notification_integrations
    >>> new_ni = NotificationIntegration(
    ...     name="my_notification_integration",
    ...     enabled=True,
    ...     notification_hook=NotificationWebhook(
    ...         webhook_url="https://events.pagerduty.com/v2/enqueue",
    ...         webhook_secret=WebhookSecret(
    ...             name="mySecret".upper(), database_name=database, schema_name=schema
    ...         ),
    ...         webhook_body_template='{"key": "SNOWFLAKE_WEBHOOK_SECRET", "msg": "SNOWFLAKE_WEBHOOK_MESSAGE"}',
    ...         webhook_headers={"content-type": "application/json", "user-content": "chrome"},
    ...     )
    ... )
    >>> notification_integrations.create(new_ni)
    """

    def __init__(self, root: "Root") -> None:
        super().__init__(root, ref_class=NotificationIntegrationResource)
        self._api = NotificationIntegrationApi(
            root=root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        notification_integration: NotificationIntegration,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "NotificationIntegrationResource":
        """Create a notification integration in Snowflake.

        Parameters
        __________
        notification_integration: NotificationIntegration
            The ``NotificationIntegration`` object, together with the ``NotificationIntegration``'s properties:
            name, enabled, notification_hook; comment is optional.
        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the notification integration already exists in Snowflake.  Equivalent to SQL
            ``create notification integration <name> ...``.

            ``CreateMode.or_replace``: Replace if the notification integration already exists in
            Snowflake. Equivalent to SQL ``create or replace notification integration <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the notification integration already exists in Snowflake.
            Equivalent to SQL ``create notification integration <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        __________
        Creating a notification integration instance:

        >>> notification_integrations.create(
        ...     NotificationIntegration(
        ...         name="my_notification_integration",
        ...         notification_hook=NotificationEmail(
        ...             allowed_recipients=["my_email@company.com"],
        ...             default_recipients=["my_email@company.com"],
        ...             default_subject="test default subject",
        ...         ),
        ...         comment="This is a comment",
        ...         enabled=True,
        ...     ),
        ...     mode=CreateMode.if_not_exists,
        ... )
        """
        real_mode = CreateMode[mode].value
        self._api.create_notification_integration(
            notification_integration=notification_integration,
            create_mode=real_mode,
            async_req=False,
        )
        return self[notification_integration.name]

    @api_telemetry
    def create_async(
        self,
        notification_integration: NotificationIntegration,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["NotificationIntegrationResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_notification_integration(
            notification_integration=notification_integration,
            create_mode=real_mode,
            async_req=True,
        )
        return PollingOperation(future, lambda _: self[notification_integration.name])

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
    ) -> Iterator[NotificationIntegration]:
        """Iterate through ``NotificationIntegration`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        _________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all notification integrations that you have access to see:

        >>> notification_integrations = notification_integrations.iter()

        Showing information of the exact notification integration you want to see:

        >>> notification_integrations = notification_integrations.iter(like="your-notification-integration-name")

        Showing notification integrations starting with 'your-notification-integration-name-':

        >>> notification_integrations = notification_integrations.iter(like="your-notification-integration-name-%")

        Using a for-loop to retrieve information from iterator:

        >>> for notification_integration in notification_integrations:
        >>>     print(
        ...         notification_integration.name,
        ...         notification_integration.enabled,
        ...         repr(notification_integration.notification_hook),
        ...     )
        """
        return iter(
            self._api.list_notification_integrations(
                like,
                async_req=False,
            )
        )

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
    ) -> PollingOperation[Iterator[NotificationIntegration]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_notification_integrations(
            like,
            async_req=True,
        )
        return PollingOperations.iterator(future)


class NotificationIntegrationResource(ObjectReferenceMixin[NotificationIntegrationCollection]):
    """Represents a reference to a Snowflake Notification Integration resource.

    With this notification integration reference you can delete, and fetch information about them.
    """

    def __init__(self, name: str, collection: NotificationIntegrationCollection) -> None:
        self.name = name
        self.collection: NotificationIntegrationCollection = collection

    @property
    def _api(self) -> NotificationIntegrationApi:
        return self.collection._api

    @api_telemetry
    def fetch(self) -> NotificationIntegration:
        """Fetch the details of a notification integration.

        Examples
        ________
        Fetching a notification integration reference to print its name, whether it's enabled and some information
        about its hook:

        >>> my_ni = ni_reference.fetch()
        >>> print(my_ni.name, my_ni.enabled, repr(my_ni.notification_hook))
        """
        return self.collection._api.fetch_notification_integration(
            self.name,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[NotificationIntegration]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_notification_integration(
            self.name,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this notification integration.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this notification integration before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a notification integration using its reference:

        >>> ni_reference.drop()

        Deleting a notification integration using its reference if it exists:

        >>> ni_reference.drop(if_exists=True)
        """
        self.collection._api.delete_notification_integration(
            name=self.name, if_exists=if_exists, async_req=False,
        )

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_notification_integration(
            name=self.name, if_exists=if_exists, async_req=True,
        )
        return PollingOperations.empty(future)
