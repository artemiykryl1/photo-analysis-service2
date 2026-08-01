"""Behavioural coverage of `app.worker.main` (worker entrypoint).

Design §5.1/§5.6: `_run()` wires metrics server + gRPC client + Kafka
consumer + `consume_loop`, then tears them down in order on exit
(`consumer.stop()` -> `analyzer.close()` -> `engine.dispose()`), even if
`consume_loop` raises. `_install_shutdown_handlers` wires SIGTERM/SIGINT to
set `stop_event`, with a `signal.signal` fallback on platforms where
`loop.add_signal_handler` is unavailable (Windows).

Every collaborator (`AIOKafkaConsumer`, `AnalyzerGrpcClient`,
`start_http_server`, `consume_loop`, `engine`) is monkeypatched - no real
Kafka broker, gRPC server, or Postgres connection is made.
"""

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import app.worker.main as worker_main_module


class TestInstallShutdownHandlers:
    async def test_add_signal_handler_registers_sigterm_and_sigint(self):
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        registered = {}

        def _fake_add_signal_handler(sig, callback, *args):
            registered[sig] = (callback, args)

        with patch.object(loop, "add_signal_handler", side_effect=_fake_add_signal_handler):
            worker_main_module._install_shutdown_handlers(stop_event)

        assert signal.SIGTERM in registered
        assert signal.SIGINT in registered

        # Simulate the loop invoking the registered callback (as it would
        # on a real signal) - stop_event must end up set.
        callback, args = registered[signal.SIGTERM]
        callback(*args)
        assert stop_event.is_set()

    async def test_falls_back_to_signal_signal_when_add_signal_handler_unsupported(self):
        """`loop.add_signal_handler` raises `NotImplementedError` on
        Windows event loops - the fallback must still make the process
        respond to a signal by setting `stop_event`."""
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        with patch.object(loop, "add_signal_handler", side_effect=NotImplementedError):
            with patch("signal.signal") as mock_signal:
                worker_main_module._install_shutdown_handlers(stop_event)

        assert mock_signal.call_count == 2
        registered_signals = {call.args[0] for call in mock_signal.call_args_list}
        assert registered_signals == {signal.SIGTERM, signal.SIGINT}

        # Invoke one of the fallback handlers as `signal.signal` would.
        _sig, handler = mock_signal.call_args_list[0].args
        handler(signal.SIGTERM, None)
        assert stop_event.is_set()


class TestRunWiring:
    async def test_run_starts_metrics_server_consumer_and_disposes_on_exit(self):
        with patch.object(worker_main_module, "start_http_server") as mock_metrics_server, \
             patch.object(worker_main_module, "AnalyzerGrpcClient") as mock_analyzer_cls, \
             patch.object(worker_main_module, "ObjectStorage") as mock_storage_cls, \
             patch.object(worker_main_module, "AIOKafkaConsumer") as mock_consumer_cls, \
             patch.object(worker_main_module, "consume_loop", new_callable=AsyncMock) as mock_consume_loop, \
             patch.object(worker_main_module, "AnalysisProcessor") as mock_processor_cls, \
             patch.object(worker_main_module, "engine") as mock_engine, \
             patch.object(worker_main_module, "_install_shutdown_handlers") as mock_install_handlers:

            fake_analyzer = MagicMock()
            fake_analyzer.close = AsyncMock()
            mock_analyzer_cls.return_value = fake_analyzer

            fake_storage = MagicMock()
            mock_storage_cls.return_value = fake_storage

            fake_consumer = AsyncMock()
            mock_consumer_cls.return_value = fake_consumer

            mock_engine.dispose = AsyncMock()

            await worker_main_module._run()

            mock_metrics_server.assert_called_once()
            mock_consumer_cls.assert_called_once()
            fake_consumer.start.assert_awaited_once()
            mock_install_handlers.assert_called_once()
            mock_consume_loop.assert_awaited_once()
            fake_consumer.stop.assert_awaited_once()
            fake_analyzer.close.assert_awaited_once()
            mock_engine.dispose.assert_awaited_once()

            # TASK-003 A9 (design §5.1): the worker constructs an
            # `ObjectStorage` and passes it through to `AnalysisProcessor`
            # as the required `storage=` keyword argument.
            mock_storage_cls.assert_called_once()
            mock_processor_cls.assert_called_once()
            assert mock_processor_cls.call_args.kwargs["storage"] is fake_storage

    async def test_teardown_still_runs_when_consume_loop_raises(self):
        """The `finally` block (design §5.6) must close the consumer/
        analyzer/engine even if `consume_loop` propagates an exception."""
        with patch.object(worker_main_module, "start_http_server"), \
             patch.object(worker_main_module, "AnalyzerGrpcClient") as mock_analyzer_cls, \
             patch.object(worker_main_module, "ObjectStorage"), \
             patch.object(worker_main_module, "AIOKafkaConsumer") as mock_consumer_cls, \
             patch.object(
                 worker_main_module, "consume_loop", new_callable=AsyncMock
             ) as mock_consume_loop, \
             patch.object(worker_main_module, "engine") as mock_engine, \
             patch.object(worker_main_module, "_install_shutdown_handlers"):

            fake_analyzer = MagicMock()
            fake_analyzer.close = AsyncMock()
            mock_analyzer_cls.return_value = fake_analyzer

            fake_consumer = AsyncMock()
            mock_consumer_cls.return_value = fake_consumer
            mock_engine.dispose = AsyncMock()

            mock_consume_loop.side_effect = RuntimeError("boom")

            try:
                await worker_main_module._run()
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected RuntimeError to propagate")

            fake_consumer.stop.assert_awaited_once()
            fake_analyzer.close.assert_awaited_once()
            mock_engine.dispose.assert_awaited_once()

    async def test_consumer_constructed_with_configured_topic_and_group(self):
        with patch.object(worker_main_module, "start_http_server"), \
             patch.object(worker_main_module, "AnalyzerGrpcClient") as mock_analyzer_cls, \
             patch.object(worker_main_module, "ObjectStorage"), \
             patch.object(worker_main_module, "AIOKafkaConsumer") as mock_consumer_cls, \
             patch.object(worker_main_module, "consume_loop", new_callable=AsyncMock), \
             patch.object(worker_main_module, "engine") as mock_engine, \
             patch.object(worker_main_module, "_install_shutdown_handlers"):

            fake_analyzer = MagicMock()
            fake_analyzer.close = AsyncMock()
            mock_analyzer_cls.return_value = fake_analyzer

            fake_consumer = AsyncMock()
            mock_consumer_cls.return_value = fake_consumer
            mock_engine.dispose = AsyncMock()

            settings = worker_main_module.get_settings()

            await worker_main_module._run()

            call = mock_consumer_cls.call_args
            assert call.args[0] == settings.KAFKA_TOPIC_ANALYSIS_REQUESTED
            assert call.kwargs["group_id"] == settings.KAFKA_CONSUMER_GROUP
            assert call.kwargs["enable_auto_commit"] is False


def test_main_delegates_to_asyncio_run():
    def _consume_and_close(coro):
        coro.close()  # avoid a "coroutine was never awaited" warning

    with patch.object(worker_main_module.asyncio, "run", side_effect=_consume_and_close) as mock_run:
        worker_main_module.main()
        mock_run.assert_called_once()
