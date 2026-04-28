import importlib
import logging
import typing

from types import ModuleType

from snowflake.core.exceptions import MissingModuleError


logger = logging.getLogger(__name__)

snowpark_post_error_blurb = (
    "Please install it, as it is required for this functionality. For "
    "installation instructions see: https://developers.snowflake.com/snowpark/"
)


class MissingModule:
    """A class to replace missing modules.

    The only thing this class is supposed to do is raise a MissingModuleError when __getattr__ is called.
    This will be triggered whenever module.member is going to be called.
    """

    def __init__(
        self,
        mod_name:str,
        post_error_blurb: typing.Optional[str] = None,
    ) -> None:
        self.mod_name = mod_name
        self.post_error_blurb = post_error_blurb

    def __getattr__(self, item: str) -> typing.NoReturn:
        raise MissingModuleError(self.mod_name, self.post_error_blurb)


ModuleLikeObject = typing.Union[ModuleType, MissingModule]


def _import_or_missing_snowpark() -> (
    tuple[ModuleLikeObject, bool]
):
    try:
        snowpark = importlib.import_module("snowflake.snowpark")
        return snowpark, True
    except ImportError:
        return MissingModule("snowflake.snowpark", snowpark_post_error_blurb), False


(snowpark, installed_snowpark) = _import_or_missing_snowpark()

logger.info(
    "checked whether snowflake-snowpark-python is available: %s",
    installed_snowpark,
)


def require_snowpark() -> None:
    """Raise an exception if snowflake-snowpark-python is missing."""
    if not installed_snowpark:
        raise MissingModuleError("snowflake.snowpark", snowpark_post_error_blurb)


__all__ = [
    "snowpark",
    "installed_snowpark",
    "require_snowpark",
]
