"""Service package marker.

Import concrete services from their modules. Keeping this package initializer
empty prevents provider-mode imports from loading Docker or bot orchestration.
"""

__all__: list[str] = []
