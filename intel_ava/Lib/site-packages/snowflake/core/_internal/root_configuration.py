import re

from typing import Optional


class RootConfiguration:
    UA_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_]+$")
    UA_VALUE_REGEX = re.compile(r"^[a-zA-Z0-9_\.]+$")

    def __init__(self) -> None:
        """Store the metadata about the root configuration."""
        self._user_agents : list[list[str]] = []

    def append_user_agent(self, name: str, value: Optional[str] = None) -> None:
        """Append a user agent to the list of user agents.

        It would show as:

        User-Agent: <name>/<value> <existing value> or <name> <not existing value>

        Parameters
        __________
        name: str
            The name of the user agent.
        value: str
            The value of the user agent.

        Examples
        ________
        Appending a user agent with a value `Snowflake/1.2.3`:

        >>> append_user_agent("Snowflake", "1.2.3")

        Appending a user agent without a value `custom_ui`:

        >>> append_user_agent("custom_ui")
        """
        if name == 'python_api':
            raise ValueError("The user agent name 'python_api' is reserved for internal use.")
        if not self.UA_NAME_REGEX.fullmatch(name):
            raise ValueError(
                "The name of the user agent must only contain alphanumeric characters or underscores."
            )
        if value is not None and not self.UA_VALUE_REGEX.fullmatch(value):
            raise ValueError(
                "The value of the user agent must only contain alphanumeric characters, underscores, or periods."
            )

        self._user_agents.append([name, value] if value else [name])

    def get_user_agents(self) -> str:
        """Get the list of user agents, separated by spaces.

        Returns
        _______
        str:
            The list of user agents.

        Examples
        ________
        Getting the list of user agents:

        >>> get_user_agents()
        """
        return " ".join(
            [f"{ua[0]}/{ua[1]}" if len(ua) == 2 else ua[0] for ua in self._user_agents]
        )

    def has_user_agents(self) -> bool:
        """Check if user agents exist.

        Returns
        _______
        bool
            True if the user agent exists, False otherwise.
        """
        return bool(self._user_agents)
