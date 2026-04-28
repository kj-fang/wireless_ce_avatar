__version__ = '1.0.1'


from typing import Optional


SNOWFLAKE_FILE = '/etc/snowflake'


# 2023-09-05(bwarsaw): Verified by examination of existing code, this returns a string.
# https://github.com/shaddi/snowflake/blob/master/snowflake_uuid.py#L15
#
# Also per-email discussion, we will not validate the UUID by any means.
def snowflake(snowflake_file: Optional[str] = None) -> str:
    # Do it this way rather than defaulting the argument -- which is what the upstream package does -- for
    # better testability.  We can mock the module global, but not the argument default value.
    if snowflake_file is None:
        snowflake_file = SNOWFLAKE_FILE
    # Let the FileNotFoundError exception percolate up.
    with open(snowflake_file) as fp:
        return fp.read()


# 2023-09-05(bwarsaw): Per email discussion, this should raise NotImplementedError, but we can also be helpful
# by including a pointer to the new package.
def make_snowflake(snowflake_file: str = SNOWFLAKE_FILE) -> str:
    raise NotImplementedError('This function is only available in the snowflake-uuid package')
