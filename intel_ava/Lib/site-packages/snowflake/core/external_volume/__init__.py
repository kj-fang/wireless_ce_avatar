"""Manages Snowpark Container External Volume.

Example:
    >>> new_external_volume_def = ExternalVolume(
    ...     name="MY_EXTERNAL_VOLUME",
    ...     storage_location=StorageLocationS3(
    ...         name="abcd-my-s3-us-west-2",
    ...         storage_base_url="s3://MY_EXAMPLE_BUCKET/",
    ...         storage_aws_role_arn="arn:aws:iam::123456789022:role/myrole",
    ...         encryption=Encryption(type="AWS_SSE_KMS",
    ...                                kms_key_id="1234abcd-12ab-34cd-56ef-1234567890ab")
    ...     ),
    ...     comment="This is my external volume",
    ... )
    >>> new_external_volume = root.external_volumes.create(new_external_volume_def)
    >>> external_volume_snapshot = new_external_volume.fetch()
    >>> external_volume_data = root.external_volumes.iter(like=”%MY_EXTERNAL_VOLUME)
    >>> new_external_volume.drop()

Refer to :class:`snowflake.core.Root` to create the ``root``.
"""


from ..external_volume._generated.models import (
    Encryption,
    StorageLocationAzure,
    StorageLocationGcs,
    StorageLocationS3,
    StorageLocationS3Gov,
)
from ._external_volume import ExternalVolume, ExternalVolumeCollection, ExternalVolumeResource


__all__ = [
    "ExternalVolume",
    "ExternalVolumeCollection",
    "ExternalVolumeResource",
    "StorageLocationS3",
    "StorageLocationAzure",
    "StorageLocationGcs",
    "Encryption",
    "StorageLocationS3Gov",
]
