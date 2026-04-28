import re

from typing import Any, Optional

from snowflake.core._utils import check_version_gte, check_version_lte
from snowflake.core.version import __version__


MINIMUM_SUPPORT_VERSION = "minimumSupportedVersion"
MINIMUM_NEARING_END_OF_SUPPORT_VERSION = "minimumNearingEndOfSupportVersion"
RECOMMENDED_VERSION = "recommendedVersion"


class ClientInfo:

    def __init__(self, client_support_info: Optional[dict[str, str]]) -> None:
        """Store the metadata about the client, and server configuration for the client.

        Parameters
        __________
            client_support_info: dict[str, str], optional
                A parsed (from JSON) dictionary of client support information.
        """
        self._client_support_info = None
        if self._validate_client_info(client_support_info):
            self._client_support_info = client_support_info

    @property
    def client_version(self) -> str:
        return __version__

    @property
    def minimum_supported_version(self) -> Optional[str]:
        if self._client_support_info is not None and self._client_support_info[MINIMUM_SUPPORT_VERSION] != "":
            return self._client_support_info[MINIMUM_SUPPORT_VERSION]

        return None

    @property
    def end_of_support_version(self) -> Optional[str]:
        if (self._client_support_info is not None and
                self._client_support_info[MINIMUM_NEARING_END_OF_SUPPORT_VERSION] != ""):
            return self._client_support_info[MINIMUM_NEARING_END_OF_SUPPORT_VERSION]

        return None

    @property
    def recommended_version(self) -> Optional[str]:
        if self._client_support_info is not None and self._client_support_info[RECOMMENDED_VERSION] != "":
            return self._client_support_info[RECOMMENDED_VERSION]

        return None

    def version_is_supported(self) -> bool:
        # If the minimum supported version is empty, then we assume that the client is supported
        if self._client_support_info is None or self.minimum_supported_version is None:
            return True

        return check_version_gte(self.client_version, self.minimum_supported_version)

    def version_is_nearing_end_of_support(self) -> bool:
        if self._client_support_info is None or self.end_of_support_version is None:
            return False

        return check_version_lte(self.client_version,
                                 self.end_of_support_version)

    @staticmethod
    def _validate_client_info(client_support_info: Optional[dict[str, Any]]) -> bool:
        # If the client support information is None, then we assume that the client is supported
        if client_support_info is not None:
            # Check for the clientId, and that it is "PyCore"
            if "clientId" not in client_support_info or client_support_info["clientId"] != "PyCore":
                return False

            # Regex pattern for valid version strings, which can be empty
            pattern = re.compile(r'^(\d+\.\d+\.\d+)?$')

            try:
                if MINIMUM_SUPPORT_VERSION not in client_support_info or pattern.match(
                        client_support_info[MINIMUM_SUPPORT_VERSION]) is None:
                    return False

                if MINIMUM_NEARING_END_OF_SUPPORT_VERSION not in client_support_info or pattern.match(
                        client_support_info[MINIMUM_NEARING_END_OF_SUPPORT_VERSION]) is None:
                    return False

                if RECOMMENDED_VERSION not in client_support_info or pattern.match(
                        client_support_info[RECOMMENDED_VERSION]) is None:
                    return False

            except TypeError:
                # This can happen if the Dict does not have string values when trying to match the regex
                return False

        return True
