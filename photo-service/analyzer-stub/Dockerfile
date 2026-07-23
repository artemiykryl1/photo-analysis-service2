# Minimal, standalone image for the analyzer-stub gRPC server.
#
# Deliberately does NOT reuse the main photo-service image/Dockerfile -
# this simulates an external analyzer service (constitution.md §2.1) and
# only needs grpcio/protobuf, not fastapi/sqlalchemy/minio (tasks/TASK-002/
# 20_design.md §6). Build context is `photo-service/` (see docker-compose.yml
# `analyzer-stub.build.context: .`), so paths below are relative to that.
FROM python:3.12-slim

WORKDIR /app

COPY analyzer-stub/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Generate the flat (non-package) gRPC stubs at build time - this
# container is a leaf service with no local test suite depending on them
# (unlike app/grpc_gen/, which is committed - see design §6/§7.1).
COPY protos/analyzer.proto ./protos/analyzer.proto
RUN python -m grpc_tools.protoc -I protos --python_out=. --grpc_python_out=. protos/analyzer.proto

COPY analyzer-stub/server.py ./

EXPOSE 50051

HEALTHCHECK --interval=5s --timeout=5s --retries=10 \
    CMD python -c "import socket; socket.create_connection(('localhost', 50051), 2)"

CMD ["python", "server.py"]
