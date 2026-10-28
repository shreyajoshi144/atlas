"""Challenge & adversarial investigation (M7).

Import-light on purpose: importing this package loads no embedding model, vector
database or network client. Submodules: ``models`` (vocabulary/limits), ``queries``
(deterministic challenge queries), ``classify`` (pure result classification and
updated interpretation), ``engine`` (fixed pipeline over the tool registry),
``repository`` (persistence + state machine) and ``service``.
"""
