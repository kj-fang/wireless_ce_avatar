import io
import json
import logging
import typing


if typing.TYPE_CHECKING:
    from urllib3.response import HTTPHeaderDict, HTTPResponse  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)
_FIELD_SEPARATOR = ':'


class RESTResponse(io.IOBase):
    def __init__(self, resp: "HTTPResponse") -> None:
        self.urllib3_response = resp
        self.status = resp.status
        self.reason = resp.reason
        self.data = resp.data
        logger.debug(
            "created a RESTResponse with status %d and length of %d",
            self.status,
            len(self.data),
        )

    def getheaders(self) -> "HTTPHeaderDict":
        """Return a dictionary of the response headers."""
        return self.urllib3_response.headers

    def getheader(self, name: str, default: typing.Any = None) -> typing.Any:
        """Return a given response header."""
        return self.urllib3_response.headers.get(name, default)


class SSEClient:
    """Implementation of a SSE client."""

    def __init__(self, event_source: typing.Any, char_enc: str = 'utf-8') -> None:
        """Initialize the SSE client over an existing, ready to consume event source.

        The event source is expected to be a binary stream and have a close()
        method. That would usually be something that implements
        io.BinaryIOBase, like an httplib or urllib3 HTTPResponse object.
        """
        logger.debug('Initialized SSE client from event source %s',
                           event_source)
        self._event_source = event_source
        self._char_enc = char_enc

    def _read(self) -> typing.Generator[bytes, None, None]:
        r"""Read the incoming event source stream and yield event chunks.

        The event source might be a server side event stream, which looks like:

        >>> event: message.content.delta\n
        >>> data: {"type":"text","text_delta":"text blob 1","index":0}\n\n

        or it might be an array of JSON:

        >>> [{"event":"message.content.delta","data":{"type":"text","text_delta":"text blob 1","index":0}}, ...]
        """
        data = b''
        for chunk in self._event_source:
            for line in chunk.splitlines(True):
                data += line
                if data.endswith((b'\r\r', b'\n\n', b'\r\n\r\n')):
                    yield data
                    data = b''
        if data:
            yield data

    def events(self) -> typing.Generator["Event", None, None]:
        content_type = self._event_source.headers.get('Content-Type')
        if content_type == 'text/event-stream':
            logger.debug("Handling as SSE stream.")
            return self._handle_sse()
        elif content_type and content_type.startswith('application/json'):
        # content_type can also be of type 'application/json;charset=utf-8' so we need to use startswith() here.
            logger.debug("Handling as JSON response.")
            return self._handle_json()
        else:
            raise ValueError(f"Unknown Content-Type: {content_type}. Data: {self._event_source.data!r}")

    def _handle_sse(self) -> typing.Generator["Event", None, None]:
        for chunk in self._read():
            event = Event()
            # Split before decoding so splitlines() only uses \r and \n
            for line_bytes in chunk.splitlines():
                # Decode the line.
                line = line_bytes.decode(self._char_enc)

                # Lines starting with a separator are comments and are to be
                # ignored.
                if not line.strip() or line.startswith(_FIELD_SEPARATOR):
                    continue

                data = line.split(_FIELD_SEPARATOR, 1)
                field = data[0]

                # Ignore unknown fields.
                if field not in event.__dict__:
                    logger.debug('Saw invalid field %s while parsing '
                                       'Server Side Event', field)
                    continue

                if len(data) > 1:
                    # From the spec:
                    # "If value starts with a single U+0020 SPACE character,
                    # remove it from value."
                    if data[1].startswith(' '):
                        value = data[1][1:]
                    else:
                        value = data[1]
                else:
                    # If no value is present after the separator,
                    # assume an empty value.
                    value = ''

                # The data field may come over multiple lines and their values
                # are concatenated with each other.
                if field == 'data':
                    event.__dict__[field] += value + '\n'
                else:
                    event.__dict__[field] = value

            # Events with no data are not dispatched.
            if not event.data:
                continue

            # If the data field ends with a newline, remove it.
            if event.data.endswith('\n'):
                event.data = event.data[0:-1]

            # Empty event names default to 'message'
            event.event = event.event or 'message'

            # Dispatch the event
            logger.debug('Dispatching %s...', event)
            yield event

    def _handle_json(self) -> typing.Generator["Event", None, None]:
        data_list = json.loads(self._event_source.data.decode(self._char_enc))
        if isinstance(data_list, dict):
            # For complete API which goes through XP path, data_list is a dictionary which looks like
            # {'choices': [{'message': {'content': 'Hello! \n'}}]}
            # We do not have any id, event, comment or retry fields.
            yield Event(data=json.dumps(data_list))
        elif isinstance(data_list, list):
            for data in data_list:
                yield Event(id=data.get("id"),
                            event=data.get("event"),
                            data=data.get("data"),
                            comment=data.get("comment"),
                            retry=data.get("retry"))

    def close(self) -> None:
        """Manually close the event source stream."""
        self._event_source.close()


class Event:
    """Representation of an event from the event stream."""

    def __init__(self,
                 id:typing.Optional[str]=None,
                 event:typing.Optional[str]='message',
                 data:typing.Optional[str]='',
                 comment:typing.Optional[str]=None,
                 retry:typing.Optional[int]=None) -> None:
        self.id = id
        self.event = event
        self.data = data
        self.comment = comment
        self.retry = retry

    def __str__(self) -> str:
        s = f'{self.event} event'
        if self.id:
            s += f' #{self.id}'
        if self.data:
            s += ', {} byte{}'.format(len(self.data),
                                        's' if len(self.data) else '')
        else:
            s += ', no data'
        if self.comment:
            s += f', comment: {self.comment}'
        if self.retry:
            s += f', retry in {self.retry}ms'
        return s
