"""Analyzer integration stub.

The actual photo analysis is performed by an external Analyzer Service,
consumed asynchronously via Kafka (`photos-to-analyze` topic, see
constitution.md §2.4). Kafka is entirely out of scope for TASK-001 (no
producer/consumer configured here, no client instantiated, no import of
this module by any other module yet).

The gRPC-shaped contract this stub documents is defined in
`protos/analyzer.proto` (`PhotoAnalyzer.AnalyzePhoto`); TASK-001
deliberately does NOT generate Python stubs from it (no `grpcio-tools`/
`protobuf` dependency added - see tasks/TASK-001/20_design.md §9). Actual
analyzer invocation happens over Kafka, not a direct gRPC call, so this
class only documents the request/response shape for future TASK-002 work.

TODO(TASK-002): implement a Kafka producer wrapper here, e.g.:

    async def request_analysis(photo_id: str, object_key: str,
                                trace_id: str) -> None:
        \"\"\"Publish a message to `photos-to-analyze` (key=photo_id).\"\"\"
        ...
"""


class AnalyzerClient:
    """Documented placeholder for the future analyzer integration.

    Mirrors `protos/analyzer.proto::PhotoAnalyzer.AnalyzePhoto`. Not wired
    into any request path in TASK-001.
    """

    async def analyze_photo(self, photo_id: str, object_key: str) -> None:
        """TODO(TASK-002): publish to Kafka `photos-to-analyze` instead of a
        direct RPC call. Raises until implemented so an accidental call
        cannot silently no-op.
        """
        raise NotImplementedError("AnalyzerClient.analyze_photo lands in TASK-002")
