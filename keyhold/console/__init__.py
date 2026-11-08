"""Console backends: the protocol, a QEMU QMP backend, a scripted fake, and image handling.

- ``base``: the ``Console`` protocol, key names and the text -> key chord table.
- ``fake``: ``FakeConsole`` for tests.
- ``qmp``: a small QMP client.
- ``qemu``: ``QemuConsole`` (a ``Console`` over QMP) and ``QemuVM`` (one guest in a container).
- ``docker``: the docker CLI wrapper that runs the QEMU runner image.
- ``image``: base image download and SHA512 check, the cloud-init seed, golden image, overlays.
"""
