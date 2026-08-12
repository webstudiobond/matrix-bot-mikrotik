from collections.abc import Iterable, Iterator, Mapping
from typing import override

class Menu(Iterable[Mapping[str, object]]):
    @override
    def __iter__(self) -> Iterator[Mapping[str, object]]: ...
    def add(self, **kwargs: str) -> Iterable[Mapping[str, object]]: ...

class Api:
    def path(self, *args: str) -> Menu: ...
    def close(self) -> None: ...

def connect(
    host: str,
    username: str,
    password: str,
    port: int,
    ssl_wrapper: object = None,
) -> Api: ...
