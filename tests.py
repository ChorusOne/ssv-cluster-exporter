import asyncio
from collections.abc import AsyncGenerator
import socket

from aiohttp import client, web
from prometheus_client.parser import text_string_to_metric_families
import pytest
import pytest_asyncio

import ssv_cluster_exporter


def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def metrics_server(exporter_data: dict) -> AsyncGenerator[str, None]:
    exporter_data["session"] = client.ClientSession()
    exporter = ssv_cluster_exporter.SSVClusterExporter(**exporter_data)
    port = find_free_port()
    # Acquire data once
    await exporter.tick()
    app = ssv_cluster_exporter.get_application()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()
    yield f"http://localhost:{port}"
    await site.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exporter_data",
    [
        {
            "owners": [
                {
                    "network": "holesky",
                    "address": "0xd4bb555d3b0d7ff17c606161b44e372689c14f4b",
                }
            ],
            "clusters": [],
        },
        {
            "owners": [],
            "clusters": [
                {
                    "network": "holesky",
                    "cluster_id": "0xde12c5ce1bc895c3ed8b81afcbbb55b3efff7ae9ebac5dbd2ebac3bd29474c09",
                }
            ],
        },
    ],
)
async def test_metrics(metrics_server: str):
    session = client.ClientSession()
    response = await session.get(f"{metrics_server}/metrics")
    assert response.status == 200
    matched_metrics = set()
    for metric in text_string_to_metric_families(await response.text()):
        if metric.name.startswith("ssv_cluster"):
            sample = metric.samples[0]
            assert (
                sample.labels["cluster_id"]
                == "0xde12c5ce1bc895c3ed8b81afcbbb55b3efff7ae9ebac5dbd2ebac3bd29474c09"
            )
            assert sample.labels["id"] == "1278541"
            assert sample.labels["network"] == "holesky"
            assert sample.labels["operators"] == "1092,1093,1094,1095"
            assert (
                sample.labels["owner"] == "0xD4BB555d3B0D7fF17c606161B44E372689C14F4B"
            )
            matched_metrics.add(metric.name)
    assert matched_metrics == {
        "ssv_cluster_validators_count",
        "ssv_cluster_balance",
        "ssv_cluster_network_fee_index",
    }
