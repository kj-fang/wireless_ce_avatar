# Copyright (c) 2012-2023 Snowflake Computing Inc. All rights reserved.

import platform
import sys


SESSION_TOKEN_EXPIRED_ERROR_CODE = "390112"

# Don't change the constant after this point
PYTHON_VERSION = ".".join(str(v) for v in sys.version_info[:3])
PLATFORM = platform.platform()
