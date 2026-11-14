"""The server's event pipeline (ARCHITECTURE.md section 7).

- ``detection``: spectrum frames to detections, one ``core.detect.SignalTracker`` per
  monitor window, and the console spectrum frames.
- ``correlator``: event identity, evidence rows, capability and the uplink-or-local
  verdict (MATH.md section 7).
- ``events``: the ``EventRecord`` the pipeline mutates, the ``EventBook`` that owns them
  (with the mutation API tasking uses) and their console form (``schemas.Event``).
- ``advisory``: plain-language guidance from an event.
- ``tasking``: captures, correlation and solving for active events (separate unit).

Nothing is imported here, so that importing one module does not pull in the others.
"""
