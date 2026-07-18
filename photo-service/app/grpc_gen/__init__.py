"""Committed, generated gRPC stubs for `protos/analyzer.proto`.

Why committed (not generated at build/test time) - see
tasks/TASK-002/20_design.md §7.1: tests run locally via `uv run pytest`
(not in Docker), so generating stubs only inside the Docker build would
make any local import of the worker/analyzer client raise
`ModuleNotFoundError`. `grpcio-tools` therefore stays a **dev**
dependency (regeneration only); the runtime image only needs `grpcio` +
`protobuf`.

Regenerate after editing `protos/analyzer.proto` (run from `photo-service/`):

    uv run python -m grpc_tools.protoc -I protos \\
      --python_out=app/grpc_gen --grpc_python_out=app/grpc_gen \\
      protos/analyzer.proto

`grpc_tools.protoc` emits a plain top-level `import analyzer_pb2 as
analyzer__pb2` in `analyzer_pb2_grpc.py` - after regenerating, fix that
one line back to a package-relative import:

    from app.grpc_gen import analyzer_pb2 as analyzer__pb2

(a known generator quirk, not hand-written protobuf logic - see design
doc §7.1). `analyzer_pb2.py` itself needs no edits.
"""
