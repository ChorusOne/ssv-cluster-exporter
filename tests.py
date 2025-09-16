import asyncio
from collections.abc import AsyncGenerator
import socket
import typing

from aiohttp import client, web
from prometheus_client.parser import text_string_to_metric_families
import pytest
import pytest_asyncio

import ssv_cluster_exporter


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest_asyncio.fixture
async def metrics_server(exporter_data: typing.Any) -> AsyncGenerator[str, None]:
    exporter_data["loop"] = asyncio.get_event_loop()
    exporter = ssv_cluster_exporter.SSVClusterExporter(**exporter_data)
    port = find_free_port()
    app = ssv_cluster_exporter.get_application(exporter)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()
    yield f"http://localhost:{port}"
    await runner.shutdown()
    await site.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exporter_data",
    [
        {
            "network": "hoodi",
            "ethereum_rpc": "https://ethereum-hoodi-rpc.publicnode.com/",
            "owners": [
                {
                    "address": "0x6f1Ae8C21e37034578A0252F7CC1b19DDe3472cb",
                }
            ],
            "clusters": [],
        },
        {
            "network": "hoodi",
            "ethereum_rpc": "https://ethereum-hoodi-rpc.publicnode.com/",
            "owners": [],
            "clusters": [
                {
                    "cluster_id": "0x0025347a086dc4576d34406c4f28aef122dd69592edcbc885312f06aaa6c0ba3",
                }
            ],
        },
    ],
)
async def test_metrics(metrics_server: str) -> None:
    matched_metrics = set()
    async with client.ClientSession() as session:
        response = await session.get(f"{metrics_server}/metrics")
        assert response.status == 200
        for metric in text_string_to_metric_families(await response.text()):
            if metric.name.startswith("ssv_cluster"):
                sample = metric.samples[0]
                assert (
                    sample.labels["cluster_id"]
                    == "0x0025347a086dc4576d34406c4f28aef122dd69592edcbc885312f06aaa6c0ba3"  # noqa: W503
                )
                assert (
                    sample.labels["id"]
                    == "0x0025347a086dc4576d34406c4f28aef122dd69592edcbc885312f06aaa6c0ba3-hoodi"
                )
                assert sample.labels["network"] == "hoodi"
                assert sample.labels["operators"] == "52,55,57,59"
                assert (
                    sample.labels["owner"]
                    == "0x6f1Ae8C21e37034578A0252F7CC1b19DDe3472cb"
                )
                matched_metrics.add(metric.name)
            elif metric.name in (
                "ssv_network_fee",
                "ssv_minimum_liquidation_collateral",
                "ssv_liquidation_threshold_period",
            ):
                sample = metric.samples[0]
                assert sample.labels["network"] == "hoodi"
                matched_metrics.add(metric.name)
    assert matched_metrics == {
        "ssv_cluster_validators_count",
        "ssv_cluster_balance",
        "ssv_cluster_burn_rate",
        "ssv_network_fee",
        "ssv_minimum_liquidation_collateral",
        "ssv_liquidation_threshold_period",
    }
