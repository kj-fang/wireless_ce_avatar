import json
import sys

from enum import Enum, EnumMeta
from typing import Annotated, Any, Optional, Union

from pydantic import BaseModel, Field, StringConstraints


class CaseInsensitiveEnumMeta(EnumMeta):
    def __init__(cls, *args, **kws) -> None:  # type: ignore
        super().__init__(*args, **kws)

        class lookup(dict):  # type: ignore
            def get(self, key, default=None):  # type: ignore
                return super().get(key.lower(), key.lower())

        cls._legacy_mode_map_ = lookup({item.value.lower(): item.name for item in cls})  # type: ignore

    def __getitem__(cls, name: str) -> Any:
        converted_name = cls._legacy_mode_map_.get(name)
        return super().__getitem__(converted_name)


class CreateMode(str, Enum, metaclass=CaseInsensitiveEnumMeta):
    """Enum for Snowflake create modes."""

    error_if_exists = "errorIfExists"
    or_replace = "orReplace"
    if_not_exists = "ifNotExists"


class DeleteMode(str, Enum, metaclass=CaseInsensitiveEnumMeta):
    """Enum for delete modes."""

    cascade = "cascade"
    restrict = "restrict"


class Clone(BaseModel):
    source: Annotated[str, StringConstraints(strict=True)] = Field(...)
    point_of_time: Optional["PointOfTime"] = None


class PointOfTime(BaseModel):
    point_of_time_type: Annotated[str, StringConstraints(strict=True)] = Field(...)
    reference: Annotated[str, StringConstraints(strict=True)] = Field(
        ..., description="The relation to the point of time. At the time of writing at and before are supported."
    )
    when: Annotated[
        str,
        StringConstraints(strict=True),
    ] = Field(..., description="The actual description of the point of time.")
    __properties = ["point_of_time_type", "reference", "when"]

    __discriminator_property_name = "point_of_time_type"

    __discriminator_value_class_map = {
        "offset": "PointOfTimeOffset",
        "statement": "PointOfTimeStatement",
        "timestamp": "PointOfTimeTimestamp",
    }

    class Config:
        allow_population_by_field_name = True
        validate_assignment = True

    def to_dict(self) -> dict[str, str]:
        d = {p: getattr(self, p) for p in self.__properties}
        # Need to map "when" to the discriminator value as per our OAS
        d[d[self.__discriminator_property_name]] = d["when"]
        del d["when"]
        return d

    @classmethod
    def get_discriminator_value(cls, obj: dict[str, Optional[str]]) -> str:
        """Return the discriminator value (object type) of the data."""
        discriminator_name = obj[cls.__discriminator_property_name]
        assert discriminator_name is not None
        discriminator = cls.__discriminator_value_class_map.get(discriminator_name)
        assert discriminator is not None
        return discriminator

    @classmethod
    def from_dict(
            cls,
            obj: dict[str, Optional[str]],
    ) -> Union[
        "PointOfTimeOffset",
        "PointOfTimeStatement",
        "PointOfTimeTimestamp",
    ]:
        """Create an instance of PointOfTime from a dict."""
        object_type = cls.get_discriminator_value(obj)
        if not object_type:
            raise ValueError(
                "PointOfTime failed to lookup discriminator value from "
                + json.dumps(obj)
                + ". Discriminator property name: "
                + cls.__discriminator_property_name
                + ", mapping: "
                + json.dumps(cls.__discriminator_value_class_map)
            )
        return getattr(sys.modules[__name__], object_type).from_dict(obj)


class PointOfTimeOffset(PointOfTime):
    point_of_time_type: Annotated[str, StringConstraints(strict=True)] = "offset"


class PointOfTimeTimestamp(PointOfTime):
    point_of_time_type: Annotated[str, StringConstraints(strict=True)] = "timestamp"


class PointOfTimeStatement(PointOfTime):
    point_of_time_type: Annotated[str, StringConstraints(strict=True)] = "statement"


class TokenType(Enum):
    SESSION_TOKEN = "ST"
    EXTERNAL_SESSION_WITH_PAT = "ESPAT"


# Now that everything has been defined, let's resolve forward declarations!
Clone.update_forward_refs()
